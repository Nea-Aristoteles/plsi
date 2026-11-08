"""SQLite access for the event index: the optional import, damaged-file detection and a connection whose cursors turn
a damaged file into IndexDamaged (removing it) instead of a bare SQLite error."""

from __future__ import annotations

import contextlib
import weakref
from typing import Any

from plsi.errors import DependencyMissing, ValidationError

try:
    import sqlite3
except ImportError:  # a Python built without SQLite
    sqlite3 = None  # type: ignore[assignment]


def require() -> None:
    if sqlite3 is None:
        raise DependencyMissing("this Python has no sqlite3 module, which the event index needs", hint="install a Python build with SQLite")


def corrupt(exc: sqlite3.DatabaseError) -> bool:
    """A damaged or foreign file (rebuild it) rather than a busy, read-only or full one (try again or fall back)."""
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc)
        return "no such" in text or "malformed" in text or "not a database" in text or "disk image" in text or "disk I/O" in text
    return True


class IndexStale(ValidationError):
    """A line the index points at is not the event it recorded: the log changed under it."""

    code = "INDEX_STALE"


class IndexDamaged(ValidationError):
    """The index file is damaged; it has been removed and the next command rebuilds it from the log."""

    code = "INDEX_DAMAGED"


def guarded(fn):  # noqa: ANN001, ANN202
    def inner(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        try:
            return fn(self, *args, **kwargs)
        except sqlite3.DatabaseError as exc:
            owner = getattr(self.connection, "owner", None)
            owner = owner() if owner is not None else None
            if owner is not None and corrupt(exc) and "no such" not in str(exc):
                owner.damaged(exc)
            raise

    return inner


class Cursor(sqlite3.Cursor if sqlite3 else object):  # type: ignore[misc]
    """A cursor that turns a damaged-file error into IndexDamaged (and removes the file) instead of a bare SQLite error."""

    execute = guarded(sqlite3.Cursor.execute if sqlite3 else None)
    executemany = guarded(sqlite3.Cursor.executemany if sqlite3 else None)
    fetchone = guarded(sqlite3.Cursor.fetchone if sqlite3 else None)
    fetchmany = guarded(sqlite3.Cursor.fetchmany if sqlite3 else None)
    fetchall = guarded(sqlite3.Cursor.fetchall if sqlite3 else None)


class Conn(sqlite3.Connection if sqlite3 else object):  # type: ignore[misc]
    owner: weakref.ReferenceType[Any] | None = None  # the LogIndex, weakly: no cycle, so an unused index closes its file at once

    def execute(self, sql, parameters=(), /):  # noqa: ANN001, ANN201
        return self.cursor(Cursor).execute(sql, parameters)

    def executemany(self, sql, seq, /):  # noqa: ANN001, ANN201
        return self.cursor(Cursor).executemany(sql, seq)


def close_quietly(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.Error):
        conn.close()
