<!-- Short manual test guide: commands to run by hand and the output to expect. -->

# Manual test guide

Run the steps in order from the project folder. Before each psql step: `source setup/env.example.sh`. The numbers below are from our machine; row counts and filter sizes must be the same on yours (the data uses a fixed random seed), times will differ.

## 1. Build and regression test

```bash
make -C sjbloom && make -C sjbloom install && make -C sjbloom installcheck
```

Expected: no compiler warnings, then

```
ok 1         - sjbloom_basic                             ... ms
1..1
# All 1 tests passed.
```

If it fails, look at `sjbloom/regression.diffs`.

## 2. The two sites

```bash
setup/setup_all.sh
```

Expected: `=== Step 1/5 ...` to `=== Step 5/5 ...`, two `extension "sjbloom" already exists, skipping` notices, the server options

```
  srvname   |                         srvoptions
------------+-------------------------------------------------------------
 site_b_srv | {host=localhost,port=5432,dbname=site_b,extensions=sjbloom}
```

and `=== Setup finished ===`.

## 3. Plain postgres_fdw (the baseline)

```bash
psql -X -d site_a -c "EXPLAIN VERBOSE SELECT o.order_id, c.name FROM orders o
  JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 1000"
```

Expected under the Foreign Scan:

```
         Remote SQL: SELECT customer_id, name FROM public.customers
```

with **no WHERE** and no `Semijoin` / `Bloom` lines. `psql -X -d site_a -f queries/baseline.sql` runs it with ANALYZE: `rows=1000000.00` from the Foreign Scan, about 1.5 s.

## 4. The Bloom semi-join by hand

```bash
psql -X -d site_a -v n=10000 -v b=10 -f queries/bloom_manual.sql
```

Expected under the Foreign Scan:

```
   ->  Foreign Scan on public.customers c  (...) (actual time=... rows=18021.00 loops=1)
         Remote SQL: SELECT customer_id, name FROM public.customers WHERE (public.sjb_contains($1::bytea, customer_id))
         Semijoin Filter: Bloom (sjbloom)
         Bloom Keys: 9955
         Bloom Size: m=99550 bits, k=7, 10.00 bits/key, 50.25% bits set
         Bloom Predicted FP Rate: 0.00819372
         Bloom Bytes Sent: 24938 bytes
```

18,021 − 9,955 = 8,066 false positives; predicted 0.00819 × 990,045 ≈ 8,112. If you see `Filter: sjb_contains(...)` *above* the Foreign Scan instead of inside the Remote SQL, the server option `extensions 'sjbloom'` is missing (run `setup/enable_sjbloom.sql`).

## 5. Correctness

```bash
psql -X -d site_a -f queries/correctness_bloom.sql
psql -X -d site_a -f queries/auto_manual.sql
psql -X -d site_a -f queries/edge_cases.sql
```

Expected: every `baseline_except_bloom`, `bloom_except_baseline`, `missing_rows` and `extra_rows` is **0**. `auto_manual.sql` prints the filters it built:

```
 100    | mode=bloom nkeys=100 m=2761 k=19 bits_set=49.58% bytes=370
 10000  | mode=bloom nkeys=9955 m=179299 k=12 bits_set=48.70% bytes=22437
 150000 | mode=bloom nkeys=139294 m=1703339 k=8 bits_set=48.03% bytes=212942
```

`edge_cases.sql` prints 11 lines (E1–E11) with 0 and 0, then `keys = 100000`, `false_negatives_small = 0`, `false_negatives_auto = 0`. E2 shows that an empty R′ with `array_agg` gives a NULL filter (0 rows, correct); E3 the same with an empty array (rule a). It takes about a minute.

## 6. The EXPLAIN lines

```bash
psql -X -d site_a -f queries/explain_demo.sql
```

Run it in a new psql session. Expected: for n = 1000, 10000 and 150000 a plain plan (Remote SQL without WHERE, `rows=1000000.00`) and a Bloom plan with the five Bloom lines, then a check row:

