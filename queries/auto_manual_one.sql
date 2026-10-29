--
-- auto_manual_one.sql
--
-- For ONE value of :n, builds the adaptive filter with sjb_build_auto and
-- checks that the join with it returns exactly the baseline rows.
-- Prints: n, sjb_info of the filter, missing_rows and extra_rows.
-- missing_rows = (baseline EXCEPT ALL auto_join) and
-- extra_rows   = (auto_join EXCEPT ALL baseline); both must be 0.
--
-- Do not run it alone: auto_manual.sql sets :n, :s_rows, :rows_per_key,
-- :row_width, :network_mbit and :t_check_ns and then includes this file.
--

WITH auto_filter AS MATERIALIZED (
    -- Step 1: the filter, built once.
    SELECT sjb_build_auto(array_agg(customer_id),
                          :s_rows, :rows_per_key, :row_width,
                          :network_mbit, :t_check_ns) AS filter
    FROM orders
    WHERE order_id <= :n
),
baseline AS MATERIALIZED (
    -- Step 2: the plain join (site B sends all customers).
    SELECT o.order_id, c.name
    FROM orders o
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE o.order_id <= :n
),
auto_join AS MATERIALIZED (
    -- Step 3: the join with the filter. The scalar sub-SELECT is computed
    -- once and sent to site B as a parameter.
    SELECT o.order_id, c.name
    FROM orders o
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE o.order_id <= :n
      AND sjb_contains((SELECT filter FROM auto_filter), c.customer_id)
)
-- Step 4: the filter description and the two correctness counts.
SELECT :n AS n,
       (SELECT sjb_info(filter) FROM auto_filter) AS filter_info,
       (SELECT count(*) FROM (TABLE baseline EXCEPT ALL TABLE auto_join) AS missing) AS missing_rows,
       (SELECT count(*) FROM (TABLE auto_join EXCEPT ALL TABLE baseline) AS extra) AS extra_rows;
