--
-- site_a_setup.sql
--
-- Sets up site A: creates and fills the local table R = orders, and makes
-- the remote table customers (in site_b) visible here through postgres_fdw.
--
-- Run inside site_a, passing the password as a psql variable (it is never
-- written into a file):
--     psql -v ON_ERROR_STOP=1 -v pgpass="$PGPASSWORD" -d site_a -f setup/site_a_setup.sql
--

-- Step 1: the local orders table.
CREATE TABLE orders (
    order_id    int PRIMARY KEY,
    customer_id int,
    amount      numeric
);

-- Step 2: fix the random seed so every run produces the same orders.
SELECT setseed(0.42);

-- Step 3: insert 200,000 orders. Each order belongs to a random customer
-- between 1 and 1000000, so only a small part of customers is ever needed.
INSERT INTO orders (order_id, customer_id, amount)
SELECT g,
       (random() * 999999)::int + 1,
       round((random() * 1000)::numeric, 2)
FROM generate_series(1, 200000) AS g;

-- Step 4: the postgres_fdw EXTENSION. This installs the "foreign data
-- wrapper": the code that knows how to read tables living in another
-- PostgreSQL server, by sending it SQL over a normal client connection.
CREATE EXTENSION postgres_fdw;

-- Step 5: the foreign SERVER. It records WHERE the other site is: host,
-- port and database name. postgres_fdw connects there over TCP
-- (localhost), so data really travels through a network connection.
CREATE SERVER site_b_srv
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host 'localhost', port '5432', dbname 'site_b');

-- Step 6: the USER MAPPING. It records WHO to log in as at site B when a
-- given local user (here: whoever runs this script) uses site_b_srv.
-- The password comes from the psql variable pgpass, never from this file.
CREATE USER MAPPING FOR CURRENT_USER
    SERVER site_b_srv
    OPTIONS (user 'postgres', password :'pgpass');

-- Step 7: the FOREIGN TABLE. IMPORT FOREIGN SCHEMA reads the definition of
-- customers from site B and creates a local foreign table "customers"
-- with the same columns. It holds no data: every query on it is turned
-- into SQL that runs at site B.
IMPORT FOREIGN SCHEMA public LIMIT TO (customers)
    FROM SERVER site_b_srv
    INTO public;

-- Step 8: statistics. ANALYZE orders reads the local table. ANALYZE
-- customers makes site A fetch a sample of rows from site B, so the
-- planner at A knows how big the remote table is.
ANALYZE orders;
ANALYZE customers;
