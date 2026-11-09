"""TEST ORACLE: the in-memory projection of PLSI v0.2.1 (17c433c), verbatim, plus the reference mapper functions that used it.
The engine no longer reads the whole log; tests compare its results to this on small synthetic histories.

The projection of one map over an instance's active observations: which observations each coordinate accepts,
its measured and derived values (with provenance) and the days they exist. Used only by plsi.map.mapper."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from plsi import errors, paths
from plsi.events import Event
from plsi.life.models import Observation
from plsi.life.store import EventStore
from plsi.map import biolearn, derive, units
from plsi.map.models import SERIES_CAP, CoordinateValue, Derive, MapContract, MapCoordinate, Provenance, SeriesPoint, SetItem
from plsi.vocab import ClinicalStatus, Comparator, CoordinateKind, DeriveEngine, MatchKind, ObservationKind, TimeBasis, ValueStatus


def hybrid(c: MapCoordinate) -> bool:
    """A measured coordinate that is also derived when not measured (BMI): accepts.codes and derive."""
    return c.derive is not None and bool(c.accepts.codes)


def _accepts(c: MapCoordinate, o: Observation) -> bool:
    if o.code is None or any((r.system, r.code) == (o.code.system, o.code.code) for r in c.accepts.reject_codes):
        return False
    code = next((x for x in c.accepts.codes if (x.system, x.code) == (o.code.system, o.code.code)), None)
    if code is None or not c.accepts.requires_fasting:
        return code is not None
    # Fasting-specific codes (their display says fasting) qualify unless the observation says it was not fasting.
    return o.fasting is True or (o.fasting is None and "fasting" in (code.display or "").lower())


@dataclass
class Point:
    t: datetime
    value: float | str | None  # str = methylation artifact sha256
    event_ids: list[str]
    recorded_at: datetime
    basis: TimeBasis
    provenance: Provenance
    comparator: Comparator | None = None
    error: str | None = None

    def coordinate_value(self, c: MapCoordinate, status: ValueStatus | None = None) -> CoordinateValue:
        artifact = isinstance(self.value, str)
        return CoordinateValue(
            value=None if artifact or self.error else self.value, unit=c.unit, comparator=self.comparator,
            observed_at=self.t, event_ids=self.event_ids, status=ValueStatus.ERROR if self.error else status or ValueStatus.PRESENT,
            note=self.error or (f"artifact {self.value}" if artifact else None), provenance=self.provenance,
        )


def set_items(events: list[Event], kind: ObservationKind, as_of: datetime) -> list[SetItem]:
    """Active items of one kind: the latest observation per item (code, else lower text) is active/unknown and open."""
    latest: dict[tuple[str, str], Event] = {}
    for e in sorted((e for e in events if e.payload.kind == kind), key=lambda e: e.payload.effective_at):
        o = e.payload
        latest[(o.code.system.value, o.code.code) if o.code else ("text", o.original_text.lower())] = e
    items = [
        SetItem(text=e.payload.original_text, code=e.payload.code, dose=e.payload.dose,
                dose_quantity=e.payload.dose_quantity, dose_unit=e.payload.dose_unit,
                clinical_status=e.payload.clinical_status, event_id=e.id)
        for e in latest.values()
        if e.payload.clinical_status not in (ClinicalStatus.STOPPED, ClinicalStatus.RESOLVED)
        and (e.payload.effective_end is None or e.payload.effective_end > as_of)
    ]
    return sorted(items, key=lambda i: (i.text.lower(), i.event_id))


def _input_days(d: Derive, days: dict[str, set[date]]) -> list[date]:
    """Days a derived value exists, from each input's days with a value: every same-day input has one that day and
    every carried input one on or before it. The one rule for coverage (day_set) and projection (_derived)."""
    same = [days[i] for i in d.inputs if i not in d.carry_forward]
    firsts = [min(days[i], default=date.max) for i in d.carry_forward]
    return sorted(day for day in set.intersection(*same) if all(day >= f for f in firsts)) if same else []


class Projection:
    def __init__(self, store: EventStore, m: MapContract, as_of: datetime | None, events: list[Event] | None = None) -> None:
        """events: the store's active observations when already read (registry.select reads the log once)."""
        self.store, self.m, self.as_of = store, m, as_of
        self.subject = store.subject
        self.birth_year, self.sex = self.subject.birth_year, self.subject.sex.value
        self.events = [e for e in (store.observations() if events is None else events) if as_of is None or e.payload.effective_at <= as_of]
        self._by_code: dict[tuple[str, str], list[int]] = {}  # (system, code) -> positions in self.events
        for n, e in enumerate(self.events):
            if e.payload.code is not None:
                self._by_code.setdefault((e.payload.code.system.value, e.payload.code.code), []).append(n)
        self._matches: dict[str, list[Event]] = {}
        self._points: dict[str, list[Point]] = {}
        self._clock_runs: dict[tuple[str, date], dict[str, float | str]] = {}

    def matches(self, c: MapCoordinate) -> list[Event]:
        """Observations the coordinate accepts, by effective_at then log order."""
        if c.id not in self._matches:
            hits = sorted({n for x in c.accepts.codes for n in self._by_code.get((x.system.value, x.code), [])})
            self._matches[c.id] = sorted((self.events[n] for n in hits if _accepts(c, self.events[n].payload)),
                                         key=lambda e: e.payload.effective_at)
        return self._matches[c.id]

    def points(self, cid: str) -> list[Point]:
        """All values of a measured or derived coordinate, oldest first (hybrid: both; on one day the measured last)."""
        if cid not in self._points:
            c = self.m.coordinate(cid)
            assert c is not None
            if hybrid(c):
                both = [self._measured(c, e) for e in self.matches(c)] + self._derived(c)
                self._points[cid] = sorted(both, key=lambda p: (p.t.date(), p.provenance.match == MatchKind.CODE, p.t))
            elif c.derive:
                self._points[cid] = self._derived(c)
            else:
                self._points[cid] = [self._measured(c, e) for e in self.matches(c)]
        return self._points[cid]

    def day_set(self, c: MapCoordinate) -> set[date]:
        """Calendar days with a value (derived: days on which every same-day input has one, after the first value of
        each carried input; hybrid: plus the measured days); computes nothing."""
        measured_days = {e.payload.effective_at.date() for e in self.matches(c)} if c.accepts.codes else set()
        if not c.derive:
            return measured_days
        inputs = {i: self.day_set(self.m.coordinate(i)) for i in c.derive.inputs}  # type: ignore[arg-type]
        return measured_days | set(_input_days(c.derive, inputs))

    def _measured(self, c: MapCoordinate, e: Event) -> Point:
        o = e.payload
        p = Point(o.effective_at, None, [e.id], e.recorded_at, o.effective_basis,
                   Provenance(match=MatchKind.CODE, rule=f"accepts:{o.code.system.value}:{o.code.code}"), o.value_comparator)  # type: ignore[union-attr]
        if o.value_quantity is None:
            if c.unit is None and o.value_text:
                p.value = o.value_text
            else:
                p.error = "not a numeric value"
            return p
        try:
            p.value, p.provenance.conversion = units.to_canonical(o.value_quantity, o.unit, c)
        except ValueError as exc:
            p.error = str(exc)
        return p

    def _derived(self, c: MapCoordinate) -> list[Point]:
        d = c.derive
        assert d is not None
        per_day = {i: {p.t.date(): p for p in self.points(i)} for i in d.inputs}  # latest value of each day
        days = _input_days(d, {i: set(x) for i, x in per_day.items()})
        if not days:
            return []
        engine = derive.ENGINES[d.engine]
        rule, safe = engine.rule_id(d), engine.censor_safe(d)  # once per coordinate (an extension reads its manifest)
        biolearn_on = d.engine == DeriveEngine.BIOLEARN and biolearn.available()
        usage = {x["name"]: x["usage_note"] for x in biolearn.clocks()} if biolearn_on else {}
        out = []
        for day in days:
            ins = {i: per_day[i][day if i not in d.carry_forward else max(x for x in per_day[i] if x <= day)] for i in d.inputs}
            t = datetime.combine(day, time(12), UTC)  # derived effective time = the draw day
            derivation = {"engine": d.engine.value, "model": d.model, "computed_at": (self.as_of or t).isoformat()}
            if usage.get(d.model):
                derivation["usage_note"] = usage[d.model]
            p = Point(t, None, list(dict.fromkeys(x for q in ins.values() for x in q.event_ids)), max(q.recorded_at for q in ins.values()),
                       TimeBasis.ASSUMED if any(q.basis == TimeBasis.ASSUMED for q in ins.values()) else TimeBasis.DOCUMENT,
                       Provenance(match=MatchKind.DERIVED, rule=rule, derivation=derivation))
            bad = [f"{i}: {q.error}" for i, q in ins.items() if q.error]
            censored = {i: q.comparator for i, q in ins.items() if q.comparator}
            if censored and not safe:
                bad.append(f"censored inputs {sorted(censored)} cannot be bounded through {d.engine.value}:{d.model}")
            if bad:
                p.error = "; ".join(bad)
                out.append(p)
                continue
            try:
                values = {i: q.value for i, q in ins.items()}
                if censored:
                    fn = lambda v: derive.evaluate(d, v, birth_year=self.birth_year, sex=self.sex, at=t)  # noqa: E731
                    p.value, p.comparator = derive.bounded(fn, values, censored)  # type: ignore[arg-type]
                    p.provenance.derivation["censored_inputs"] = sorted(censored)  # type: ignore[index]
                else:
                    p.value = self._compute(c, values, t)
            except (ValueError, KeyError, ArithmeticError, OSError, errors.PlsiError) as exc:
                p.error = str(exc)
            out.append(p)
        return out

    def _compute(self, c: MapCoordinate, values: dict[str, Any], t: datetime) -> float:
        d = c.derive
        assert d is not None
        if derive.scalar(d):
            return derive.evaluate(d, values, birth_year=self.birth_year, sex=self.sex, at=t)
        sha = next(iter(values.values()))
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("biolearn input must be a methylation artifact")
        if not biolearn.available():
            raise errors.DependencyMissing("biolearn is not installed", hint=biolearn.HINT)
        key = (sha, t.date())
        if key not in self._clock_runs:  # every biolearn clock of this map in one run per sample and draw day
            import pandas as pd  # available whenever biolearn is

            betas = pd.read_csv(paths.safe_join(self.store.dir, "artifacts", "methylation", f"{sha}.csv.gz"), index_col=0)
            names = [x.derive.model for x in self.m.coordinates if x.derive and x.derive.engine == DeriveEngine.BIOLEARN]
            age = derive.age_at(self.birth_year, t)
            self._clock_runs[key] = biolearn.run(names, betas.set_axis(["sample"], axis=1), age=age, sex=self.subject.sex)
        value = self._clock_runs[key][d.model]
        if isinstance(value, str):
            raise ValueError(value)
        return value

    def value(self, c: MapCoordinate) -> CoordinateValue:
        if c.kind == CoordinateKind.SET:
            if not any(e.payload.kind == c.collects for e in self.events):
                return CoordinateValue(status=ValueStatus.MISSING)
            items = set_items(self.events, c.collects, self.as_of)  # type: ignore[arg-type]
            return CoordinateValue(value=items, event_ids=[i.event_id for i in items], status=ValueStatus.PRESENT)
        pts = self.points(c.id)
        if not pts:
            return CoordinateValue(unit=c.unit, status=ValueStatus.MISSING)
        last = pts[-1]
        fresh = self.as_of - last.t <= timedelta(days=c.recency_days)  # type: ignore[operator]
        v = last.coordinate_value(c, ValueStatus.PRESENT if fresh else ValueStatus.STALE)
        v.series = [SeriesPoint(t=p.t, value=p.value, comparator=p.comparator, event_ids=p.event_ids)
                    for p in pts if p.error is None and isinstance(p.value, float)][-SERIES_CAP:]
        return v

    def unmapped(self) -> list[str]:
        kinds = {c.collects for c in self.m.coordinates if c.kind == CoordinateKind.SET}
        hit = {e.id for c in self.m.coordinates if c.kind == CoordinateKind.MEASURED for e in self.matches(c)}
        return [e.id for e in self.events if e.id not in hit and e.payload.kind not in kinds]


