# Scale results: this branch (indexed engine)

Python 3.12.3, bulk calls of up to 50,000 rows, 3 repetitions per measurement (mean ± sample standard deviation of wall time / peak RSS of the slowest repetition). 352 native CLI calls.

| events stored | log MiB | index MiB | dry run, 4096 new rows | dry run, last 4096 stored rows (all duplicates) | dry run, 128 new rows | real append, 4096 rows | `events list --limit 20` | `project --map blood_phenoage` | `maps select` |
|---|---|---|---|---|---|---|---|---|---|
| 8,192 | 5 | 2 | 0.33 s ± 0.01 / 52 MiB | 0.32 s ± 0.00 / 52 MiB | 0.20 s ± 0.00 / 37 MiB | 0.59 s ± 0.02 / 88 MiB | 0.19 s ± 0.00 / 36 MiB | 0.35 s ± 0.01 / 41 MiB | 0.34 s ± 0.01 / 40 MiB |
| 32,768 | 18 | 9 | 0.34 s ± 0.00 / 53 MiB | 0.34 s ± 0.02 / 54 MiB | 0.20 s ± 0.01 / 37 MiB | 0.64 s ± 0.03 / 89 MiB | 0.20 s ± 0.01 / 36 MiB | 0.38 s ± 0.01 / 42 MiB | 0.35 s ± 0.00 / 43 MiB |
| 131,072 | 74 | 36 | 0.33 s ± 0.00 / 57 MiB | 0.34 s ± 0.01 / 58 MiB | 0.20 s ± 0.01 / 38 MiB | 0.62 s ± 0.02 / 93 MiB | 0.19 s ± 0.00 / 36 MiB | 0.35 s ± 0.00 / 43 MiB | 0.38 s ± 0.00 / 54 MiB |
| 524,288 | 296 | 146 | 0.35 s ± 0.00 / 68 MiB | 0.35 s ± 0.00 / 68 MiB | 0.22 s ± 0.01 / 38 MiB | 0.77 s ± 0.01 / 104 MiB | 0.19 s ± 0.01 / 36 MiB | 0.39 s ± 0.02 / 48 MiB | 0.53 s ± 0.01 / 55 MiB |
| 1,048,576 | 593 | 293 | 0.36 s ± 0.01 / 68 MiB | 0.36 s ± 0.01 / 68 MiB | 0.20 s ± 0.00 / 38 MiB | 0.90 s ± 0.09 / 104 MiB | 0.19 s ± 0.00 / 36 MiB | 0.41 s ± 0.00 / 56 MiB | 0.74 s ± 0.04 / 55 MiB |
| 2,097,152 | 1187 | 586 | 0.37 s ± 0.02 / 68 MiB | 0.37 s ± 0.00 / 68 MiB | 0.20 s ± 0.00 / 38 MiB | 0.84 s ± 0.00 / 104 MiB | 0.18 s ± 0.00 / 36 MiB | 0.44 s ± 0.02 / 63 MiB | 1.07 s ± 0.09 / 55 MiB |
| 4,194,304 | 2375 | 1174 | 0.34 s ± 0.01 / 68 MiB | 0.35 s ± 0.00 / 68 MiB | 0.20 s ± 0.01 / 38 MiB | 0.89 s ± 0.03 / 104 MiB | 0.19 s ± 0.00 / 36 MiB | 0.51 s ± 0.03 / 71 MiB | 1.81 s ± 0.23 / 56 MiB |
| 8,388,608 | 4751 | 2356 | 0.37 s ± 0.01 / 68 MiB | 0.36 s ± 0.01 / 68 MiB | 0.23 s ± 0.00 / 39 MiB | 0.88 s ± 0.01 / 104 MiB | 0.22 s ± 0.00 / 36 MiB | 0.67 s ± 0.07 / 87 MiB | 3.30 s ± 0.26 / 58 MiB |

## Bulk ingest

169 calls of 10,000+ rows (file 50,000 rows ≈ 17 MB): 6,169 rows/s mean, sd 519, min 5,369, max 8,679; peak RSS 312 MiB.

Cost per row against events stored: slope +2.34 µs per row per million events (95% CI ± 0.49). Mean cost per row: 141 µs while the log held up to 1.1M events, 168 µs in the last 1.1M.

## Does cost grow with the log? (least-squares slope per million events, 95% CI)

| operation | slope (ms / million events) | ± 95% | at 1M events (mean) |
|---|---|---|---|
| dry run, 4096 new rows | +3.8 | 4.1 | 0.36 s at 1,048,576 |
| dry run, last 4096 stored rows (all duplicates) | +2.4 | 4.8 | 0.36 s at 1,048,576 |
| dry run, 128 new rows | +3.1 | 2.8 | 0.20 s at 1,048,576 |
| real append, 4096 rows | +28.8 | 33.8 | 0.90 s at 1,048,576 |
| `events list --limit 20` | +2.5 | 2.5 | 0.19 s at 1,048,576 |
| `project --map blood_phenoage` | +36.5 | 4.0 | 0.41 s at 1,048,576 |
| `maps select` | +352.3 | 3.9 | 0.74 s at 1,048,576 |

## Cold rebuild of the index (`plsi index rebuild`, log only read)

| events | wall | peak RSS | µs per event |
|---|---|---|---|
| 131,072 | 6.1 s | 110 MiB | 46.8 |
| 1,048,576 | 43.8 s | 235 MiB | 41.7 |
| 4,194,304 | 187.2 s | 235 MiB | 44.6 |
| 8,388,608 | 405.1 s | 235 MiB | 48.3 |

## v0.2.1 on the same operations (v0.2.1, commit 17c433c)

| events stored | log MiB | index MiB | dry run, 4096 new rows | dry run, last 4096 stored rows (all duplicates) | dry run, 128 new rows | real append, 4096 rows | `events list --limit 20` | `project --map blood_phenoage` | `maps select` |
|---|---|---|---|---|---|---|---|---|---|
| 8,192 | 5 | - | 0.88 s ± 0.05 / 116 MiB | 0.79 s ± 0.01 / 115 MiB | 0.61 s ± 0.00 / 91 MiB | 1.31 s ± 0.28 / 145 MiB | 0.39 s ± 0.01 / 86 MiB | 0.55 s ± 0.00 / 92 MiB | 0.62 s ± 0.03 / 90 MiB |
| 32,768 | 18 | - | 3.42 s ± 0.10 / 286 MiB | 3.04 s ± 0.19 / 285 MiB | 2.99 s ± 0.01 / 261 MiB | 4.42 s ± 0.39 / 315 MiB | 1.47 s ± 0.03 / 251 MiB | 1.69 s ± 0.01 / 258 MiB | 1.76 s ± 0.04 / 256 MiB |
| 65,536 | 37 | - | 5.97 s ± 0.01 / 512 MiB | 6.41 s ± 0.23 / 512 MiB | 6.35 s ± 0.06 / 486 MiB | 9.66 s ± 0.97 / 542 MiB | 3.58 s ± 0.10 / 470 MiB | 3.94 s ± 0.18 / 480 MiB | 3.46 s ± 0.23 / 477 MiB |
| 131,072 | 74 | - | 13.53 s ± 0.44 / 963 MiB | 14.47 s ± 0.21 / 963 MiB | 14.32 s ± 0.56 / 938 MiB | 20.35 s ± 1.60 / 997 MiB | 8.01 s ± 0.35 / 908 MiB | 9.03 s ± 0.41 / 924 MiB | 8.06 s ± 0.22 / 916 MiB |
