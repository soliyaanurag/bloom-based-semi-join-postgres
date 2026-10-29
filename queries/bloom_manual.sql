--
-- bloom_manual.sql
--
-- The join with a hand-written BLOOM semi-join. Site A builds a Bloom
-- filter from the customer_ids it needs and sends it to site B. Site B
-- only returns customers whose id passes the filter (all real matches,
-- plus a few false positives). The normal join at A then removes the false
-- positives, so the result is still exactly right.
--
-- Needs: sjbloom in both sites and setup/enable_sjbloom.sql
-- (setup/setup_all.sh does both).
--
-- Run in site_a:  psql -d site_a -f queries/bloom_manual.sql
--                 psql -d site_a -v n=10000 -v b=20 -f queries/bloom_manual.sql
--

-- Step 1: n = how many orders to keep (order_id <= n). Default 1000.
\if :{?n}
\else
    \set n 1000
\endif

-- Step 2: b = filter bits per distinct key. Default 10.
\if :{?b}
\else
    \set b 10
\endif

-- Step 3: show the plan.
-- The sub-SELECT inside sjb_contains(...) runs ONCE at site A (an
-- "InitPlan") and builds the filter:
--   keys = the distinct customer_ids of the kept orders
--   m    = b bits per key (at least 8 bits)
--   k    = 0.693 * b hash positions per key (at least 1); this k gives
--          the lowest false-positive rate for b bits per key
-- Its result is sent to site B as parameter $1. Look for
-- "sjb_contains($1::bytea, customer_id)" in the "Remote SQL:" line.
EXPLAIN (ANALYZE, VERBOSE)
SELECT o.order_id, c.name
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
WHERE o.order_id <= :n
  AND sjb_contains(
        (SELECT sjb_build(array_agg(DISTINCT customer_id),
                          greatest(8, ceil(count(DISTINCT customer_id) * :b))::int8,
                          greatest(1, round(0.693 * :b))::int4)
         FROM orders
         WHERE order_id <= :n),
        c.customer_id);
