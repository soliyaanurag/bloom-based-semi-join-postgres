--
-- create_sites.sql
--
-- Creates the two "sites" of the project as two databases on the same
-- server: site_a (has the local table orders) and site_b (has the remote
-- table customers). Any old copies are dropped first.
--
-- Run while connected to the "postgres" database:
--     psql -v ON_ERROR_STOP=1 -d postgres -f setup/create_sites.sql
--

-- Step 1: drop old copies. WITH (FORCE) disconnects anyone still
-- connected (for example an old postgres_fdw connection from site_a).
DROP DATABASE IF EXISTS site_a WITH (FORCE);
DROP DATABASE IF EXISTS site_b WITH (FORCE);

-- Step 2: create two empty databases.
CREATE DATABASE site_a;
CREATE DATABASE site_b;
