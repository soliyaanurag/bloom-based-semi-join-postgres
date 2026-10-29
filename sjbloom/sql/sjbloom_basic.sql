--
-- sjbloom_basic.sql
--
-- Regression test for the sjbloom extension (Bloom filter, sizing rules
-- a-d of sjb_build_auto, the cost model report).
-- Run with:  make installcheck   (inside sjbloom/)
-- Every check prints one boolean, one number or one short text, so the
-- expected output is easy to read. All cost model arguments are passed
-- explicitly, so the output does not depend on this machine.
--
-- Note: several checks use "WITH ... AS MATERIALIZED". This makes
-- PostgreSQL build the filter once and reuse it for every row. Without
-- MATERIALIZED the planner may copy the sjb_build(...) call into the WHERE
-- clause and rebuild the filter for every single row.
--

-- Step 1: load the extension into the test database.
CREATE EXTENSION sjbloom;

--
-- Part A: sjb_build (the caller chooses m and k).
--

-- Step 2: no false negatives. Build a filter of keys 1..10000
-- (m = 100000 bits, k = 7). Every one of those keys must pass,
-- so the count of keys that do NOT pass must be 0.
WITH f AS MATERIALIZED (
    SELECT sjb_build(ARRAY(SELECT generate_series(1, 10000)), 100000, 7) AS filter
)
SELECT count(*) AS false_negatives
FROM f, generate_series(1, 10000) AS key
WHERE NOT sjb_contains(f.filter, key);

-- Step 3: false-positive rate. Test 1,000,000 keys that were NOT added
-- (10001..1010000) against the same filter. Theory predicts about 0.0082,
-- so the measured rate must be between 0.004 and 0.015.
WITH f AS MATERIALIZED (
    SELECT sjb_build(ARRAY(SELECT generate_series(1, 10000)), 100000, 7) AS filter
),
measured AS (
    SELECT count(*) FILTER (WHERE sjb_contains(f.filter, key)) / 1000000.0 AS fp_rate
    FROM f, generate_series(10001, 1010000) AS key
)
SELECT fp_rate BETWEEN 0.004 AND 0.015 AS fp_rate_ok
FROM measured;

-- Step 4: an empty array gives a filter with nkeys = 0 and no bits set ...
SELECT sjb_info(sjb_build('{}'::int4[], 1000, 3)) AS empty_info;

-- ... and no key passes an empty filter (all bits are 0), so this is false.
SELECT sjb_contains(sjb_build('{}'::int4[], 1000, 3), 42) AS empty_contains_42;

-- Step 5: NULL elements in the array are skipped, so nkeys = 2 here.
SELECT sjb_info(sjb_build(ARRAY[1, NULL, 3], 1000, 3)) LIKE 'mode=bloom nkeys=2 %' AS null_elements_skipped;

-- Step 6: a NULL key or a NULL filter gives NULL (the functions are
-- STRICT), and "WHERE NULL" drops the row, like a NULL key in a join.
SELECT sjb_contains(sjb_build(ARRAY[1, 2, 3], 1000, 3), NULL) IS NULL AS null_key_gives_null,
       sjb_contains(NULL, 1) IS NULL AS null_filter_gives_null;

-- Step 7: repeated, negative and int4-limit keys. sjb_build counts a
-- repeated key each time (nkeys = 7), and all 5 different keys pass.
WITH f AS MATERIALIZED (
    SELECT sjb_build(ARRAY[7, -5, -2147483648, 2147483647, 0, 7, 7], 1000, 7) AS filter
)
SELECT sjb_info(f.filter) LIKE 'mode=bloom nkeys=7 %' AS repeats_counted,
       count(*) FILTER (WHERE NOT sjb_contains(f.filter, key)) AS false_negatives
FROM f, unnest(ARRAY[7, -5, -2147483648, 2147483647, 0]) AS key
GROUP BY f.filter;

-- Step 8: the smallest allowed filter, m = 8 bits, works ...
SELECT sjb_contains(sjb_build(ARRAY[1, 2, 3], 8, 3), 2) AS m8_works;

-- ... and m = 4 or k = 0 give an error.
SELECT sjb_build(ARRAY[1, 2, 3], 4, 3);
SELECT sjb_build(ARRAY[1, 2, 3], 1000, 0);

--
-- Part B: sjb_build_auto and its sizing rules a-d.
--
-- The arguments of sjb_build_auto are
--   (keys, s_rows, rows_per_key, row_width, network_mbit, t_check_ns,
--    max_filter_bytes, wire_factor, per_message_ms, per_row_us,
--    t_build_us, fetch_size)
-- and sjb_bloom_estimate takes (n, s_rows, rows_per_key, row_width,
-- network_mbit, t_check_ns, per_message_ms, per_row_us, t_build_us,
-- fetch_size, max_filter_bytes, wire_factor).
--

