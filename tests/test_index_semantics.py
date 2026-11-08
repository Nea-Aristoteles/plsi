"""Identity, dedupe, conflicts, retractions, bounded work and hostile input on the indexed engine (synthetic data only)."""

from __future__ import annotations

import json
import tracemalloc

import pytest
from index_support import cli, home, init, ingest, log_lines, naive_events, obs, readings, write  # noqa: F401

from plsi import errors
from plsi.events import NewEvent
from plsi.life import index as ix_mod
from plsi.life import index_scan
from plsi.life import ingest as I
from plsi.life import store
from plsi.life.models import Retraction
from plsi.life.store import EventStore
from plsi.map import mapper, registry
from plsi.vocab import EventType


def _why(home):
    return write(home.parent / "why.json", {"reason": "synthetic"})


def test_equal_values_with_distinct_origin_ordinals_all_survive_and_replay_dedupes(home):
    init()
    batch = readings(500)
    first = ingest(home, batch)
    assert first["counts"] == {"accepted": 500, "duplicate": 0, "rejected": 0}
    replay = ingest(home, batch, name="replay.json")
    assert replay["counts"] == {"accepted": 0, "duplicate": 500, "rejected": 0} and replay["duplicates"] == first["accepted"]
    stored = [e["payload"] for e in naive_events(home)]
    assert [p["origin"]["record_id"] for p in stored] == [f"ord-{i}" for i in range(500)]
    assert all(p["value_quantity"] == 5.0 and p["unit"] == "mmol/L" and p["effective_at"] == "2026-01-01T00:00:00Z" and p["origin"]["file_sha256"] == "b" * 64 for p in stored)


def test_every_part_of_the_identity_counts(home):
    init()
    base = dict(record="r1", sha="c" * 64)
    ids = ingest(home, [obs(**base)])["accepted"]
    variants = [obs(record="r2", sha="c" * 64), obs(record="r1", sha="d" * 64), obs(record="r1"), obs(value=5.1, **base), obs(unit="mg/dL", **base),
                obs(day="2026-01-02T00:00:00Z", **base), obs(code="1558-6", **base), obs(specimen="serum", **base), obs(fasting=True, **base),
                obs(value_comparator="<", **base), obs(clinical_status="active", **base), obs(effective_end="2026-02-01", **base),
                obs(text="other text", code=None, **base), obs(record="x" * 128, sha="c" * 64)]
    result = ingest(home, variants, name="v.json")
    assert result["counts"]["accepted"] == len(variants) and not result["duplicates"]
    same = ingest(home, [obs(**base), obs(unit="MMOL/L", **base)], name="s.json")
    assert same["duplicates"] == ids * 2  # the unit is compared after UCUM-syntax normalization
    assert ingest(home, [obs(text="Other TEXT", code=None, **base)], name="t.json")["duplicates"] == [e["id"] for e in naive_events(home) if e["payload"]["original_text"] == "other text"]


def test_repeat_flag_in_file_log_and_after_retraction(home):
    init()
    a = ingest(home, [obs(repeat=True, record="rep"), obs(repeat=True, record="rep")])
    assert a["counts"]["accepted"] == 2
    again = ingest(home, [obs(record="rep")], name="n.json")
    assert again["duplicates"] == [a["accepted"][-1]], "a duplicate points at the latest active event with that identity"
    first_two = ingest(home, [obs(record="solo"), obs(record="solo")], name="same.json")
    assert first_two["counts"] == {"accepted": 1, "duplicate": 1, "rejected": 0} and first_two["duplicates"] == first_two["accepted"]
    I.retract("t", first_two["accepted"][0], _why(home))
    revived = ingest(home, [obs(record="solo")], name="solo.json")
    assert revived["counts"]["accepted"] == 1 and revived["accepted"] != first_two["accepted"]
    assert ingest(home, [obs(record="solo")], name="solo2.json")["duplicates"] == revived["accepted"]


def test_rejected_rows_keep_their_positions_and_text(home):
    init()
    result = ingest(home, [obs(record="ok"), {"kind": "bogus"}, obs(value=None) | {"value_text": None}, obs(record="ok2")])
    assert result["counts"] == {"accepted": 2, "duplicate": 0, "rejected": 2}
    assert [r["index"] for r in result["rejected"]] == [1, 2] and result["rejected"][0]["error"].startswith("VALIDATION_ERROR")


