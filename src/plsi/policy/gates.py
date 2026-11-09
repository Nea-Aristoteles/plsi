"""Plan gates G1-G8.

Gates never trust trust-fields written by the bot. World access only via plsi.map (registry, mapper).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pydantic

from plsi.clock import now
from plsi.errors import GateFailed, NetworkError, NotFound, ValidationError, violation
from plsi.events import Event
from plsi.files import load_json
from plsi.map import mapper, registry
from plsi.map.models import CoordinateValue, MapContract, StateDocument
from plsi.models import SourceRef, violations_from
from plsi.policy import evidence
from plsi.policy.fold import LIVE_STATES, PlanView, plans
from plsi.policy.models import PlanInput
from plsi.policy.screen import SCREEN_MAX_AGE_DAYS, screening_context
from plsi.vocab import CoordinateKind, EventType, EvidenceClass, GateId, PredictionTarget, Severity, TimeBasis, ValueStatus

SOURCE_UNVERIFIED = f"SOURCE_UNVERIFIED: no ok ledger entry within {evidence.LEDGER_MAX_AGE_DAYS} days"


@dataclass
class GateContext:
    """What the gates looked up: each map the predictions name, its projected state and the newest valid screening
    event per action."""

    maps: dict[str, MapContract] = field(default_factory=dict)
    states: dict[str, StateDocument] = field(default_factory=dict)
    screening: dict[str, Event] = field(default_factory=dict)


def numeric(cv: CoordinateValue | None) -> bool:
    return cv is not None and cv.status == ValueStatus.PRESENT and isinstance(cv.value, float)


def _screening(instance: str, action_hash: str) -> list[Event]:
    subject_id = mapper.subject(instance).subject_id
    return [e for e in mapper.event_store(instance).events(EventType.SCREENING)
            if e.subject_id == subject_id and e.payload.action_hash == action_hash]


def _matching_screening(instance: str, plan: PlanInput) -> tuple[dict[str, Event], list[dict[str, str]]]:
    """G2 + G3: the newest valid screening event per action local_id, and the violations."""
    care = mapper.context(instance)
    found: dict[str, Event] = {}
    out = []
    for i, action in enumerate(plan.actions):
        path, hint = f"actions.{i}", f"plsi screen --instance {instance} --file plan.json"
        events = _screening(instance, action.compute_action_hash())
        ctx_hash = screening_context(care, plan, action).compute_context_hash()
        current = [e for e in events if e.payload.context_hash == ctx_hash]
        fresh = [e for e in current if now() - e.recorded_at <= timedelta(days=SCREEN_MAX_AGE_DAYS)]
        if not fresh:
            why = ("no screening event for this action" if not events else "care context changed since screening" if not current
                   else f"screening is older than {SCREEN_MAX_AGE_DAYS} days")
            out.append(violation(path, GateId.G2, why, hint))
            continue
        found[action.local_id] = fresh[-1]
        record = fresh[-1].payload
        if record.outcome == Severity.BLOCK:
            out.append(violation(path, GateId.G3, "screening outcome is block: " + "; ".join(
                f.summary for f in record.findings if f.severity == Severity.BLOCK), "drop or replace this action"))
        elif record.outcome == Severity.CAUTION and not (action.acknowledged and action.acknowledgement_note):
            out.append(violation(path, GateId.G3, "screening outcome is caution and the action is not acknowledged",
                                 "after the user's question-card confirmation set acknowledged=true and acknowledgement_note"))
    return found, out


def _g1(instance: str, path: str, m: MapContract) -> tuple[StateDocument | None, list[dict[str, str]]]:
    """Freshness of one map's latest.json; `path` is the first prediction naming the map."""
    hint = f"re-project: plsi project --instance {instance} --map {m.id}"
    state = mapper.latest_state(instance, m.id)
    if state is None:
        return None, [violation(path, GateId.G1, f"{m.id}: no projected state (latest.json); re-project", hint)]
    problems = []
    if not state.verify_hash():
        problems.append("latest.json state_hash does not verify")
    if state.map_hash != m.compute_hash():
        problems.append("latest.json was projected with a different map version")
    if not mapper.is_current(state, mapper.last_world_change(instance)):
        problems.append("observations changed after the last projection")
    return state, [violation(path, GateId.G1, f"{m.id}: {p}; re-project", hint) for p in problems]


def _g5(plan: PlanInput, live_verify: bool) -> list[dict[str, str]]:
    refs: list[tuple[str, SourceRef]] = [(f"claims.{i}.sources.{j}", s) for i, c in enumerate(plan.claims) for j, s in enumerate(c.sources)]
    refs += [(f"predictions.{i}.basis.{j}", s) for i, p in enumerate(plan.predictions) for j, s in enumerate(p.basis)]
    seen: dict[tuple[str, str], str | None] = {}
    out = []
    for path, ref in refs:
        key = (ref.kind.value, ref.id)
        if key not in seen:
            seen[key] = None if evidence.is_verified(ref) else SOURCE_UNVERIFIED
            if seen[key] and live_verify:
                try:
                    seen[key] = None if evidence.verify(*key)["ok"] else f"{ref.kind.value} {ref.id} does not resolve"
                except NetworkError as exc:
                    seen[key] = f"SOURCE_UNVERIFIED: live verification failed ({exc.code})"
                except ValidationError as exc:
                    seen[key] = exc.message
        if seen[key]:
            out.append(violation(path, GateId.G5, seen[key], f"plsi evidence verify --kind {ref.kind.value} --id {ref.id}"))
    return out


