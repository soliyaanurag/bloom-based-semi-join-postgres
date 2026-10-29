--
-- correctness_bloom.sql
--
-- Checks that the Bloom query (bloom_manual.sql) returns exactly the same
-- rows as the baseline query (baseline.sql), duplicates included, for
-- n = 100, 1000 and 10000. Every count printed must be 0.
--
-- The check itself is in correctness_bloom_one.sql; this file only runs it
-- once for each n. (\ir = include a file from the same folder.)
--
-- Run in site_a:  psql -d site_a -f queries/correctness_bloom.sql
--                 psql -d site_a -v b=4 -f queries/correctness_bloom.sql
--

-- Step 1: b = filter bits per distinct key. Default 10.
\if :{?b}
\else
    \set b 10
\endif

-- Step 2: n = 100.
\set n 100
\ir correctness_bloom_one.sql

-- Step 3: n = 1000.
\set n 1000
\ir correctness_bloom_one.sql

-- Step 4: n = 10000.
\set n 10000
\ir correctness_bloom_one.sql
