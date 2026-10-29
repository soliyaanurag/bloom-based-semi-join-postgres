--
-- site_b_data.sql
--
-- Creates and fills the table S = customers at site B.
-- Site A will later see this table as a postgres_fdw foreign table.
--
-- Run inside site_b:
--     psql -v ON_ERROR_STOP=1 -d site_b -f setup/site_b_data.sql
--

-- Step 1: the customers table. "padding" makes each row about 100 bytes
-- wide, so sending rows over the network has a real cost.
CREATE TABLE customers (
    customer_id int PRIMARY KEY,
    name        text,
    city        text,
    padding     text
);

-- Step 2: fix the random seed so every run produces the same data.
SELECT setseed(0.42);

-- Step 3: insert 1,000,000 customers with ids 1..1000000.
INSERT INTO customers (customer_id, name, city, padding)
SELECT g,
       'cust_' || g,
       'city_' || (g % 100),
       repeat('x', 80)
FROM generate_series(1, 1000000) AS g;

-- Step 4: collect statistics so the planner knows the table size.
ANALYZE customers;
