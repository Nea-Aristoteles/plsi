"""The projection of one map over an instance's active observations: which observations each coordinate accepts,
its measured and derived values (with provenance) and the days they exist. Used only by plsi.map.mapper.

Nothing here holds the history in memory. Observations are found through the event index (plsi.life.observed) in
(effective time, log order) order and only the few that a value, a series point or a day needs are read back from the
log: the latest points of a coordinate come from the newest end of its scan, a derived coordinate needs the latest
point of each day, and the unmapped count is a group-by. Results equal the earlier in-memory projection exactly."""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import chain
from typing import Any

from plsi import errors, paths
from plsi.events import Event
from plsi.life import observed
from plsi.life.index_rows import micros
from plsi.life.store import EventStore
from plsi.life.observed import Wanted
from plsi.map import biolearn, derive, units
from plsi.map.models import SERIES_CAP, CoordinateValue, Derive, MapContract, MapCoordinate, Provenance, SeriesPoint, SetItem
from plsi.vocab import ClinicalStatus, Comparator, CoordinateKind, DeriveEngine, MatchKind, ObservationKind, TimeBasis, ValueStatus


def hybrid(c: MapCoordinate) -> bool:
    """A measured coordinate that is also derived when not measured (BMI): accepts.codes and derive."""
    return c.derive is not None and bool(c.accepts.codes)


def accepts_pair(c: MapCoordinate, system: str, code: str, fasting: int | None) -> bool:
    """Whether the coordinate accepts an observation with this code and fasting flag (1, 0 or None)."""
    if any((r.system.value, r.code) == (system, code) for r in c.accepts.reject_codes):
        return False
    entry = next((x for x in c.accepts.codes if (x.system.value, x.code) == (system, code)), None)
    if entry is None or not c.accepts.requires_fasting:
        return entry is not None
    # Fasting-specific codes (their display says fasting) qualify unless the observation says it was not fasting.
    return fasting == 1 or (fasting is None and "fasting" in (entry.display or "").lower())


def wanted(c: MapCoordinate) -> list[Wanted]:
    """The codes a coordinate accepts as index scan terms: the same set accepts_pair lets through."""
    out: dict[tuple[str, str], Wanted] = {}
    for x in c.accepts.codes:
        pair = (x.system.value, x.code)
        if pair in out or any((r.system.value, r.code) == pair for r in c.accepts.reject_codes):
            continue
        null_ok = "fasting" in (x.display or "").lower()
        out[pair] = Wanted(*pair, None if not c.accepts.requires_fasting else null_ok)
    return list(out.values())


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


