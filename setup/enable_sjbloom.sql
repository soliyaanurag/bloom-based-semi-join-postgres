--
-- enable_sjbloom.sql
--
-- Lets postgres_fdw send our sjbloom functions (sjb_contains) to site B.
--
-- Run inside site_a (setup_all.sh runs it for you at the end):
--     psql -v ON_ERROR_STOP=1 -d site_a -f setup/enable_sjbloom.sql
--
-- Run it only once after setup_all.sh: running it a second time fails with
-- 'option "extensions" provided more than once', because the option is
-- already there.
--

-- Step 1: make sure sjbloom exists in site_b. The remote SQL that site A
-- sends will call public.sjb_contains(...) at site B, so the function must
-- exist there too. \connect switches this psql session to site_b (same
-- host, port and user). IF NOT EXISTS: do nothing if it is already there.
\connect site_b
CREATE EXTENSION IF NOT EXISTS sjbloom;

-- Step 2: back to site_a, and make sure sjbloom exists here as well
-- (site A calls sjb_build to build the filter).
\connect site_a
CREATE EXTENSION IF NOT EXISTS sjbloom;

-- Step 3: tell postgres_fdw that site B has the sjbloom extension too.
--
-- Why: postgres_fdw only sends a WHERE condition to the remote server if
-- it is sure the remote server can run it. For built-in functions it
-- knows this. For functions of an extension it only trusts the extensions
-- listed in the server option "extensions".
--
-- Without this option, postgres_fdw will NOT send sjb_contains to site B.
-- It would fetch EVERY row of customers and run sjb_contains at site A
-- (shown as "Filter: sjb_contains(...)" above the Foreign Scan). The result
-- is still correct, but nothing is saved on the network.
ALTER SERVER site_b_srv OPTIONS (ADD extensions 'sjbloom');

-- Step 4: show the server options, so we can see the new option.
SELECT srvname, srvoptions
FROM pg_foreign_server
WHERE srvname = 'site_b_srv';
