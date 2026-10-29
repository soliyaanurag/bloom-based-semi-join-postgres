--
-- fp_rate.sql
--
-- Compares the MEASURED false-positive rate of our Bloom filter with the
-- rate PREDICTED by theory, for different numbers of bits per key.
--
--   n = 10000 keys (1..10000) are added to the filter
--   b = bits per key, one of 4, 8, 12, 16, 22
--   m = n * b                  (filter size in bits)
--   k = round(0.693 * b)       (best k for b bits per key: ln(2) * b)
--   measured FP = fraction of 1,000,000 non-member keys (10001..1010000)
--                 for which sjb_contains returns true
--   predicted FP = (1 - exp(-k*n/m))^k
--
-- Run in site_a:  psql -d site_a -f queries/fp_rate.sql
--

-- Step 1: one row of parameters per value of b.
WITH params AS (
    SELECT b,
           10000 AS n,
           10000 * b AS m,
           round(0.693 * b)::int AS k
    FROM unnest(ARRAY[4, 8, 12, 16, 22]) AS b
),

-- Step 2: build one filter per row. MATERIALIZED makes PostgreSQL build
-- each filter only once, instead of once per tested key.
filters AS MATERIALIZED (
    SELECT b, n, m, k,
           sjb_build(ARRAY(SELECT generate_series(1, n)), m, k) AS filter
    FROM params
),

-- Step 3: count how many of the 1,000,000 non-member keys pass each filter.
measured AS (
    SELECT f.b, f.n, f.m, f.k,
           count(*) FILTER (WHERE sjb_contains(f.filter, key)) / 1000000.0 AS measured_fp
    FROM filters AS f, generate_series(10001, 1010000) AS key
    GROUP BY f.b, f.n, f.m, f.k
)

-- Step 4: print measured and predicted side by side.
SELECT b AS bits_per_key,
       m,
       k,
       round(measured_fp, 6) AS measured_fp,
       round(power(1 - exp(-k * n::numeric / m), k), 6) AS predicted_fp
FROM measured
ORDER BY b;