```
   n    | distinct_keys | bloom_keys | keys_ok |   bloom_size                                          | rows_from_b | measured_fp | predicted_fp
--------+---------------+------------+---------+-------------------------------------------------------+-------------+-------------+-------------
   1000 |           999 |        999 | t       | m=22792 bits, k=16, 22.81 bits/key, 50.48% bits set   |        1019 |          20 |         17.3
  10000 |          9955 |       9955 | t       | m=179299 bits, k=12, 18.01 bits/key, 48.70% bits set  |       10140 |         185 |        173.5
 150000 |        139294 |     139294 | t       | m=1703339 bits, k=8, 12.23 bits/key, 48.03% bits set  |      141770 |        2476 |       2431.5
```

(one row per n, also with `plain_ms` and `bloom_ms`: the Bloom query is about 3× to 10× faster). At the end:

```
  filter_case   |   filter_kind   | bloom_keys |                   bloom_size                   | fp_rate | bytes_sent | rows_from_b | lines_present
----------------+-----------------+------------+------------------------------------------------+---------+------------+-------------+---------------
 n = 0 (rule a) | Bloom (sjbloom) |          0 | m=8 bits, k=1, 0.00% bits set                  | 0       |         52 |           0 | t
 m = 8          | Bloom (sjbloom) |       1000 | m=8 bits, k=1, 0.01 bits/key, 100.00% bits set | 1       |         52 |     1000000 | t
```

Plain `EXPLAIN` without ANALYZE never shows the Bloom lines (the filter does not exist yet).

## 7. Filters of another version or mode are refused

```bash
psql -X -d site_a -c "SELECT sjb_contains(set_byte(sjb_build(ARRAY[1,2], 64, 3), 0, 2), 1)"
psql -X -d site_a -c "SELECT sjb_contains(set_byte(sjb_build(ARRAY[1,2], 64, 3), 1, 2), 1)"
```

Expected:

```
ERROR:  sjbloom: filter has version 2, but this sjbloom understands only version 3
HINT:  Site A and site B must run the same sjbloom build. Install it on both sites and rebuild the filter.
ERROR:  sjbloom: filter has mode 2, but this sjbloom understands only mode 1 (Bloom)
HINT:  Site A and site B must run the same sjbloom build.
```

## 8. The cost model

```bash
psql -X -d site_a -f queries/bloom_cost_demo.sql
psql -X -d site_a -c "SELECT sjb_bloom_report(10000, 1e6, 1, 32, 1000, 150, 0.06, 0.5, 0.25, 100, 1048576, 2)"
```

Expected for the second command (also in the regression test): `b* = 18.14`, `m = 181358 bits, k = 13`, `predicted fp rate = 0.000165`, `bytes A->B = 45390`, `rows back = ... = 10163.2`, `total = 166.727 ms`. The rules:

```bash
psql -X -d site_a -c "SELECT n, s_rows, max_filter_bytes, e.size_rule, e.m, e.k
  FROM (VALUES (0, 1e6::float8, 1048576::int8), (5000, 1e6, 1048576), (5000, 5000, 1048576), (5000, 1e6, 1000))
       AS c(n, s_rows, max_filter_bytes),
  LATERAL sjb_bloom_estimate(n, s_rows, 1, 30, 1000, 100, 0, 0, 0, 100, max_filter_bytes, 2) AS e"
```

Expected: rule `a` (m 8, k 1), `b` (97273, 13), `c` (8, 1), `d` (7808, 1).

## 9. Benchmarks

Charger in, best-performance power mode on, then:

```bash
.venv/bin/python bench/calibrate.py
bench/run_all.sh
.venv/bin/python bench/validate_model.py
```

Expected: `calibrate.py` ends with suggested values near ours (t_check_ns ≈ 145, per_message_ms ≈ 0.061, per_row_us ≈ 0.56, t_build_us ≈ 0.24); `run_all.sh` prints `All rows correct.`, `narrow: measured minimum at b = 18 ...; predicted b* = 18.13` and the list of saved charts; `validate_model.py` prints ratios between about 0.9 and 1.1. Compare the charts in `results/` with `docs/midsem/`.
