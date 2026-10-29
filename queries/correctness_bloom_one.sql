--
-- correctness_bloom_one.sql
--
-- One correctness check of the Bloom query for one value of n and b.
-- Called by correctness_bloom.sql, which sets n and b first.
--
-- It prints one row:
--   baseline_except_bloom  rows of baseline missing from the Bloom query
--   bloom_except_baseline  rows of the Bloom query not in baseline
-- EXCEPT ALL keeps duplicates, so both 0 means the two queries return
-- exactly the same rows, the same number of times.
--

-- Step 1: the two EXCEPT ALL counts. The Bloom query is the query of
-- bloom_manual.sql, without EXPLAIN.
SELECT :n AS n,
       :b AS bits_per_key,
       (SELECT count(*)
        FROM (
            SELECT o.order_id, c.name
            FROM orders o
            JOIN customers c ON c.customer_id = o.customer_id
            WHERE o.order_id <= :n
          EXCEPT ALL
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
                    c.customer_id)
        ) AS missing_rows) AS baseline_except_bloom,
       (SELECT count(*)
        FROM (
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
                    c.customer_id)
          EXCEPT ALL
            SELECT o.order_id, c.name
            FROM orders o
            JOIN customers c ON c.customer_id = o.customer_id
            WHERE o.order_id <= :n
        ) AS extra_rows) AS bloom_except_baseline;
