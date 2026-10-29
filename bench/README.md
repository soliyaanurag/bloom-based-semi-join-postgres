<!-- bench/: Python (psycopg 3 + matplotlib) scripts that run the benchmarks and draw the result plots. -->

# bench/

| File | Job |
|---|---|
| `requirements.txt` | Python packages (psycopg 3, matplotlib) |
| `common.py` | helpers: connect, EXPLAIN JSON, find the Foreign Scan, text and wire row width, correctness check, table printing |
| `strategies.py` | SQL of each strategy (baseline, bloom_fixed10, bloom_auto) and of its bytes A->B |
| `run_experiments.py` | main benchmark, appends to `results/results.csv` |
| `b_sweep.py` | total bytes vs Bloom bits per key b, writes `results/b_sweep.csv` |
| `plot.py` | draws the PNG charts in `results/` (and `results/fp_rate.csv`) |
| `run_all.sh` | runs the three scripts above in order |
| `time_contains.py` | times site B's full scan with `sjb_contains` (median of 5), to compare two builds of the C code |
| `calibrate.py` | measures `t_check_ns`, `per_message_ms`, `per_row_us` and `t_build_us` on separate calibration workloads (parallel workers off), writes `results/calibration.json` with the fitted lines and R^2 |
| `validate_model.py` | predicted time (`sjb_bloom_estimate`) vs measured Foreign Scan time (and end-to-end) for bloom_auto, narrow rows |
| `capture_bytes.sh` | prints (does not run) tcpdump/capinfos commands for a packet-capture cross-check |

All scripts read EXPLAIN as JSON and look fields up by name, so the "Bloom ..." fields that sjbloom's EXPLAIN hook adds do not disturb them.

Rerun everything: `source setup/env.example.sh && bench/run_all.sh`. See README.md and docs/WALKTHROUGH.md.
