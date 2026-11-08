"""Scale benchmark for native ingest, dry run, events and projection with per-reading histories (synthetic data only).

    python tests/bench/scale.py --src SRC --home HOME --stages 1000,8000,... --out results.json [--batch-rows 50000]

Runs the real CLI (`python -m plsi.cli`) in subprocesses against an isolated PLSI_HOME, so every number is wall time and
peak RSS of one native command. `--src` picks the engine under test (this checkout's `src`, or an extracted v0.2.1 for
the comparison). Per stage it grows the instance to the stage size with bulk ingest calls, then measures: a 4096-row dry
run of new rows, a replay dry run of the last 4096 stored rows, a real 4096-row warm append, `events list --limit 20`,
`project --map blood_phenoage` and `maps select`, repeated `--reps` times; and, where asked, a cold index rebuild.

Rows are identical-valued readings that differ only in their origin ordinal (`record_id`), alternating a mapped glucose
code and an unmapped heart-rate code, one reading per minute from 2017-01-01 (so histories reach ~9.5 years per 5M rows),
plus a few full PhenoAge panels so the derived coordinate has values. Nothing here is real or personal.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WRAP = (
    "import resource,subprocess,sys;"
    "p=subprocess.run([sys.executable,'-m','plsi.cli',*sys.argv[1:]],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True);"
    "sys.stdout.write(p.stdout);sys.stderr.write(p.stderr+'\\n'+str(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)+'\\n')"
)
T0 = datetime(2017, 1, 1, tzinfo=timezone.utc)
SHA = "b" * 64
PHENO = [("1751-7", "g/L", 45.0), ("2160-0", "umol/L", 80.0), ("2345-7", "mmol/L", 5.0), ("1988-5", "mg/dL", 0.2), ("736-9", "%", 30.0),
         ("787-2", "fL", 90.0), ("788-0", "%", 13.0), ("6768-6", "U/L", 70.0), ("6690-2", "10*3/uL", 6.0)]


def reading(i: int) -> dict:
    mapped = i % 2 == 0
    at = (T0 + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {"kind": "measurement", "original_text": "synthetic glucose" if mapped else "synthetic heart rate", "effective_at": at,
           "value_quantity": 5.0 if mapped else 62.0, "unit": "mmol/L" if mapped else "/min",
           "code": {"system": "loinc", "code": "2345-7" if mapped else "8867-4"},
           "origin": {"kind": "device", "file_sha256": SHA, "record_id": f"ord-{i}"}}
    return row


def panels() -> list[dict]:
    out = []
    for d in range(12):
        day = (datetime(2020, 1, 15, tzinfo=timezone.utc) + timedelta(days=90 * d)).strftime("%Y-%m-%d")
        out += [{"kind": "measurement", "original_text": f"panel {code}", "code": {"system": "loinc", "code": code}, "value_quantity": v * (1 + d / 100),
                 "unit": u, "effective_at": day, "origin": {"kind": "user_upload", "record_id": f"panel-{d}-{code}"}} for code, u, v in PHENO]
    return out


class Bench:
    def __init__(self, src: str, home: Path, scratch: Path) -> None:
        self.env = {**os.environ, "PLSI_HOME": str(home), "PYTHONPATH": os.pathsep.join([src, *filter(None, [os.environ.get("PYTHONPATH")])]),
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.home, self.scratch = home, scratch
        scratch.mkdir(parents=True, exist_ok=True)
        self.calls = 0

    def run(self, *args: str) -> dict:
        began = time.monotonic()
        p = subprocess.run([sys.executable, "-c", WRAP, *args], env=self.env, capture_output=True, text=True)
        wall = time.monotonic() - began
        self.calls += 1
        lines = p.stderr.strip().splitlines()
        try:
            doc = json.loads(p.stdout)
        except ValueError:
            doc = {"ok": False, "raw": p.stdout[:200], "stderr": p.stderr[-400:]}
        return {"wall_s": round(wall, 3), "rss_kib": int(lines[-1]) if lines and lines[-1].isdigit() else None, "doc": doc}

    def ingest(self, rows: list[dict], dry: bool = False) -> dict:
        path = self.scratch / "batch.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        r = self.run("ingest", "--instance", "bench", "--file", str(path), *(["--dry-run"] if dry else []))
        r["rows"], r["bytes"] = len(rows), path.stat().st_size
        r["counts"] = r["doc"].get("data", {}).get("counts")
        del r["doc"]
        return r


def summarize(runs: list[dict]) -> dict:
    walls, rss = [r["wall_s"] for r in runs], [r["rss_kib"] for r in runs if r["rss_kib"]]
    out = {"n": len(runs), "wall_s_mean": round(statistics.mean(walls), 3), "wall_s_min": min(walls), "wall_s_max": max(walls),
           "wall_s_sd": round(statistics.stdev(walls), 3) if len(walls) > 1 else 0.0, "rss_kib_max": max(rss) if rss else None,
           "rss_kib_min": min(rss) if rss else None}
    if runs[0].get("counts") is not None:
        out["counts"] = runs[0]["counts"]
        out["rows"], out["bytes"] = runs[0]["rows"], runs[0]["bytes"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--stages", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch-rows", type=int, default=50_000)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--rebuild-at", default="")
    ap.add_argument("--no-project", action="store_true")
    ap.add_argument("--resume", action="store_true", help="measure an instance this script already filled (no init, no panels)")
    a = ap.parse_args()
    home, scratch = Path(a.home), Path(a.home).parent / "bench-scratch"
    b = Bench(a.src, home, scratch)
    results: dict = {"src": a.src, "python": sys.version.split()[0], "batch_rows": a.batch_rows, "reps": a.reps, "stages": []}
    if a.resume:
        population = b.run("index", "status", "--instance", "bench")["doc"]["data"]["events"]
        nxt = population - len(panels())  # every other row is a reading with the next ordinal
    else:
        b.run("init", "--instance", "bench", "--birth-year", "1975", "--sex", "M")
        b.ingest(panels())
        population, nxt = len(panels()), 0  # next reading ordinal
    rebuild_at = {int(x) for x in a.rebuild_at.split(",") if x}
    for target in (int(x) for x in a.stages.split(",")):
        stage: dict = {"target_events": target, "bulk_calls": []}
        while population < target:  # (a stage at or below the current size only measures)
            n = min(a.batch_rows, target - population)
            r = b.ingest([reading(nxt + k) for k in range(n)])
            nxt += n
            population += n
            stage["bulk_calls"].append(r)
            print(f"  bulk {n} rows -> {population} events: {r['wall_s']}s rss={r['rss_kib']}KiB", flush=True)
        status = b.run("index", "status", "--instance", "bench")["doc"].get("data", {})
        stage["events"] = status.get("events", population)
        stage["log_bytes"] = (home / "bench" / "events.jsonl").stat().st_size
        stage["index_bytes"] = status.get("index_bytes")
        new = [reading(10**9 + k) for k in range(4096)]
        replay = [reading(k) for k in range(max(0, nxt - 4096), nxt)]
        stage["dry_run_4096_new"] = summarize([b.ingest(new, dry=True) for _ in range(a.reps)])
        stage["dry_run_4096_replay"] = summarize([b.ingest(replay, dry=True) for _ in range(a.reps)])
        stage["dry_run_128_new"] = summarize([b.ingest(new[:128], dry=True) for _ in range(a.reps)])
        stage["events_list_limit_20"] = summarize([b.run("events", "list", "--instance", "bench", "--limit", "20") for _ in range(a.reps)])
        if not a.no_project:
            stage["project_blood_phenoage"] = summarize([b.run("project", "--instance", "bench", "--map", "blood_phenoage") for _ in range(a.reps)])
            stage["maps_select"] = summarize([b.run("maps", "select", "--instance", "bench") for _ in range(a.reps)])
        warm = []
        for k in range(a.reps):
            warm.append(b.ingest([reading(nxt + j) for j in range(4096)]))
            nxt += 4096
            population += 4096
        stage["warm_append_4096"] = summarize(warm)
        if target in rebuild_at:
            r = b.run("index", "rebuild", "--instance", "bench")
            stage["cold_rebuild"] = {"wall_s": r["wall_s"], "rss_kib": r["rss_kib"], "events": r["doc"].get("data", {}).get("events"),
                                     "state": r["doc"].get("data", {}).get("state")}
        results["stages"].append(stage)
        Path(a.out).write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
        print(f"stage {target}: dry {stage['dry_run_4096_new']['wall_s_mean']}s/{stage['dry_run_4096_new']['rss_kib_max']}KiB "
              f"warm {stage['warm_append_4096']['wall_s_mean']}s project {stage.get('project_blood_phenoage', {}).get('wall_s_mean')}s", flush=True)
    results["cli_calls"] = b.calls
    Path(a.out).write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
