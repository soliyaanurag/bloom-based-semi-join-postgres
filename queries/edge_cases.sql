--
-- edge_cases.sql
--
-- Extra correctness checks of the hand-written Bloom semi-join on unusual
-- keys and query shapes. Each check runs the plain query (baseline) and
-- the same query with a Bloom filter (sjb_contains in the WHERE clause)
-- and compares them with EXCEPT ALL in both directions:
--   missing_rows = (baseline EXCEPT ALL bloom)   rows the Bloom query lost
--   extra_rows   = (bloom EXCEPT ALL baseline)   rows it added
-- EXCEPT ALL keeps duplicates, so both 0 means the two queries return
-- exactly the same rows, the same number of times.
--
-- Cases: NULL / duplicate / missing / negative / int4-limit keys, an empty
-- R', only NULL keys, the smallest filters, and several query shapes. The
-- last check makes sure no key is ever lost by its own filter (no false
-- negatives), for 100,000 random keys.
--
-- Pass: every missing_rows and extra_rows is 0, and both false-negative
-- counts are 0.
--
-- Run in site_a:  psql -X -d site_a -f queries/edge_cases.sql
-- It creates the table edge_orders in site_a (dropped and recreated on
-- every run).
--

\pset pager off

-- Step 1: a small local table with edge keys:
--   5 twice (a duplicate key), NULL, 999999 and 1000000 (the largest
--   existing customers), -7, 2147483647 and -2147483648 (negative and int4
--   limits, no such customer), 0 (no such customer), 1 (exists).
DROP TABLE IF EXISTS edge_orders;
CREATE TABLE edge_orders (
    order_id    int PRIMARY KEY,
    customer_id int,
    note        text
);
INSERT INTO edge_orders VALUES
    (1, 5, 'a'), (2, 5, 'a'), (3, NULL, 'b'), (4, 999999, 'c'), (5, 1000000, 'c'),
    (6, -7, 'd'), (7, 2147483647, 'd'), (8, -2147483648, 'd'), (9, 0, 'e'), (10, 1, 'e');
ANALYZE edge_orders;

-- Step 2: a helper that runs both queries and returns the four counts.
-- It lives in pg_temp, so it disappears at the end of the session.
CREATE FUNCTION pg_temp.compare(test_name text, baseline_sql text, bloom_sql text,
                                OUT test text, OUT baseline_rows bigint, OUT bloom_rows bigint,
                                OUT missing_rows bigint, OUT extra_rows bigint)
LANGUAGE plpgsql AS $$
BEGIN
    test := test_name;
    EXECUTE 'SELECT count(*) FROM (' || baseline_sql || ') AS q' INTO baseline_rows;
    EXECUTE 'SELECT count(*) FROM (' || bloom_sql || ') AS q' INTO bloom_rows;
    EXECUTE 'SELECT count(*) FROM ((' || baseline_sql || ') EXCEPT ALL (' || bloom_sql || ')) AS q'
        INTO missing_rows;
    EXECUTE 'SELECT count(*) FROM ((' || bloom_sql || ') EXCEPT ALL (' || baseline_sql || ')) AS q'
        INTO extra_rows;
END
$$;