def set_items(store: EventStore, kind: ObservationKind, as_of: datetime) -> list[SetItem]:
    """Active items of one kind: the latest observation per item (code, else lower text) is active/unknown and open."""
    latest: dict[tuple[str, Any], observed.ItemRow] = {}
    for row in observed.kind_rows(store.index, ObservationKind(kind).value, micros(as_of)):
        latest[(row.code_system, row.code) if row.code_system else ("text", row.text_key)] = row
    events = store.load(latest.values())
    items = [
        SetItem(text=e.payload.original_text, code=e.payload.code, dose=e.payload.dose,
                dose_quantity=e.payload.dose_quantity, dose_unit=e.payload.dose_unit,
                clinical_status=e.payload.clinical_status, event_id=e.id)
        for e in events
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
    def __init__(self, store: EventStore, m: MapContract, as_of: datetime | None) -> None:
        """Read through `store`'s index (call inside `store.snapshot()` for one consistent view)."""
        self.store, self.m, self.as_of = store, m, as_of
        self.subject = store.subject
        self.birth_year, self.sex = self.subject.birth_year, self.subject.sex.value
        self.hi = None if as_of is None else micros(as_of)
        self._per_day_cache: dict[str, dict[date, Point]] = {}
        self._derived_cache: dict[str, list[Point]] = {}
        self._clock_runs: dict[tuple[str, date], dict[str, float | str]] = {}

    def _rows(self, c: MapCoordinate, *, lo: int | None = None, hi: int | None = None, descending: bool = False) -> Iterator[observed.Row]:
        """Index rows of the observations the coordinate accepts, by effective time then log order (until as_of)."""
        top = self.hi if hi is None else hi if self.hi is None else min(hi, self.hi)
        return observed.scan(self.store.index, wanted(c), hi=top, lo=lo, descending=descending)

    def _points_of(self, c: MapCoordinate, rows: Iterator[observed.Row]) -> Iterator[Point]:
        for row in rows:
            yield self._measured(c, self.store.load([row])[0])

    def _measured_desc(self, c: MapCoordinate) -> Iterator[Point]:
        return self._points_of(c, self._rows(c, descending=True))

    def _measured_last_per_day(self, c: MapCoordinate) -> dict[date, Point]:
        """The last accepted observation of each calendar day as a point (what a derived coordinate reads)."""
        last: dict[date, observed.Row] = {}
        for row in self._rows(c):
            last[observed.day_of(row.eff)] = row
        return {day: self._measured(c, e) for day, e in zip(last, self.store.load(last.values()), strict=True)}

    def _per_day(self, cid: str) -> dict[date, Point]:
        """Latest point of each day of a measured, derived or hybrid coordinate (hybrid: the measured one wins)."""
        if cid not in self._per_day_cache:
            c = self.m.coordinate(cid)
            assert c is not None
            out: dict[date, Point] = {}
            if c.derive:
                out |= {p.t.date(): p for p in self._derived(c)}
            if not c.derive or hybrid(c):
                out |= self._measured_last_per_day(c)
            self._per_day_cache[cid] = out
        return self._per_day_cache[cid]

    def points_desc(self, cid: str) -> Iterator[Point]:
        """Values of a measured or derived coordinate, newest first (hybrid: both; on one day the measured last)."""
        c = self.m.coordinate(cid)
        assert c is not None
        if hybrid(c):
            key = lambda p: (p.t.date(), p.provenance.match == MatchKind.CODE, p.t)  # noqa: E731
            return heapq.merge(self._measured_desc(c), reversed(self._derived(c)), key=key, reverse=True)
        if c.derive:
            return iter(reversed(self._derived(c)))
        return self._measured_desc(c)

    def points(self, cid: str, *, since: datetime | None = None, until: datetime | None = None) -> list[Point]:
        """Values of a coordinate oldest first, optionally only those effective in [since, until]. Unbounded, this holds
        every value of a measured coordinate: callers that need only a window or a few latest values say so."""
        c = self.m.coordinate(cid)
        assert c is not None
        if since is None and until is None:
            return list(reversed(list(self.points_desc(cid))))
        lo, hi = None if since is None else micros(since), None if until is None else micros(until)
        inside = [p for p in (self._derived(c) if c.derive else []) if (since is None or p.t >= since) and (until is None or p.t <= until)]
        if not c.derive or hybrid(c):
            inside += list(self._points_of(c, self._rows(c, lo=lo, hi=hi)))
        if c.derive:
            inside.sort(key=lambda p: (p.t.date(), p.provenance.match == MatchKind.CODE, p.t))
        return inside

    def day_set(self, c: MapCoordinate) -> set[date]:
        """Calendar days with a value (derived: days on which every same-day input has one, after the first value of
        each carried input; hybrid: plus the measured days); computes nothing."""
        measured_days = observed.days(self.store.index, wanted(c), self.hi) if c.accepts.codes else set()
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
        if c.id not in self._derived_cache:
            self._derived_cache[c.id] = self._derive(c)
        return self._derived_cache[c.id]

    def _derive(self, c: MapCoordinate) -> list[Point]:
        d = c.derive
        assert d is not None
        per_day = {i: self._per_day(i) for i in d.inputs}  # latest value of each day
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
            if not observed.has_kind(self.store.index, ObservationKind(c.collects).value, self.hi):
                return CoordinateValue(status=ValueStatus.MISSING)
            items = set_items(self.store, c.collects, self.as_of)  # type: ignore[arg-type]
            return CoordinateValue(value=items, event_ids=[i.event_id for i in items], status=ValueStatus.PRESENT)
        newest = self.points_desc(c.id)
        last = next(newest, None)
        if last is None:
            return CoordinateValue(unit=c.unit, status=ValueStatus.MISSING)
        fresh = self.as_of - last.t <= timedelta(days=c.recency_days)  # type: ignore[operator]
        v = last.coordinate_value(c, ValueStatus.PRESENT if fresh else ValueStatus.STALE)
        series: list[Point] = []
        for p in chain([last], newest):
            if p.error is None and isinstance(p.value, float):
                series.append(p)
                if len(series) == SERIES_CAP:
                    break
        v.series = [SeriesPoint(t=p.t, value=p.value, comparator=p.comparator, event_ids=p.event_ids) for p in reversed(series)]  # type: ignore[arg-type]
        return v

    def unmapped(self, cap: int) -> tuple[list[str], int]:
        """Ids (first `cap`, log order) and the number of active observations no measured coordinate accepts and no set collects."""
        index = self.store.index
        measured = [c for c in self.m.coordinates if c.kind == CoordinateKind.MEASURED]
        kinds = {ObservationKind(c.collects).value for c in self.m.coordinates if c.kind == CoordinateKind.SET}
        loose = {(k, s, c, f): n for k, s, c, f, n in observed.combos(index, self.hi)
                 if k not in kinds and not (s is not None and any(accepts_pair(m, s, c, f) for m in measured))}
        total = sum(loose.values())
        ids: list[str] = []
        if total:
            for _, event_id, k, s, c, f in observed.observation_rows(index, self.hi):
                if (k, s, c, f) in loose:
                    ids.append(event_id)
                    if len(ids) == min(cap, total):
                        break
        return ids, total
