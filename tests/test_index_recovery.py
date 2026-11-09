"""The event index is a disposable cache: missing, stale, corrupt, torn or tampered state is detected and rebuilt, and the
log is never changed by any of it."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3

import pytest
from index_support import cli, db_rows, home, init, ingest, log_lines, naive_events, obs, readings, write  # noqa: F401

from plsi import errors, files
from plsi.life import index as ix_mod
from plsi.life import index_rows, index_scan
from plsi.life import store


def _build(home, n=300):
    init()
    ingest(home, readings(n))
    ingest(home, [obs("Albumin", "1751-7", 40.0, "g/L", record="a1"), obs("Sleep", "93832-4", 420.0, "min", "2026-02-28", origin="device", record="s1")], name="b2.json")
    first = store.list_events("t", limit=1)["events"][0]["id"]
    I_why = write(home.parent / "why.json", {"reason": "synthetic"})
    from plsi.life import ingest as I

    I.retract("t", first, I_why)
    return db_rows(home)


def _log_bytes(home):
    return (home / "t" / "events.jsonl").read_bytes()


def test_incremental_rows_equal_a_cold_build(home):
    rows = _build(home)
    assert rows
    cli(home, "index", "rebuild", "--instance", "t")
    assert db_rows(home) == rows
    assert cli(home, "index", "verify", "--instance", "t")["data"]["ok"]


def test_missing_index_is_rebuilt_without_touching_the_log(home):
    rows = _build(home)
    before = _log_bytes(home)
    import shutil

    shutil.rmtree(home / "t" / "index")
    assert len(store.list_events("t")["events"]) == len(rows)
    assert db_rows(home) == rows and _log_bytes(home) == before


@pytest.mark.parametrize("damage", ["garbage", "truncated", "foreign", "empty"])
def test_unreadable_index_is_rebuilt(home, damage):
    rows = _build(home)
    db = home / "t" / "index" / "events.sqlite3"
    for extra in ("-wal", "-shm"):
        (home / "t" / "index" / f"events.sqlite3{extra}").unlink(missing_ok=True)
    if damage == "garbage":
        db.write_bytes(os.urandom(5000))
    elif damage == "truncated":
        db.write_bytes(db.read_bytes()[: db.stat().st_size // 2])
    elif damage == "foreign":
        db.unlink()
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE something_else(x)")
        conn.commit()
        conn.close()
    else:
        db.write_bytes(b"")
    assert len(naive_events(home)) == len(store.list_events("t")["events"])
    assert db_rows(home) == rows


def test_changed_fingerprint_rebuilds_on_next_use(home):
    rows = _build(home)
    db = home / "t" / "index" / "events.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET v='stale' WHERE k='fingerprint'")
    conn.execute("UPDATE events SET key=x'00', eff=0 WHERE seq=5")
    conn.commit()
    conn.close()
    assert cli(home, "index", "status", "--instance", "t")["data"]["state"] == "stale"
    store.EventStore("t").index
    assert db_rows(home) == rows


def test_a_new_key_definition_applies_to_existing_events(home, monkeypatch):
    """dedupe_key is recomputed from stored payloads (as ingest always did): redefining it rebuilds the index and a replay
    of old rows is still all duplicates under the new definition."""
    init()
    batch = readings(40)
    first = ingest(home, batch)
    real = index_rows.dedupe_key

    def v2(o):
        return hashlib.sha256((real(o) + "v2").encode()).hexdigest()

    monkeypatch.setattr(index_rows, "dedupe_key", v2)
    monkeypatch.setattr("plsi.life.ingest.dedupe_key", v2)
    monkeypatch.setattr(index_rows, "_FINGERPRINT", [])
    again = ingest(home, batch, name="again.json")
    assert again["counts"] == {"accepted": 0, "duplicate": 40, "rejected": 0}
    assert again["duplicates"] == first["accepted"]
    conn = sqlite3.connect(home / "t" / "index" / "events.sqlite3")
    key, skey = conn.execute("SELECT key, skey FROM events WHERE seq=1").fetchone()
    conn.close()
    assert skey is not None and key != skey


def test_crash_between_log_append_and_index_commit(home, monkeypatch):
    init()
    ingest(home, readings(50))
    batch = readings(30, start=1000)

    def die(self, events):
        raise KeyboardInterrupt("process killed after the log append")

    with monkeypatch.context() as m:
        m.setattr(ix_mod.LogIndex, "add", die)
        with pytest.raises(KeyboardInterrupt):
            ingest(home, batch, name="crash.json")
    assert len(log_lines(home)) == 80 and len(db_rows(home)) == 50
    replay = ingest(home, batch, name="crash.json")
    assert replay["counts"] == {"accepted": 0, "duplicate": 30, "rejected": 0}
    assert len(db_rows(home)) == 80
    cli(home, "index", "rebuild", "--instance", "t")
    assert len(db_rows(home)) == 80 and cli(home, "index", "verify", "--instance", "t")["data"]["ok"]


def test_index_commit_failure_after_the_append_is_a_warning(home, monkeypatch):
    init()
    ingest(home, readings(10))

    def refuse(self, events):
        raise sqlite3.OperationalError("database or disk is full")

    ix = ix_mod.LogIndex
    real = ix.add

    def failing(self, events):
        conn = self.conn
        conn.execute = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error"))  # type: ignore[assignment]
        return real(self, events)

    with monkeypatch.context() as m:
        m.setattr(ix, "add", failing)
        result = ingest(home, readings(5, start=500), name="x.json")
    assert result["counts"]["accepted"] == 5
    assert any(w["code"] == "INDEX_STALE" for w in errors.drain_warnings())
    again = ingest(home, readings(5, start=500), name="x.json")
    assert again["counts"]["duplicate"] == 5 and len(db_rows(home)) == 15


def test_log_written_by_another_process_is_caught_up(home):
    """Events appended to the log by something other than this engine's writer (an older PLSI) are indexed on next use."""
    init()
    ingest(home, readings(20))
    store.init_instance("other", birth_year=1980, sex="M")
    from plsi.life import ingest as I

    other_rows = readings(15, start=700)
    I.ingest("other", file=write(home.parent / "o.json", other_rows))
    extra = (home / "other" / "events.jsonl").read_bytes()
    with open(home / "t" / "events.jsonl", "ab") as fh:
        fh.write(extra)
    result = ingest(home, other_rows, name="o2.json")
    assert result["counts"] == {"accepted": 0, "duplicate": 15, "rejected": 0}
    assert len(store.list_events("t")["events"]) == 35


