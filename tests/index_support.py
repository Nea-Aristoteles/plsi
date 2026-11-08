"""Helpers for the event-index tests: synthetic rows, an isolated PLSI_HOME, the CLI in a subprocess.

Everything here is synthetic: placeholder drug names (`test-drug-a`), invented values, throwaway directories.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden"
NOW = "2026-06-01T00:00:00Z"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty PLSI_HOME with a pinned clock; yields its path."""
    monkeypatch.setenv("PLSI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLSI_TEST_MODE", "1")
    monkeypatch.setenv("PLSI_NOW", NOW)
    from plsi import errors

    errors.drain_warnings()
    return tmp_path / "home"


def env(home: Path, **more: str) -> dict[str, str]:
    return {**os.environ, "PLSI_HOME": str(home), "PLSI_TEST_MODE": "1", "PLSI_NOW": NOW, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]),
            "PYTHONDONTWRITEBYTECODE": "1", **more}


def cli(home: Path, *args: str, stdin: str | None = None, **more: str) -> dict[str, Any]:
    """Run `plsi ARGS` in a subprocess; the parsed envelope (ok, data or error, warnings)."""
    p = subprocess.run([sys.executable, "-m", "plsi.cli", *args], env=env(home, **more), input=stdin, capture_output=True, text=True, timeout=600)
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise AssertionError(f"plsi {' '.join(args)} -> {p.returncode}: {p.stdout[:300]} {p.stderr[:500]}") from None


def obs(text: str = "synthetic metric", code: str | None = "2345-7", value: Any = 5.0, unit: str | None = "mmol/L",
        day: str = "2026-01-01T00:00:00Z", kind: str = "measurement", origin: str = "user_upload", record: str | None = None,
        sha: str | None = None, system: str = "loinc", **more: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": kind, "original_text": text, "effective_at": day, "origin": {"kind": origin}, **more}
    if code:
        row["code"] = {"system": system, "code": code}
    row["value_bool" if isinstance(value, bool) else "value_text" if isinstance(value, str) else "value_quantity"] = value
    if unit:
        row["unit"] = unit
    if record:
        row["origin"]["record_id"] = record
    if sha:
        row["origin"]["file_sha256"] = sha
    return row


def readings(n: int, start: int = 0, **more: Any) -> list[dict[str, Any]]:
    """n identical-valued readings that differ only by their origin ordinal (record_id)."""
    return [obs(record=f"ord-{i}", sha="b" * 64, **more) for i in range(start, start + n)]


def write(path: Path, rows: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def ingest(home: Path, rows: list[dict[str, Any]], *, dry_run: bool = False, name: str = "batch.json", instance: str = "t") -> dict[str, Any]:
    """The native ingest function on a list of rows."""
    from plsi.life import ingest as I

    return I.ingest(instance, file=write(home.parent / name, rows), dry_run=dry_run)


def init(instance: str = "t") -> None:
    from plsi.life import store

    store.init_instance(instance, birth_year=1970, sex="F")


def log_lines(home: Path, instance: str = "t") -> list[str]:
    return (home / instance / "events.jsonl").read_text(encoding="utf-8").splitlines()


def naive_events(home: Path, instance: str = "t") -> list[dict[str, Any]]:
    """The log read the plain way (no index): each event dict with `retracted` set from the retraction events."""
    events = [json.loads(line) for line in log_lines(home, instance) if line.strip()]
    gone = {e["payload"]["target_event_id"] for e in events if e["type"] == "retraction"}
    return [{**e, "retracted": e["id"] in gone} for e in events]


def db_rows(home: Path, instance: str = "t") -> list[tuple]:
    import sqlite3

    conn = sqlite3.connect(home / instance / "index" / "events.sqlite3")
    try:
        return conn.execute("SELECT seq, id, off, ln, type, rec, gone, key, skey, kind, code_system, code, text_key, eff, fasting, origin FROM events ORDER BY seq").fetchall()
    finally:
        conn.close()
