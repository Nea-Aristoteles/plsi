"""The event-log index: a disposable SQLite cache of `events.jsonl`, kept beside it in `<instance>/index/`.

The log stays the only source of truth. The index holds, per event, where its line is (offset, length), its id,
type, recorded time, dedupe keys and retraction flag, and for observations the few fields queries filter on (kind,
code, effective time, fasting, origin kind). Everything returned to a caller is read back from the log line and
validated again, so a wrong index can slow or skip a lookup but never invent an event; `plsi index verify` audits it
and `plsi index rebuild` replaces it. It is versioned and fingerprinted: a different key definition, row mapping or
schema rebuilds it on first use. It never leaves the instance and is removed with it.

Writers update it under the instance lock, right after the log append (the commit point). An index that is behind
(crash between append and index commit, or a log appended by an older PLSI) catches up from the last indexed byte;
one that does not match the log (shorter, other head or tail bytes, unreadable) is rebuilt. A cold build reads the
log without the lock up to a snapshot, then takes the lock only to catch up the tail and publish the file.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pydantic

from plsi import errors, files
from plsi.errors import ValidationError
from plsi.events import PAYLOAD_TYPES, Event
from plsi.life.dedupe import dedupe_key
from plsi.life.models import Observation
from plsi.vocab import EventType

INDEX_VERSION = 1
CHUNK = 8 << 20  # bytes of log read at a time
MAX_LINE = 16 << 20  # one event line longer than this is refused, not buffered
BATCH = 5000  # rows per insert while scanning
WINDOW = 4096  # bytes of the log's head and of the indexed tail that are compared on every open
TORN_SHOWN = 20  # TORN_LINE warnings listed before the rest are counted
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


def _blob(key: str) -> bytes | str:
    """A sha256 hex key as 32 bytes; anything else (a hostile or foreign key) stays text."""
    try:
        raw = bytes.fromhex(key)
    except ValueError:
        return key
    return raw if len(raw) == 32 and raw.hex() == key else key


def blob(key: str) -> bytes | str:
    return _blob(key)


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
        key = _blob(dedupe_key(p))  # recomputed from the stored payload, as ingest always did
        s = _blob(stored) if stored else b""
        text_key = None if p.code else hashlib.sha256(p.original_text.lower().encode("utf-8")).digest()
        return (event.id, off, ln, event.type.value, micros(event.recorded_at), key, None if s == key else s, p.kind.value,
                p.code.system.value if p.code else None, p.code.code if p.code else None, text_key,
                micros(p.effective_at), None if p.fasting is None else int(p.fasting), p.origin.kind.value)
    return (event.id, off, ln, event.type.value, micros(event.recorded_at), _blob(stored) if stored else None, None,
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


def _corrupt(exc: sqlite3.DatabaseError) -> bool:
    """A damaged or foreign file (rebuild it) rather than a busy, read-only or full one (try again or fall back)."""
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc)
        return "no such" in text or "malformed" in text or "not a database" in text
    return True


class _Stale(Exception):
    """The log no longer matches what the index recorded about it."""


class IndexStale(ValidationError):
    """A line the index points at is not the event it recorded: the log changed under it."""

    code = "INDEX_STALE"


class LogIndex:
    def __init__(self, folder: Path, log: Path) -> None:
        self.folder, self.log = folder, log
        self.db = folder / "index" / "events.sqlite3"
        self._conn: sqlite3.Connection | None = None
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._depth = 0
        self._warned = False
        self._seen: tuple[int, int, int] | None = None  # (log size, mtime_ns, indexed bytes) last verified

    # ------------------------------------------------------------ connection

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    @property
    def ephemeral(self) -> bool:
        return self._tmp is not None

    def _connect(self, path: Path, build: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA synchronous=OFF" if build else "PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-65536" if build else "PRAGMA cache_size=-16384")
        if build:
            conn.execute("PRAGMA page_size=8192")
            conn.execute("PRAGMA journal_mode=OFF")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _open(self) -> sqlite3.Connection:
        if self._tmp is None:
            try:
                self.db.parent.mkdir(parents=True, exist_ok=True)
                return self._connect(self.db)
            except (OSError, sqlite3.OperationalError):
                self._use_temp()
        return self._connect(self.db)

    def _use_temp(self) -> None:
        """No writable index folder (read-only media): this run builds its own copy in a temporary directory."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._tmp = tempfile.TemporaryDirectory(prefix="plsi-index-")
        atexit.register(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "events.sqlite3"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------ meta

    def meta(self, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        try:
            rows = (conn or self.conn).execute("SELECT k, v FROM meta").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        return dict(rows) or None

    def _digests(self, off: int) -> tuple[str, str]:
        with open(self.log, "rb") as fh:
            head = fh.read(min(WINDOW, off))
            fh.seek(max(0, off - WINDOW))
            tail = fh.read(min(WINDOW, off))
        return hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()

    def _log_stat(self) -> tuple[int, int]:
        try:
            st = self.log.stat()
        except FileNotFoundError:
            return 0, 0
        return st.st_size, st.st_mtime_ns

    def _verified(self, meta: dict[str, Any], size: int) -> bool:
        """The log still holds the bytes the index was built from: not shorter, same head, same indexed tail."""
        if meta.get("version") != INDEX_VERSION or meta.get("fingerprint") != fingerprint():
            return False
        off = meta["off"]
        if size < off:
            return False
        if off == 0:
            return True
        if (size, self._log_stat()[1], off) == self._seen:
            return True
        if (meta["head"], meta["tail"]) != self._digests(off):
            return False
        if meta["unterminated"] and size > off:
            with open(self.log, "rb") as fh:
                fh.seek(off)
                if fh.read(1) != b"\n":
                    return False
        self._seen = (size, self._log_stat()[1], off)
        return True

    def _fresh(self, meta: dict[str, Any], size: int) -> bool:
        if size == meta["off"] + meta["pending_len"]:
            if not meta["pending_len"]:
                return True
            with open(self.log, "rb") as fh:
                fh.seek(meta["off"])
                return hashlib.sha256(fh.read(meta["pending_len"])).hexdigest() == meta["pending_digest"]
        return False

    # ------------------------------------------------------------ keeping it current

    def sync(self) -> None:
        """Make the index match the log (build, catch up or rebuild); a no-op inside a snapshot and when current."""
        if self._depth:
            return
        for attempt in (0, 1):
            try:
                self._sync_once()
                break
            except sqlite3.DatabaseError as exc:
                if attempt or not _corrupt(exc):
                    raise
                self._discard()
        self._warn_torn()

    def _sync_once(self) -> None:
        size, _ = self._log_stat()
        meta = self.meta()
        if meta is None or not self._verified(meta, size):
            self._rebuild()
        elif not self._fresh(meta, size):
            try:
                self._catch_up()
            except _Stale:
                self._rebuild()

    def _discard(self) -> None:
        self.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.db}{suffix}").unlink(missing_ok=True)
        self._seen = None

    def _lock(self) -> contextlib.AbstractContextManager[Any]:
        if self.ephemeral or files.holding(self.folder):
            return contextlib.nullcontext()
        return files.lock(self.folder)

    def _catch_up(self) -> None:
        try:
            with self._lock():
                size, _ = self._log_stat()
                meta = self.meta()
                if meta is None or not self._verified(meta, size):
                    raise _Stale
                if not self._fresh(meta, size):
                    self._scan(self.conn, meta, size)
        except (OSError, sqlite3.OperationalError) as exc:
            if isinstance(exc, sqlite3.OperationalError) and not any(w in str(exc) for w in ("readonly", "unable to open")):
                raise
            self._use_temp()
            self._rebuild()

    def _rebuild(self) -> None:
        """Cold build from the log: scan to a snapshot without the lock, then lock to catch up and publish."""
        if not self.ephemeral:
            try:
                self.db.parent.mkdir(parents=True, exist_ok=True)
                self._sweep()
                tmp = self.db.parent / f".build-{secrets.token_hex(4)}.sqlite3"
                tmp.touch()
            except OSError:
                self._use_temp()
        if self.ephemeral:
            self._discard()
            tmp = self.db
        self.close()
        size, _ = self._log_stat()
        conn = self._connect(tmp, build=True)
        try:
            for statement in SCHEMA:
                conn.execute(statement)
            meta = self._blank()
            self._scan(conn, meta, size)
            with self._lock():
                size, _ = self._log_stat()
                meta = self.meta(conn)
                if not self._fresh(meta, size):
                    self._scan(conn, meta, size)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.close()
                if tmp != self.db:
                    if self._published_current():
                        tmp.unlink(missing_ok=True)
                    else:
                        for suffix in ("-wal", "-shm"):
                            Path(f"{self.db}{suffix}").unlink(missing_ok=True)
                        os.replace(tmp, self.db)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
            if tmp != self.db:
                tmp.unlink(missing_ok=True)
            raise
        self._seen = None

    def _published_current(self) -> bool:
        """Another process published a usable index while this one was building: keep theirs."""
        try:
            probe = self._connect(self.db)
            try:
                meta = self.meta(probe)
            finally:
                probe.close()
            size, _ = self._log_stat()
            return meta is not None and self._verified(meta, size)
        except sqlite3.DatabaseError:
            return False

    def _sweep(self) -> None:
        """Remove half-built files an earlier crash left behind (older than an hour, so a live build is never touched)."""
        for stale in self.db.parent.glob(".build-*.sqlite3*"):
            with contextlib.suppress(OSError):
                if stale.stat().st_mtime < _now() - 3600:
                    stale.unlink()

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {"off": 0, "lines": 0, "unterminated": 0, "pending_len": 0, "pending_digest": "", "world": 0}

    # ------------------------------------------------------------ scanning the log

    def _scan(self, conn: sqlite3.Connection, meta: dict[str, Any], until: int) -> None:
        """Index every complete line of the log from the last indexed byte to `until`, in one transaction per call
        (a build commits per batch), then record the new position. A final line without a newline counts when it is
        valid JSON (as the original reader did) and is otherwise left pending."""
        scanner = _Scanner(conn, meta)
        conn.execute("BEGIN")
        try:
            scanner.run(self.log, until)
            self._finish(conn, scanner)
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def _finish(self, conn: sqlite3.Connection, s: _Scanner) -> None:
        s.flush()
        head, tail = self._digests(s.off)
        values = {"version": INDEX_VERSION, "fingerprint": fingerprint(), "off": s.off, "lines": s.lines, "unterminated": s.unterminated,
                  "pending_len": s.pending_len, "pending_digest": s.pending_digest, "head": head, "tail": tail, "world": s.world,
                  "built_by": "plsi"}
        conn.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", values.items())

    # ------------------------------------------------------------ writer path

    def add(self, events: list[tuple[Event, int, int]]) -> None:
        """Index events the caller has just appended (offset and length from files.append_jsonl_many), under the lock.
        When the log did not end on a clean line before the append, scan instead of trusting the offsets."""
        conn, size = self.conn, self._log_stat()[0]
        try:
            meta = self.meta()
            clean = meta is not None and not meta["pending_len"] and events and events[0][1] == meta["off"] + meta["unterminated"]
            if not clean:
                return self._catch_up()
            scanner = _Scanner(conn, meta)
            conn.execute("BEGIN")
            try:
                for event, off, ln in events:
                    scanner.push(event, off, ln)
                scanner.lines += len(events)
                scanner.off = events[-1][1] + events[-1][2] + 1
                scanner.unterminated = 0
                if scanner.off != size:
                    raise _Stale
                self._finish(conn, scanner)
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        except (_Stale, sqlite3.Error, OSError) as exc:
            errors.warn("INDEX_STALE", f"events were appended but the index could not follow ({type(exc).__name__}); it catches up on the next command")

    def _warn_torn(self) -> None:
        if self._warned:
            return
        self._warned = True
        meta = self.meta()
        if meta is None:
            return
        lines = [r[0] for r in self.conn.execute("SELECT line FROM torn ORDER BY line LIMIT ?", (TORN_SHOWN,))]
        for n in lines:
            errors.warn("TORN_LINE", f"{self.log.name} line {n} is not valid JSON and was ignored")
        total = self.conn.execute("SELECT COUNT(*) FROM torn").fetchone()[0]
        if total > TORN_SHOWN:
            errors.warn("TORN_LINE", f"{self.log.name} has {total - TORN_SHOWN} more lines that are not valid JSON and were ignored")
        if meta["pending_len"]:
            errors.warn("TORN_LINE", f"{self.log.name} line {meta['lines'] + 1} is not valid JSON and was ignored")

    # ------------------------------------------------------------ reading

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[LogIndex]:
        """One consistent view for a series of queries: events indexed when it began, whatever writers do meanwhile."""
        self.sync()
        outer = self._depth
        if not outer:
            self.conn.execute("BEGIN")
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
            if not outer:
                with contextlib.suppress(sqlite3.Error):
                    self.conn.execute("COMMIT")

    def event_at(self, seq: int, event_id: str, off: int, ln: int, fh: Any = None) -> Event:
        """Read one event back from its line; the id must be the indexed one, or the index is stale."""
        try:
            if fh is None:
                with open(self.log, "rb") as own:
                    own.seek(off)
                    data = own.read(ln)
            else:
                fh.seek(off)
                data = fh.read(ln)
            event = load_event(files.loads_json(data.decode("utf-8", errors="replace")), seq)
        except (ValueError, RecursionError, OSError):
            event = None
        if event is None or event.id != event_id:
            raise IndexStale(f"the log no longer matches its index at event {seq}", hint="plsi index rebuild --instance <name>")
        return event

    def world_change(self) -> datetime | None:
        meta = self.meta()
        return from_micros(meta["world"]) if meta and meta["world"] is not None and meta["lines"] and self.conn.execute(
            "SELECT 1 FROM events WHERE type IN ('observation','retraction') LIMIT 1").fetchone() else None


def _now() -> float:
    import time

    return time.time()


class _Scanner:
    """Reads log lines into index rows and the bookkeeping the meta table records."""

    def __init__(self, conn: sqlite3.Connection, meta: dict[str, Any]) -> None:
        self.conn = conn
        self.off, self.lines, self.unterminated = meta["off"], meta["lines"], meta["unterminated"]
        self.pending_len, self.pending_digest = 0, ""
        self.world = meta["world"] or 0
        self.rows: list[tuple[Any, ...]] = []
        self.retracted: list[str] = []
        self.torn: list[int] = []
        last = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        self.next_seq, self.first_seq = last + 1, last + 1

    def run(self, log: Path, until: int) -> None:
        with open(log, "rb") as fh:
            pos = self.off
            if self.unterminated and self.off < until:
                fh.seek(pos)
                if fh.read(1) != b"\n":
                    raise _Stale
                pos += 1
            fh.seek(pos)
            tail, tail_pos = b"", pos
            while True:
                chunk = fh.read(max(0, min(CHUNK, until - tail_pos - len(tail))))
                data, base, i = tail + chunk, tail_pos, 0
                while (j := data.find(b"\n", i)) >= 0:
                    self.line(data[i:j], base + i)
                    self.off = base + j + 1
                    i = j + 1
                tail, tail_pos = data[i:], base + i
                if len(tail) > MAX_LINE:
                    raise ValidationError(f"events.jsonl line at byte {tail_pos} is longer than {MAX_LINE} bytes", code="CORRUPT_EVENT")
                if not chunk:
                    break
        self.unterminated = 0
        if tail:
            if tail.strip() and self.line(tail, tail_pos, final=True):
                self.off, self.unterminated = tail_pos + len(tail), 1
            else:
                self.pending_len, self.pending_digest = len(tail), hashlib.sha256(tail).hexdigest()
                self.off = tail_pos

    def line(self, raw: bytes, off: int, final: bool = False) -> bool:
        """Index one line; True when it was an event (or, for a final line, valid JSON that was one)."""
        if not raw.strip():
            self.lines += not final
            return False
        try:
            obj = files.loads_json(raw.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            if not final:
                self.lines += 1
                self.torn.append(self.lines)
            return False
        self.lines += 1
        self.push(load_event(obj, self.next_seq), off, len(raw))
        return True

    def push(self, event: Event, off: int, ln: int) -> None:
        self.rows.append(row_of(event, off, ln))
        self.next_seq += 1
        if event.type in (EventType.OBSERVATION, EventType.RETRACTION):
            self.world = max(self.world, self.rows[-1][4])
        if event.type == EventType.RETRACTION:
            self.retracted.append(event.payload.target_event_id)  # type: ignore[union-attr]
        if len(self.rows) >= BATCH:
            self.flush()

    def flush(self) -> None:
        c = self.conn
        if self.rows:
            c.executemany(INSERT, self.rows)
            c.execute("UPDATE events SET gone=1 WHERE seq>=? AND gone=0 AND id IN (SELECT target FROM retractions)", (self.first_seq,))
            self.rows.clear()
        if self.retracted:
            c.executemany("INSERT OR IGNORE INTO retractions(target) VALUES (?)", [(t,) for t in self.retracted])
            c.executemany("UPDATE events SET gone=1 WHERE id=?", [(t,) for t in self.retracted])
            self.retracted.clear()
        if self.torn:
            c.executemany("INSERT OR IGNORE INTO torn(line) VALUES (?)", [(n,) for n in self.torn])
            self.torn.clear()
        self.first_seq = self.next_seq
