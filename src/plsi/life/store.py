"""Instance storage: subject.json and the append-only events.jsonl (never edited in place)."""

from __future__ import annotations

import contextlib
import re
import secrets
import shutil
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from plsi.clock import now
from plsi.errors import NotFound, ValidationError
from plsi.files import append_jsonl_many, atomic_write_json, load_json, lock
from plsi.events import Event, NewEvent, PAYLOAD_TYPES
from plsi.life import observed
from plsi.life.deid import deid_subject_fields
from plsi.life.index import LogIndex, from_micros
from plsi.life.models import Subject
from plsi.paths import instance_dir, new_id, plsi_home, validate_id
from plsi.vocab import Ethnicity, EventType, ObservationKind, Relation, Sex


def _dump(event: Event) -> dict[str, Any]:
    payload = event.payload
    data = event.model_dump(mode="json", exclude_none=True, exclude={"payload"})
    data["payload"] = payload.model_dump(mode="json", exclude_none=True) if isinstance(payload, BaseModel) else payload
    return data


def _types(type: EventType | str | Iterable[EventType | str] | None) -> list[str]:
    if type is None:
        return []
    return [EventType(t).value for t in ([type] if isinstance(type, str) else type)]


class EventStore:
    """The instance's append-only log. Reads and the dedupe check go through the log index (plsi.life.index): memory and
    time depend on what a call returns, not on how many events are stored."""

    def __init__(self, instance: str) -> None:
        self.instance = instance
        self.dir = instance_dir(instance)
        self.path = self.dir / "events.jsonl"
        self._index: LogIndex | None = None

    @property
    def subject(self) -> Subject:
        return Subject.model_validate(load_json(self.dir / "subject.json"))

    @property
    def index(self) -> LogIndex:
        """The log index, made current with the log (built on first use; see plsi.life.index)."""
        if self._index is None:
            self._index = LogIndex(self.dir, self.path)
        self._index.sync()
        return self._index

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[LogIndex]:
        """One consistent view of the log for a series of reads: the events indexed when it began."""
        if self._index is None:
            self._index = LogIndex(self.dir, self.path)
        with self._index.snapshot() as ix:
            yield ix

    # -------------------------------------------------------------- writing

    def append(self, type: EventType, payload: BaseModel, *, dedupe_key: str | None = None, dedupe: bool = False) -> tuple[str, bool]:
        """Append one event under the instance lock -> (event_id, created). created=False means a dedupe hit."""
        return self.append_many([NewEvent(type, payload, dedupe_key, dedupe)])[0]

    def append_many(self, items: Iterable[NewEvent]) -> list[tuple[str, bool]]:
        """Append events in order under ONE lock; ids and recorded_at are tool-set. Payloads are re-validated. An event
        with dedupe set whose key an active event already has is not appended (its id is returned, created False)."""
        batch = list(items)
        _ = self.index  # a cold index is built before the lock is taken, so writers never wait on a build
        out: list[tuple[str, bool]] = []
        new: list[Event] = []
        with lock(self.dir):
            ix = self.index
            active_keys = observed.active_keys(ix, [i.dedupe_key for i in batch if i.dedupe and i.dedupe_key])
            subject_id = self.subject.subject_id
            for item in batch:
                if item.dedupe and item.dedupe_key in active_keys:
                    out.append((active_keys[item.dedupe_key], False))
                    continue
                event_type = EventType(item.type)
                payload = PAYLOAD_TYPES[event_type].model_validate(item.payload.model_dump(mode="json"))
                event = Event(id=new_id("evt"), type=event_type, recorded_at=now(), subject_id=subject_id,
                              dedupe_key=item.dedupe_key, payload=payload)
                new.append(event)
                if item.dedupe_key:
                    active_keys[item.dedupe_key] = event.id
                out.append((event.id, True))
            placed = append_jsonl_many(self.path, [_dump(e) for e in new])
            if placed:
                ix.add([(e, off, ln) for e, (off, ln) in zip(new, placed, strict=True)])
        return out

    # -------------------------------------------------------------- reading

    def iter_events(self, type: EventType | str | Iterable[EventType | str] | None = None, *, active: bool = True,
                    kinds: Iterable[ObservationKind | str] | None = None) -> Iterator[Event]:
        """Events in log order, one at a time (memory does not grow with the log); `type` may be one or several types.
        active=True drops those targeted by a retraction (retraction events themselves stay listed). An invalid line
        raises ValidationError (CORRUPT_EVENT) when the index reads it. `kinds` keeps only observations of those kinds."""
        ix = self.index
        where, args = ["1"], []
        if types := _types(type):
            where.append(f"type IN ({','.join('?' * len(types))})")
            args += types
        if kinds is not None:
            wanted = [ObservationKind(k).value for k in kinds]
            where.append(f"kind IN ({','.join('?' * len(wanted))})")
            args += wanted
        if active:
            where.append("gone=0")
        cur = ix.conn.execute(f"SELECT seq, id, off, ln FROM events WHERE {' AND '.join(where)} ORDER BY seq", args)
        with open(self.path, "rb") as fh:
            while page := cur.fetchmany(2048):
                for seq, event_id, off, ln in page:
                    yield ix.event_at(seq, event_id, off, ln, fh)

    def events(self, type: EventType | str | Iterable[EventType | str] | None = None, *, active: bool = True) -> list[Event]:
        """`iter_events` as a list: use it when the result is small (one type of policy events), not for the whole log."""
        return list(self.iter_events(type, active=active))

    def load(self, rows: Iterable[Any]) -> list[Event]:
        """Events for index rows (anything with seq, id, off, ln), in the order given."""
        ix = self.index
        with open(self.path, "rb") as fh:
            return [ix.event_at(r.seq, r.id, r.off, r.ln, fh) for r in rows]

    def get(self, event_id: str) -> Event:
        ix = self.index
        row = ix.conn.execute("SELECT seq, id, off, ln FROM events WHERE id=? ORDER BY seq LIMIT 1", (event_id,)).fetchone()
        if row is None:
            raise NotFound(f"event {event_id!r} not found", code="EVENT_NOT_FOUND")
        return ix.event_at(*row)

    def retracted_ids(self) -> set[str]:
        return {t for (t,) in self.index.conn.execute("SELECT target FROM retractions")}

    def observations(self) -> list[Event]:
        """Every active observation as a list: O(history) memory, kept for small histories and tests; the engine's own
        reads use `iter_events` and the index lookups in plsi.life.observed."""
        return self.events(EventType.OBSERVATION)

    def world_change(self) -> datetime | None:
        """Latest recorded_at of any observation or retraction, from the index (no log read)."""
        return self.index.world_change()

    # -------------------------------------------------------------- subject

    def update_subject(self, *, birth_year: int | None = None, sex: Sex | str | None = None, relation: Relation | str | None = None,
                       country: str | None = None, ethnicity: Ethnicity | str | None = None,
                       update: bool = False) -> tuple[Subject, list[str]]:
        """Fill unset subject fields; change existing values only with update=True. Applies the 90+ rule. -> (subject, changed fields)."""
        with lock(self.dir):
            subject = self.subject
            fields = deid_subject_fields(birth_year, sex)
            changed: dict[str, Any] = {}
            if birth_year is not None and (update or (subject.birth_year is None and subject.age_group is None)):
                changed |= {"birth_year": fields["birth_year"], "age_group": fields["age_group"]}
            if sex is not None and (update or subject.sex == Sex.UNKNOWN):
                changed["sex"] = fields["sex"]
            if relation is not None and (update or subject.relation is None):
                changed["relation"] = Relation(relation)
            if country is not None and (update or subject.country is None):
                changed["country"] = _country(country)
            if ethnicity is not None and (update or subject.ethnicity is None):
                changed["ethnicity"] = Ethnicity(ethnicity)
            changed = {k: v for k, v in changed.items() if getattr(subject, k) != v}
            if changed:
                subject = Subject.model_validate({**subject.model_dump(mode="json"), **changed})
                atomic_write_json(self.dir / "subject.json", subject.dump())
            return subject, sorted(changed)