-- Step 9, rule a: zero keys give m = 8, k = 1 and no bits set ...
SELECT sjb_info(sjb_build_auto('{}'::int4[], 1000000, 1, 30, 1000, 100, 1048576, 2, 0, 0, 0, 100)) AS rule_a_info;

-- ... an array of only NULLs counts as zero keys too ...
SELECT sjb_info(sjb_build_auto(ARRAY[NULL, NULL]::int4[], 1000000, 1, 30, 1000, 100, 1048576, 2, 0, 0, 0, 100)) AS only_nulls_info;

-- ... and nothing passes it (the join with an empty R' is empty).
WITH f AS MATERIALIZED (
    SELECT sjb_build_auto('{}'::int4[], 1000000, 1, 30, 1000, 100, 1048576, 2, 0, 0, 0, 100) AS filter
)
SELECT count(*) AS rule_a_passing
FROM f, generate_series(-1000, 1000) AS key
WHERE sjb_contains(f.filter, key);

-- Step 10, rule b: keys 1..5000, |S| = 1,000,000, rows of 30 bytes.
-- b* = ln(3.844 * 995000 * 30 / (2 * 5000)) / 0.4805 = 19.45, so
-- m = ceil(5000 * 19.45...) and k = round(0.693 * 19.45...) = 13.
-- sjb_build_auto removes NULLs and repeated keys, so nkeys = 5000.
SELECT sjb_info(sjb_build_auto(ARRAY(SELECT generate_series(1, 5000)) || ARRAY[1, 2, NULL],
                               1e6, 1, 30, 1000, 100, 1048576, 2, 0, 0, 0, 100)) AS rule_b_info;

-- Step 11, rule c: |S| = 5000 and all 5000 keys match, so a Bloom filter
-- cannot save bytes (b* = 0): the smallest filter, m = 8, k = 1.
SELECT sjb_info(sjb_build_auto(ARRAY(SELECT generate_series(1, 5000)),
                               5000, 1, 30, 1000, 100, 1048576, 2, 0, 0, 0, 100)) AS rule_c_info;

-- Step 12, rule d: the rule b filter (about 12 KB) is bigger than
-- max_filter_bytes = 1000, so m is capped to (1000 - 24) * 8 = 7808 bits
-- (exactly 1000 bytes with the header) and k = round(0.693 * 7808 / 5000)
-- = 1. With max_filter_bytes = 10 not even the 8-bit filter fits; we
-- still build it (m = 8, k = 1).
SELECT sjb_info(sjb_build_auto(ARRAY(SELECT generate_series(1, 5000)),
                               1e6, 1, 30, 1000, 100, 1000, 2, 0, 0, 0, 100)) AS rule_d_info,
       sjb_info(sjb_build_auto(ARRAY(SELECT generate_series(1, 5000)),
                               1e6, 1, 30, 1000, 100, 10, 2, 0, 0, 0, 100)) AS rule_d_tiny_info;

-- Step 13: no false negatives under every rule: keys 1..5000 all pass
-- the filters of rules b, c and d built from them.
WITH filters AS MATERIALIZED (
    SELECT 'b' AS size_rule,
           sjb_build_auto(ARRAY(SELECT generate_series(1, 5000)), 1e6, 1, 30, 1000, 100, 1048576, 2, 0, 0, 0, 100) AS filter
    UNION ALL
    SELECT 'c',
           sjb_build_auto(ARRAY(SELECT generate_series(1, 5000)), 5000, 1, 30, 1000, 100, 1048576, 2, 0, 0, 0, 100)
    UNION ALL
    SELECT 'd',
           sjb_build_auto(ARRAY(SELECT generate_series(1, 5000)), 1e6, 1, 30, 1000, 100, 1000, 2, 0, 0, 0, 100)
)
SELECT f.size_rule,
       count(*) FILTER (WHERE NOT sjb_contains(f.filter, key)) AS false_negatives
FROM filters f, generate_series(1, 5000) AS key
GROUP BY f.size_rule
ORDER BY f.size_rule;

-- Step 14: duplicate, negative and int4-limit keys with sjb_build_auto:
-- nkeys = 5 distinct keys, and all of them pass.
WITH f AS MATERIALIZED (
    SELECT sjb_build_auto(ARRAY[7, -5, -2147483648, 2147483647, 0, 7, -5, NULL],
                          1e6, 1, 30, 1000, 100, 1048576, 2, 0, 0, 0, 100) AS filter
)
SELECT split_part(sjb_info(f.filter), ' ', 2) AS nkeys,
       count(*) FILTER (WHERE NOT sjb_contains(f.filter, key)) AS false_negatives
FROM f, unnest(ARRAY[7, -5, -2147483648, 2147483647, 0]) AS key
GROUP BY f.filter;

-- Step 15: sjb_bloom_estimate runs the same C code as sjb_build_auto, so
-- for n = 5000 it reports the same rule, m and k as the filters above.
SELECT case_name, e.size_rule, e.m, e.k
FROM (VALUES ('rule a', 0, 1e6::float8, 1048576::int8),
             ('rule b', 5000, 1e6, 1048576),
             ('rule c', 5000, 5000, 1048576),
             ('rule d', 5000, 1e6, 1000),
             ('rule d tiny', 5000, 1e6, 10)) AS c(case_name, n, s_rows, max_filter_bytes),
     LATERAL sjb_bloom_estimate(c.n, c.s_rows, 1, 30, 1000, 100, 0, 0, 0, 100,
                                c.max_filter_bytes, 2) AS e;

-- Step 16: the SQL function sjb_best_bits and the C cost model use the
-- same formula: b* for n = 10000, |S| = 1,000,000, 10000 matches, rows of
-- 100 bytes is about 20.5 in both.
SELECT round(sjb_best_bits(10000, 1000000, 10000, 100, 2)::numeric, 4) AS sql_best_bits,
       round(best_bits::numeric, 4) AS c_best_bits
FROM sjb_bloom_estimate(10000, 1000000, 1, 100, 1000, 100, 0, 0, 0, 100, 1048576, 2);

--
-- Part C: damaged filters and filters of another version or mode are
-- rejected with an error, never read with the wrong layout.
--

-- Step 17: a filter of version 2 (byte 0) ...
SELECT sjb_contains(set_byte(sjb_build(ARRAY[1, 2], 64, 3), 0, 2), 1);

-- ... a filter of mode 2 or mode 9 (byte 1) ...
SELECT sjb_contains(set_byte(sjb_build(ARRAY[1, 2], 64, 3), 1, 2), 1);
SELECT sjb_info(set_byte(sjb_build(ARRAY[1, 2], 64, 3), 1, 9));

-- ... a value shorter than the 24-byte header ...
SELECT sjb_contains('\x0102'::bytea, 1);

-- ... k = 0 (byte 2 is the low byte of k) ...
SELECT sjb_contains(set_byte(sjb_build(ARRAY[1, 2], 64, 3), 2, 0), 1);

-- ... and a payload that is one byte too long or too short.
SELECT sjb_contains(sjb_build(ARRAY[1, 2], 64, 3) || '\x00'::bytea, 1);
SELECT sjb_info(substr(sjb_build(ARRAY[1, 2], 64, 3), 1, 31));

--
-- Part D: the cost model report.
--

-- Step 18: n = 10000 keys, |S| = 1,000,000, one row per key, rows of
-- 32 bytes, 1000 Mbit/s, 150 ns per check, 0.06 ms per message, 0.5 us per
-- returned row, 0.25 us per key to build the filter, 100 rows per FETCH,
-- 1 MB filter limit, hex text (wire factor 2).
SELECT sjb_bloom_report(10000, 1e6, 1, 32, 1000, 150, 0.06, 0.5, 0.25, 100, 1048576, 2) AS report;

-- Step 19: the same n on a slow network (1 Mbit/s): the same filter (its
-- size depends only on bytes), but the network part of the time grows.
SELECT round(network_ms::numeric, 3) AS network_ms,
       round(total_ms::numeric, 3) AS total_ms
FROM sjb_bloom_estimate(10000, 1e6, 1, 32, 1, 150, 0.06, 0.5, 0.25, 100, 1048576, 2);

-- Step 20: the report says which rule applied (rules a, c and d).
SELECT sjb_bloom_report(0, 1e6, 1, 32, 1000, 150, 0.06, 0.5, 0.25, 100, 1048576, 2) AS report_rule_a;
SELECT sjb_bloom_report(10000, 10000, 1, 32, 1000, 150, 0.06, 0.5, 0.25, 100, 1048576, 2) AS report_rule_c;
SELECT sjb_bloom_report(200000, 1e6, 1, 32, 1000, 150, 0.06, 0.5, 0.25, 100, 65536, 2) AS report_rule_d;

-- Step 21: invalid arguments give clear errors.
SELECT sjb_build_auto(ARRAY[1, 2, 3], 1e6, 1, 30, 0, 100);
SELECT sjb_bloom_report(-1, 1e6, 1, 30, 1000, 100);
SELECT sjb_bloom_report(10000, 1e6, 1, 30, 1000, 100, 0.06, 0.5, 0.25, 0);