def test_monthly_conflicts_see_the_log_this_input_and_retractions(home):
    init()
    m = lambda d, v, rec, origin="device": obs("Sleep", "93832-4", v, "min", d, origin=origin, record=rec)  # noqa: E731
    first = ingest(home, [m("2026-02-28", 400.0, "a")])
    assert first["conflicts"] == []
    out = ingest(home, [m("2026-02-27", 410.0, "b"), m("2026-02-28", 411.0, "c", "csv"), m("2026-02-28", 412.0, "d", "user_upload"), m("2026-03-31", 420.0, "e"),
                        m("2026-03-30", 421.0, "f")], name="c.json")
    assert [(c["index"], c["with"], c["month"]) for c in out["conflicts"]] == [(0, first["accepted"][0], "2026-02"), (1, first["accepted"][0], "2026-02"), (4, "index 3", "2026-03")]
    assert out["warnings"] == ["CONFLICT: 0", "CONFLICT: 1", "CONFLICT: 4"]
    I.retract("t", first["accepted"][0], _why(home))
    later = ingest(home, [m("2026-02-26", 399.0, "g")], name="d.json")
    assert [(c["with"]) for c in later["conflicts"]] == [out["accepted"][0]], "the earliest ACTIVE value is the one reported"


def test_dry_run_reports_what_the_real_run_does(home):
    init()
    ingest(home, readings(30))
    rows = readings(40, start=15) + [{"kind": "x"}]
    dry = ingest(home, rows, dry_run=True, name="x.json")
    real = ingest(home, rows, name="x.json")
    assert dry["counts"] == real["counts"] == {"accepted": 25, "duplicate": 15, "rejected": 1}
    assert dry["duplicates"] == real["duplicates"] and dry["rejected"] == real["rejected"] and dry["dry_run"] and not real["dry_run"]
    assert dry["accepted"] == list(range(15, 40)) and len(real["accepted"]) == 25


def test_append_many_dedupes_by_stored_key_under_the_lock(home):
    init()
    s = EventStore("t")
    payload = Retraction(target_event_id="evt_" + "1" * 16, reason="x")
    ids = [s.append(EventType.RETRACTION, payload, dedupe_key=k, dedupe=True) for k in ("k1", "k1", "zzz-not-hex", "zzz-not-hex")]
    assert [created for _, created in ids] == [True, False, True, False] and ids[0][0] == ids[1][0]
    again = EventStore("t").append_many([NewEvent(EventType.RETRACTION, payload, "k1", True), NewEvent(EventType.RETRACTION, payload, "k1", False)])
    assert again == [(ids[0][0], False), (again[1][0], True)]


def test_ingest_cost_does_not_grow_with_the_log(home, monkeypatch):
    """No stored event is parsed or validated to ingest, dry-run, project or list: model validations stay flat as the log grows."""
    init()
    calls = []
    real = ix_mod.load_event

    def counting(raw, n):
        calls.append(n)
        return real(raw, n)

    monkeypatch.setattr(ix_mod, "load_event", counting)
    monkeypatch.setattr(index_scan, "load_event", counting)
    measured = {}
    for size in (200, 4000):
        ingest(home, readings(size - len(naive_events(home)), start=len(naive_events(home))), name=f"grow{size}.json")
        calls.clear()
        ingest(home, readings(20, start=size - 10), dry_run=True, name="d.json")
        ingest(home, readings(20, start=10_000 + size), name="r.json")
        mapper.build_state("t", registry.get("blood_phenoage", "t"))
        store.list_events("t", limit=5)
        measured[size] = len(calls)
    assert measured[4000] <= measured[200] + 5 and measured[4000] < 100, measured


def test_memory_does_not_grow_with_the_log(home):
    init()
    peaks = {}
    for size in (500, 6000):
        ingest(home, readings(size - len(naive_events(home)), start=len(naive_events(home))), name=f"m{size}.json")
        tracemalloc.start()
        ingest(home, readings(200, start=size * 3), dry_run=True, name="dm.json")
        mapper.build_state("t", registry.get("blood_phenoage", "t"))
        peaks[size] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert peaks[6000] < peaks[500] * 1.5 + 2_000_000, peaks


