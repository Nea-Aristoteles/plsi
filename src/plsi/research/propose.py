"""Candidate topics for the topic card, assembled from what the record and the field already say. Chooses nothing.

A personal topic must point at the data it came from (`medication:<event>`, `symptom:<event>`,
`<map>:<coordinate>`), and the pointer is checked against the record before the topic is created. An open
question in the field cannot be read off this computer: the bot proposes it and backs it with a source id.
"""

from __future__ import annotations

from typing import Any

from plsi.errors import ValidationError
from plsi.life.store import EventStore
from plsi.map import mapper
from plsi.mothership.public import findings
from plsi.research import notes, store
from plsi.vocab import EventType, ObservationKind
from plsi.vocab.research import PERSONAL_SOURCES, TopicSource

MOVED_BY = 0.2  # a coordinate whose series changed by this fraction is worth a question
MAX_PER_KIND = 6


def _covered(topics: list[dict[str, Any]], filed: list[dict[str, Any]], about: str) -> str | None:
    """The research topic or note that already covers this subject, if any. This card is the one place where
    both research bots' work is visible, so it is where a question opened twice is caught."""
    needle = about.strip().lower()
    hit = next((t["topic"] for t in topics if needle and needle in
                f"{t['topic']} {t.get('question_in_chief') or ''}".lower()), None)
    if hit:
        return f"the research topic {hit!r}"
    note = next((n for n in filed if needle and needle in str(n.get("title") or "").lower()), None)
    return f"the research note {note['name']!r}" if note else None


def _personal(instance: str) -> list[dict[str, Any]]:
    """Symptoms, medications, conditions, map gaps and markers that moved, from the State Map and care context."""
    out: list[dict[str, Any]] = []
    care = mapper.context(instance)
    out += _symptoms(instance)
    for kind, items in (("medication", care.medications), ("condition", care.conditions)):
        for item in items[:MAX_PER_KIND]:
            out.append({
                "kind": str(TopicSource.PERSONAL_CASE), "about": item.text,
                "rests_on": [f"{kind}:{item.event_id}"],
                "why": f"an active {kind} in the record"
                       + (f", {item.dose}" if item.dose else ""),
                "ask": f"what is known about {item.text} in someone with this profile, "
                       f"and what would explain any effect they have noticed?",
            })
    for state in mapper.latest_states(instance):
        for gap in state.gaps[:MAX_PER_KIND]:
            out.append({
                "kind": str(TopicSource.PERSONAL_DATA), "about": gap.coordinate,
                "rests_on": [f"{state.map_id}:{gap.coordinate}"],
                "why": f"the {state.map_id} map says this is missing: {gap.reason}",
                "ask": f"does {gap.coordinate} matter enough to measure, and what would it change?",
            })
        for name, value in state.coordinates.items():
            series = [p for p in (value.series or []) if p.value is not None]
            if len(series) < 2:
                continue
            first, last = series[0].value, series[-1].value
            if first and abs(last - first) / abs(first) >= MOVED_BY:
                out.append({
                    "kind": str(TopicSource.PERSONAL_DATA), "about": name,
                    "rests_on": [f"{state.map_id}:{name}"],
                    "why": f"moved from {first} to {last} across {len(series)} readings",
                    "ask": f"what moves {name} in the direction it moved, and is the change meaningful?",
                })
    return out


def _symptoms(instance: str) -> list[dict[str, Any]]:
    """Symptoms the person reported, grouped by wording, with when they were first reported."""
    seen: dict[str, list[Any]] = {}
    for event in EventStore(instance).iter_events(EventType.OBSERVATION, kinds=[ObservationKind.SYMPTOM]):
        if event.payload.kind == ObservationKind.SYMPTOM:
            seen.setdefault(str(event.payload.original_text), []).append(event)
    out = []
    for text, events in list(seen.items())[:MAX_PER_KIND]:
        first = min(events, key=lambda e: e.payload.effective_at)
        out.append({
            "kind": str(TopicSource.PERSONAL_CASE), "about": text,
            "rests_on": [f"symptom:{e.id}" for e in events],
            "why": f"reported {len(events)} time(s), first on {first.payload.effective_at.date().isoformat()}",
            "ask": f"what explains {text}, and does anything in this record line up with when it started?",
        })
    return out


def propose(instance: str | None = None, online: bool = False) -> dict[str, Any]:
    """`research propose`: grounded candidates for the topic card. Chooses nothing."""
    topics = []
    for name in store.list_topics():
        graph = store.load(name)
        topics.append({"topic": name, "question_in_chief": graph.topic.get("question_in_chief"),
                       "status": str(graph.topic.get("status")), "source": graph.topic.get("source")})
    candidates = _personal(instance) if instance else []
    filed = notes.list_notes() if candidates else []
    for candidate in candidates:
        candidate["already_covered_by"] = _covered(topics, filed, candidate["about"])

    field: list[dict[str, Any]] = []
    if online:
        for item in (findings().get("findings") or {}).get("findings", [])[:MAX_PER_KIND]:
            field.append({"kind": str(TopicSource.OPEN_QUESTION), "about": str(item.get("title") or item)[:120],
                          "rests_on": [f"mothership:{item.get('id')}"] if item.get("id") else [],
                          "why": "on the Longevity Mothership findings ledger",
                          "ask": "what would move this finding from where it stands?"})

    return {
        "already_have": topics,
        "candidates": [c for c in candidates if not c["already_covered_by"]],
        "already_covered": [c for c in candidates if c["already_covered_by"]],
        "from_the_field": field,
        "field_is_grounded": online,
        "how_to_use": (
            "Show the user three, at least one personal and one from the field, each as a curious non-scientist "
            "would ask it. The user picks, writes their own, or leaves it to you - then "
            "`plsi research topic --new ... --kind ... --because ... --rests-on ...`. "
            "An open question is not on this computer: propose it from what you know and back it with a source id "
            "that `plsi evidence verify` resolves."),
    }


def check_source(kind: str, rests_on: list[str], instance: str | None) -> None:
    """Refuse a topic that rests on nothing, and a personal topic whose pointers do not resolve in the record."""
    if kind not in tuple(TopicSource):
        raise ValidationError(f"unknown topic kind {kind!r}",
                              hint=f"one of {', '.join(k.value for k in TopicSource)}")
    if not rests_on:
        raise ValidationError("a topic records what it rests on",
                              hint="--rests-on 'symptom:<event>' | 'medication:<event>' | 'condition:<event>' | "
                                   "'<map>:<coordinate>' | 'PMID:<id>' | 'user'")
    if TopicSource(kind) not in PERSONAL_SOURCES:
        return
    if not instance:
        raise ValidationError(f"a {kind} topic needs the instance whose record it rests on",
                              hint="--instance <name>")
    known = {r for c in _personal(instance) for r in c["rests_on"]}
    missing = [r for r in rests_on if r not in known and r != "user"]
    if missing:
        raise ValidationError(
            f"nothing in the record matches {', '.join(missing)}", code="SOURCE_NOT_FOUND",
            hint="this record offers: " + (", ".join(sorted(known)[:12]) or "nothing") +
                 ". `plsi research propose --instance <name>` shows each with what it is")
