"""Reading the log for the event index: lines with their byte offsets, and the scanner that turns them into rows."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from plsi import files
from plsi.errors import ValidationError
from plsi.events import Event
from plsi.life.index_rows import INSERT, UPSERT_COMBO, UPSERT_TYPE, load_event, row_of, tally
from plsi.vocab import EventType

CHUNK = 8 << 20  # bytes of log read at a time
MAX_LINE = 16 << 20  # one event line longer than this is refused, not buffered
BATCH = 5000  # rows per insert while scanning


class Stale(Exception):
    """The log no longer matches what the index recorded about it."""


def iter_lines(log: Path, start: int, until: int, skip_newline: bool = False) -> Iterator[tuple[int, bytes, bool]]:
    """(byte offset, line without its newline, newline-terminated) for each line of the log from `start` to `until`;
    the last line may be unterminated. `skip_newline`: the byte at `start` ends a line that was already taken in."""
    if until <= start:
        return
    with open(log, "rb") as fh:
        pos = start
        fh.seek(start)
        if skip_newline and start < until:
            if fh.read(1) != b"\n":
                raise Stale
            pos += 1
        fh.seek(pos)
        tail, tail_pos = b"", pos
        while True:
            chunk = fh.read(max(0, min(CHUNK, until - tail_pos - len(tail))))
            data, base, i = tail + chunk, tail_pos, 0
            while (j := data.find(b"\n", i)) >= 0:
                yield base + i, data[i:j], True
                i = j + 1
            tail, tail_pos = data[i:], base + i
            if len(tail) > MAX_LINE:
                raise ValidationError(f"events.jsonl line at byte {tail_pos} is longer than {MAX_LINE} bytes", code="CORRUPT_EVENT")
            if not chunk:
                break
    if tail:
        yield tail_pos, tail, False


class Scanner:
    """Reads log lines into index rows and the bookkeeping the meta table records."""

    def __init__(self, conn: sqlite3.Connection, meta: dict[str, Any]) -> None:
        self.conn = conn
        self.off, self.lines, self.unterminated = meta["off"], meta["lines"], meta["unterminated"]
        self.pending_len, self.pending_digest = 0, ""
        self.world: int | None = meta["world"]
        self.rows: list[tuple[Any, ...]] = []
        self.retracted: list[str] = []
        self.torn: list[int] = []
        self.types: dict[str, list[int]] = {}  # type -> [events, first seq]
        self.combos: dict[tuple[str, str, str, int], list[int]] = {}  # (kind, system, code, fasting) -> [n, min eff, max eff]
        last = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        self.next_seq, self.first_seq = last + 1, last + 1

    def run(self, log: Path, until: int) -> None:
        tail, tail_pos = b"", self.off
        for off, raw, terminated in iter_lines(log, self.off, until, bool(self.unterminated)):
            if terminated:
                self.line(raw, off)
                self.off = off + len(raw) + 1
            else:
                tail, tail_pos = raw, off
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
        row = row_of(event, off, ln)
        self.rows.append(row)
        tally(row, self.next_seq, self.types, self.combos)
        self.next_seq += 1
        if event.type in (EventType.OBSERVATION, EventType.RETRACTION):
            self.world = self.rows[-1][4] if self.world is None else max(self.world, self.rows[-1][4])
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
        c.executemany(UPSERT_TYPE, [(t, n, first) for t, (n, first) in self.types.items()])
        c.executemany(UPSERT_COMBO, [(*key, *slot) for key, slot in self.combos.items()])
        self.types.clear()
        self.combos.clear()
        self.first_seq = self.next_seq
