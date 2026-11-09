"""`plsi revise`: a new version of one source document (a lab report re-issued, a clinic table re-shared).

Rows are matched to what the document said before by identity (kind, code or label, effective_at, specimen):
unchanged rows are skipped, changed rows are retracted with their replacement, new rows are added and rows the new
version no longer has are retracted. Writes only through EventStore; events.jsonl is never edited.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from plsi.events import NewEvent
from plsi.errors import UsageError, ValidationError
from plsi.files import decode_input, parse_input_json, read_input_bytes, sha256_hex
from plsi.life import importers
from plsi.life.dedupe import dedupe_key
from plsi.life.ingest import to_observation
from plsi.life.models import DOCUMENT_RE, Observation, Origin, Retraction
from plsi.life.store import EventStore
from plsi.vocab import EventType


def _identity(obs: Observation) -> tuple[Any, ...]:
    code = (obs.code.system.value, obs.code.code) if obs.code else obs.original_text.lower()
    return obs.kind.value, code, obs.effective_at.isoformat(), obs.specimen


def _content(obs: Observation) -> tuple[str, Any, Any, Any]:
    """The dedupe key without the file hash and row number, which change with every version of a document, plus
    the report's own range and flag (a re-issue may change only those)."""
    key = dedupe_key(obs.model_copy(update={"origin": Origin(kind=obs.origin.kind, document=obs.origin.document)}))
    return key, obs.ref_low, obs.ref_high, obs.flag


def revise(instance: str, document: str, file: str | None = None, csv: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Apply the new version of `document` (ObservationInput JSON list or canonical CSV; exactly one).

    Returns {document, counts: {unchanged, replaced, added, removed, rejected}, replaced: [{old, new}], added: [id],
             removed: [id], rejected: [{index, error}], dry_run} (dry_run writes nothing; new ids are then indexes).
    """
    if (file is None) == (csv is None):
        raise UsageError("give exactly one of --file, --csv")
    data = read_input_bytes(file or csv)  # type: ignore[arg-type]
    text = decode_input(data)
    imported = (importers.Imported.from_items(importers.observation_items(parse_input_json(text))) if file is not None
                else importers.from_csv(text, sha256_hex(data)))
    if imported.rejected and not dry_run:  # a rejected row would otherwise read as dropped, and be retracted
        raise ValidationError(f"{len(imported.rejected)} row(s) of the new version are invalid; fix them first",
                              code="REVISE_REJECTED", hint="the dry run lists them under rejected")
    if not re.match(DOCUMENT_RE, document):
        raise ValidationError(f"invalid document id: {document!r}", hint="a stable id such as the Drive file id; never a file name")
    rows = [to_observation(o.model_copy(update={"origin": o.origin.model_copy(update={"document": document})})) for o in imported.observations]

    store = EventStore(instance)
    before: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    for e in store.iter_events(EventType.OBSERVATION):
        if e.payload.origin.document == document:
            before[_identity(e.payload)].append(e)
    unchanged, replaced, added = [], [], []
    for i, obs in zip(imported.indexes, rows, strict=True):
        olds = before.get(_identity(obs))
        if not olds:
            added.append((i, obs))
            continue
        old = olds.pop(0)
        (unchanged if _content(old.payload) == _content(obs) else replaced).append((i, obs, old.id))
    removed = [e.id for olds in before.values() for e in olds]

    fresh = [(i, obs) for i, obs, _ in replaced] + added
    if dry_run:
        new_ids: list[Any] = [f"index {i}" for i, _ in fresh]
    else:
        written = store.append_many([NewEvent(EventType.OBSERVATION, obs, dedupe_key=dedupe_key(obs)) for _, obs in fresh]) if fresh else []
        new_ids = [event_id for event_id, _ in written]
        reason = f"new version of document {document}"
        retractions = [Retraction(target_event_id=old, reason=reason, replacement_event_id=new)
                       for (_, _, old), new in zip(replaced, new_ids, strict=False)]
        retractions += [Retraction(target_event_id=old, reason=f"not in the {reason}") for old in removed]
        if retractions:
            store.append_many([NewEvent(EventType.RETRACTION, r) for r in retractions])
    return {
        "document": document,
        "counts": {"unchanged": len(unchanged), "replaced": len(replaced), "added": len(added), "removed": len(removed),
                   "rejected": len(imported.rejected)},
        "replaced": [{"old": old, "new": new} for (_, _, old), new in zip(replaced, new_ids, strict=False)],
        "added": new_ids[len(replaced):],
        "removed": removed,
        "rejected": imported.rejected,
        "dry_run": dry_run,
    }