def test_torn_tail_is_warned_terminated_by_the_next_append_and_kept(home):
    init()
    ingest(home, readings(10))
    good = _log_bytes(home)
    with open(home / "t" / "events.jsonl", "ab") as fh:
        fh.write(b'{"id": "evt_abc')
    errors.drain_warnings()
    assert len(store.list_events("t")["events"]) == 10
    assert any(w["code"] == "TORN_LINE" for w in errors.drain_warnings())
    assert cli(home, "index", "status", "--instance", "t")["data"]["state"] == "current"
    ingest(home, readings(5, start=50), name="after.json")
    raw = _log_bytes(home)
    assert raw.startswith(good) and raw[len(good):].startswith(b'{"id": "evt_abc\n')
    assert len(store.list_events("t")["events"]) == 15
    rows = db_rows(home)
    cli(home, "index", "rebuild", "--instance", "t")
    assert db_rows(home) == rows and cli(home, "index", "verify", "--instance", "t")["data"]["ok"]
    assert cli(home, "index", "status", "--instance", "t")["data"]["torn_lines"] == 1


def test_a_valid_last_line_without_newline_counts_as_it_did_before(home):
    init()
    ingest(home, readings(10))
    store.init_instance("other", birth_year=1980, sex="M")
    from plsi.life import ingest as I

    I.ingest("other", file=write(home.parent / "o.json", readings(1, start=900)))
    line = (home / "other" / "events.jsonl").read_bytes().rstrip(b"\n")
    with open(home / "t" / "events.jsonl", "ab") as fh:
        fh.write(line)
    assert len(store.list_events("t")["events"]) == 11
    ingest(home, readings(4, start=60), name="more.json")
    assert len(store.list_events("t")["events"]) == 15
    rows = db_rows(home)
    cli(home, "index", "rebuild", "--instance", "t")
    assert db_rows(home) == rows and cli(home, "index", "verify", "--instance", "t")["data"]["ok"]


def test_a_changed_head_or_shorter_log_is_not_trusted(home):
    _build(home)
    path = home / "t" / "events.jsonl"
    raw = path.read_bytes()
    first, rest = raw.split(b"\n", 1)
    assert b'"value_quantity":5.0' in first
    path.write_bytes(first.replace(b'"value_quantity":5.0', b'"value_quantity":6.0') + b"\n" + rest)
    assert store.list_events("t", limit=303)["events"][0]["payload"]["value_quantity"] == 6.0
    assert cli(home, "index", "verify", "--instance", "t")["data"]["ok"]
    keep = raw.split(b"\n")[:100]
    path.write_bytes(b"\n".join(keep) + b"\n")
    assert len(store.list_events("t")["events"]) == 100
    assert len(db_rows(home)) == 100


def test_a_middle_edit_is_found_by_verify_and_cured_by_rebuild(home):
    _build(home)
    path = home / "t" / "events.jsonl"
    lines = path.read_bytes().split(b"\n")
    lines[150] = lines[150].replace(b'"value_quantity":5.0', b'"value_quantity":9.0')
    path.write_bytes(b"\n".join(lines))
    out = cli(home, "index", "verify", "--instance", "t")["data"]
    assert not out["ok"] and out["mismatches"] >= 1
    cli(home, "index", "rebuild", "--instance", "t")
    assert cli(home, "index", "verify", "--instance", "t")["data"]["ok"]


