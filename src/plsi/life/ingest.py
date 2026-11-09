"""`plsi ingest` and `plsi retract`.

No network calls. Writes only through plsi.life.store.EventStore (never edits events.jsonl).
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from plsi.events import NewEvent
from plsi.life.models import Observation, ObservationInput, Retraction, RetractionInput
from plsi.models import parse
from plsi.life import importers, observed
from plsi.life.dedupe import dedupe_key
from plsi.life.normalize import MAPS_DIR, normalize_unit
from plsi.life.store import EventStore
from plsi.errors import NotFound, UsageError, ValidationError
from plsi.files import decode_input, load_json, parse_input_json, read_input_bytes, sha256_file, sha256_hex
from plsi.vocab import EventType, OriginKind

MONTHLY_MAP = MAPS_DIR / "sleep_activity.yaml"  # raw YAML: plsi.life never imports plsi.map
MONTHLY_LABEL = "calendar-month mean"  # coordinates labelled so hold one mean per closed month (wearables/import step 3)
MONTHLY_ORIGINS = (OriginKind.DEVICE, OriginKind.CSV, OriginKind.EXTENSION)  # the wearables/import paths
APPEND_CHUNK = 8192  # events appended per lock and fsync, so memory does not grow with the rows of one call


def _monthly_codes() -> dict[tuple[str, str], str]:
    """(system, code) -> coordinate id of every code a calendar-month-mean coordinate accepts (not vo2_max)."""
    doc = yaml.safe_load(MONTHLY_MAP.read_text(encoding="utf-8")) or {}
    return {(str(k["system"]), str(k["code"])): c["id"] for c in doc.get("coordinates") or []
            if MONTHLY_LABEL in c.get("label", "") for k in (c.get("accepts") or {}).get("codes") or []}


def _month(obs: ObservationInput, codes: dict[tuple[str, str], str]) -> tuple[str, str] | None:
    """(coordinate id, YYYY-MM) of a device or CSV-imported monthly mean, else None."""
    if obs.code is None or obs.origin.kind not in MONTHLY_ORIGINS:
        return None
    coordinate = codes.get((obs.code.system.value, obs.code.code))
    return (coordinate, obs.effective_at.strftime("%Y-%m")) if coordinate else None


def _conflicts(store: EventStore, fresh: list[tuple[int, Observation, str]]) -> list[dict[str, str | int]]:
    """New monthly means for a metric and month that already has a value (recorded or earlier in this input)."""
    codes = _monthly_codes()
    keys = [(i, k) for i, obs, _ in fresh if (k := _month(obs, codes)) is not None]
    taken: dict[tuple[str, str], str] = {}
    if keys:
        ix = store.index
        for coordinate, month in sorted({k for _, k in keys}):
            found = observed.first_in_month(ix, [c for c, v in codes.items() if v == coordinate], [o.value for o in MONTHLY_ORIGINS], month)
            if found is not None:
                taken[(coordinate, month)] = found
    out = []
    for i, k in keys:
        if k in taken:
            out.append({"index": i, "with": taken[k], "coordinate": k[0], "month": k[1]})
        else:
            taken[k] = f"index {i}"
    return out


def to_observation(obs: ObservationInput) -> Observation:
    """Stored form: unit_original as reported, unit UCUM-syntax-normalized; the value is never converted."""
    return Observation.model_validate({**obs.dump(), "unit": normalize_unit(obs.unit), "unit_original": obs.unit})


def _read(path: str) -> tuple[str, str]:
    """(utf-8 text, sha256 hex of the raw bytes)."""
    data = read_input_bytes(path)
    return decode_input(data), sha256_hex(data)


def _from_json_list(text: str) -> importers.Imported:
    return importers.Imported.from_items(importers.observation_items(parse_input_json(text)))


def _from_extension(ext_id: str, path: str) -> importers.Imported:
    """Run an importer extension; its items get origin {kind: extension, file_sha256} unless they set one."""
    from plsi import extensions

    data = read_input_bytes(path)
    items = extensions.importer(ext_id)(data)
    if not isinstance(items, list):
        raise ValidationError(f"importer {ext_id} must return a list of ObservationInput dicts")
    origin = {"kind": OriginKind.EXTENSION.value, "file_sha256": sha256_hex(data)}
    return importers.Imported.from_items([{**item, "origin": item.get("origin") or origin} if isinstance(item, dict) else item for item in items])


def ingest(
    instance: str,
    file: str | None = None,
    fhir: str | None = None,
    csv: str | None = None,
    methylation: str | None = None,
    ext: str | None = None,
    input: str | None = None,  # noqa: A002 - CLI dest name
    sample: str | None = None,
    effective_at: datetime | None = None,
    update: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate, normalize unit strings, assign ids and append observations (exactly one source; '-' = stdin).

    Returns {counts: {accepted, duplicate, rejected}, accepted: [event_id], duplicates: [event_id],
             rejected: [{index, error}], conflicts: [{index, with, coordinate, month}],
             warnings: ["UNCODED: <index>", "CONFLICT: <index>", ...], dropped_columns?, subject_updated?}
    (dry_run writes nothing; accepted then holds indexes).
    """
    sources = {k: v for k, v in {"file": file, "fhir": fhir, "csv": csv, "methylation": methylation, "ext": ext}.items() if v is not None}
    if len(sources) != 1:
        raise UsageError("give exactly one of --file, --fhir, --csv, --methylation, --ext")
    if (ext is None) != (input is None):
        raise UsageError("--ext and --input go together")
    store = EventStore(instance)
    result: dict[str, Any] = {}
    if methylation is not None:
        path = Path(methylation)
        if not path.is_file():
            raise NotFound(f"input file not found: {path.name}", code="FILE_NOT_FOUND")
        with tempfile.TemporaryDirectory() as scratch:  # dry run: the artifact is written to a throwaway dir
            imported = importers.from_methylation(path, Path(scratch) if dry_run else store.dir, sample, sha256_file(path), effective_at)
    elif ext is not None:
        imported = _from_extension(ext, input)  # type: ignore[arg-type]
    else:
        text, sha = _read(next(iter(sources.values())))
        if file is not None:
            imported = _from_json_list(text)
        elif fhir is not None:
            imported = importers.from_fhir(parse_input_json(text), sha)
        else:
            imported = importers.from_csv(text, sha)
            result["dropped_columns"] = imported.dropped_columns
    if imported.subject and not dry_run:
        _, changed = store.update_subject(**imported.subject, update=update)
        result["subject_updated"] = changed

    observations: list[Observation] = []
    for n in range(len(imported.observations)):  # converted in place: the input objects are released as they go
        observations.append(to_observation(imported.observations[n]))
        imported.observations[n] = None  # type: ignore[call-overload]
    imported.observations.clear()
    keys = [dedupe_key(o) for o in observations]
    indexes = imported.indexes
    warnings = list(imported.warnings) + [f"UNCODED: {i}" for i, o in zip(indexes, observations, strict=True) if o.code is None]
    logged = observed.observation_ids(store.index, [k for o, k in zip(observations, keys, strict=True) if not o.repeat])  # the log's active observations
    local: dict[str, int] = {}  # first position in `fresh` of each key not in the log
    fresh: list[tuple[int, Observation, str]] = []
    dupes: list[str | int] = []  # an event id, or a position in fresh (same-file repeat)
    for i, obs, key in zip(indexes, observations, keys, strict=True):
        known = logged[key] if key in logged else local.get(key)
        if not obs.repeat and known is not None:
            dupes.append(known)
        else:
            if key not in logged:
                local.setdefault(key, len(fresh))
            fresh.append((i, obs, key))
    conflicts = _conflicts(store, fresh)  # reported, never refused
    warnings += [f"CONFLICT: {c['index']}" for c in conflicts]
    if dry_run:
        ids: list[str] = [f"index {i}" for i, _, _ in fresh]
        accepted: list[Any] = [i for i, _, _ in fresh]
    else:  # append_many dedupes again under the lock, against concurrent ingests
        written = []
        for n in range(0, len(fresh), APPEND_CHUNK):  # one lock and one fsync per chunk; a replay after a crash dedupes the chunks already written
            written += store.append_many([NewEvent(EventType.OBSERVATION, obs, dedupe_key=key, dedupe=not obs.repeat) for _, obs, key in fresh[n:n + APPEND_CHUNK]])
        ids = [event_id for event_id, _ in written]
        accepted = [event_id for event_id, created in written if created]
        dupes.sort(key=lambda d: isinstance(d, int))  # log duplicates first, then same-file and concurrent repeats
        dupes += [event_id for event_id, created in written if not created]
    duplicates = [ids[d] if isinstance(d, int) else d for d in dupes]
    return {
        "counts": {"accepted": len(accepted), "duplicate": len(duplicates), "rejected": len(imported.rejected)},
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": imported.rejected,
        "conflicts": conflicts,
        "warnings": warnings,
        "dry_run": dry_run,
        **result,
    }


