"""Cross-process writers, readers and builders on one instance (synthetic data, the native CLI in subprocesses)."""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor

from index_support import cli, db_rows, home, init, ingest, naive_events, obs, readings, write  # noqa: F401


def _ingest(home, rows, name, **kw):
    out = cli(home, "ingest", "--instance", "t", "--file", write(home.parent / name, rows), **kw)
    assert out["ok"], out
    return out["data"]


def test_concurrent_writers_never_lose_duplicate_or_misindex_an_event(home):
    init()
    shared = readings(120, start=10_000)
    jobs = []
    for w in range(6):
        for r in range(3):
            jobs.append((f"w{w}r{r}.json", readings(80, start=w * 1000 + r * 100) + shared))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda j: _ingest(home, j[1], j[0]), jobs))
    accepted = sum(r["counts"]["accepted"] for r in results)
    unique = 6 * 3 * 80 + 120
    assert accepted == unique and len(naive_events(home)) == unique
    ids = [e["id"] for e in naive_events(home)]
    assert len(set(ids)) == unique
    assert cli(home, "index", "verify", "--instance", "t")["data"]["ok"]
    assert cli(home, "index", "status", "--instance", "t")["data"]["state"] == "current"
    assert ingest(home, shared, dry_run=True, name="again.json")["counts"]["duplicate"] == 120


def test_readers_see_consistent_state_while_writers_append(home):
    init()
    ingest(home, [obs("Albumin", "1751-7", 40.0, "g/L", record="seed")])

    def write_job(i):
        return _ingest(home, readings(60, start=i * 100), f"rw{i}.json")

    def read_job(i):
        outs = [cli(home, "events", "list", "--instance", "t", "--limit", "3"), cli(home, "project", "--instance", "t", "--map", "blood_phenoage"),
                cli(home, "maps", "select", "--instance", "t")]
        assert all(o["ok"] for o in outs), outs
        count = cli(home, "status", "--instance", "t")["data"]["events"]["active_observations"]
        assert count >= 1
        return count

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(write_job, i) for i in range(5)] + [pool.submit(read_job, i) for i in range(5)]
        for f in futures:
            f.result()
    assert len(naive_events(home)) == 1 + 5 * 60 and cli(home, "index", "verify", "--instance", "t")["data"]["ok"]


def test_several_processes_building_a_missing_index_at_once(home):
    init()
    ingest(home, readings(2500))
    shutil.rmtree(home / "t" / "index")
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(cli, home, "events", "list", "--instance", "t", "--limit", "1"),
                   pool.submit(cli, home, "index", "rebuild", "--instance", "t"),
                   pool.submit(_ingest, home, readings(40, start=9000), "late.json"),
                   pool.submit(cli, home, "maps", "select", "--instance", "t"),
                   pool.submit(cli, home, "index", "status", "--instance", "t")]
        outs = [f.result() for f in futures]
    assert all(o["ok"] if isinstance(o, dict) and "ok" in o else True for o in outs)
    assert len(naive_events(home)) == 2540 and len(db_rows(home)) == 2540
    assert cli(home, "index", "verify", "--instance", "t")["data"]["ok"]
    assert not list((home / "t" / "index").glob(".build-*"))