def test_a_line_the_index_no_longer_matches_is_refused_not_trusted(home):
    _build(home)
    path = home / "t" / "events.jsonl"
    lines = path.read_bytes().split(b"\n")
    victim = json.loads(lines[120])
    victim["id"] = "evt_" + "f" * 16
    lines[120] = json.dumps(victim, separators=(",", ":")).encode()
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(errors.PlsiError) as exc:
        store.list_events("t")
    assert exc.value.code == "INDEX_STALE"


def test_an_invalid_event_is_refused_like_before(home):
    init()
    ingest(home, readings(5))
    with open(home / "t" / "events.jsonl", "ab") as fh:
        fh.write(b'{"id":"evt_0123456789abcdef","type":"observation","recorded_at":"2026-01-01T00:00:00Z","subject_id":"s","payload":{"kind":"bogus"}}\n')
    with pytest.raises(errors.PlsiError) as exc:
        store.list_events("t")
    assert exc.value.code == "CORRUPT_EVENT"
    assert len(db_rows(home)) == 5


def test_read_only_media_falls_back_to_a_temporary_index(home):
    init()
    ingest(home, readings(25))
    import shutil

    shutil.rmtree(home / "t" / "index")
    (home / "t" / "index").write_text("not a directory", encoding="utf-8")
    before = _log_bytes(home)
    assert len(store.list_events("t")["events"]) == 25
    result = ingest(home, readings(25), dry_run=True, name="d.json")
    assert result["counts"] == {"accepted": 0, "duplicate": 25, "rejected": 0}
    assert (home / "t" / "index").is_file() and _log_bytes(home) == before


def test_dry_run_changes_no_event_state(home):
    init()
    ingest(home, readings(20))
    snapshot = {p: p.read_bytes() for p in (home / "t").iterdir() if p.is_file() and p.name != ".lock"}
    result = ingest(home, readings(30, start=10), dry_run=True, name="dry.json")
    assert result["counts"] == {"accepted": 20, "duplicate": 10, "rejected": 0} and result["dry_run"]
    assert snapshot == {p: p.read_bytes() for p in (home / "t").iterdir() if p.is_file() and p.name != ".lock"}
    assert len(store.list_events("t")["events"]) == 20


def test_stale_build_leftovers_are_swept(home):
    _build(home)
    old = home / "t" / "index" / ".build-dead.sqlite3"
    old.write_bytes(b"x")
    os.utime(old, (1, 1))
    fresh = home / "t" / "index" / ".build-live.sqlite3"
    fresh.write_bytes(b"x")
    cli(home, "index", "rebuild", "--instance", "t")
    assert not old.exists() and fresh.exists()


def test_cold_build_reads_the_log_without_the_lock(home, monkeypatch):
    init()
    ingest(home, readings(40))
    import shutil

    shutil.rmtree(home / "t" / "index")
    seen = []
    real = index_scan.Scanner.run

    def spy(self, log, until):
        seen.append((home / "t" / ".lock").exists())
        return real(self, log, until)

    monkeypatch.setattr(index_scan.Scanner, "run", spy)
    store.list_events("t")
    assert seen and not seen[0], "the first (snapshot) scan of a cold build runs unlocked"


def test_readers_never_wait_on_a_busy_lock_when_the_index_is_current(home):
    init()
    ingest(home, readings(10))
    (home / "t" / ".lock").write_text("999999", encoding="utf-8")
    assert len(store.list_events("t")["events"]) == 10
    old = files.LOCK_TIMEOUT_S
    files.LOCK_TIMEOUT_S = 0.2
    try:
        with pytest.raises(errors.InstanceLocked):
            from plsi.life import ingest as I

            I.ingest("t", file=write(home.parent / "w.json", readings(2, start=900)))
    finally:
        files.LOCK_TIMEOUT_S = old
    (home / "t" / ".lock").unlink()


def test_project_holding_the_lock_can_still_catch_the_index_up(home):
    """`project` reads inside the instance lock; an index that is behind catches up without waiting on that lock."""
    _build(home)
    with open(home / "t" / "events.jsonl", "ab") as fh:
        pass
    store.init_instance("other", birth_year=1980, sex="M")
    from plsi.life import ingest as I

    I.ingest("other", file=write(home.parent / "o.json", [obs("Albumin", "1751-7", 44.0, "g/L", "2026-05-01", record="late")]))
    with open(home / "t" / "events.jsonl", "ab") as fh:
        fh.write((home / "other" / "events.jsonl").read_bytes())
    out = cli(home, "project", "--instance", "t", "--map", "blood_phenoage")
    assert out["ok"], out
    assert out["data"]["state"]["coordinates"]["albumin"]["value"] == 44.0