def retract(instance: str, event_id: str, file: str, replacement: str | None = None) -> dict[str, Any]:
    """Append a Retraction of `event_id` (reason from RetractionInput in `file`).

    replacement: ONE ObservationInput JSON, appended first; its id goes into replacement_event_id.
    Retraction events cannot be retracted; a retracted target raises ALREADY_RETRACTED. Retracting a plan event
    makes Policy treat its unscored predictions as void. Events are never edited.
    """
    store = EventStore(instance)
    reason = parse(RetractionInput, load_json(file), "retraction").reason
    target = store.get(event_id)
    if target.type == EventType.RETRACTION:
        raise ValidationError("retraction events cannot be retracted", code="CANNOT_RETRACT_RETRACTION")
    if event_id in store.retracted_ids():
        raise ValidationError(f"event {event_id} is already retracted", code="ALREADY_RETRACTED")
    out: dict[str, Any] = {"target_event_id": event_id}
    payload: dict[str, Any] = {"target_event_id": event_id, "reason": reason}
    if replacement is not None:
        obs = to_observation(parse(ObservationInput, load_json(replacement), "replacement observation"))
        new_id, _ = store.append(EventType.OBSERVATION, obs, dedupe_key=dedupe_key(obs))  # never deduped: may equal the target
        out["replacement_event_id"] = payload["replacement_event_id"] = new_id
    out["retraction_event_id"], _ = store.append(EventType.RETRACTION, Retraction.model_validate(payload))
    return out
