"""Mapper: project the event log into a StateDocument.

This is the ONLY gateway from Policy to the world: Policy calls context(), coordinate_observations(),
latest_state(), is_current(), last_world_change(), subject() and event_store() and never imports plsi.life.
Never invents values: units convert only through plsi.map.units (identity, a sourced map conversion or a generic UCUM scale).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from plsi import clock, errors, files, paths
from plsi.life.models import Subject
from plsi.map.models import UNMAPPED_CAP, CareContext, CoordinateValue, Gap, MapContract, MapCoordinate, ObservedValue, StateDocument
from plsi.models import parse
from plsi.life import observed
from plsi.life.store import EventStore
from plsi.map import registry
from plsi.map import storyline as _storyline
from plsi.map.projection import Projection, hybrid, set_items
from plsi.vocab import CoordinateKind, EventType, ObservationKind, ValueStatus

FREE_TEXT_KINDS = {ObservationKind.GOAL, ObservationKind.PREFERENCE, ObservationKind.LIFESTYLE}  # set items with no code system
LOCAL_MAPS = frozenset({"personal_context"})  # goals and preferences: free text about the user's life, never sent


def _gaps(c: MapCoordinate, v: CoordinateValue) -> list[Gap]:
    if v.status == ValueStatus.MISSING:
        if c.kind == CoordinateKind.SET:
            hint = f"ask the user about {c.label.lower()} and ingest them (or confirm there are none)"
        elif c.derive:
            same = [i for i in c.derive.inputs if i not in c.derive.carry_forward]
            hint = f"ingest {', '.join(same)} from one calendar day" + "".join(f" and a {i} on or before it" for i in c.derive.carry_forward)
            hint = f"measure {c.label.lower()}, or {hint[0].lower()}{hint[1:]}" if hybrid(c) else hint
        else:
            codes = ", ".join(x.code for x in c.accepts.codes[:3])
            hint = f"measure {c.label} and ingest the result" + (f" (e.g. {codes})" if codes else "")
        return [Gap(coordinate=c.id, reason="missing", suggestion=hint)]
    if v.status == ValueStatus.STALE:
        return [Gap(coordinate=c.id, reason=f"stale: last value {v.observed_at:%Y-%m-%d}", suggestion=f"re-measure {c.label}")]
    if v.status == ValueStatus.ERROR:
        return [Gap(coordinate=c.id, reason=f"error: {v.note}", suggestion="fix the value or unit (retract and re-ingest); units convert only via the map")]
    uncoded = [i.text for i in v.value if i.code is None] if isinstance(v.value, list) and c.collects not in FREE_TEXT_KINDS else []
    if uncoded:
        return [Gap(coordinate=c.id, reason=f"{len(uncoded)} uncoded item(s)", suggestion="plsi normalize observations, then retract and re-ingest coded items")]
    return []


def build_state(instance: str, m: MapContract, as_of: datetime | None = None) -> StateDocument:
    """Pure projection (writes nothing) of the ACTIVE observations with effective_at <= as_of (default clock.now()),
    read through the event index from one consistent view; memory does not grow with the history.
    Returns a sealed() StateDocument (generated_at = clock.now())."""
    as_of = as_of or clock.now()
    store = EventStore(instance)
    with store.snapshot():
        proj = Projection(store, m, as_of)
        coordinates = {c.id: proj.value(c) for c in m.coordinates}
        unmapped, total = proj.unmapped(UNMAPPED_CAP)
    return StateDocument(
        subject_id=store.subject.subject_id, map_id=m.id, map_version=m.map_version, map_hash=m.compute_hash(),
        as_of=as_of, generated_at=clock.now(), coordinates=coordinates,
        gaps=[g for c in m.coordinates for g in _gaps(c, coordinates[c.id])], unmapped_observations=unmapped,
        unmapped_count=total if total > len(unmapped) else None,
    ).sealed()


def project(instance: str, map_id: str, as_of: datetime | None = None, storyline: bool = True) -> dict[str, Any]:
    """`plsi project`: snapshot the map, build_state, write states/<map_id>/<ts>_<hash12>.json and latest.json
    under the instance lock; storyline vs the previous latest.json. Returns {state, storyline, paths}."""
    m = registry.get(map_id, instance)
    store = EventStore(instance)
    folder = paths.safe_join(store.dir, "states", m.id)
    with files.lock(store.dir):
        registry.snapshot(instance, m)
        try:
            previous = latest_state(instance, m.id)
        except errors.PlsiError:  # an unreadable latest.json is replaced, not compared
            previous = None
        state = build_state(instance, m, as_of)
        doc = state.dump()
        file = folder / f"{clock.ts_filename(state.generated_at)}_{files.hash12(state.state_hash)}.json"
        files.atomic_write_json(file, doc)
        files.atomic_write_json(folder / "latest.json", doc)
    return {"state": doc, "storyline": _storyline.build(state, previous) if storyline else None,
            "paths": {"latest": str(folder / "latest.json"), "file": str(file)}}


def latest_state(instance: str, map_id: str) -> StateDocument | None:
    """Parse states/<map_id>/latest.json, or None if absent. Does NOT verify the hash (callers use verify_hash())."""
    path = paths.safe_join(paths.instance_dir(instance), "states", map_id, "latest.json")
    return parse(StateDocument, files.load_json(path), "latest.json") if path.is_file() else None


def latest_states(instance: str) -> list[StateDocument]:
    """Every states/*/latest.json, by map id. An unreadable file raises errors.ValidationError (code INVALID_JSON
    for bad JSON, VALIDATION_ERROR for one that is not a StateDocument). Does NOT verify hashes."""
    folder = paths.instance_dir(instance) / "states"
    return [parse(StateDocument, files.load_json(p), "latest.json") for p in sorted(folder.glob("*/latest.json"))]


