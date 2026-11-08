"""Turn scale.py results into the markdown tables of tests/bench/results: `python summarize.py RESULTS.json [OLD.json] > out.md`.

Adds what a single run cannot show: the least-squares slope of per-call wall time against log size with its 95% confidence
interval (is the cost flat?), bulk-call throughput statistics, and, when a v0.2.1 run is given, the same operations side by
side at the sizes both reached.
"""

from __future__ import annotations

import json
import math
import statistics
import sys

OPS = [("dry_run_4096_new", "dry run, 4096 new rows"), ("dry_run_4096_replay", "dry run, last 4096 stored rows (all duplicates)"),
       ("dry_run_128_new", "dry run, 128 new rows"), ("warm_append_4096", "real append, 4096 rows"),
       ("events_list_limit_20", "`events list --limit 20`"), ("project_blood_phenoage", "`project --map blood_phenoage`"),
       ("maps_select", "`maps select`")]


def slope(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares slope and the half-width of its 95% interval (t quantile for n-2 degrees of freedom)."""
    n = len(xs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    resid = sum((y - (my + b * (x - mx))) ** 2 for x, y in zip(xs, ys, strict=True))
    se = math.sqrt(resid / (n - 2) / sxx) if n > 2 else float("nan")
    t = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23}.get(n - 2, 2.0 if n > 20 else 2.1)
    return b, t * se


def table(res: dict) -> list[str]:
    lines = ["| events stored | log MiB | index MiB | " + " | ".join(label for _, label in OPS) + " |", "|---|---|---|" + "---|" * len(OPS)]
    for s in res["stages"]:
        cells = []
        for key, _ in OPS:
            m = s.get(key)
            cells.append(f"{m['wall_s_mean']:.2f} s ± {m['wall_s_sd']:.2f} / {m['rss_kib_max'] / 1024:.0f} MiB" if m else "-")
        idx = f"{s['index_bytes'] / 2**20:.0f}" if s.get("index_bytes") else "-"
        lines.append(f"| {s['events']:,} | {s['log_bytes'] / 2**20:.0f} | {idx} | " + " | ".join(cells) + " |")
    return lines


def main() -> None:
    res = json.load(open(sys.argv[1], encoding="utf-8"))
    old = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 else None
    out = [f"# Scale results: {res['src']}", "", f"Python {res['python']}, bulk calls of up to {res['batch_rows']:,} rows, {res['reps']} repetitions per measurement "
           f"(mean ± sample standard deviation of wall time / peak RSS of the slowest repetition). {res.get('cli_calls', '?')} native CLI calls.", ""]
    out += table(res) + [""]
    bulk = [(c["rows"], c["wall_s"], c["rss_kib"], s["events"]) for s in res["stages"] for c in s["bulk_calls"] if c["rows"] >= 10_000]
    if bulk:
        rate = [r / w for r, w, _, _ in bulk]
        out += ["## Bulk ingest", "", f"{len(bulk)} calls of 10,000+ rows (file {res['batch_rows']:,} rows ≈ 17 MB): {statistics.mean(rate):,.0f} rows/s mean, "
                f"sd {statistics.stdev(rate):,.0f}, min {min(rate):,.0f}, max {max(rate):,.0f}; peak RSS {max(c[2] for c in bulk) / 1024:.0f} MiB.", ""]
        b, half = slope([c[3] for c in bulk], [c[1] / c[0] * 1e6 for c in bulk])
        first, last = [c for c in bulk if c[3] <= 1_100_000], [c for c in bulk if c[3] >= max(x[3] for x in bulk) - 1_100_000]
        out += [f"Cost per row against events stored: slope {b * 1e6:+.2f} µs per row per million events (95% CI ± {half * 1e6:.2f}). "
                f"Mean cost per row: {statistics.mean(c[1] / c[0] * 1e6 for c in first):.0f} µs while the log held up to 1.1M events, "
                f"{statistics.mean(c[1] / c[0] * 1e6 for c in last):.0f} µs in the last 1.1M.", ""]
    out += ["## Does cost grow with the log? (least-squares slope per million events, 95% CI)", "", "| operation | slope (ms / million events) | ± 95% | at 1M events (mean) |", "|---|---|---|---|"]
    for key, label in OPS:
        pts = [(s["events"], s[key]["wall_s_mean"]) for s in res["stages"] if key in s]
        if len(pts) >= 3:
            b, half = slope([p[0] for p in pts], [p[1] for p in pts])
            near = min(pts, key=lambda p: abs(p[0] - 1_048_576))
            out.append(f"| {label} | {b * 1e9:+.1f} | {half * 1e9:.1f} | {near[1]:.2f} s at {near[0]:,} |")
    rebuilds = [(s["events"], s["cold_rebuild"]) for s in res["stages"] if "cold_rebuild" in s]
    if rebuilds:
        out += ["", "## Cold rebuild of the index (`plsi index rebuild`, log only read)", "", "| events | wall | peak RSS | µs per event |", "|---|---|---|---|"]
        out += [f"| {e:,} | {r['wall_s']:.1f} s | {r['rss_kib'] / 1024:.0f} MiB | {r['wall_s'] / e * 1e6:.1f} |" for e, r in rebuilds]
    if old:
        out += ["", f"## v0.2.1 on the same operations ({old['src']})", ""] + table(old)
    print("\n".join(out))


if __name__ == "__main__":
    main()
