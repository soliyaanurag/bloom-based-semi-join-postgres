--
-- baseline.sql
--
-- The plain join with NO semi-join filter: what postgres_fdw does today.
-- We expect site A to fetch ALL 1,000,000 customers from site B.
--
-- Run in site_a:  psql -d site_a -f queries/baseline.sql
--                 psql -d site_a -v n=5000 -f queries/baseline.sql
--

-- Step 1: n = how many orders to keep (order_id <= n). Default 1000.
\if :{?n}
\else
    \set n 1000
\endif

-- Step 2: show the plan with real run times and row counts.
-- VERBOSE adds the "Remote SQL:" line: the query text sent to site B.
EXPLAIN (ANALYZE, VERBOSE)
SELECT o.order_id, c.name
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
WHERE o.order_id <= :n;
