# Scale results: this branch (indexed engine)

Python 3.12.3, bulk calls of up to 50,000 rows, 3 repetitions per measurement (mean ± sample standard deviation of wall time / peak RSS of the slowest repetition). 352 native CLI calls.

| events stored | log MiB | index MiB | dry run, 4096 new rows | dry run, last 4096 stored rows (all duplicates) | dry run, 128 new rows | real append, 4096 rows | `events list --limit 20` | `project --map blood_phenoage` | `maps select` |
|---|---|---|---|---|---|---|---|---|---|
| 8,192 | 5 | 2 | 0.33 s ± 0.00 / 52 MiB | 0.34 s ± 0.01 / 52 MiB | 0.20 s ± 0.01 / 37 MiB | 0.57 s ± 0.01 / 88 MiB | 0.19 s ± 0.01 / 36 MiB | 0.36 s ± 0.01 / 42 MiB | 0.34 s ± 0.01 / 40 MiB |
| 32,768 | 18 | 9 | 0.33 s ± 0.00 / 53 MiB | 0.32 s ± 0.00 / 54 MiB | 0.20 s ± 0.00 / 37 MiB | 0.58 s ± 0.01 / 89 MiB | 0.19 s ± 0.00 / 36 MiB | 0.35 s ± 0.00 / 42 MiB | 0.34 s ± 0.00 / 43 MiB |
| 131,072 | 74 | 37 | 0.33 s ± 0.01 / 57 MiB | 0.33 s ± 0.01 / 58 MiB | 0.21 s ± 0.01 / 38 MiB | 0.63 s ± 0.01 / 93 MiB | 0.18 s ± 0.00 / 36 MiB | 0.36 s ± 0.01 / 43 MiB | 0.39 s ± 0.02 / 54 MiB |
| 524,288 | 296 | 146 | 0.35 s ± 0.01 / 68 MiB | 0.35 s ± 0.01 / 68 MiB | 0.20 s ± 0.00 / 38 MiB | 0.78 s ± 0.02 / 103 MiB | 0.19 s ± 0.00 / 36 MiB | 0.38 s ± 0.01 / 49 MiB | 0.52 s ± 0.02 / 55 MiB |
| 1,048,576 | 593 | 293 | 0.36 s ± 0.01 / 68 MiB | 0.37 s ± 0.01 / 68 MiB | 0.21 s ± 0.01 / 38 MiB | 0.76 s ± 0.01 / 104 MiB | 0.19 s ± 0.00 / 36 MiB | 0.40 s ± 0.01 / 56 MiB | 0.69 s ± 0.03 / 55 MiB |
| 2,097,152 | 1187 | 586 | 0.34 s ± 0.00 / 68 MiB | 0.35 s ± 0.00 / 68 MiB | 0.19 s ± 0.00 / 38 MiB | 0.81 s ± 0.02 / 104 MiB | 0.19 s ± 0.01 / 36 MiB | 0.43 s ± 0.02 / 63 MiB | 1.03 s ± 0.09 / 55 MiB |
| 4,194,304 | 2375 | 1173 | 0.40 s ± 0.05 / 68 MiB | 0.34 s ± 0.00 / 68 MiB | 0.20 s ± 0.01 / 38 MiB | 0.87 s ± 0.05 / 104 MiB | 0.19 s ± 0.00 / 36 MiB | 0.49 s ± 0.04 / 71 MiB | 1.78 s ± 0.19 / 56 MiB |
| 8,388,608 | 4751 | 2356 | 0.36 s ± 0.00 / 68 MiB | 0.35 s ± 0.01 / 68 MiB | 0.21 s ± 0.00 / 38 MiB | 0.91 s ± 0.03 / 104 MiB | 0.19 s ± 0.00 / 36 MiB | 2.46 s ± 0.27 / 74 MiB | 2.10 s ± 0.04 / 56 MiB |

## Bulk ingest

169 calls of 10,000+ rows (file 50,000 rows ≈ 17 MB): 6,263 rows/s mean, sd 675, min 4,978, max 9,003; peak RSS 312 MiB.

Cost per row against events stored: slope +4.19 µs per row per million events (95% CI ± 0.43). Mean cost per row: 134 µs while the log held up to 1.1M events, 172 µs in the last 1.1M.

## Does cost grow with the log? (least-squares slope per million events, 95% CI)

| operation | slope (ms / million events) | ± 95% | at 1M events (mean) |
|---|---|---|---|
| dry run, 4096 new rows | +4.9 | 7.4 | 0.36 s at 1,048,576 |
| dry run, last 4096 stored rows (all duplicates) | +2.1 | 4.4 | 0.37 s at 1,048,576 |
| dry run, 128 new rows | +0.4 | 2.2 | 0.21 s at 1,048,576 |
| real append, 4096 rows | +36.1 | 26.1 | 0.76 s at 1,048,576 |
| `events list --limit 20` | +0.6 | 1.1 | 0.19 s at 1,048,576 |
| `project --map blood_phenoage` | +226.3 | 107.1 | 0.40 s at 1,048,576 |
| `maps select` | +225.6 | 65.3 | 0.69 s at 1,048,576 |

## Cold rebuild of the index (`plsi index rebuild`, log only read)

| events | wall | peak RSS | µs per event |
|---|---|---|---|
| 131,072 | 5.7 s | 111 MiB | 43.3 |
| 1,048,576 | 43.1 s | 235 MiB | 41.1 |
| 4,194,304 | 181.1 s | 235 MiB | 43.2 |
| 8,388,608 | 395.1 s | 235 MiB | 47.1 |