-- Step 3: the checks. In every Bloom query the sub-SELECT builds the
-- filter ONCE at site A from the keys of the same rows of R; it is sent
-- to site B as a parameter. sjb_build_auto gets fixed statistics of
-- customers here (1,000,000 rows, 1 row per key, 32 bytes per row,
-- 1000 Mbit/s, 158.19 ns per check); they only change the filter size.
SELECT * FROM pg_temp.compare(
    'E1 NULL, duplicate, missing, negative and int4-limit keys',
    $q$SELECT o.order_id, c.customer_id, c.name
       FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id$q$,
    $q$SELECT o.order_id, c.customer_id, c.name
       FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19)
                           FROM edge_orders), c.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E2 empty R'' (array_agg of no rows is NULL, so the filter is NULL)',
    $q$SELECT o.order_id, c.name
       FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id > 100$q$,
    $q$SELECT o.order_id, c.name
       FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id > 100
         AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19)
                           FROM edge_orders WHERE order_id > 100), c.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E3 empty R'' with an empty array (rule a: m = 8, no bit set)',
    $q$SELECT o.order_id, c.name
       FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id > 100$q$,
    $q$SELECT o.order_id, c.name
       FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id > 100
         AND sjb_contains((SELECT sjb_build_auto(ARRAY(SELECT customer_id FROM edge_orders WHERE order_id > 100),
                                                 1e6, 1, 32, 1000, 158.19)), c.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E4 only NULL keys',
    $q$SELECT o.order_id, c.name
       FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id = 3$q$,
    $q$SELECT o.order_id, c.name
       FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id = 3
         AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19)
                           FROM edge_orders WHERE order_id = 3), c.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E5 smallest filter, m = 8 bits (almost every row passes)',
    $q$SELECT o.order_id, c.name
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 2000$q$,
    $q$SELECT o.order_id, c.name
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 2000
         AND sjb_contains((SELECT sjb_build(array_agg(customer_id), 8, 1)
                           FROM orders WHERE order_id <= 2000), c.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E6 capped filter (rule d, max_filter_bytes = 1000: many false positives)',
    $q$SELECT o.order_id, c.name
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 5000$q$,
    $q$SELECT o.order_id, c.name
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 5000
         AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19, 1000)
                           FROM orders WHERE order_id <= 5000), c.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E7 extra condition on the foreign table (c.city)',
    $q$SELECT o.order_id, c.name
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 5000 AND c.city = 'city_7'$q$,
    $q$SELECT o.order_id, c.name
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 5000 AND c.city = 'city_7'
         AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19)
                           FROM orders WHERE order_id <= 5000), c.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E8 comma join, reversed equality, no aliases',
    $q$SELECT orders.order_id, customers.name
       FROM orders, customers
       WHERE orders.customer_id = customers.customer_id
         AND orders.order_id BETWEEN 100 AND 3000$q$,
    $q$SELECT orders.order_id, customers.name
       FROM orders, customers
       WHERE orders.customer_id = customers.customer_id
         AND orders.order_id BETWEEN 100 AND 3000
         AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19)
                           FROM orders WHERE order_id BETWEEN 100 AND 3000), customers.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E9 DISTINCT + ORDER BY + LIMIT',
    $q$SELECT DISTINCT c.city
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 20000
       ORDER BY 1 LIMIT 50$q$,
    $q$SELECT DISTINCT c.city
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 20000
         AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19)
                           FROM orders WHERE order_id <= 20000), c.customer_id)
       ORDER BY 1 LIMIT 50$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E10 OR on the local table and NOT IN',
    $q$SELECT o.order_id, c.name
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE (o.order_id < 200 OR o.order_id > 199800) AND o.order_id NOT IN (5, 6, 7)$q$,
    $q$SELECT o.order_id, c.name
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE (o.order_id < 200 OR o.order_id > 199800) AND o.order_id NOT IN (5, 6, 7)
         AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19)
                           FROM orders
                           WHERE (order_id < 200 OR order_id > 199800) AND order_id NOT IN (5, 6, 7)),
                          c.customer_id)$q$)
UNION ALL
SELECT * FROM pg_temp.compare(
    'E11 aggregate over the join (one row)',
    $q$SELECT count(*), sum(c.customer_id::bigint)
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 60000$q$,
    $q$SELECT count(*), sum(c.customer_id::bigint)
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id
       WHERE o.order_id <= 60000
         AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1e6, 1, 32, 1000, 158.19)
                           FROM orders WHERE order_id <= 60000), c.customer_id)$q$);

-- Step 4: no false negatives. 100,000 random keys anywhere in the int4
-- range (setseed makes them the same on every run) must all pass their
-- own filter: a tiny one (sjb_build, 50,000 bits for about 100,000 keys,
-- k = 3) and the one sjb_build_auto sizes.
SELECT setseed(0.42);
CREATE TEMP TABLE random_keys AS
    SELECT DISTINCT (random() * 4e9 - 2e9)::int AS key
    FROM generate_series(1, 100000);
WITH f AS MATERIALIZED (
    SELECT sjb_build(array_agg(key), 50000, 3) AS small_filter,
           sjb_build_auto(array_agg(key), 1e6, 1, 32, 1000, 158.19) AS auto_filter
    FROM random_keys
)
SELECT count(*) AS keys,
       count(*) FILTER (WHERE NOT sjb_contains(f.small_filter, r.key)) AS false_negatives_small,
       count(*) FILTER (WHERE NOT sjb_contains(f.auto_filter, r.key)) AS false_negatives_auto,
       (SELECT sjb_info(auto_filter) FROM f) AS auto_filter
FROM random_keys r, f;