def is_current(state: StateDocument, changed: datetime | None) -> bool:
    """False when an observation or retraction was recorded after the state was generated (changed = last_world_change)."""
    return changed is None or state.generated_at >= changed


def context(instance: str, as_of: datetime | None = None) -> CareContext:
    """care_context sets (active medications, conditions, allergies) — Policy's only view of them.
    Uncoded items are kept (the care_context projection adds a gap suggesting normalization)."""
    as_of = as_of or clock.now()
    store = EventStore(instance)
    with store.snapshot():
        return CareContext(medications=set_items(store, ObservationKind.MEDICATION, as_of),
                           conditions=set_items(store, ObservationKind.CONDITION, as_of),
                           allergies=set_items(store, ObservationKind.ALLERGY, as_of))


def coordinate_observations(instance: str, m: MapContract, coordinate_id: str, since: datetime | None = None,
                            until: datetime | None = None) -> list[ObservedValue]:
    """Every candidate value of one measured or derived coordinate, oldest first, for scoring (converted through `m`;
    derived: one per input-complete day, recorded_at = max input recorded_at, basis assumed if any input is).
    since/until keep only values effective inside [since, until] (a window needs no other reading, so a coordinate with
    millions of readings is read for that window only). Raises errors.NotFound for an unknown coordinate; set
    coordinates -> ValidationError."""
    c = m.coordinate(coordinate_id)
    if c is None:
        raise errors.NotFound(f"map {m.id} has no coordinate {coordinate_id!r}", code="COORDINATE_NOT_FOUND")
    if c.kind == CoordinateKind.SET:
        raise errors.ValidationError(f"{coordinate_id} is a set coordinate")
    store = EventStore(instance)
    with store.snapshot():
        proj = Projection(store, m, None)
        return [ObservedValue(event_ids=p.event_ids, effective_at=p.t, recorded_at=p.recorded_at, effective_basis=p.basis,
                              value=p.coordinate_value(c)) for p in proj.points(c.id, since=since, until=until)]


def coverage(instance: str, m: MapContract, store: EventStore | None = None) -> list[str]:
    """Coordinate ids with a recent value (sets: any item recorded); used by registry.select, computes no clock.
    store: the instance's store when the caller keeps one view across several maps."""
    as_of = clock.now()
    store = store or EventStore(instance)
    with store.snapshot():
        proj = Projection(store, m, as_of)
        out = []
        for c in m.coordinates:
            if c.kind == CoordinateKind.SET:
                ok = observed.has_kind(store.index, ObservationKind(c.collects).value, proj.hi)
            else:
                days = proj.day_set(c)
                ok = bool(days) and (as_of.date() - max(days)).days <= c.recency_days
            if ok:
                out.append(c.id)
    return out


def last_world_change(instance: str) -> datetime | None:
    """Max recorded_at over observation and retraction events (G1: latest.json must be generated at or after it)."""
    return EventStore(instance).world_change()


def subject(instance: str) -> Subject:
    """The instance's deidentified Subject."""
    return EventStore(instance).subject


def event_store(instance: str) -> EventStore:
    """The instance EventStore, for Policy to append/read policy events (screening, plan, plan_status,
    prediction_score). Policy must not read observation payloads from it; use the functions above."""
    return EventStore(instance)
