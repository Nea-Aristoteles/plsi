"""Reproduce the reported symptom with the native CLI: `python repro_symptom.py --src SRC --home HOME [--rows 32768] [--calls 4096,1024,128]`.

For each batch size it builds a fresh instance and ingests `--rows` identical-valued readings that differ only in their
origin ordinal (`origin.record_id`), `rows / batch` calls with `plsi ingest --file`, recording wall time and peak RSS of every
call; then, on the full instance, a 4096-row dry run of new rows and of the last stored batch (all duplicates), and the same
dry run on an empty instance. Prints one JSON document. Synthetic data only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WRAP = (
    "import resource,subprocess,sys;"
    "p=subprocess.run([sys.executable,'-m','plsi.cli',*sys.argv[1:]],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True);"
    "sys.stdout.write(p.stdout);sys.stderr.write(p.stderr+'\\n'+str(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)+'\\n')"
)


def rows(start: int, n: int) -> list[dict]:
    return [{"kind": "measurement", "code": {"system": "loinc", "code": "2345-7"}, "original_text": "synthetic metric", "value_quantity": 5.0,
             "unit": "mmol/L", "effective_at": "2026-01-01T00:00:00Z", "origin": {"kind": "user_upload", "record_id": f"ord-{i}"}}
            for i in range(start, start + n)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--rows", type=int, default=32768)
    ap.add_argument("--calls", default="4096,1024,128")
    a = ap.parse_args()
    home, scratch = Path(a.home), Path(a.home).parent / "repro-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PLSI_HOME": str(home), "PYTHONPATH": os.pathsep.join([a.src, *filter(None, [os.environ.get("PYTHONPATH")])]), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(*args: str) -> dict:
        t = time.monotonic()
        p = subprocess.run([sys.executable, "-c", WRAP, *args], env=env, capture_output=True, text=True)
        return {"wall_s": round(time.monotonic() - t, 3), "rss_kib": int(p.stderr.strip().splitlines()[-1]), "counts": json.loads(p.stdout).get("data", {}).get("counts")}

    def ingest(batch: list[dict], dry: bool = False) -> dict:
        path = scratch / "batch.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
        r = run("ingest", "--instance", "t", "--file", str(path), *(["--dry-run"] if dry else []))
        r["rows"], r["bytes"] = len(batch), path.stat().st_size
        return r

    out: dict = {"src": a.src, "rows": a.rows, "runs": {}}
    shutil.rmtree(home, ignore_errors=True)
    run("init", "--instance", "t")
    out["empty_home_dry_run_4096"] = ingest(rows(10**6, 4096), dry=True)
    for size in (int(x) for x in a.calls.split(",")):
        shutil.rmtree(home, ignore_errors=True)
        run("init", "--instance", "t")
        calls = [ingest(rows(k * size, size)) for k in range(a.rows // size)]
        full = {"calls": len(calls), "total_wall_s": round(sum(c["wall_s"] for c in calls), 2), "first_call_s": calls[0]["wall_s"], "last_call_s": calls[-1]["wall_s"],
                "first_call_rss_kib": calls[0]["rss_kib"], "last_call_rss_kib": calls[-1]["rss_kib"], "accepted": sum(c["counts"]["accepted"] for c in calls),
                "input_bytes": sum(c["bytes"] for c in calls), "log_bytes": (home / "t" / "events.jsonl").stat().st_size}
        if size == 4096:
            full["dry_run_4096_new"] = ingest(rows(10**6, 4096), dry=True)
            full["dry_run_replay_last_batch"] = ingest(rows(a.rows - 4096, 4096), dry=True)
        out["runs"][str(size)] = full
        print(json.dumps({size: full}), flush=True)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
