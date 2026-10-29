--
-- byte_estimate.sql
--
-- Estimates how many bytes the Bloom query (bloom_manual.sql) moves over
-- the network, for one value of n and b:
--   bytes A->B  the filter, sent as hex text (octet_length(filter::text))
--   bytes B->A  (rows site B sends back) * (wire width of a row)
-- and compares it with the baseline, which fetches every customer.
--
-- Remember: postgres_fdw sends parameters and rows as TEXT, so we measure
-- text sizes, not the binary sizes. Every returned row is a DataRow
-- message: 7 bytes per row + 4 bytes per column + the values as text, so
-- the wire width of a row is (average text width) + 7 + 4 * 2 for our two
-- columns. This is the same row_width the cost model and sjb_build_auto
-- use. (TCP/IP headers and the other protocol messages are not counted.)
--
-- Run in site_a:  psql -d site_a -v n=10000 -v b=10 -f queries/byte_estimate.sql
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

-- Step 3: compute everything in one query, one CTE per idea.
-- "AS MATERIALIZED" makes each CTE run once.
WITH
-- 3a: the same filter as in bloom_manual.sql.
bloom AS MATERIALIZED (
    SELECT sjb_build(array_agg(DISTINCT customer_id),
                     greatest(8, ceil(count(DISTINCT customer_id) * :b))::int8,
                     greatest(1, round(0.693 * :b))::int4) AS filter,
           count(DISTINCT customer_id) AS distinct_keys
    FROM orders
    WHERE order_id <= :n
),
-- 3b: average wire width of one returned row (customer_id and name are
-- the only columns the join needs, so the only ones site B sends): their
-- text + 7 + 4 * 2 bytes of DataRow overhead. postgres_fdw runs this
-- aggregate at site B and returns one number.
row_width AS MATERIALIZED (
    SELECT avg(octet_length(customer_id::text) + octet_length(name::text)) + 7 + 4 * 2 AS avg_width,
           count(*) AS all_rows
    FROM customers
),
-- 3c: how many rows site B sends back for this filter. This is the same
-- condition as the Foreign Scan in bloom_manual.sql, so the number equals
-- its "actual rows". The filter goes to site B as a parameter ($1).
remote AS MATERIALIZED (
    SELECT count(*) AS remote_rows
    FROM customers
    WHERE sjb_contains((SELECT filter FROM bloom), customer_id)
)
-- 3d: put the numbers together. false_positives = remote_rows minus the
-- distinct keys, because every customer_id in orders exists in customers
-- (ids 1..1000000), so each distinct key matches exactly one customer.
SELECT :n AS n,
       :b AS bits_per_key,
       bloom.distinct_keys,
       octet_length(bloom.filter) AS filter_bytes_binary,
       octet_length(bloom.filter::text) AS bytes_a_to_b,
       round(row_width.avg_width, 2) AS avg_row_width,
       remote.remote_rows,
       remote.remote_rows - bloom.distinct_keys AS false_positives,
       round(remote.remote_rows * row_width.avg_width) AS bytes_b_to_a,
       round(octet_length(bloom.filter::text)
             + remote.remote_rows * row_width.avg_width) AS total_bytes,
       round(row_width.all_rows * row_width.avg_width) AS baseline_bytes_b_to_a
FROM bloom, row_width, remote;
