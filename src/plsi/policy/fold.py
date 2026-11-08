"""Plans folded from the log: each plan with its plan_status history and prediction scores, in log order.

One pass over the active events; retracted plans drop out with their scores. World access only via plsi.map.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from plsi.clock import parse_time
from plsi.map import mapper
from plsi.map.models import MapContract
from plsi.policy.models import Plan, PlanStatusPayload, Prediction, PredictionScore
from plsi.vocab import EventType, PlanDecision, PlanState

LIVE_STATES = (PlanState.PROPOSED, PlanState.ADOPTED)


@dataclass
class PlanView:
    """One plan folded from the log: the stored plan, its status history and the first score of each prediction."""

    plan: Plan
    statuses: list[PlanStatusPayload] = field(default_factory=list)
    scores: dict[str, PredictionScore] = field(default_factory=dict)

    @property
    def state(self) -> PlanState:
        return self.statuses[-1].state_after if self.statuses else PlanState.PROPOSED

    @property
    def adoption(self) -> PlanStatusPayload | None:
        return next((s for s in reversed(self.statuses) if s.decision == PlanDecision.ADOPT), None)


def plans(instance: str) -> dict[str, PlanView]:
    """Every active (not retracted) plan of the instance, folded over plan_status and prediction_score events."""
    out: dict[str, PlanView] = {}
    owner: dict[str, PlanView] = {}  # prediction id -> its plan
    for e in mapper.event_store(instance).events((EventType.PLAN, EventType.PLAN_STATUS, EventType.PREDICTION_SCORE)):
        if e.type == EventType.PLAN:
            out[e.payload.id] = owner_view = PlanView(e.payload)
            owner |= {p.id: owner_view for p in e.payload.predictions}
        elif e.type == EventType.PLAN_STATUS and e.payload.plan_id in out:
            out[e.payload.plan_id].statuses.append(e.payload)
        elif e.type == EventType.PREDICTION_SCORE and e.payload.prediction_id in owner:
            owner[e.payload.prediction_id].scores.setdefault(e.payload.prediction_id, e.payload)  # one score per prediction
    return out


def window(m: MapContract, pred: Prediction, horizon: date) -> tuple[datetime, datetime]:
    """[H - W-, H + W+] from the coordinate in the prediction's map snapshot `m`."""
    w = m.coordinate(pred.coordinate).score_window_days
    h = parse_time(horizon)
    return h - timedelta(days=w.before), h + timedelta(days=w.after)
