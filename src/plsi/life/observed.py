"""Lookups on the event index that ingest and the map projection ask instead of reading the whole log.

Each function runs bounded queries and returns identifiers or small rows; callers read the few events they need back
from the log (EventStore.load). Time is microseconds since the epoch, as in plsi.life.index.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

from plsi.life.index import LogIndex
from plsi.life.index_rows import DAY_US, blob, micros

IN_CHUNK = 500  # keys per IN (...) list
PAGE = 2048  # rows fetched at a time from a scan


class Row(NamedTuple):
    seq: int
    id: str
    off: int
    ln: int
    eff: int


def day_of(eff: int) -> date:
    return date(1970, 1, 1) + timedelta(days=eff // DAY_US)


def _chunks(items: list[Any]) -> Iterator[list[Any]]:
    for i in range(0, len(items), IN_CHUNK):
        yield items[i:i + IN_CHUNK]


def active_keys(ix: LogIndex, keys: Iterable[str]) -> dict[str, str]:
    """Stored dedupe key -> id of the latest active event that carries it (what append_many dedupes against)."""
    want = {blob(k): k for k in set(keys)}
    out: dict[str, str] = {}
    for part in _chunks(list(want)):
        marks = ",".join("?" * len(part))
        for event_id, key, skey in ix.conn.execute(
                f"SELECT id, key, skey FROM events WHERE gone=0 AND ((skey IS NULL AND key IN ({marks})) OR skey IN ({marks})) ORDER BY seq",
                part + part):
            stored = key if skey is None else skey
            if stored in want:
                out[want[stored]] = event_id
    return out


def observation_ids(ix: LogIndex, keys: Iterable[str]) -> dict[str, str]:
    """Recomputed dedupe key -> id of the latest active observation with it (what ingest dedupes against)."""
    want = {blob(k): k for k in set(keys)}
    out: dict[str, str] = {}
    for part in _chunks(list(want)):
        marks = ",".join("?" * len(part))
        for event_id, key in ix.conn.execute(
                f"SELECT id, key FROM events WHERE gone=0 AND type='observation' AND key IN ({marks}) ORDER BY seq", part):
            out[want[key]] = event_id
    return out


def first_in_month(ix: LogIndex, codes: Iterable[tuple[str, str]], origins: Iterable[str], month: str) -> str | None:
    """Id of the earliest active observation of any of `codes` from one of `origins` in a UTC calendar month (YYYY-MM)."""
    origins = tuple(origins)
    year, mon = int(month[:4]), int(month[5:7])
    lo = micros(datetime(year, mon, 1, tzinfo=UTC))
    hi = micros(datetime(year + mon // 12, mon % 12 + 1, 1, tzinfo=UTC))
    marks = ",".join("?" * len(origins))
    best: tuple[int, str] | None = None
    for system, code in codes:
        row = ix.conn.execute(
            f"SELECT seq, id FROM events WHERE code_system=? AND code=? AND eff>=? AND eff<? AND gone=0 AND origin IN ({marks}) ORDER BY seq LIMIT 1",
            (system, code, lo, hi, *origins)).fetchone()
        if row and (best is None or row[0] < best[0]):
            best = row
    return best[1] if best else None


class Wanted(NamedTuple):
    """One accepted code of a coordinate; `fasting` None = no requirement, False = only fasting=true, True = also unknown."""

    system: str
    code: str
    fasting: bool | None


def _scan(ix: LogIndex, w: Wanted, hi: int | None, lo: int | None, descending: bool) -> Iterator[Row]:
    where = ["code_system=?", "code=?", "gone=0"]
    args: list[Any] = [w.system, w.code]
    if hi is not None:
        where.append("eff<=?")
        args.append(hi)
    if lo is not None:
        where.append("eff>=?")
        args.append(lo)
    if w.fasting is not None:
        where.append("(fasting=1 OR fasting IS NULL)" if w.fasting else "fasting=1")
    order = "DESC" if descending else "ASC"
    cur = ix.conn.execute(f"SELECT seq, id, off, ln, eff FROM events WHERE {' AND '.join(where)} ORDER BY eff {order}, seq {order}", args)
    while page := cur.fetchmany(PAGE):
        for row in page:
            yield Row(*row)


def scan(ix: LogIndex, wanted: list[Wanted], *, hi: int | None = None, lo: int | None = None, descending: bool = False) -> Iterator[Row]:
    """Active observations of the wanted codes in (effective time, log order) order, oldest first or newest first."""
    streams = [_scan(ix, w, hi, lo, descending) for w in wanted]
    if len(streams) == 1:
        return streams[0]
    return heapq.merge(*streams, key=lambda r: (r.eff, r.seq), reverse=descending)


def _clauses(w: Wanted, hi: int | None, lo: int | None) -> tuple[str, list[Any]]:
    where, args = ["code_system=?", "code=?", "gone=0"], [w.system, w.code]
    if hi is not None:
        where.append("eff<=?")
        args.append(hi)
    if lo is not None:
        where.append("eff>=?")
        args.append(lo)
    if w.fasting is not None:
        where.append("(fasting=1 OR fasting IS NULL)" if w.fasting else "fasting=1")
    return " AND ".join(where), args


def _edge(ix: LogIndex, w: Wanted, hi: int | None, lo: int | None, descending: bool) -> Row | None:
    where, args = _clauses(w, hi, lo)
    order = "DESC" if descending else "ASC"
    row = ix.conn.execute(f"SELECT seq, id, off, ln, eff FROM events WHERE {where} ORDER BY eff {order}, seq {order} LIMIT 1", args).fetchone()
    return Row(*row) if row else None


def last_per_day(ix: LogIndex, wanted: list[Wanted], hi: int | None) -> dict[date, Row]:
    """The last active observation (effective time, then log order) of each UTC calendar day, among the wanted codes.

    Jumps from one occupied day to the next with two index probes per day and code, so the work follows the number of
    days that have data, not the number of readings."""
    best: dict[int, Row] = {}
    for w in wanted:
        lo = None
        while (first := _edge(ix, w, hi, lo, False)) is not None:
            day = first.eff // DAY_US
            last = _edge(ix, w, min(hi, (day + 1) * DAY_US - 1) if hi is not None else (day + 1) * DAY_US - 1, day * DAY_US, True)
            assert last is not None
            if day not in best or (last.eff, last.seq) > (best[day].eff, best[day].seq):
                best[day] = last
            lo = (day + 1) * DAY_US
    return {day_of(d * DAY_US): row for d, row in sorted(best.items())}


def has_kind(ix: LogIndex, kind: str, hi: int | None) -> bool:
    return ix.conn.execute("SELECT 1 FROM events WHERE kind=? AND gone=0 AND eff<=? LIMIT 1", (kind, hi if hi is not None else 1 << 62)).fetchone() is not None


class ItemRow(NamedTuple):
    seq: int
    id: str
    off: int
    ln: int
    code_system: str | None
    code: str | None
    text_key: bytes | None


def kind_rows(ix: LogIndex, kind: str, hi: int | None) -> Iterator[ItemRow]:
    """Active observations of one kind in (effective time, log order) order."""
    cur = ix.conn.execute("SELECT seq, id, off, ln, code_system, code, text_key FROM events WHERE kind=? AND gone=0 AND eff<=? ORDER BY eff, seq",
                          (kind, hi if hi is not None else 1 << 62))
    while page := cur.fetchmany(PAGE):
        for row in page:
            yield ItemRow(*row)


def combos(ix: LogIndex, hi: int | None) -> list[tuple[str, str | None, str | None, int | None, int]]:
    """(kind, code system, code, fasting, count) of every distinct combination among active observations effective by `hi`.

    Read from the insert-only totals table minus the retracted rows (a handful, through ix_gone); only a combination whose
    effective times straddle `hi` is counted from the index."""
    c = ix.conn
    gone = {(k, s, co, f): n for k, s, co, f, n in c.execute(
        "SELECT kind, code_system, code, fasting, COUNT(*) FROM events INDEXED BY ix_gone WHERE gone=1 AND kind IS NOT NULL GROUP BY kind, code_system, code, fasting")}
    out = []
    for kind, system, code, fasting, n, low, high in c.execute("SELECT kind, code_system, code, fasting, n, min_eff, max_eff FROM combos").fetchall():
        key = (kind, system or None, code or None, None if fasting == -1 else fasting)
        if hi is None or high <= hi:
            count = n - gone.get(key, 0)
        elif low > hi:
            count = 0
        else:
            where, args = ["kind=?", "gone=0", "eff<=?", "code_system IS ?", "code IS ?", "fasting IS ?"], [kind, hi, key[1], key[2], key[3]]
            count = c.execute(f"SELECT COUNT(*) FROM events WHERE {' AND '.join(where)}", args).fetchone()[0]
        if count:
            out.append((*key, count))
    return out


def observation_rows(ix: LogIndex, hi: int | None) -> Iterator[tuple[int, str, str, str | None, str | None, int | None]]:
    """(seq, id, kind, code system, code, fasting) of every active observation in log order."""
    cur = ix.conn.execute("SELECT seq, id, kind, code_system, code, fasting FROM events WHERE kind IS NOT NULL AND gone=0 AND eff<=? ORDER BY seq",
                          (hi if hi is not None else 1 << 62,))
    while page := cur.fetchmany(PAGE):
        yield from page


def combo_ids(ix: LogIndex, key: tuple[str, str | None, str | None, int | None], hi: int | None) -> Iterator[tuple[int, str]]:
    """(seq, id) of the active observations of one (kind, code system, code, fasting) combination effective by `hi`."""
    kind, system, code, fasting = key
    where, args = ["kind=?", "gone=0", "eff<=?", "fasting IS ?"], [kind, hi if hi is not None else 1 << 62, fasting]
    if system is not None:
        where += ["code_system=?", "code=?"]
        args += [system, code]
    else:
        where.append("code_system IS NULL")
    cur = ix.conn.execute(f"SELECT seq, id FROM events WHERE {' AND '.join(where)}", args)
    while page := cur.fetchmany(PAGE):
        yield from page
