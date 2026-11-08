"""What the event index stores per event, and how a row is made: the schema, the row mapping, the fingerprint that
ties an index file to the code that wrote it, and reading one log record back into an Event."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pydantic

from plsi.errors import ValidationError
from plsi.events import PAYLOAD_TYPES, Event
from plsi.life.dedupe import dedupe_key
from plsi.life.models import Observation
from plsi.vocab import EventType

INDEX_VERSION = 1
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DAY_US = 86_400_000_000

SCHEMA = (
    "CREATE TABLE meta(k TEXT PRIMARY KEY, v) WITHOUT ROWID",
    """CREATE TABLE events(seq INTEGER PRIMARY KEY, id TEXT NOT NULL, off INTEGER NOT NULL, ln INTEGER NOT NULL,
       type TEXT NOT NULL, rec INTEGER NOT NULL, gone INTEGER NOT NULL DEFAULT 0, key BLOB, skey BLOB,
       kind TEXT, code_system TEXT, code TEXT, text_key BLOB, eff INTEGER, fasting INTEGER, origin TEXT)""",
    "CREATE INDEX ix_id ON events(id)",
    "CREATE INDEX ix_key ON events(key) WHERE key IS NOT NULL",
    "CREATE INDEX ix_skey ON events(skey) WHERE skey IS NOT NULL",
    "CREATE INDEX ix_code ON events(code_system, code, eff, seq) WHERE code_system IS NOT NULL",
    "CREATE INDEX ix_kind ON events(kind, eff, seq) WHERE kind IS NOT NULL",
    "CREATE TABLE retractions(target TEXT PRIMARY KEY) WITHOUT ROWID",
    "CREATE TABLE torn(line INTEGER PRIMARY KEY)",
)
INSERT = "INSERT INTO events(id, off, ln, type, rec, key, skey, kind, code_system, code, text_key, eff, fasting, origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"


def micros(t: datetime) -> int:
    d = t - EPOCH
    return (d.days * 86400 + d.seconds) * 1_000_000 + d.microseconds


def from_micros(n: int) -> datetime:
    return EPOCH + timedelta(microseconds=n)


def blob(key: str) -> bytes | str:
    """A sha256 hex key as 32 bytes; anything else (a hostile or foreign key) stays text."""
    try:
        raw = bytes.fromhex(key)
    except ValueError:
        return key
    return raw if len(raw) == 32 and raw.hex() == key else key




def load_event(raw: Any, record: int) -> Event:
    """One parsed log line -> Event with its payload model; an invalid record is refused, never skipped."""
    try:
        event = Event.model_validate(raw)
        if payload_type := PAYLOAD_TYPES.get(event.type):
            event.payload = payload_type.model_validate(event.payload)
    except pydantic.ValidationError as exc:
        raise ValidationError(f"events.jsonl record {record} is not a valid event: {exc.errors()[0]['msg']}", code="CORRUPT_EVENT") from None
    return event


def row_of(event: Event, off: int, ln: int) -> tuple[Any, ...]:
    """The index row of one event (without seq), computed the same way when a line is written and when it is scanned."""
    p = event.payload
    stored = event.dedupe_key
    if event.type == EventType.OBSERVATION and isinstance(p, Observation):
        key = blob(dedupe_key(p))  # recomputed from the stored payload, as ingest always did
        s = blob(stored) if stored else b""
        text_key = None if p.code else hashlib.sha256(p.original_text.lower().encode("utf-8")).digest()
        return (event.id, off, ln, event.type.value, micros(event.recorded_at), key, None if s == key else s, p.kind.value,
                p.code.system.value if p.code else None, p.code.code if p.code else None, text_key,
                micros(p.effective_at), None if p.fasting is None else int(p.fasting), p.origin.kind.value)
    return (event.id, off, ln, event.type.value, micros(event.recorded_at), blob(stored) if stored else None, None,
            None, None, None, None, None, None, None)


def _fingerprint() -> str:
    """Hash of everything a stored row depends on: version, schema, the key definition (probed on fixed observations,
    covering unit normalization) and the row mapping. A change in any of them makes every old index stale."""
    probes = []
    for n, (text, code, unit) in enumerate([("Probe A", {"system": "loinc", "code": "2345-7"}, "mg/dl"), ("Probe B", None, " MMOL/L "),
                                            ("Probe C", {"system": "free", "code": "x"}, "10^3/uL"), ("Probe D", None, None)]):
        o = Observation.model_validate({
            "kind": "measurement", "original_text": text, "code": code, "value_quantity": 1.5, "unit": unit, "unit_original": unit,
            "effective_at": "2026-01-02T03:04:05.678901Z", "origin": {"kind": "csv", "file_sha256": "a" * 64, "record_id": f"r{n}"},
            "fasting": n % 2 == 0, "clinical_status": "active", "effective_end": "2026-01-03"})
        event = Event.model_validate({"id": "evt_" + "0" * 16, "type": "observation", "recorded_at": "2026-01-01T00:00:00Z", "subject_id": "s", "dedupe_key": dedupe_key(o), "payload": o})
        probes.append([repr(row_of(event, 1, 2))])
    return hashlib.sha256(json.dumps([INDEX_VERSION, SCHEMA, INSERT, probes]).encode()).hexdigest()


_FINGERPRINT: list[str] = []


def fingerprint() -> str:
    if not _FINGERPRINT:
        _FINGERPRINT.append(_fingerprint())
    return _FINGERPRINT[0]
