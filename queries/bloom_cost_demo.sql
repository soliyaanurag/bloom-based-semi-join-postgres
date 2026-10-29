--
-- bloom_cost_demo.sql
--
-- The presentation demo of the Bloom cost model. sjb_bloom_report shows,
-- for n distinct keys, the filter sjb_build_auto would build (b*, the
-- sizing rule, m, k, predicted false-positive rate) and the predicted
-- time of the remote part of the join, split into its parts (Ozsu &
-- Valduriez): network (bytes / bandwidth), messages, returned rows, work
-- at site A (building the filter) and work at site B (checking every row
-- of customers).
--
-- It runs for n = 100, 10000 and 150000 keys, each on a fast network
-- (1000 Mbit/s) and a slow one (1 Mbit/s). The filter is the same on both
-- networks (its size depends only on bytes); only the network part of
-- the time changes. On the fast network site B's work (checking all
-- 1,000,000 rows) dominates; on the slow network the bytes do.
--
-- A table with the same numbers for more n follows at the end
-- (sjb_bloom_estimate, the same C code).
--
-- Run in site_a:  psql -X -d site_a -f queries/bloom_cost_demo.sql
--

-- Step 1: the calibrated site B constant (bench/calibrate.py,
-- results/calibration.json "suggested"). Override with -v on the command
-- line after recalibrating on your machine. per_message_ms, per_row_us,
-- t_build_us and fetch_size are not passed below, so the functions use
-- their defaults, which are our calibrated values.
\if :{?t_check_ns}
\else
    \set t_check_ns 158.19
\endif

-- Step 2: statistics of customers from site_a's catalog
-- (sets :s_rows, :rows_per_key and :row_width).
\ir catalog_stats.sql

-- Step 3: the reports, printed as plain text (one line per line of the
-- report) instead of a table.
\pset format unaligned
\pset tuples_only on

\echo
\echo ===== n = 100 keys, fast network (1000 Mbit/s) =====
SELECT sjb_bloom_report(100, :s_rows, :rows_per_key, :row_width, 1000, :t_check_ns);
\echo
\echo ===== n = 100 keys, slow network (1 Mbit/s) =====
SELECT sjb_bloom_report(100, :s_rows, :rows_per_key, :row_width, 1, :t_check_ns);
\echo
\echo ===== n = 10000 keys, fast network (1000 Mbit/s) =====
SELECT sjb_bloom_report(10000, :s_rows, :rows_per_key, :row_width, 1000, :t_check_ns);
\echo
\echo ===== n = 10000 keys, slow network (1 Mbit/s) =====
SELECT sjb_bloom_report(10000, :s_rows, :rows_per_key, :row_width, 1, :t_check_ns);
\echo
\echo ===== n = 150000 keys, fast network (1000 Mbit/s) =====
SELECT sjb_bloom_report(150000, :s_rows, :rows_per_key, :row_width, 1000, :t_check_ns);
\echo
\echo ===== n = 150000 keys, slow network (1 Mbit/s) =====
SELECT sjb_bloom_report(150000, :s_rows, :rows_per_key, :row_width, 1, :t_check_ns);

-- Step 4: back to normal tables.
\pset format aligned
\pset tuples_only off

-- Step 5: the same model for more n, one row per (n, network), next to
-- what plain postgres_fdw would send back (all s_rows rows). The arguments
-- after t_check_ns are the defaults written out: per_message_ms,
-- per_row_us, t_build_us, fetch_size, max_filter_bytes, wire_factor.
\echo
\echo ===== summary =====
SELECT n,
       network_mbit,
       e.size_rule,
       round(e.best_bits::numeric, 2) AS b_star,
       e.m,
       e.k,
       round(e.fp_rate::numeric, 6) AS fp_rate,
       round(e.bytes_a_to_b) AS bytes_a_to_b,
       round(e.rows_back) AS rows_back,
       round(e.bytes_b_to_a) AS bytes_b_to_a,
       round((:s_rows * :row_width)::numeric) AS plain_fdw_bytes_b_to_a,
       round(e.network_ms::numeric, 1) AS network_ms,
       round(e.work_b_ms::numeric, 1) AS work_b_ms,
       round(e.total_ms::numeric, 1) AS total_ms
FROM unnest(ARRAY[100, 1000, 10000, 50000, 150000]) AS n,
     unnest(ARRAY[1000, 1]::float8[]) AS network_mbit,
     LATERAL sjb_bloom_estimate(n, :s_rows, :rows_per_key, :row_width, network_mbit, :t_check_ns,
                                0.06194, 0.54819, 0.245, 100, 1048576, 2) AS e
ORDER BY network_mbit DESC, n;
