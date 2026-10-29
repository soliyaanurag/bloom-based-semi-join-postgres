--
-- edge_cases.sql: extra correctness checks written during the independent
-- verification (docs/verification_report.md, docs/manual_test_guide.md Part 7.2).
--
-- It compares the result with the rewrite off and on (EXCEPT ALL in both
-- directions, through temp tables) for the auto, index and filter paths on:
-- NULL / duplicate / missing / negative / int4-limit keys, an empty R', and
-- several query shapes. It also checks that unsafe queries are NOT rewritten,
-- that damaged filters give errors, and that Bloom has no false negatives.
--
-- Run in site_a:  psql -X -d site_a -f queries/edge_cases.sql
-- Pass: every "off=X on=X off-on=0 on-off=0" line, T11 counts are 0.
-- It creates the table edge_orders in site_a (dropped and recreated each run).
--
\pset pager off
\set ON_ERROR_STOP 0
SET postgres_fdw.semijoin_verbose = on;
-- E-1: edge data in a small local table
DROP TABLE IF EXISTS edge_orders;
CREATE TABLE edge_orders(order_id int primary key, customer_id int, note text);
INSERT INTO edge_orders VALUES
 (1, 5, 'a'), (2, 5, 'a'), (3, NULL, 'b'), (4, 999999, 'c'), (5, 1000000, 'c'),
 (6, -7, 'd'), (7, 2147483647, 'd'), (8, -2147483648, 'd'), (9, 0, 'e'), (10, 1, 'e');
ANALYZE edge_orders;
-- helper: compare on vs off via temp tables
CREATE OR REPLACE FUNCTION cmp(q text) RETURNS text LANGUAGE plpgsql AS $$
DECLARE a bigint; b bigint; n_off bigint; n_on bigint;
BEGIN
  EXECUTE 'SET postgres_fdw.enable_semijoin = off';
  EXECUTE 'DROP TABLE IF EXISTS t_off; CREATE TEMP TABLE t_off AS ' || q;
  EXECUTE 'SET postgres_fdw.enable_semijoin = on';
  EXECUTE 'DROP TABLE IF EXISTS t_on; CREATE TEMP TABLE t_on AS ' || q;
  SELECT count(*) INTO n_off FROM t_off; SELECT count(*) INTO n_on FROM t_on;
  SELECT count(*) INTO a FROM (SELECT * FROM t_off EXCEPT ALL SELECT * FROM t_on) x;
  SELECT count(*) INTO b FROM (SELECT * FROM t_on EXCEPT ALL SELECT * FROM t_off) x;
  RETURN format('off=%s on=%s off-on=%s on-off=%s', n_off, n_on, a, b);
END $$;
-- run each check for all three forced paths
CREATE OR REPLACE FUNCTION cmp3(q text) RETURNS TABLE(path text, result text) LANGUAGE plpgsql AS $$
DECLARE p text;
BEGIN
  FOREACH p IN ARRAY ARRAY['auto','index','filter'] LOOP
    EXECUTE format('SET postgres_fdw.semijoin_force_path = %L', p);
    path := p; result := cmp(q); RETURN NEXT;
  END LOOP;
  EXECUTE 'RESET postgres_fdw.semijoin_force_path';
END $$;
\echo == T1 NULL / duplicate / boundary / missing keys
SELECT * FROM cmp3('SELECT o.order_id, c.customer_id, c.name FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id');
\echo == T2 empty R prime
SELECT * FROM cmp3('SELECT o.order_id, c.name FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id > 100');
\echo == T3 only NULL keys
SELECT * FROM cmp3('SELECT o.order_id, c.name FROM edge_orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id = 3');
\echo == T4 condition on foreign table and text condition on local
SELECT * FROM cmp3($q$SELECT o.order_id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 5000 AND c.city = 'city_7'$q$);
\echo == T5 comma join syntax, reversed equality, no alias
SELECT * FROM cmp3('SELECT orders.order_id, customers.name FROM orders, customers WHERE orders.customer_id = customers.customer_id AND orders.order_id BETWEEN 100 AND 3000');
\echo == T6 DISTINCT + ORDER BY + LIMIT
SELECT * FROM cmp3('SELECT DISTINCT c.city FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 20000 ORDER BY 1 LIMIT 50');
\echo == T7 OR on local table only and IN list
SELECT * FROM cmp3('SELECT o.order_id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE (o.order_id < 200 OR o.order_id > 199800) AND o.order_id NOT IN (5,6,7)');
\echo == T8 aggregate over join
SELECT * FROM cmp3('SELECT count(*), sum(c.customer_id::bigint) FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 60000');
\echo == T9 not rewritten: LEFT JOIN, OR mixing tables, volatile, two join conditions
SET postgres_fdw.semijoin_force_path = 'index';
EXPLAIN (COSTS OFF, VERBOSE) SELECT o.order_id, c.name FROM orders o LEFT JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 10;
EXPLAIN (COSTS OFF, VERBOSE) SELECT o.order_id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 10 OR c.city = 'x';
EXPLAIN (COSTS OFF, VERBOSE) SELECT o.order_id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 10 + (random()*0)::int;
EXPLAIN (COSTS OFF, VERBOSE) SELECT o.order_id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id AND c.customer_id = o.order_id;
PREPARE p1(int) AS SELECT o.order_id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= $1;
EXPLAIN (COSTS OFF, VERBOSE) EXECUTE p1(10);
RESET postgres_fdw.semijoin_force_path;
\echo == T10 filter errors: wrong version, garbage, NULL
SELECT sjb_contains(set_byte(sjb_build(ARRAY[1,2], 64, 3), 0, 2), 1);
SELECT sjb_contains('\x0102'::bytea, 1);
SELECT sjb_contains(set_byte(sjb_build(ARRAY[1], 64, 3), 1, 9), 1);
SELECT sjb_contains(NULL, 1) IS NULL AS null_filter, sjb_contains(sjb_build(ARRAY[1],64,3), NULL) IS NULL AS null_key;
\echo == T11 no false negatives: all 100000 random keys pass their own Bloom filter (tiny m and also sjb_build_auto)
CREATE TEMP TABLE rk AS SELECT DISTINCT (random()*4e9 - 2e9)::int AS k FROM generate_series(1,100000);
WITH f AS MATERIALIZED (SELECT sjb_build(array_agg(k), 50000, 3) AS f1,
                               sjb_build_auto(array_agg(k), 1e6, 1, 30, 1000, 158) AS f2 FROM rk)
SELECT count(*) AS keys, count(*) FILTER (WHERE NOT sjb_contains(f1, k)) AS fn_bloom_small,
       count(*) FILTER (WHERE NOT sjb_contains(f2, k)) AS fn_auto, (SELECT sjb_info(f2) FROM f) AS auto_filter
FROM rk, f;
