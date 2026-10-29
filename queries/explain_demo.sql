--
-- explain_demo.sql
--
-- Shows the Bloom lines that sjbloom adds to EXPLAIN ANALYZE (its
-- explain_per_node_hook, sjbloom/sjb_explain.c), and checks them.
--
-- For n = 1000, 10000 and 150000 (explain_demo_one.sql):
--   1. a warm-up run of the plain join and of the Bloom join (output
--      thrown away), so both measured runs find the data in memory;
--   2. EXPLAIN (ANALYZE, VERBOSE) of the plain join (plain postgres_fdw:
--      site B sends all customers) and of the Bloom join (sjb_build_auto),
--      in TEXT format with timings, so both "Execution Time" lines can be
--      compared; the Bloom plan has the "Bloom ..." lines under its
--      Foreign Scan;
--   3. a check row, read from EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON):
--        bloom_keys = the "Bloom Keys" line; keys_ok says whether it equals
--                     count(DISTINCT customer_id) of the selected orders
--        measured_fp = Foreign Scan rows - keys (every key exists at site
--                     B exactly once, so every other row that came back
--                     is a false positive)
--        predicted_fp = "Bloom Predicted FP Rate" * (s_rows - keys)
--        plain_ms, bloom_ms = Execution Time of the two queries
-- At the end, the Bloom lines are also checked for an empty filter (n = 0,
-- sizing rule a) and for the smallest filter (m = 8 bits).
--
-- Run it in a FRESH session: sjbloom is not loaded until the first Bloom
-- query runs, and its EXPLAIN hook is installed when it is loaded. That
-- happens before EXPLAIN prints, so the lines appear anyway.
--
-- Run in site_a:  psql -X -d site_a -f queries/explain_demo.sql
--

\pset pager off

-- Step 1: settings for sjb_build_auto (see auto_manual.sql).
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

-- Step 3: a helper that runs EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) of a
-- query and returns the whole plan as jsonb. It lives in pg_temp, so it
-- disappears at the end of the session. (EXPLAIN with FORMAT JSON returns
-- one row with one json value; EXECUTE ... INTO stores it.)
CREATE FUNCTION pg_temp.explain_json(query text) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    plan json;
BEGIN
    EXECUTE 'EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) ' || query INTO plan;
    RETURN plan::jsonb;
END
$$;

-- Step 4: a second helper that picks the Foreign Scan node out of such a
-- plan. "$.** ? (...)" is a JSON path: search the whole tree for the
-- object whose "Node Type" is "Foreign Scan".
CREATE FUNCTION pg_temp.foreign_scan(plan jsonb) RETURNS jsonb
LANGUAGE sql AS $$
    SELECT jsonb_path_query_first(plan, '$.** ? (@."Node Type" == "Foreign Scan")')
$$;

-- Step 5: the three values of n. (\ir = include a file from the same folder.)
\set n 1000
\ir explain_demo_one.sql

\set n 10000
\ir explain_demo_one.sql

\set n 150000
\ir explain_demo_one.sql

-- Step 6: the Bloom lines also appear for the two smallest filters. The
-- query reads customers only, so the Foreign Scan always runs.
--   n = 0: ARRAY(SELECT ...) of no rows is an empty array '{}', and
--          sjb_build_auto builds the rule a filter (m = 8, k = 1, no bit
--          set): nothing passes, 0 rows come back. (array_agg of no rows
--          would be NULL instead; then the filter is NULL, sjb_contains is
--          NULL for every row, nothing passes either, and there is no
--          filter to show.)
--   m = 8: sjb_build with m = 8 bits and k = 1 for the keys of 1000
--          orders: all 8 bits are set, so almost every row passes.
-- Both filters are only 25 bytes, so PostgreSQL stores them with a short
-- 1-byte length header; the hook must read those too.
\echo
\echo ===== the Bloom lines for the smallest filters =====
WITH plans AS MATERIALIZED (
    SELECT 'n = 0 (rule a)' AS filter_case,
           pg_temp.foreign_scan(pg_temp.explain_json(format(
               'SELECT c.customer_id, c.name FROM customers c '
               'WHERE sjb_contains((SELECT sjb_build_auto(ARRAY(SELECT customer_id FROM orders WHERE order_id <= 0), '
               '%s, %s, %s, %s, %s)), c.customer_id)',
               :s_rows, :rows_per_key, :row_width, :network_mbit, :t_check_ns))) AS node
    UNION ALL
    SELECT 'm = 8',
           pg_temp.foreign_scan(pg_temp.explain_json(
               'SELECT c.customer_id, c.name FROM customers c '
               'WHERE sjb_contains((SELECT sjb_build(array_agg(customer_id), 8, 1) '
               'FROM orders WHERE order_id <= 1000), c.customer_id)'))
)
SELECT filter_case,
       node->>'Semijoin Filter' AS semijoin_filter,
       (node->>'Bloom Keys')::int AS bloom_keys,
       node->>'Bloom Size' AS bloom_size,
       node->>'Bloom Predicted FP Rate' AS fp_rate,
       (node->>'Bloom Bytes Sent')::int AS bytes_sent,
       round((node->>'Actual Rows')::numeric * (node->>'Actual Loops')::numeric) AS rows_from_b,
       node ? 'Bloom Keys' AS lines_present
FROM plans;