def test_state_freshness_follows_the_log(home):
    init()
    ingest(home, [obs("Albumin", "1751-7", 40.0, "g/L", record="a")])
    cli(home, "project", "--instance", "t", "--map", "blood_phenoage")
    state = mapper.latest_state("t", "blood_phenoage")
    assert mapper.is_current(state, mapper.last_world_change("t"))
    later = "2026-06-02T00:00:00Z"
    import os

    os.environ["PLSI_NOW"] = later
    ingest(home, [obs("Albumin", "1751-7", 41.0, "g/L", record="b")], name="b.json")
    assert mapper.last_world_change("t").isoformat() == "2026-06-02T00:00:00+00:00"
    assert not mapper.is_current(state, mapper.last_world_change("t"))
    os.environ["PLSI_NOW"] = "2026-06-03T00:00:00Z"
    I.retract("t", naive_events(home)[0]["id"], _why(home))
    assert mapper.last_world_change("t").isoformat() == "2026-06-03T00:00:00+00:00"


def test_retracted_events_leave_every_read(home):
    init()
    ids = ingest(home, [obs("Albumin", "1751-7", float(v), "g/L", f"2026-0{v}-01", record=f"r{v}") for v in (1, 2, 3)])["accepted"]
    I.retract("t", ids[2], _why(home))
    state = mapper.build_state("t", registry.get("blood_phenoage", "t"))
    assert state.coordinates["albumin"].value == 2.0 and state.coordinates["albumin"].event_ids == [ids[1]]
    assert [s.value for s in state.coordinates["albumin"].series] == [1.0, 2.0]
    assert store.event_counts("t")["active_observations"] == 2
    with pytest.raises(errors.PlsiError) as exc:
        I.retract("t", ids[2], _why(home))
    assert exc.value.code == "ALREADY_RETRACTED"
    assert EventStore("t").get(ids[2]).id == ids[2]


# ------------------------------------------------------------------ hostile input


def _append(home, raw: bytes):
    with open(home / "t" / "events.jsonl", "ab") as fh:
        fh.write(raw)


def test_valid_json_that_is_not_an_event_is_refused(home):
    init()
    ingest(home, readings(3))
    _append(home, b"[]\n")
    with pytest.raises(errors.PlsiError) as exc:
        store.list_events("t")
    assert exc.value.code == "CORRUPT_EVENT"


def test_unparseable_lines_warn_and_are_counted_once_each(home):
    init()
    ingest(home, readings(4))
    for line in (b'{"a": NaN}\n', b"[" * 200_000 + b"\n", b"\xff\xfe garbage\n", b"\n", b"not json\n"):
        _append(home, line)
    ingest(home, readings(2, start=40), name="more.json")
    errors.drain_warnings()
    assert len(store.list_events("t")["events"]) == 6
    torn = [w for w in errors.drain_warnings() if w["code"] == "TORN_LINE"]
    assert len(torn) == 4 and "line 5 " in torn[0]["message"]
    assert cli(home, "index", "status", "--instance", "t")["data"]["torn_lines"] == 4


def test_a_line_longer_than_the_limit_is_refused_not_buffered(home, monkeypatch):
    init()
    ingest(home, readings(3))
    monkeypatch.setattr(index_scan, "MAX_LINE", 2000)
    _append(home, b"x" * 5000)
    with pytest.raises(errors.PlsiError) as exc:
        store.list_events("t")
    assert exc.value.code == "CORRUPT_EVENT"


def test_odd_ids_and_duplicate_ids_do_not_break_lookups(home):
    init()
    ids = ingest(home, readings(3))["accepted"]
    s = EventStore("t")
    for bad in ("x' OR 1=1 --", "%", "evt_" + "0" * 16, "", "\x00", "a" * 10_000):
        with pytest.raises(errors.NotFound):
            s.get(bad)
    _append(home, (log_lines(home)[0] + "\n").encode())
    assert len(s.events()) == 4 and s.get(ids[0]).id == ids[0]
    I.retract("t", ids[0], _why(home))
    assert all(e["retracted"] for e in store.list_events("t")["events"] if e["id"] == ids[0])
    assert store.event_counts("t")["active_observations"] == 2


def test_the_log_is_only_ever_appended_to(home):
    init()
    ingest(home, readings(50))
    raw = (home / "t" / "events.jsonl").read_bytes()
    ingest(home, readings(50), dry_run=True, name="a.json")
    I.retract("t", naive_events(home)[3]["id"], _why(home))
    ingest(home, readings(10, start=100), name="b.json")
    mapper.build_state("t", registry.get("blood_phenoage", "t"))
    cli(home, "index", "rebuild", "--instance", "t")
    cli(home, "index", "verify", "--instance", "t")
    now = (home / "t" / "events.jsonl").read_bytes()
    assert now.startswith(raw) and now[len(raw):].count(b"\n") == 11
    assert json.loads(now.split(b"\n")[-2])["type"] == "observation"