def _country(value: str | None) -> str | None:
    """ISO 3166 alpha-3, upper-cased; anything else is refused before any file is written."""
    if value is None:
        return None
    if not re.fullmatch(r"[A-Za-z]{3}", value):
        raise ValidationError(f"country must be an ISO 3166 alpha-3 code (e.g. FRA), not {value!r}")
    return value.upper()


def init_instance(instance: str, birth_year: int | None = None, sex: Sex | str | None = None, relation: Relation | str | None = None,
                  country: str | None = None, ethnicity: Ethnicity | str | None = None, update: bool = False) -> dict[str, Any]:
    """Create <PLSI_HOME>/<instance>/ with subject.json and an empty events.jsonl. Existing -> created False (+ subject update).
    One instance per person; `relation` says who it is to the user."""
    validate_id(instance, "instance name")
    country = _country(country)
    path = instance_dir(instance, must_exist=False)
    created = not (path / "subject.json").exists()
    if created:
        path.mkdir(parents=True, exist_ok=True)
        with lock(path):
            fields = deid_subject_fields(birth_year, sex)
            subject = Subject(subject_id=f"s_{secrets.token_hex(6)}", created_at=now(), relation=relation,
                              country=country, ethnicity=ethnicity, **fields)
            atomic_write_json(path / "subject.json", subject.dump())
            (path / "events.jsonl").touch()
        changed: list[str] = []
    else:
        subject, changed = EventStore(instance).update_subject(birth_year=birth_year, sex=sex, relation=relation,
                                                               country=country, ethnicity=ethnicity, update=update)
    return {
        "instance": instance,
        "created": created,
        "path": str(path),
        "subject": subject.dump(),
        "updated_fields": changed,
    }


