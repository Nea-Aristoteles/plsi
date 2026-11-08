"""The indexed engine against v0.2.1: golden outputs, a log the old engine wrote, and the old in-memory projection."""

from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from index_support import GOLDEN, ROOT, env, home, init, ingest, naive_events, obs, readings, write  # noqa: F401

from plsi import errors
from plsi.life import ingest as I
from plsi.life import store
from plsi.map import mapper, registry
from reference_projection import reference_context, reference_coverage, reference_observations, reference_state

EXPECTED = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))


def _scenario(tmp_path, *extra):
    out = tmp_path / "out.json"
    p = subprocess.run([sys.executable, str(GOLDEN / "scenario.py"), str(out), *extra], env=env(tmp_path / "unused"), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_current_engine_reproduces_v021_outputs(tmp_path):
    """Ingest, dedupe, conflicts, retraction, revision, events, counts, projection and scoring candidates: byte for byte
    what v0.2.1 produced for the same synthetic history (event ids normalized to log position)."""
    got = _scenario(tmp_path)
    assert got == EXPECTED


def test_log_written_by_v021_reads_the_same(tmp_path):
    """A log the old engine wrote, with no index: the first command builds one and every read matches."""
    got = _scenario(tmp_path, "--legacy", str(GOLDEN / "legacy" / "g"))
    assert got and all(got[k] == EXPECTED[k] for k in got)
    assert set(got) >= {"events_all", "counts", "select", "context", "project_blood_phenoage", "observed_cardio_bmi"}


@pytest.mark.parametrize("seed", range(6))
def test_random_histories_reproduce_v021_outputs(tmp_path, seed):
    """Seeded random sequences of ingest, dry run, replay, retract-with-replacement and revise, then every read: equal to
    what v0.2.1 produced for the same seed (compared to a recording, event ids normalized to log position)."""
    import gzip

    expected = json.loads(gzip.open(GOLDEN / "expected_random.json.gz", "rt", encoding="utf-8").read())[str(seed)]
    out = tmp_path / "out.json"
    p = subprocess.run([sys.executable, str(GOLDEN / "random_history.py"), str(seed), str(out)], env=env(tmp_path / "unused"), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    got = json.loads(out.read_text(encoding="utf-8"))
    assert [g[0] for g in got] == [e[0] for e in expected]
    for g, e in zip(got, expected, strict=True):
        assert g == e, g[0]


MAPS = ("blood_phenoage", "cardiometabolic", "sleep_activity", "care_context", "lifestyle_context", "cgm", "routine_blood")
TEMPLATES = [  # text, code, unit choices, value range
    ("Albumin", "1751-7", ["g/L", "g/dL"], (3.0, 50.0)), ("Creatinine", "2160-0", ["umol/L", "mg/dL"], (0.5, 120.0)),
    ("Glucose", "2345-7", ["mmol/L", "mg/dL", "furlongs"], (3.0, 140.0)), ("Fasting glucose", "1558-6", ["mg/dL", "mmol/L"], (60.0, 130.0)),
    ("Fasting insulin", "27873-9", ["u[IU]/mL"], (2.0, 20.0)), ("CRP", "1988-5", ["mg/dL", "mg/L"], (0.1, 5.0)),
    ("Lymphocytes", "736-9", ["%"], (10.0, 50.0)), ("MCV", "787-2", ["fL"], (70.0, 100.0)), ("RDW", "788-0", ["%"], (11.0, 16.0)),
    ("ALP", "6768-6", ["U/L"], (30.0, 150.0)), ("WBC", "6690-2", ["10*3/uL"], (3.0, 11.0)),
    ("Height", "8302-2", ["m", "cm"], (1.5, 190.0)), ("Weight", "29463-7", ["kg", "[lb_av]"], (50.0, 160.0)), ("BMI", "39156-5", ["kg/m2"], (18.0, 35.0)),
    ("Sleep", "93832-4", ["min", "h"], (300.0, 520.0)), ("Resting HR", "40443-4", ["/min"], (45.0, 80.0)),
    ("Unmapped", "5792-7", ["mg/dL"], (0.0, 3.0)), ("CGM mean", "97507-8", ["mg/dL", "mmol/L"], (80.0, 150.0)),
]
SETS = [("test-drug-a", "rxnorm", "1000", "medication"), ("test-drug-b", "rxnorm", "1001", "medication"), ("test condition", "icd10cm", "I10", "condition"),
        ("test-allergen", "snomed", "99", "allergy"), ("walks daily", None, None, "lifestyle"), ("test goal", None, None, "goal")]


def _random_history(home, seed: int) -> None:
    rng = random.Random(seed)
    init()
    days = [f"{y}-{m:02d}-{d:02d}" for y, m in ((2025, 9), (2025, 12), (2026, 3), (2026, 5)) for d in (3, 4, 18)] + ["2026-07-01"]
    for batch in range(4):
        rows = []
        for n in range(90):
            if rng.random() < 0.18:
                text, system, code, kind = rng.choice(SETS)
                rows.append(obs(text, code, rng.choice(["active", "stopped", "resolved", "text"]), None, rng.choice(days), kind=kind, system=system or "loinc",
                                clinical_status=rng.choice([None, "active", "stopped", "resolved"]),
                                **({"effective_end": rng.choice(["2026-02-01", "2027-01-01"])} if rng.random() < 0.2 else {})))
                continue
            text, code, units, (lo, hi) = rng.choice(TEMPLATES)
            more: dict = {}
            if rng.random() < 0.3:
                more["fasting"] = rng.choice([True, False])
            if rng.random() < 0.1:
                more["value_comparator"] = rng.choice(["<", ">", "<=", ">="])
            if rng.random() < 0.08:
                more["effective_basis"] = "assumed"
            value = round(rng.uniform(lo, hi), 2) if rng.random() > 0.05 else "text"
            rows.append(obs(text, code if rng.random() > 0.05 else None, value, rng.choice(units), rng.choice(days), record=f"s{seed}-{batch}-{n % 40}",
                            origin=rng.choice(["user_upload", "device", "csv"]), repeat=rng.random() < 0.05, **more))
        ingest(home, rows, name=f"batch{batch}.json")
    events = store.list_events("t")["events"]
    why = write(home.parent / "why.json", {"reason": "synthetic"})
    for e in rng.sample(events, 12):
        if e["type"] == "observation" and not e["retracted"]:
            try:
                I.retract("t", e["id"], why)
            except errors.PlsiError:
                pass
    ingest(home, [obs("Albumin", "1751-7", 41.0, "g/L", "2026-03-03", record="late")], name="late.json")


@pytest.mark.parametrize("seed", range(8))
def test_projection_equals_the_in_memory_projection(home, seed):
    """Random synthetic histories (units, fasting flags, comparators, assumed dates, repeats, retractions, sets, future
    dates): every map's state, care context, coverage and candidate values equal v0.2.1's, for several as_of times."""
    _random_history(home, seed)
    for as_of in (None, datetime(2025, 11, 1, tzinfo=UTC), datetime(2026, 4, 15, tzinfo=UTC)):
        for map_id in MAPS:
            m = registry.get(map_id, "t")
            assert mapper.build_state("t", m, as_of).dump() == reference_state("t", m, as_of).dump(), (map_id, as_of)
        assert mapper.context("t", as_of) == reference_context("t", as_of)
    for map_id in MAPS:
        m = registry.get(map_id, "t")
        assert mapper.coverage("t", m) == reference_coverage("t", m)
        for c in m.coordinates:
            if c.kind.value == "set":
                continue
            want = reference_observations("t", m, c.id)
            assert mapper.coordinate_observations("t", m, c.id) == want, (map_id, c.id)
            if want:
                lo, hi = want[len(want) // 3].effective_at, want[2 * len(want) // 3].effective_at
                inside = [o for o in want if lo <= o.effective_at <= hi]
                assert mapper.coordinate_observations("t", m, c.id, since=lo, until=hi) == inside, (map_id, c.id, "window")
    assert registry.select("t") == _reference_select(), "select"


def _reference_select():
    out = []
    for m in registry.load_all("t").values():
        present = reference_coverage("t", m)
        missing = [c.id for c in m.coordinates if c.id not in present]
        out.append({"map_id": m.id, "title": m.title, "present": present, "missing": missing,
                    "ratio": round(len(present) / len(m.coordinates), 3), "tags": m.tags})
    return sorted(out, key=lambda x: (-x["ratio"], x["map_id"]))


@pytest.mark.parametrize("seed", range(3))
def test_events_and_counts_equal_the_plain_log_reading(home, seed):
    _random_history(home, seed)
    plain = naive_events(home)
    listed = store.list_events("t")
    assert [e["id"] for e in listed["events"]] == [e["id"] for e in plain]
    assert [e["retracted"] for e in listed["events"]] == [e["retracted"] for e in plain]
    assert all({k: v for k, v in e.items() if k != "retracted"} == {k: v for k, v in p.items() if k != "retracted"} for e, p in zip(listed["events"], plain, strict=True))
    counts = store.event_counts("t")
    assert counts["active_observations"] == sum(1 for e in plain if e["type"] == "observation" and not e["retracted"])
    assert counts["retracted"] == sum(1 for e in plain if e["retracted"])
    assert counts["last_recorded_at"] == plain[-1]["recorded_at"].replace("Z", "+00:00")
    assert {e.id for e in store.EventStore("t").observations()} == {e["id"] for e in plain if e["type"] == "observation" and not e["retracted"]}
    assert store.EventStore("t").retracted_ids() == {e["payload"]["target_event_id"] for e in plain if e["type"] == "retraction"}
    for limit in (1, 5, 40):
        assert store.list_events("t", limit=limit)["events"] == listed["events"][-limit:]
    assert store.list_events("t", limit=0)["events"] == []
    assert store.list_events("t", type="observation", kind="measurement", limit=7)["events"] == [
        e for e in listed["events"] if e["type"] == "observation" and e["payload"]["kind"] == "measurement"][-7:]


def test_paging_forward_covers_the_log_exactly_once(home):
    init()
    ingest(home, readings(53))
    pages, after = [], "start"
    while True:
        page = store.list_events("t", limit=10, after=after)
        pages += page["events"]
        after = page.get("next_after")
        if after is None:
            break
    assert [e["id"] for e in pages] == [e["id"] for e in store.list_events("t")["events"]]
    with pytest.raises(errors.PlsiError):
        store.list_events("t", after="evt_" + "0" * 16, limit=3)
    with pytest.raises(errors.PlsiError):
        store.list_events("t", after=pages[0]["id"])


def test_state_document_lists_a_bounded_number_of_unmapped_ids(home, monkeypatch):
    from plsi.map import models

    init()
    ingest(home, [obs("Unmapped", "5792-7", 1.0, "mg/dL", record=f"u{i}") for i in range(30)] + [obs("Albumin", "1751-7", 40.0, "g/L")])
    monkeypatch.setattr(mapper, "UNMAPPED_CAP", 10)
    state = mapper.build_state("t", registry.get("blood_phenoage", "t"))
    ids = [e["id"] for e in naive_events(home) if e["payload"]["code"]["code"] == "5792-7"]
    assert state.unmapped_observations == ids[:10] and state.unmapped_count == 30
    monkeypatch.setattr(mapper, "UNMAPPED_CAP", models.UNMAPPED_CAP)
    state = mapper.build_state("t", registry.get("blood_phenoage", "t"))
    assert state.unmapped_observations == ids and state.unmapped_count is None and state.verify_hash()
