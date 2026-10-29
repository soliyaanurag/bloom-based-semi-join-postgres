--
-- auto_manual.sql
--
-- The join with an AUTOMATICALLY SIZED Bloom filter. Site A collects the
-- customer_ids of the kept orders, and sjb_build_auto sizes the Bloom
-- filter at RUNTIME, from the true number of distinct keys and the
-- catalog statistics of customers (sizing rules a-d, see
-- sjbloom/costmodel.c). Site B keeps the rows that pass sjb_contains; the
-- normal join at A removes the Bloom false positives.
--
-- For n = 100, 10000 and 150000 it prints sjb_info of the filter that was
-- built and the two correctness counts (both must be 0). For n = 10000 it
-- also shows the plan, with sjb_contains in the Remote SQL and the Bloom
-- lines of our EXPLAIN hook under the Foreign Scan.
--
-- Needs: sjbloom in both sites and setup/enable_sjbloom.sql.
--
-- Run in site_a:  psql -X -d site_a -f queries/auto_manual.sql
--                 psql -X -d site_a -v network_mbit=1 -f queries/auto_manual.sql
--

-- Step 1: settings. network_mbit = network speed (default 1000 Mbit/s).
-- t_check_ns = site B's time to scan and check one row; the default is
-- the value bench/calibrate.py measured on our machine
-- (results/calibration.json, "suggested"). Rerun it on yours. (These two
-- only change the predicted time, not the filter: its size depends only
-- on bytes.)
\if :{?network_mbit}
\else
    \set network_mbit 1000
\endif
\if :{?t_check_ns}
\else
    \set t_check_ns 158.19
\endif

-- Step 2: statistics of customers from site_a's catalog
-- (sets :s_rows, :rows_per_key and :row_width).
\ir catalog_stats.sql

-- Step 3: the plan for n = 10000. The sub-SELECT inside sjb_contains(...)
-- runs ONCE at site A (an "InitPlan"): array_agg collects the keys
-- (sjb_build_auto removes NULLs and repeats itself, so no DISTINCT is
-- needed), and sjb_build_auto builds the filter. It is sent to site B as
-- parameter $1: look for "sjb_contains($1::bytea, customer_id)" in the
-- "Remote SQL:" line, and for the "Bloom ..." lines below it.
\set n 10000
EXPLAIN (ANALYZE, VERBOSE, COSTS OFF)
SELECT o.order_id, c.name
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
WHERE o.order_id <= :n
  AND sjb_contains(
        (SELECT sjb_build_auto(array_agg(customer_id),
                               :s_rows, :rows_per_key, :row_width,
                               :network_mbit, :t_check_ns)
         FROM orders
         WHERE order_id <= :n),
        c.customer_id);

-- Step 4: the filter and the correctness counts for each n.
-- (\ir = include a file from the same folder.)
\set n 100
\ir auto_manual_one.sql

\set n 10000
\ir auto_manual_one.sql

\set n 150000
\ir auto_manual_one.sql