def list_instances() -> list[dict[str, Any]]:
    """`plsi instances list`: every instance under PLSI_HOME with its subject, relation and activity, sorted by name."""
    out = []
    for path in sorted(plsi_home().glob("*/subject.json")):
        store = EventStore(path.parent.name)
        counts = event_counts(store.instance)
        out.append({"instance": store.instance, "subject": store.subject.dump(),
                    "active_observations": counts["active_observations"], "last_recorded_at": counts["last_recorded_at"],
                    "states": sorted(p.parent.name for p in (store.dir / "states").glob("*/latest.json"))})
    return out


def delete_instance(instance: str, confirm: str) -> dict[str, Any]:
    """`instances delete`: remove <PLSI_HOME>/<instance>/ (log, states, plans) for good; confirm must repeat the name."""
    path = instance_dir(instance)
    if confirm != instance:
        raise ValidationError(f"refusing to delete {instance!r}: --confirm must repeat the instance name", code="CONFIRM_MISMATCH",
                              hint=f"plsi export --instance {instance} --out <PLSI_HOME>/exports/{instance} first (outside the instance folder), then plsi instances delete --instance {instance} --confirm {instance}")
    counts = event_counts(instance)
    with lock(path):
        removed = sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file() and p.name != ".lock")
        for child in path.iterdir():
            if child.name != ".lock":
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    shutil.rmtree(path, ignore_errors=True)
    return {"instance": instance, "deleted": not path.exists(), "path": str(path), "files_removed": removed,
            "events_removed": sum(counts["by_type"].values())}


def list_events(instance: str, type: EventType | str | None = None, kind: str | None = None, limit: int | None = None,
                after: str | None = None) -> dict[str, Any]:
    """`events list`: newest last; optional event type, observation kind and limit (last N).

    `after` pages forward instead: the first `limit` events (same filters) that follow event id `after`, oldest first,
    with `next_after` (pass it as the next `after`) while more remain; memory depends on `limit`, not on the log."""
    store = EventStore(instance)
    ix = store.index
    where, args = ["1"], []
    if type is not None:
        where.append("type=?")
        args.append(EventType(type).value)
    if kind is not None:
        where.append("kind=?")
        args.append(str(kind))
    if after is not None:
        if limit is None or limit <= 0:
            raise ValidationError("--after needs a --limit above zero", code="USAGE")
        row = ix.conn.execute("SELECT seq FROM events WHERE id=? ORDER BY seq LIMIT 1", (after,)).fetchone()
        if row is None:
            raise NotFound(f"event {after!r} not found", code="EVENT_NOT_FOUND")
        where.append("seq>?")
        args.append(row[0])
    order = "DESC" if after is None else "ASC"
    sql = f"SELECT seq, id, off, ln, gone FROM events WHERE {' AND '.join(where)} ORDER BY seq {order}"
    if limit is not None:
        if limit <= 0:
            return {"count": 0, "events": []}
        sql += f" LIMIT {int(limit) + (after is not None)}"
    rows = ix.conn.execute(sql, args).fetchall()
    more = after is not None and len(rows) > limit  # type: ignore[operator]
    rows = rows[:limit] if after is not None else rows[::-1]
    with open(store.path, "rb") as fh:
        events = [{**_dump(ix.event_at(seq, event_id, off, ln, fh)), "retracted": bool(gone)} for seq, event_id, off, ln, gone in rows]
    out: dict[str, Any] = {"count": len(events), "events": events}
    if more:
        out["next_after"] = events[-1]["id"]
    return out


def event_counts(instance: str) -> dict[str, Any]:
    ix = EventStore(instance).index
    c = ix.conn
    last = c.execute("SELECT rec FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return {
        "by_type": {EventType(t): n for t, n in c.execute("SELECT type, COUNT(*) FROM events GROUP BY type ORDER BY MIN(seq)")},
        "retracted": c.execute("SELECT COUNT(*) FROM retractions").fetchone()[0],
        "active_observations": c.execute("SELECT COUNT(*) FROM events WHERE type='observation' AND gone=0").fetchone()[0],
        "last_recorded_at": from_micros(last[0]).isoformat() if last else None,
    }
