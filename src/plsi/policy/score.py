"""Due predictions, scoring and calibration.

Observed values come only from plsi.map.mapper.coordinate_observations() resolved through each prediction's map snapshot.
Retracted plans drop out of the fold; stop/modify write void scores. One score event per prediction (dedupe key).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from statistics import fmean
from typing import Any

from plsi.clock import now, parse_time
from plsi.errors import UsageError
from plsi.events import NewEvent
from plsi.map import mapper, registry
from plsi.map.models import MapContract, ObservedValue
from plsi.paths import is_valid_id, plsi_home, validate_id
from plsi.policy.fold import plans, window
from plsi.policy.models import Plan, Prediction, PredictionScore
from plsi.vocab import Comparator, CoordinateKind, DueState, EventType, PredictionTarget, ScoreStatus, TimeBasis, ValueStatus

Open = tuple[Plan, MapContract, Prediction, datetime, datetime, datetime, datetime]


def _open(instance: str, t: datetime) -> Iterator[Open]:
    """Unscored predictions of adopted-at-some-point plans whose window has opened by t
    -> (plan, map snapshot, prediction, H, from, to, adopted_at)."""
    for view in plans(instance).values():
        if (adoption := view.adoption) is None:
            continue
        for p in view.plan.predictions:
            if p.id in view.scores or p.id not in adoption.horizons:
                continue
            m = registry.load_snapshot(instance, p.map_id, p.map_hash)
            lo, hi = window(m, p, adoption.horizons[p.id])
            if t < lo:
                continue
            yield view.plan, m, p, parse_time(adoption.horizons[p.id]), lo, hi, adoption.at


def _eligible(instance: str, m: MapContract, p: Prediction, lo: datetime, hi: datetime, adopted_at: datetime) -> list[ObservedValue]:
    """Active values in [H-W-, H+W+], effective AND recorded after adoption (no retrodiction), date not assumed."""
    return [o for o in mapper.coordinate_observations(instance, m, p.coordinate, since=lo, until=hi)
            if lo <= o.effective_at <= hi and o.effective_at > adopted_at and o.recorded_at > adopted_at
            and o.effective_basis != TimeBasis.ASSUMED and o.value.status != ValueStatus.ERROR
            and isinstance(o.value.value, float)]


def _hint(m: MapContract, p: Prediction) -> str:
    coord = m.coordinate(p.coordinate)
    if coord.kind == CoordinateKind.DERIVED:
        return f"measure the inputs of {coord.label} on one day: {', '.join(coord.derive.inputs)}"
    return f"measure {coord.label}" + (f" ({coord.unit})" if coord.unit else "")


def due(instance: str | None = None, all_instances: bool = False) -> dict[str, Any]:
    """`plsi due --instance N | --all`: due / awaiting / missed predictions (not-yet-due ones are omitted)."""
    home = plsi_home()
    if all_instances:
        names = sorted(d.name for d in home.iterdir() if is_valid_id(d.name) and (d / "subject.json").is_file()) if home.is_dir() else []
    elif instance:
        names = [validate_id(instance, "instance name")]
    else:
        raise UsageError("give --instance N or --all")
    t = now()
    out: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        items = []
        for plan, m, p, h, lo, hi, adopted_at in _open(name, t):
            state = DueState.DUE if _eligible(name, m, p, lo, hi, adopted_at) else DueState.MISSED if t > hi else DueState.AWAITING
            items.append({"prediction_id": p.id, "plan_id": plan.id, "coordinate": p.coordinate, "horizon": h.date(),
                          "window": {"from": lo.date(), "to": hi.date()}, "state": state.value, "measure_hint": _hint(m, p)})
        out[name] = items
    return {"instances": out}


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _within(y: float, comparator: Comparator | None, low: float, high: float) -> bool | None:
    """Interval hit; for a censored value only when the bound still decides it (else None)."""
    if comparator is None:
        return low <= y <= high
    decided_out = {Comparator.LT: y <= low, Comparator.LE: y < low, Comparator.GT: y >= high, Comparator.GE: y > high}[comparator]
    return False if decided_out else None


def _metrics(p: Prediction, ov: ObservedValue) -> PredictionScore:
    level = float(ov.value.value)  # type: ignore[arg-type]
    base = p.baseline.value if p.baseline and isinstance(p.baseline.value, float) else None
    change = p.target == PredictionTarget.CHANGE_FROM_BASELINE
    y = level - base if change and base is not None else level
    error = y - p.expected
    score = PredictionScore(prediction_id=p.id, status=ScoreStatus.SCORED, observed=ov.value, y=y, error=error,
                            abs_error=abs(error), within_interval=_within(y, ov.value.comparator, p.low, p.high), scored_at=now())
    if base is not None:
        expected_level = p.expected + base if change else p.expected
        score.direction_correct = _sign(expected_level - base) == _sign(level - base)
        score.beat_persistence = abs(error) < abs(level - base)
    return score


def score(instance: str) -> dict[str, Any]:
    """`plsi score --instance N`: append prediction_score events for newly scorable or missed predictions."""
    t = now()
    new: list[PredictionScore] = []
    for _plan, m, p, h, lo, hi, adopted_at in _open(instance, t):
        if eligible := _eligible(instance, m, p, lo, hi, adopted_at):
            nearest = min(eligible, key=lambda o: (abs((o.effective_at - h).total_seconds()), o.effective_at))
            new.append(_metrics(p, nearest))
        elif t > hi:
            new.append(PredictionScore(prediction_id=p.id, status=ScoreStatus.MISSED, scored_at=t))
    created = mapper.event_store(instance).append_many(
        [NewEvent(EventType.PREDICTION_SCORE, s, dedupe_key=f"score:{s.prediction_id}", dedupe=True) for s in new]) if new else []
    scored = [s.dump() for s, (_, ok) in zip(new, created, strict=True) if ok]
    return {"scored": scored, "calibration": calibration(instance)}


def _summary(rows: list[tuple[Prediction, PredictionScore]]) -> dict[str, Any]:
    scored = [(p, s) for p, s in rows if s.status == ScoreStatus.SCORED]
    hits = [(p, s) for p, s in scored if s.within_interval is not None]

    def rate(values: list[bool | None]) -> float | None:
        known = [float(v) for v in values if v is not None]
        return fmean(known) if known else None

    return {
        "n_scored": len(scored),
        "n_missed": sum(s.status == ScoreStatus.MISSED for _, s in rows),
        "within_interval_rate": rate([s.within_interval for _, s in hits]),
        "nominal_level": fmean(p.interval_level for p, _ in hits) if hits else None,
        "mean_abs_error": fmean(s.abs_error or 0.0 for _, s in scored) if scored else None,
        "beat_persistence_rate": rate([s.beat_persistence for _, s in scored]),
        "direction_accuracy": rate([s.direction_correct for _, s in scored]),
    }


def calibration(instance: str) -> dict[str, Any]:
    """Per coordinate and overall; misses always reported; void excluded."""
    rows = [(p, view.scores[p.id]) for view in plans(instance).values() for p in view.plan.predictions
            if p.id in view.scores and view.scores[p.id].status != ScoreStatus.VOID]
    coords = sorted({p.coordinate for p, _ in rows})
    return {"overall": _summary(rows), "by_coordinate": {c: _summary([r for r in rows if r[0].coordinate == c]) for c in coords}}