# ------------------------------------------------------------------ the v0.2.1 mapper functions that used it

from plsi import clock  # noqa: E402
from plsi.map.mapper import _gaps  # noqa: E402
from plsi.map.models import CareContext, ObservedValue, StateDocument  # noqa: E402


def reference_state(instance: str, m: MapContract, as_of: datetime | None = None) -> StateDocument:
    as_of = as_of or clock.now()
    store = EventStore(instance)
    proj = Projection(store, m, as_of)
    coordinates = {c.id: proj.value(c) for c in m.coordinates}
    return StateDocument(
        subject_id=store.subject.subject_id, map_id=m.id, map_version=m.map_version, map_hash=m.compute_hash(),
        as_of=as_of, generated_at=clock.now(), coordinates=coordinates,
        gaps=[g for c in m.coordinates for g in _gaps(c, coordinates[c.id])], unmapped_observations=proj.unmapped(),
    ).sealed()


def reference_context(instance: str, as_of: datetime | None = None) -> CareContext:
    as_of = as_of or clock.now()
    events = [e for e in EventStore(instance).observations() if e.payload.effective_at <= as_of]
    return CareContext(medications=set_items(events, ObservationKind.MEDICATION, as_of),
                       conditions=set_items(events, ObservationKind.CONDITION, as_of),
                       allergies=set_items(events, ObservationKind.ALLERGY, as_of))


def reference_observations(instance: str, m: MapContract, coordinate_id: str) -> list[ObservedValue]:
    c = m.coordinate(coordinate_id)
    proj = Projection(EventStore(instance), m, None)
    return [ObservedValue(event_ids=p.event_ids, effective_at=p.t, recorded_at=p.recorded_at, effective_basis=p.basis,
                          value=p.coordinate_value(c)) for p in proj.points(c.id)]


def reference_coverage(instance: str, m: MapContract) -> list[str]:
    as_of = clock.now()
    proj = Projection(EventStore(instance), m, as_of)
    out = []
    for c in m.coordinates:
        if c.kind == CoordinateKind.SET:
            ok = any(e.payload.kind == c.collects for e in proj.events)
        else:
            days = proj.day_set(c)
            ok = bool(days) and (as_of.date() - max(days)).days <= c.recency_days
        if ok:
            out.append(c.id)
    return out
