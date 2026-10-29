# CS631 Project (mid-sem version): Bloom Filter Semi-Join for postgres_fdw

## What this repository is
Site A joins a local table R (orders) with a table S (customers) that lives at site B and is visible at A as a postgres_fdw foreign table. Plain postgres_fdw fetches all of S. Instead, a HAND-WRITTEN query:
1. At A, builds a Bloom filter from the distinct non-null join keys of the filtered local rows R' (an InitPlan: `(SELECT sjb_build_auto(array_agg(customer_id), ...) FROM orders WHERE ...)`).
2. Sends it to B as a query parameter: `sjb_contains($1::bytea, customer_id)` in the Remote SQL. B keeps only rows of S whose key passes (S ⋉ R' plus false positives).
3. A runs the normal join, which removes the false positives, so the result is ALWAYS correct.
The sjbloom extension adds the filter functions, a cost model that sizes the filter (rules a-d), and Bloom lines in EXPLAIN ANALYZE. It runs on plain PostgreSQL 18 with the original postgres_fdw; no PostgreSQL source code is changed and there is no automatic query rewrite.

This repository is a STRICT SUBSET of the full CS631 version (https://github.com/soliyaanurag/Bloom-Filter-based-semi-join), so it can be extended back later. Do not change what the subset rule below fixes.

## Environment
- Machine: WSL2 Ubuntu. Repository: ~/bloom-semijoin-midsem.
- PostgreSQL 18.6 is installed at ~/postgres/postgresql/install (built from source by the user). Its bin/ is on PATH, so `pg_config`, `psql` and `pg_ctl` come from there. sjbloom is built with PGXS using that pg_config; `make -C sjbloom install` writes into that install folder and needs NO sudo.
- NEVER build, configure or change anything in ~/postgres/postgresql (the source tree), and never replace files in its install/lib. Do not rebuild PostgreSQL.
- NEVER modify the user's old project folder ~/postgre-project.
- The postgres_fdw library in install/lib is the build from the user's other (full) project. It behaves exactly like the original postgres_fdw unless that project's own server option is set, which this repository never does (user decision, 2026-10-29: do not swap the library). Prove it whenever needed with the plain-join check below.
- Data directory: ~/postgres/postgresql/install/data (PGDATA). The server runs as the user; WSL does not start it: `pg_ctl -l ~/pg.log start`, check with `pg_ctl status`.
- Superuser roles: anuragsoliya and postgres. Port 5432. pg_hba.conf uses trust for local connections. Keep the password option in the user mapping anyway (teammates' servers may check passwords); pass it as a psql variable, never hard-code it.
- The two "sites" are two databases, site_a and site_b, on the same server. postgres_fdw connects from site_a to site_b over localhost TCP.
- Run psql with `source setup/env.example.sh` (PGUSER=postgres): the user mapping exists only for postgres.
- Python venv: `python3 -m venv` fails here (ensurepip missing). Use `python3 -m venv --without-pip .venv` then `pip --python .venv/bin/python install -r bench/requirements.txt`.

## Fixed design decisions (do not change without asking the user)
- Subset rule: extension name `sjbloom`, version 1.0 (no upgrade scripts; after changing the SQL file: `make install`, then DROP EXTENSION sjbloom CASCADE; CREATE EXTENSION sjbloom; or rerun setup/setup_all.sh). The 24-byte header layout, SJB_VERSION = 3, SJB_MODE_BLOOM = 1, SJB_SEED and the hashing logic in bloom.c stay exactly as they are (comments may change, logic may not). The SQL signatures and defaults of sjb_build, sjb_contains, sjb_info, sjb_best_bits and sjb_build_auto stay unchanged. Mode numbers 2 and 3 are reserved: this build rejects them with a clear error.
- Scope: INNER JOIN, one equality join condition, int4 join keys.
- No change to PostgreSQL or postgres_fdw, no automatic query rewrite, no preloaded library: setup never preloads postgres_fdw and never sets any server option except host, port, dbname and extensions. Check: EXPLAIN VERBOSE of the plain join must show `Remote SQL: SELECT customer_id, name FROM public.customers` with no WHERE and no Bloom lines.
- Hashing: PostgreSQL's hashint4extended(key, seed) via DirectFunctionCall2, ONE call per key; h1 = low 32 bits, h2 = high 32 bits (not forced odd). Enhanced double hashing (Dillinger & Manolios, FMCAD 2004; as in PostgreSQL's bloomfilter.c): x = h1 mod m, y = h2 mod m; position 0 = x; for i = 1..k-1: x = (x + y) mod m, y = (y + i) mod m. sjb_bloom_test stops at the first 0 bit.
- A filter is a bytea: 24-byte header (version uint8, mode uint8, k uint16, nkeys uint32, m uint64, seed uint64), then ceil(m/8) bytes of bits. Always memcpy the header, never cast pointers. A filter of another version or mode, or with bad m/k/size, is an error in sjb_contains / sjb_info (never read with the wrong layout). sjb_check_filter (bloom.c) does these checks without raising errors; sjbloom.c turns its codes into errors.
- False-positive model: ONLY (1 - exp(-k*n/m))^k (Broder & Mitzenmacher 2004). The extra false positives of very small m (double hashing, Kirsch & Mitzenmacher 2006) are documented in docs/fp_investigation.md and NOT modelled.
- SQL functions are IMMUTABLE (postgres_fdw only ships immutable functions), the C ones STRICT PARALLEL SAFE (STRICT gives correct NULL handling). Cost model settings are function ARGUMENTS, never GUCs.
- postgres_fdw sends parameters and rows as TEXT: a bytea filter is 2 * bytes + 2 on the wire (wire_factor 2).
- row_width = width of a returned row ON THE WIRE = value text + 7 + 4 * (columns in the Remote SQL) (DataRow message). ALL byte numbers (bench CSVs and charts, byte_estimate.sql, the b sweep) use this wire width, the same as sjb_build_auto (user decision, 2026-10-29). Narrow 31.78, wide 115.78 bytes.
- Size: M = min(s_rows, n * rows_per_key); b* = ln(3.844 * (s_rows - M) * row_width / (wire_factor * n)) / 0.4805 (0 when the value inside ln is <= 1 or n <= 0). sjb_build_auto ALWAYS builds a Bloom filter with rules (costmodel.c, choose_filter_size): a) n = 0: m = 8, k = 1, no bits set; b) m = max(8, ceil(n*b*)), k = clamp(round(0.693*b*), 1, 32); c) b* = 0: m = 8, k = 1; d) rule-b filter > max_filter_bytes (header included) or > SJB_MAX_BITS: m = min(SJB_MAX_BITS, (max_filter_bytes - 24) * 8) but at least 8, k = clamp(round(0.693 * m / n), 1, 32). All rules are correct because the join at A removes false positives.
- Time model (Ozsu & Valduriez, time = T_CPU*work + T_MSG*messages + T_TR*bytes): bytes/(network_mbit*125000) + (1 + ceil(rows_back/fetch_size))*per_message_ms + rows_back*per_row_us + n*t_build_us + s_rows*t_check_ns, with rows_back = M + (s_rows - M)*fp. It does not change the size (bytes only).
- ONE function, sjb_estimate_bloom (costmodel.c), sizes the filter and fills the estimate; sjb_build_auto, sjb_bloom_report and sjb_bloom_estimate all call it. Scripts read numbers from sjb_bloom_estimate (OUT parameters), never by parsing the report text.
- EXPLAIN hook (sjb_explain.c, PostgreSQL 18 explain_per_node_hook, installed in _PG_init, previous hook saved and called first): only with es->analyze and only on a ForeignScan; for each fdw_exprs entry that is a PARAM_EXEC Param of type BYTEAOID, read es_param_exec_vals[paramid] (skip if isnull or execPlan != NULL), unpack with DatumGetByteaPP and read with VARDATA_ANY / VARSIZE_ANY_EXHDR (small filters have a 1-byte header), print only if sjb_check_filter says OK: Semijoin Filter, Bloom Keys, Bloom Size, Bloom Predicted FP Rate, Bloom Bytes Sent. It must NEVER raise an error and must reuse the extension's helpers (no duplicated formulas). The library loads by itself before EXPLAIN prints (sjb_build_auto runs first); no session_preload_libraries setting is needed (verified in a fresh session).
- Correctness test for every strategy: (baseline EXCEPT ALL new) and (new EXCEPT ALL baseline) both 0 rows.
- Benchmark strategies: baseline (plain postgres_fdw), bloom_fixed10 (b = 10), bloom_auto (sjb_build_auto).
- Constants come from bench/calibrate.py (results/calibration.json), with max_parallel_workers_per_gather = 0 (postgres_fdw's cursor never runs in parallel), each on its own calibration workload, never the benchmark queries. Never tune constants to the validation numbers. The SQL defaults of sjb_build_auto stay as they are (subset rule).

## Coding style (VERY IMPORTANT: the team are beginners)
- Prefer simple, readable code over clever or fast code.
- C: one small function per job; plain for/while loops; descriptive names (num_keys, not nk); no macro tricks; no clever pointer arithmetic.
- Every C function has a comment block above it saying, in plain English, what it does, its inputs and output, and why it exists.
- Every file starts with a header comment explaining its role in the project.
- The first time a file uses a PostgreSQL-specific macro or function (PG_GETARG_INT32, palloc, SET_VARSIZE, ereport, ...), add a short comment explaining it.
- Python: plain scripts with functions, no classes; psycopg (version 3) and matplotlib only.
- SQL: commented, one step per block.
- Keep docs/WALKTHROUGH.md and docs/manual_test_guide.md up to date when something changes.

## How to work
- Do only what you are asked for. Run the checks, show the real outputs, commit after each logical step, then stop. Do not push unless asked.
- NEVER run sudo yourself. Print the full sudo command, ask the user to run it, and wait.
- Never write passwords into committed files.
- If a check fails, do not hack around it. Show the output and explain what you think is wrong.
- BEFORE timing anything, ask the user to plug in the charger and set Windows to best-performance mode, and wait for their OK.

## Current status
- Done (2026-10-29): Bloom-only extension (rules a-d, sjb_bloom_report, sjb_bloom_estimate, EXPLAIN hook), setup, queries, bench, docs. installcheck passes; correctness, auto_manual and edge cases all 0; explain_demo: Bloom Keys = count(DISTINCT) for n = 1000, 10000, 150000, lines also for n = 0 and m = 8 filters. Benchmark rerun in best-performance mode: docs/midsem/ (24 rows correct; b sweep minimum narrow b = 18 vs b* 18.13; validation ratios 0.92-1.10). Calibrated: t_check_ns 145.2, per_message_ms 0.06115, per_row_us 0.56177, t_build_us 0.2424, fetch_size 100.
- Not pushed. The GitHub remote is still to be connected (the user creates an empty repository).

## Lessons learned
- PGXS does not track headers: sjbloom/Makefile has `$(OBJS): sjb_filter.h costmodel.h`. After a header change, check e.g. `SELECT get_byte(sjb_build(ARRAY[1], 64, 1), 0);` (= 3).
- `array_agg` over no rows is NULL, so an empty R' gives a NULL filter: sjb_contains is NULL for every row, the result is correct (empty), and EXPLAIN shows no Bloom lines. `ARRAY(SELECT ...)` gives '{}' and the rule-a filter.
- An IMMUTABLE call with only constant arguments is folded at plan time: a literal filter becomes a constant in the Remote SQL (no Param, no Bloom lines), and timing it shows ~0. Pass inputs as (SELECT ...).
- If the Foreign Scan never starts (e.g. the other join side is empty), the InitPlan never runs and there are no Bloom lines. To test the lines for tiny filters, query the foreign table alone.
- An InitPlan whose result is a Foreign Scan parameter runs INSIDE the Foreign Scan's time (postgres_fdw evaluates parameters when opening the cursor). Never add InitPlan time to Foreign Scan time.
- In test queries, build filters inline or in `WITH ... AS MATERIALIZED`, built once. Never store a big filter in a table and read it with (SELECT filter FROM t): TOAST makes sjb_contains re-fetch it per row (5.5 s instead of 0.18 s).
- At site B, `EXPLAIN (ANALYZE) DECLARE c CURSOR FOR ...` (inside a transaction) gives the plan postgres_fdw's cursor gets. The row-returning sjb_contains scan never goes parallel (planner guesses 1/3 of rows pass); only count(*) does (134 -> 61 ms), so time_contains.py (count(*)) understates B's real work.
- per_message_ms and per_row_us cannot be fitted with one fetch_size (messages = 1 + R/100 is collinear with R); calibrate.py uses fetch sizes 100/1000/10000 on the throwaway foreign table calib_tmp.customers.
- pg_stats.avg_width is the STORED width (int4 = 4 bytes), so catalog_stats.sql gives row_width 30 while the measured wire width is 31.78. Both give the same filter size within a fraction of a bit.
- The model leaves out about 4 fixed round trips per remote query (START TRANSACTION, DECLARE, CLOSE, COMMIT) and site A's own join work; validation compares with the Foreign Scan time for this reason.
- CHECK THE HOST FIRST: in Windows power-saving mode a SELECT 1 round trip is ~175 us instead of ~50 us and the 1M-row fdw fetch ~3 s instead of ~1.4 s.
- Connect Python clients through the Unix socket (host=/tmp) when measuring bytes on lo, so lo only carries postgres_fdw's A<->B traffic. psycopg's execute() returns after the whole result is in libpq: time it without fetchall; use prepare_threshold=None.
- PostgreSQL 18's EXPLAIN shows "Actual Rows" with decimals (e.g. 1000.00): bench/common.py rounds rows * loops.
- In psql, `\set var 'text' :n 'more'` concatenates; `:'var'` inserts it as a quoted literal. explain_demo.sql uses this to run the same SQL as warm-up, text EXPLAIN and JSON check.
- EXPLAIN (FORMAT JSON) inside plpgsql: `EXECUTE 'EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) ' || query INTO plan;` then `jsonb_path_query_first(plan, '$.** ? (@."Node Type" == "Foreign Scan")')`.
- WSL timing is noisy: t_check_ns has been 145-188 ns across runs.
