--
-- explain_demo_one.sql
--
-- For ONE value of :n: warm-up runs, the TEXT EXPLAIN (ANALYZE, VERBOSE) of
-- the plain join and of the Bloom join, and the check row (see
-- explain_demo.sql).
--
-- Do not run it alone: explain_demo.sql sets :n, :s_rows, :rows_per_key,
-- :row_width, :network_mbit and :t_check_ns, creates the helper functions
-- and then includes this file.
--

\echo
\echo ===== n = :n =====

-- Step 1: the two queries, kept in psql variables so that the warm-up,
-- the text EXPLAIN and the JSON check run exactly the same SQL.
--   plain_query: plain postgres_fdw, site B sends all customers.
--   bloom_query: the hand-written Bloom semi-join. The sub-SELECT runs
--                once at site A (an InitPlan) and its result, the filter,
--                is sent to site B as parameter $1.
\set plain_query 'SELECT o.order_id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= ' :n
\set bloom_query :plain_query ' AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), ' :s_rows ', ' :rows_per_key ', ' :row_width ', ' :network_mbit ', ' :t_check_ns ') FROM orders WHERE order_id <= ' :n '), c.customer_id)'

-- Step 2: warm-up runs; \o /dev/null throws their output away.
\o /dev/null
EXPLAIN (ANALYZE) :plain_query;
EXPLAIN (ANALYZE) :bloom_query;
\o

-- Step 3: the plain join, then the Bloom join, with real times.
\echo
\echo ----- plain postgres_fdw, n = :n -----
EXPLAIN (ANALYZE, VERBOSE) :plain_query;
\echo
\echo ----- hand-written Bloom semi-join (sjb_build_auto), n = :n -----
EXPLAIN (ANALYZE, VERBOSE) :bloom_query;

-- Step 4: the check row. The plans come from new EXPLAIN (ANALYZE, FORMAT
-- JSON) runs of the same two queries. :'bloom_query' is the variable as a
-- quoted SQL string.
WITH plans AS MATERIALIZED (
    SELECT pg_temp.explain_json(:'plain_query') AS plain_plan,
           pg_temp.explain_json(:'bloom_query') AS bloom_plan
),
nodes AS MATERIALIZED (
    SELECT pg_temp.foreign_scan(bloom_plan) AS node,
           (plain_plan->0->>'Execution Time')::numeric AS plain_ms,
           (bloom_plan->0->>'Execution Time')::numeric AS bloom_ms
    FROM plans
),
keys AS MATERIALIZED (
    SELECT count(DISTINCT customer_id) AS distinct_keys
    FROM orders
    WHERE order_id <= :n
),
numbers AS (
    SELECT keys.distinct_keys,
           (node->>'Bloom Keys')::bigint AS bloom_keys,
           node->>'Bloom Size' AS bloom_size,
           (node->>'Bloom Predicted FP Rate')::float8 AS fp_rate,
           round((node->>'Actual Rows')::numeric * (node->>'Actual Loops')::numeric) AS rows_from_b,
           plain_ms,
           bloom_ms
    FROM nodes, keys
)
SELECT :n AS n,
       distinct_keys,
       bloom_keys,
       bloom_keys = distinct_keys AS keys_ok,
       bloom_size,
       rows_from_b,
       rows_from_b - bloom_keys AS measured_fp,
       round((fp_rate * (:s_rows - bloom_keys))::numeric, 1) AS predicted_fp,
       round(plain_ms, 1) AS plain_ms,
       round(bloom_ms, 1) AS bloom_ms
FROM numbers;
