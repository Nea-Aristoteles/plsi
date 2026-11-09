"""`plsi index status | rebuild | verify`: look after the event-log index (plsi.life.index) through supported commands.

The index is a disposable cache of events.jsonl: these commands only ever read the log, and rebuild replaces the cache.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from plsi.life.index import LogIndex
from plsi.life.index_rows import fingerprint, from_micros, load_event, row_of, tally
from plsi.life.index_scan import iter_lines
from plsi.life.store import EventStore
from plsi.files import loads_json
from plsi.vocab import EventType

COLUMNS = "seq, id, off, ln, type, rec, key, skey, kind, code_system, code, text_key, eff, fasting, origin"
SHOWN = 20  # mismatches listed


def _size(ix: LogIndex) -> int:
    return sum(p.stat().st_size for p in (ix.db.parent.glob("events.sqlite3*")) if p.is_file())


def status(instance: str) -> dict[str, Any]:
    """Where the index stands against the log, without building or changing anything."""
    store = EventStore(instance)
    ix = LogIndex(store.dir, store.path)
    log_bytes, _ = ix._log_stat()
    out: dict[str, Any] = {"instance": instance, "log_bytes": log_bytes, "index_path": str(ix.db), "state": "missing"}
    if not ix.db.is_file():
        return out
    try:
        meta = ix.meta()
        if meta is None:
            out["state"] = "unreadable"
            return out
        verified = ix._verified(meta, log_bytes)
        out |= {
            "index_bytes": _size(ix), "version": meta["version"], "fingerprint_ok": meta["fingerprint"] == fingerprint(),
            "indexed_bytes": meta["off"], "indexed_lines": meta["lines"], "behind_bytes": max(0, log_bytes - meta["off"] - meta["pending_len"]),
            "events": meta["events"],
            "torn_lines": ix.conn.execute("SELECT COUNT(*) FROM torn").fetchone()[0] + (1 if meta["pending_len"] else 0),
            "matches_log": verified,
            "state": "stale" if not verified else "current" if ix._fresh(meta, log_bytes) else "behind",
        }
    except sqlite3.DatabaseError:
        out["state"] = "unreadable"
    finally:
        ix.close()
    return out


def rebuild(instance: str) -> dict[str, Any]:
    """Build a fresh index from the log and replace the current one; safe at any time, the log is only read."""
    store = EventStore(instance)
    ix = LogIndex(store.dir, store.path)
    began = time.monotonic()
    ix.rebuild()
    seconds = round(time.monotonic() - began, 3)
    ix.close()
    return {"instance": instance, "rebuilt": True, "seconds": seconds, **{k: v for k, v in status(instance).items() if k in ("log_bytes", "index_bytes", "events", "indexed_bytes", "torn_lines", "state")}}


def verify(instance: str) -> dict[str, Any]:
    """Audit the index against the whole log: every line's row (position, id, keys, time, code, flags), the retraction
    set and the recorded end of the log. Streams the log once; reports mismatches, changes nothing (ok false -> rebuild)."""
    store = EventStore(instance)
    ix = store.index
    c = ix.conn
    meta = ix.meta()
    log_bytes, _ = ix._log_stat()
    bad: list[dict[str, Any]] = []
    count = mismatches = 0
    targets: list[tuple[str]] = []
    types: dict[str, list[int]] = {}
    combos: dict[tuple[str, str, str, int], list[int]] = {}
    rows = c.execute(f"SELECT {COLUMNS} FROM events ORDER BY seq")
    seq = 0

    def miss(what: str, **detail: Any) -> None:
        nonlocal mismatches
        mismatches += 1
        if len(bad) < SHOWN:
            bad.append({"what": what, **detail})

    end = meta["off"]
    for off, raw, terminated in iter_lines(ix.log, 0, log_bytes):
        if not raw.strip():
            continue
        try:
            obj = loads_json(raw.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            continue
        if not terminated and off + len(raw) != end:
            continue
        seq += 1
        event = load_event(obj, seq)
        if event.type == EventType.RETRACTION:
            targets.append((event.payload.target_event_id,))  # type: ignore[union-attr]
        want = (seq, *row_of(event, off, len(raw)))
        tally(want[1:], seq, types, combos)
        got = rows.fetchone()
        count += 1
        if got is None:
            miss("missing row", seq=seq)
        elif tuple(got) != want:
            miss("row differs", seq=seq, id=want[1])
    if rows.fetchone() is not None:
        miss("rows beyond the log")
    c.execute("CREATE TEMP TABLE IF NOT EXISTS expected(target TEXT PRIMARY KEY) WITHOUT ROWID")
    c.execute("DELETE FROM expected")
    c.executemany("INSERT OR IGNORE INTO expected VALUES (?)", targets)
    flags = c.execute("SELECT COUNT(*) FROM events WHERE (gone=1) <> (id IN (SELECT target FROM expected))").fetchone()[0]
    if flags:
        miss("retraction flags differ", rows=flags)
    if {t for (t,) in c.execute("SELECT target FROM retractions")} != {t for (t,) in targets}:
        miss("retraction set differs")
    c.execute("DROP TABLE expected")
    if {t: [n, first] for t, n, first in c.execute("SELECT type, n, first FROM typecount")} != types:
        miss("per-type totals differ")
    if {(k, s, co, f): [n, lo, hi] for k, s, co, f, n, lo, hi in c.execute("SELECT * FROM combos")} != combos:
        miss("per-kind totals differ")
    if meta["off"] + meta["pending_len"] != log_bytes:
        miss("indexed end differs from the log end")
    last = c.execute("SELECT MAX(rec) FROM events WHERE type IN ('observation','retraction')").fetchone()[0]
    if meta["world"] != last:
        miss("world-change time differs")
    return {"instance": instance, "ok": mismatches == 0, "events_checked": count, "mismatches": mismatches, "examples": bad,
            "log_bytes": log_bytes, "last_world_change": from_micros(meta["world"]).isoformat() if meta["world"] is not None else None}