def _g6(plan: PlanInput, maps: dict[str, MapContract]) -> list[dict[str, str]]:
    """The strictest min_evidence_class_for_actions of the plan's maps applies to every action."""
    m = max(maps.values(), key=lambda x: x.inference_boundaries.min_evidence_class_for_actions.rank)
    need = m.inference_boundaries.min_evidence_class_for_actions
    out = []
    for i, a in enumerate(plan.actions):
        best = EvidenceClass.top(c.evidence_class for c in plan.claims if a.local_id in c.supports)
        if best is None or best.rank < need.rank:
            out.append(violation(f"actions.{i}", GateId.G6, f"strongest supporting claim is {best.value if best else 'none'}; "
                                 f"map {m.id} requires {need.value}", "add a claim with stronger verified evidence"))
    return out


def _g7(instance: str, plan: PlanInput, ctx: GateContext) -> list[dict[str, str]]:
    """Each prediction's coordinate against its own map and that map's projected state."""
    actions = {a.local_id for a in plan.actions}
    out = []
    for i, p in enumerate(plan.predictions):
        if (m := ctx.maps.get(p.map_id)) is None:
            continue  # unknown map: reported once by run()
        path, coord, state = f"predictions.{i}", m.coordinate(p.coordinate), ctx.states.get(p.map_id)
        if coord is None or not coord.predictable or coord.kind == CoordinateKind.SET:
            out.append(violation(f"{path}.coordinate", GateId.G7, f"{p.coordinate} is not a predictable coordinate of {m.id}"))
            continue
        out += [violation(f"{path}.driven_by", GateId.G7, f"unknown action {d}") for d in p.driven_by if d not in actions]
        baseline = state.coordinates.get(p.coordinate) if state else None
        if p.target == PredictionTarget.CHANGE_FROM_BASELINE and not numeric(baseline):
            out.append(violation(f"{path}.target", GateId.G7, "change_from_baseline requires a present baseline", "use target level"))
        if numeric(baseline):
            ids = set(baseline.event_ids)
            if any(set(o.event_ids) == ids and o.effective_basis == TimeBasis.ASSUMED
                   for o in mapper.coordinate_observations(instance, m, p.coordinate, since=baseline.observed_at, until=baseline.observed_at)):
                out.append(violation(f"{path}.coordinate", GateId.G7, "the baseline value has an assumed date",
                                     "ingest the result again with a document or user-stated date"))
    return out


def run(instance: str, plan: PlanInput, *, live_verify: bool = False, skip: frozenset[GateId] = frozenset(),
        views: dict[str, PlanView] | None = None) -> tuple[GateContext, list[dict[str, str]]]:
    """Run G1-G8 (minus `skip`) -> (what they looked up, ALL violations [{path, rule, message, hint}]).
    `views` is the plan fold when the caller already has it (G8)."""
    ctx, out = GateContext(), []
    for map_id in plan.map_ids():
        path = f"predictions.{next(i for i, p in enumerate(plan.predictions) if p.map_id == map_id)}.map_id"
        try:
            ctx.maps[map_id] = registry.get(map_id, instance)
        except NotFound as exc:
            out.append(violation(path, GateId.G1, exc.message, "plsi maps list"))
            continue
        if GateId.G1 not in skip:
            state, found = _g1(instance, path, ctx.maps[map_id])
            out += found
            if state is not None:
                ctx.states[map_id] = state
    if not {GateId.G2, GateId.G3} <= skip:
        ctx.screening, found = _matching_screening(instance, plan)
        out += [v for v in found if v["rule"] not in skip]
    if GateId.G5 not in skip:
        out += _g5(plan, live_verify)
    if ctx.maps and GateId.G6 not in skip:
        out += _g6(plan, ctx.maps)
    if GateId.G7 not in skip:
        out += _g7(instance, plan, ctx)
    if plan.supersedes and GateId.G8 not in skip:
        view = (plans(instance) if views is None else views).get(plan.supersedes)
        if view is None or view.state not in LIVE_STATES:
            out.append(violation("supersedes", GateId.G8, f"{plan.supersedes} is not a proposed or adopted plan of this subject"))
    return ctx, out


def require(instance: str, plan: PlanInput, *, skip: frozenset[GateId] = frozenset(),
            views: dict[str, PlanView] | None = None) -> GateContext:
    """All gates (minus `skip`) with live verification; raises GateFailed listing every violation."""
    ctx, found = run(instance, plan, live_verify=True, skip=skip, views=views)
    if found:
        raise GateFailed(found)
    return ctx


def parse_plan(data: Any) -> PlanInput:
    """G4 lives in the contract: claim coherence and resolving supports are parse errors, reported as GateFailed."""
    try:
        return PlanInput.model_validate(data)
    except pydantic.ValidationError as exc:
        found = violations_from(exc)
        raise GateFailed([{**v, "rule": GateId.G4} if v["path"].startswith("claims") else v for v in found]) from None


def check(instance: str, file: str) -> dict[str, Any]:
    """`plan check`: all gates (live verification), writes nothing except ledger lines."""
    require(instance, parse_plan(load_json(file)))
    return {"ok": True, "violations": []}
