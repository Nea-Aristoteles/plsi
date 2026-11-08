# Scale results: this branch (indexed engine)

Python 3.12.3, bulk calls of up to 50,000 rows, 3 repetitions per measurement (mean ± sample standard deviation of wall time / peak RSS of the slowest repetition). 134 native CLI calls.

| events stored | log MiB | index MiB | dry run, 4096 new rows | dry run, last 4096 stored rows (all duplicates) | dry run, 128 new rows | real append, 4096 rows | `events list --limit 20` | `project --map blood_phenoage` | `maps select` |
|---|---|---|---|---|---|---|---|---|---|
| 8,192 | 5 | 2 | 0.33 s ± 0.00 / 52 MiB | 0.33 s ± 0.00 / 52 MiB | 0.20 s ± 0.00 / 37 MiB | 0.61 s ± 0.02 / 88 MiB | 0.19 s ± 0.01 / 36 MiB | 0.35 s ± 0.01 / 42 MiB | 0.33 s ± 0.01 / 40 MiB |
| 131,072 | 74 | 37 | 0.33 s ± 0.00 / 57 MiB | 0.33 s ± 0.01 / 58 MiB | 0.20 s ± 0.00 / 38 MiB | 0.65 s ± 0.02 / 94 MiB | 0.19 s ± 0.00 / 36 MiB | 0.35 s ± 0.00 / 42 MiB | 0.41 s ± 0.03 / 55 MiB |
| 1,048,576 | 593 | 293 | 0.34 s ± 0.00 / 68 MiB | 0.35 s ± 0.01 / 68 MiB | 0.20 s ± 0.00 / 38 MiB | 0.80 s ± 0.02 / 104 MiB | 0.19 s ± 0.00 / 36 MiB | 0.35 s ± 0.00 / 42 MiB | 0.66 s ± 0.05 / 54 MiB |
| 2,097,152 | 1187 | 586 | 0.34 s ± 0.01 / 68 MiB | 0.34 s ± 0.01 / 68 MiB | 0.20 s ± 0.01 / 38 MiB | 0.88 s ± 0.01 / 104 MiB | 0.18 s ± 0.00 / 36 MiB | 0.35 s ± 0.00 / 43 MiB | 0.98 s ± 0.09 / 54 MiB |

## Bulk ingest

42 calls of 10,000+ rows (file 50,000 rows ≈ 17 MB): 7,163 rows/s mean, sd 670, min 6,366, max 8,649; peak RSS 312 MiB.

Cost per row against events stored: slope +14.70 µs per row per million events (95% CI ± 3.94). Mean cost per row: 132 µs while the log held up to 1.1M events, 143 µs in the last 1.1M.

## Does cost grow with the log? (least-squares slope per million events, 95% CI)

| operation | slope (ms / million events) | ± 95% | at 1M events (mean) |
|---|---|---|---|
| dry run, 4096 new rows | +4.5 | 15.4 | 0.34 s at 1,048,576 |
| dry run, last 4096 stored rows (all duplicates) | +10.3 | 18.8 | 0.35 s at 1,048,576 |
| dry run, 128 new rows | +1.0 | 4.4 | 0.20 s at 1,048,576 |
| real append, 4096 rows | +127.7 | 81.1 | 0.80 s at 1,048,576 |
| `events list --limit 20` | -2.8 | 7.4 | 0.19 s at 1,048,576 |
| `project --map blood_phenoage` | -0.9 | 5.9 | 0.35 s at 1,048,576 |
| `maps select` | +303.0 | 57.8 | 0.66 s at 1,048,576 |
