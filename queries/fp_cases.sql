--
-- fp_cases.sql
--
-- Measured vs predicted false positives for the filter sizes that
-- bloom_dynamic uses in the benchmark (small m), plus b = 10.
-- Companion to fp_rate.sql; see docs/fp_investigation.md.
--
--   m = ceil(n * b),  k = round(0.693 * b)
--   members      = keys 1 .. n (added to the filter)
--   non-members  = keys n+1 .. n+1,000,000 (tested; every pass is a false positive)
--   predicted    = 1,000,000 * (1 - e^(-k*n/m))^k     (standard formula)
--
-- Run in site_a:  psql -X -d site_a -f queries/fp_cases.sql
--

-- Step 1: synthetic keys, one row per case.
WITH cases AS (
    SELECT n, b,
           ceil(n * b)::int8 AS m,
           round(0.693 * b)::int4 AS k
    FROM (VALUES (100, 26.4), (1000, 21.6), (10000, 16.8), (10000, 10.0)) AS c(n, b)
),
-- 1a: build one filter per case. MATERIALIZED: built only once.
filters AS MATERIALIZED (
    SELECT n, b, m, k,
           sjb_build(ARRAY(SELECT generate_series(1, n)), m, k) AS filter
    FROM cases
),
-- 1b: test the 1,000,000 non-member keys.
measured AS (
    SELECT f.n, f.b, f.m, f.k,
           count(*) FILTER (WHERE sjb_contains(f.filter, key)) AS measured_fp
    FROM filters AS f,
         generate_series(f.n + 1, f.n + 1000000) AS key
    GROUP BY f.n, f.b, f.m, f.k
)
-- 1c: false positives per 1,000,000 non-member keys.
SELECT n, b, m, k,
       round(1000000 * power(1 - exp(-k * n::numeric / m), k), 1) AS predicted_fp,
       measured_fp
FROM measured
ORDER BY n, b DESC;

-- Step 2: the same check with the REAL keys of the benchmark (n = 100 and
-- 1000): members = the distinct customer_ids of orders with
-- order_id <= n, non-members = every other id in 1 .. 1,000,000 (the
-- customers that should NOT come back). m uses the number of distinct
-- keys, exactly like bench/strategies.py.
WITH real_cases AS (
    SELECT n, b,
           ARRAY(SELECT DISTINCT customer_id FROM orders WHERE order_id <= n) AS keys
    FROM (VALUES (100, 26.4), (1000, 21.6)) AS c(n, b)
),
real_filters AS MATERIALIZED (
    SELECT n, b, keys,
           cardinality(keys) AS distinct_keys,
           greatest(8, ceil(cardinality(keys) * b))::int8 AS m,
           greatest(1, round(0.693 * b))::int4 AS k
    FROM real_cases
),
real_built AS MATERIALIZED (
    SELECT n, b, keys, distinct_keys, m, k,
           sjb_build(keys, m, k) AS filter
    FROM real_filters
),
real_measured AS (
    SELECT r.n, r.b, r.distinct_keys, r.m, r.k,
           count(*) FILTER (WHERE sjb_contains(r.filter, id)) AS measured_fp,
           count(*) AS non_members
    FROM real_built AS r,
         generate_series(1, 1000000) AS id
    WHERE id <> ALL (r.keys)
    GROUP BY r.n, r.b, r.distinct_keys, r.m, r.k
)
SELECT n, b, distinct_keys, m, k,
       round(non_members * power(1 - exp(-k * distinct_keys::numeric / m), k), 1) AS predicted_fp,
       measured_fp
FROM real_measured
ORDER BY n;

-- Step 3: WHY small filters have extra false positives. The k positions of
-- a key depend only on the pair (h1 mod m, h2 mod m). A non-member whose
-- pair equals a member's pair gets exactly the same k positions, so it is
-- ALWAYS a false positive, however large k is. Here we count those keys
-- directly from hashint4extended (same seed as SJB_SEED = 0x5EED5EED).
-- Expected count = 1,000,000 * n / (number of possible pairs) = 1,000,000 * n / m^2.
-- (Filter version 2 forced h2 odd, which left only m^2 / 2 pairs when m was
-- even and doubled this count.)
-- Step 3a: h1 and h2 of every key (as in compute_two_hashes in bloom.c).
-- "& 4294967295" keeps the low 32 bits as a positive number.
CREATE TEMP TABLE fp_hash_parts AS
    SELECT key,
           h & 4294967295 AS h1,
           (h >> 32) & 4294967295 AS h2
    FROM (SELECT key, hashint4extended(key, 1592614637) AS h
          FROM generate_series(1, 1001000) AS key) AS hashed;

-- Step 3b: the (x, y) pair of every key for each case: members are keys
-- 1 .. n, non-members n+1 .. n+1,000,000, as in Step 1.
CREATE TEMP TABLE fp_patterns AS
    SELECT c.n, c.m, p.key, p.h1 % c.m AS x, p.h2 % c.m AS y
    FROM (VALUES (100, 2640), (1000, 21600)) AS c(n, m), fp_hash_parts AS p
    WHERE p.key <= c.n + 1000000;
ANALYZE fp_patterns;

-- Step 3c: count the non-members that share a member's pair.
SELECT c.n, c.m,
       (SELECT count(*)
        FROM fp_patterns AS q
        WHERE q.n = c.n AND q.key > c.n
          AND EXISTS (SELECT 1 FROM fp_patterns AS r
                      WHERE r.n = c.n AND r.key <= c.n AND r.x = q.x AND r.y = q.y)
       ) AS same_positions_as_a_member,
       round(1000000 * c.n / (c.m::numeric * c.m), 1) AS expected
FROM (VALUES (100, 2640), (1000, 21600)) AS c(n, m)
ORDER BY c.n;

-- Step 3d: clean up (temp tables would also vanish at the end of the session).
DROP TABLE fp_patterns;
DROP TABLE fp_hash_parts;
