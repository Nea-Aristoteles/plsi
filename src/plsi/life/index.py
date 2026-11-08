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

Modules: index_rows (schema, row mapping, fingerprint), index_scan (log lines to rows), index_db (SQLite access).
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import os
import secrets
import tempfile
import time
import weakref
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from plsi import errors, files
from plsi.events import Event
from plsi.life import index_db
from plsi.life.index_db import IndexDamaged, IndexStale, sqlite3
from plsi.life.index_rows import INDEX_VERSION, SCHEMA, fingerprint, from_micros, load_event
from plsi.life.index_scan import Scanner, Stale

WINDOW = 4096  # bytes of the log's head and of the indexed tail that are compared on every open
TORN_SHOWN = 20  # TORN_LINE warnings listed before the rest are counted


class LogIndex:
    def __init__(self, folder: Path, log: Path) -> None:
        index_db.require()
        self.folder, self.log = folder, log
        self.db = folder / "index" / "events.sqlite3"
        self._conn: sqlite3.Connection | None = None
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._depth = 0
        self._warned = False
        self._probed = False
        self._seen: tuple[int, int, int] | None = None  # (log size, mtime_ns, indexed bytes) last verified

    # ------------------------------------------------------------ connection

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
            weakref.finalize(self, index_db.close_quietly, self._conn)  # SQLite connections are not freed by refcount alone
        return self._conn

    @property
    def ephemeral(self) -> bool:
        return self._tmp is not None

    def _connect(self, path: Path, build: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None, timeout=30.0, factory=index_db.Conn)
        conn.owner = weakref.ref(self)
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

    def damaged(self, exc: Exception) -> None:
        """The file is not a usable index: remove it (the log is untouched) and say so."""
        self._depth = 0
        self._discard()
        raise IndexDamaged(f"the event index was damaged ({exc}); it was removed and is rebuilt from the log on the next command",
                           hint="run the command again, or plsi index rebuild --instance <name>") from None

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

    def _truncated(self) -> bool:
        """The index file is shorter than its own header says (a cut-off copy), or not an SQLite file at all."""
        try:
            with open(self.db, "rb") as fh:
                head = fh.read(100)
            size = self.db.stat().st_size
        except OSError:
            return False
        if len(head) < 100:
            return size > 0
        if head[:16] != b"SQLite format 3\x00":
            return True
        page, pages = int.from_bytes(head[16:18], "big") or 65536, int.from_bytes(head[28:32], "big")
        return bool(pages) and head[24:28] == head[92:96] and size < page * pages

    def _intact(self, meta: dict[str, Any]) -> bool:
        """Cheap structural probe, once per open: the row count the meta records is there and the ends of each B-tree read."""
        if self._probed:
            return True
        c = self.conn
        if (c.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0) != meta["events"]:
            return False
        for probe in ("SELECT id FROM events INDEXED BY ix_id ORDER BY id DESC LIMIT 1",
                      "SELECT key FROM events INDEXED BY ix_key WHERE key IS NOT NULL ORDER BY key DESC LIMIT 1",
                      "SELECT skey FROM events INDEXED BY ix_skey WHERE skey IS NOT NULL ORDER BY skey DESC LIMIT 1",
                      "SELECT code FROM events INDEXED BY ix_code WHERE code_system IS NOT NULL ORDER BY code_system DESC, code DESC LIMIT 1",
                      "SELECT kind FROM events INDEXED BY ix_kind WHERE kind IS NOT NULL ORDER BY kind DESC LIMIT 1",
                      "SELECT MAX(line) FROM torn", "SELECT MAX(target) FROM retractions"):
            c.execute(probe).fetchone()
        self._probed = True
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
        if self._conn is None and self._truncated():
            self._discard()
        for attempt in (0, 1):
            try:
                self._sync_once()
                break
            except IndexDamaged:
                if attempt:
                    raise
            except sqlite3.DatabaseError as exc:
                if attempt or not index_db.corrupt(exc):
                    raise
                self._discard()
        self._warn_torn()

    def _sync_once(self) -> None:
        size, _ = self._log_stat()
        meta = self.meta()
        if meta is None or not self._verified(meta, size) or not self._intact(meta):
            self._rebuild()
        elif not self._fresh(meta, size):
            try:
                self._catch_up()
            except Stale:
                self._rebuild()

    def rebuild(self) -> None:
        """Replace the index with a fresh build from the log (`plsi index rebuild`)."""
        self._rebuild(force=True)
        self._warned = False
        self._warn_torn()

    def _discard(self) -> None:
        self.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.db}{suffix}").unlink(missing_ok=True)
        self._seen = None
        self._probed = False

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
                    raise Stale
                if not self._fresh(meta, size):
                    self._scan(self.conn, meta, size)
        except (OSError, sqlite3.OperationalError) as exc:
            if isinstance(exc, sqlite3.OperationalError) and not any(w in str(exc) for w in ("readonly", "unable to open")):
                raise
            self._use_temp()
            self._rebuild()

    def _rebuild(self, force: bool = False) -> None:
        """Cold build from the log: scan to a snapshot without the lock, then lock to catch up and publish
        (force: replace the published index even when it looks current)."""
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
                    if not force and self._published_current():
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
                if stale.stat().st_mtime < time.time() - 3600:
                    stale.unlink()

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {"off": 0, "lines": 0, "unterminated": 0, "pending_len": 0, "pending_digest": "", "world": None, "events": 0}

    # ------------------------------------------------------------ scanning the log

    def _scan(self, conn: sqlite3.Connection, meta: dict[str, Any], until: int) -> None:
        """Index every complete line of the log from the last indexed byte to `until`, in one transaction per call
        (a build commits per batch), then record the new position. A final line without a newline counts when it is
        valid JSON (as the original reader did) and is otherwise left pending."""
        scanner = Scanner(conn, meta)
        conn.execute("BEGIN")
        try:
            scanner.run(self.log, until)
            self._finish(conn, scanner)
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def _finish(self, conn: sqlite3.Connection, s: Scanner) -> None:
        s.flush()
        head, tail = self._digests(s.off)
        values = {"version": INDEX_VERSION, "fingerprint": fingerprint(), "off": s.off, "lines": s.lines, "unterminated": s.unterminated,
                  "pending_len": s.pending_len, "pending_digest": s.pending_digest, "events": s.next_seq - 1, "head": head, "tail": tail, "world": s.world,
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
            scanner = Scanner(conn, meta)
            conn.execute("BEGIN")
            try:
                for event, off, ln in events:
                    scanner.push(event, off, ln)
                scanner.lines += len(events)
                scanner.off = events[-1][1] + events[-1][2] + 1
                scanner.unterminated = 0
                if scanner.off != size:
                    raise Stale
                self._finish(conn, scanner)
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        except (Stale, sqlite3.Error, OSError) as exc:
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
        """Latest recorded_at of any observation or retraction (None when there is none)."""
        meta = self.meta()
        return from_micros(meta["world"]) if meta and meta["world"] is not None else None
