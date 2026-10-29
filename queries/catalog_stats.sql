--
-- catalog_stats.sql
--
-- Reads the statistics of the foreign table customers from site_a's own
-- catalog and stores them in three psql variables, for the cost model:
--
--   :s_rows        rows of customers at site B
--   :rows_per_key  rows of customers per customer_id value
--   :row_width     bytes of one returned row ON THE WIRE
--
-- It does not contact site B: the numbers were copied into site_a's
-- catalog when setup ran "ANALYZE customers" on the foreign table.
--
-- Used by auto_manual.sql, bloom_cost_demo.sql and explain_demo.sql
-- (\ir catalog_stats.sql).
--

-- Step 1: compute the three numbers and store them with \gset.
-- (\gset runs the query and puts each output column into a psql variable
-- with the column's name.)
WITH table_rows AS (
    -- s_rows: the row count the planner uses for the foreign table.
    SELECT reltuples AS s_rows
    FROM pg_class
    WHERE oid = 'public.customers'::regclass
),
key_stats AS (
    -- How many distinct customer_id values there are. pg_stats.n_distinct
    -- is either a count (> 0), or MINUS a fraction of the rows (< 0):
    -- -1 means "every row has a different value".
    SELECT n_distinct
    FROM pg_stats
    WHERE schemaname = 'public' AND tablename = 'customers' AND attname = 'customer_id'
),
width_stats AS (
    -- The columns that come back from site B: customer_id (for the join)
    -- and name. avg_width is PostgreSQL's average stored width of each
    -- column. (It is close to, but not exactly, the text width: an int4 is
    -- stored in 4 bytes but its text has about 6 or 7 digits.)
    SELECT sum(avg_width) AS values_width,
           count(*) AS num_columns
    FROM pg_stats
    WHERE schemaname = 'public' AND tablename = 'customers'
      AND attname IN ('customer_id', 'name')
)
SELECT t.s_rows,
       -- rows per key = rows / distinct values
       t.s_rows / (CASE WHEN k.n_distinct < 0 THEN -k.n_distinct * t.s_rows
                        ELSE k.n_distinct END) AS rows_per_key,
       -- on the wire every row is a DataRow message: 7 bytes per row plus
       -- 4 bytes per column, plus the values themselves
       w.values_width + 7 + 4 * w.num_columns AS row_width
FROM table_rows t, key_stats k, width_stats w
\gset

-- Step 2: show them.
SELECT :s_rows AS s_rows, :rows_per_key AS rows_per_key, :row_width AS row_width;
