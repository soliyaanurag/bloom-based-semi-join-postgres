<!-- bench/: Python (psycopg 3 + matplotlib) scripts that run the benchmarks and draw the result plots. -->

# bench/

| File | Job |
|---|---|
| `requirements.txt` | Python packages (psycopg 3, matplotlib) |
| `common.py` | helpers: connect, EXPLAIN JSON, find the Foreign Scan, row width, correctness check, table printing |
| `strategies.py` | SQL of each strategy (baseline, exact_keys, bloom_fixed10, bloom_dynamic) and of its bytes A->B |
| `run_experiments.py` | main benchmark, appends to `results/results.csv` |
| `b_sweep.py` | total bytes vs Bloom bits per key b, writes `results/b_sweep.csv` |
| `plot.py` | draws the PNG charts in `results/` |
| `run_all.sh` | runs the three scripts above in order |
| `time_contains.py` | times site B's full scan with `sjb_contains` (median of 5), to compare two builds of the C code |
| `calibrate.py` | Batch 5/5b: measures `t_check_ns`, `per_message_ms`, `per_row_us`, `t_lookup_us` and `t_build_us` on separate calibration workloads (parallel workers off), writes `results/calibration.json` with the fitted lines and R^2 |
| `validate_model.py` | Batch 5/5b: predicted time (`sjb_choice_report`) vs measured Foreign Scan time (and end-to-end) for exact_keys and bloom_dynamic, narrow rows |
| `break_even.py` | Batch 5b: the n where the filter path beats the index path, predicted vs measured, at 1000 Mbit/s and (simulated) 1 Mbit/s |
| `final_eval.py` | Batch 8: the final evaluation. Five strategies (original, fdw_param, auto, index, filter), experiments E1-E6, fresh connection + 1 warm-up + median of 3, real bytes from `/proc/net/dev` (lo), correctness via stored results + EXCEPT ALL; writes `results/final/e*.csv` |
| `final_report.py` | Batch 8: decision quality (regret, correct choice, speedup), headline numbers and all charts in `results/final/` |
| `capture_bytes.sh` | prints (does not run) tcpdump/capinfos commands for a packet-capture cross-check |

Rerun everything: `source setup/env.example.sh && bench/run_all.sh`. See docs/WALKTHROUGH.md, Batch 4.
