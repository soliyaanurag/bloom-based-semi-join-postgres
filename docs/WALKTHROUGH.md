<!-- Step-by-step walkthrough of each batch: what was built, file by file, and how to test it. -->

# Project walkthrough

## Batch 1: the two sites, the data, and the baseline queries

In this batch we built the test environment and measured what postgres_fdw does **without** our extension. No C code yet.

### The setup

```
        site_a (database)                               site_b (database)
 ┌──────────────────────────────┐                ┌──────────────────────────────┐
 │ orders      200,000 rows     │                │ customers   1,000,000 rows   │
 │ (local table R)              │   postgres_fdw │ (table S, ~100 bytes / row)  │
 │                              │ ─────────────► │                              │
 │ customers   foreign table    │  TCP localhost │                              │
 │ (no data, just a "window")   │ ◄───────────── │                              │
 └──────────────────────────────┘     rows       └──────────────────────────────┘
```

Both "sites" are databases on the same PostgreSQL server. postgres_fdw still connects from site_a to site_b over a real TCP connection (localhost:5432), so every row of customers that site A reads really travels through a network socket.

### Files

**`setup/env.example.sh`** sets `PGHOST=localhost`, `PGPORT=5432`, `PGUSER=postgres`. Load it with `source setup/env.example.sh`. Passwords never go in this file. Put yours in `~/.pgpass` (`chmod 600`), and give it to `setup_all.sh` once through `PGPASSWORD` (see the comments in the file). On our machine the server uses `trust` authentication, so no password is needed.

**`setup/create_sites.sql`** drops site_a and site_b if they exist (`WITH (FORCE)` kicks out old connections), then creates them empty.

**`setup/site_b_data.sql`** creates `customers(customer_id, name, city, padding)` in site_b with 1,000,000 rows. The `padding` column (80 × `'x'`) makes each row about 100 bytes, so transferring rows has a real cost.

**`setup/site_a_setup.sql`** runs in site_a:
- **orders**: 200,000 orders, each pointing at a random customer between 1 and 1,000,000. `setseed(0.42)` makes the "random" data the same on every run.
- **`CREATE EXTENSION postgres_fdw`** installs the *foreign data wrapper*: code that reads tables on another PostgreSQL server by sending it SQL.
- **`CREATE SERVER site_b_srv`** records *where* the other site is (host, port, database).
- **`CREATE USER MAPPING`** records *who* to log in as at site B. The password comes in as the psql variable `pgpass`, never from the file.
- **`IMPORT FOREIGN SCHEMA ... LIMIT TO (customers)`** creates a local *foreign table* `customers` with the same columns as the remote one. It stores no data: every query on it becomes SQL sent to site B.
- **`ANALYZE orders; ANALYZE customers;`** collects statistics. For the foreign table, site A fetches a sample of rows from site B, so its planner knows the remote table has about 1,000,000 rows.

**`setup/setup_all.sh`** runs the three SQL files in order. `set -e` stops the script at the first failing command, and `psql -v ON_ERROR_STOP=1` stops each SQL file at its first error. **Warning:** it drops and recreates both databases.

**`queries/baseline.sql`** is the plain join `orders ⋈ customers` for `order_id <= n`, under `EXPLAIN (ANALYZE, VERBOSE)`. `n` is a psql variable (default 1000; change it with `psql -v n=5000 ...`).

**`queries/exact_keys.sql`** is the same join, plus a hand-written semi-join: `c.customer_id = ANY ((SELECT array_agg(DISTINCT customer_id) FROM orders WHERE order_id <= n)::int[])`. Site A computes the list of needed customer ids once and sends it to site B. The `::int[]` cast is needed: without it, PostgreSQL reads `= ANY ((SELECT ...))` as "compare with each *row* of a subquery". It then fails with `operator does not exist: integer = integer[]`.

**`queries/param_nestloop.sql`** turns on the server option `use_remote_estimate` (postgres_fdw then asks site B for cost estimates), runs the baseline query, and turns it off again. Everything is inside `BEGIN ... COMMIT`, so if something fails the option is rolled back and never left on.

**`queries/correctness_exact.sql`** prints `(baseline EXCEPT ALL exact_keys)` and `(exact_keys EXCEPT ALL baseline)`. `EXCEPT ALL` keeps duplicates, so if both counts are 0 the two queries return exactly the same rows, the same number of times. This is the correctness test we will use for every strategy.

### How to read the EXPLAIN output

`EXPLAIN (ANALYZE, VERBOSE)` runs the query and prints the plan as a tree. Each `->` is a child node that feeds rows to the node above it. Every node shows two sets of numbers:

```
Foreign Scan on public.customers c  (cost=100.00..237257.00 rows=1000000 width=15) (actual time=1.064..1206.720 rows=1000000.00 loops=1)
                                     └──────── planner's GUESS ───────────────┘  └──────────── what REALLY happened ─────────────────┘
```

- **cost / rows / width** (first parentheses): the planner's estimate before running.
- **actual time / rows / loops** (second parentheses): measured. `actual rows` is the number of rows the node produced *per loop*, and `loops` is how many times the node ran. Total rows = rows × loops.
- **Foreign Scan**: the node that reads a foreign table. Its rows come from site B over the network. **This is the number our project wants to make small.**
- **Remote SQL:** (only with `VERBOSE`): the exact SQL text site A sent to site B. If a condition appears in its `WHERE`, site B filters the rows. If not, site B sends everything and site A filters.
- **InitPlan**: a sub-query that does not depend on the outer rows, so it is computed **once**, before the main plan starts. Its result is passed to the rest of the plan as a parameter (`$1`, ...). postgres_fdw can ship such a parameter to site B.
- **Output:** the columns a node produces. **Join Filter / Rows Removed by Join Filter**: a join condition checked row by row, and how many row pairs it threw away.

### Results on our machine (n = 1000)

**1. Baseline: all of customers is fetched.**
```
Hash Join  (...) (actual time=3.244..1314.900 rows=1000.00 loops=1)
  ->  Foreign Scan on public.customers c  (...) (actual time=1.064..1206.720 rows=1000000.00 loops=1)
        Remote SQL: SELECT customer_id, name FROM public.customers
Execution Time: 1318.242 ms
```
The Remote SQL has **no WHERE clause**, so site B sends all **1,000,000** rows to answer a query with 1000 result rows. postgres_fdw cannot push the join condition down, because the other side of the join (orders) lives at site A.

**2. Exact keys: only the needed customers are fetched.**
```
Nested Loop  (...) (actual time=10.692..125.596 rows=1000.00 loops=1)
  InitPlan 1
    ->  Aggregate  (...) (actual ... rows=1.00 loops=1)
          Output: array_agg(DISTINCT orders.customer_id)
  ...
  ->  Foreign Scan on public.customers c  (...) (actual time=4.579..11.715 rows=999.00 loops=1)
        Remote SQL: SELECT customer_id, name FROM public.customers WHERE ((customer_id = ANY ($1::integer[])))
Execution Time: 128.828 ms
```
`InitPlan 1` computes the array of distinct customer ids once. It goes to site B as `$1`, and B returns only **999** rows (1000 orders reference 999 distinct customers), 10× faster. One weakness is visible: the planner guessed only `rows=10` from the foreign scan, so it picked a simple nested loop that compares 1000 × 999 row pairs (`Rows Removed by Join Filter: 998000`). It is still fast here, but a cost model with good estimates matters. Sending a full key list also gets expensive when there are many keys, which is why we will add Bloom filters.

**3. use_remote_estimate: a parameterized nested loop.**
```
Nested Loop  (...) (actual time=0.311..223.948 rows=1000.00 loops=1)
  ->  Index Scan using orders_pkey on public.orders o  (...) (actual ... rows=1000.00 loops=1)
  ->  Memoize  (...) (actual ... rows=1.00 loops=1000)
        Cache Key: o.customer_id
        Hits: 1  Misses: 999  ...
        ->  Foreign Scan on public.customers c  (...) (actual ... rows=1.00 loops=999)
              Remote SQL: SELECT customer_id, name FROM public.customers WHERE ((customer_id = $1::integer))
Execution Time: 224.365 ms
```
In simple words: with real estimates from site B, the planner decided to walk through the 1000 orders and, **for each order, send one small query to site B** asking for that single customer (`customer_id = $1`). `Memoize` remembers answers it already has: 1 cache hit, 999 misses, so **999 separate remote queries**, each returning 1 row. This moves very little data, but every query is a network round trip. That is fine for 1000 keys and terrible for 100,000. Our filter approach sends **one** query, whatever the number of keys.

**4. Correctness:** `baseline_except_exact = 0` and `exact_except_baseline = 0`.

### How to test it

```bash
source setup/env.example.sh
setup/setup_all.sh                                   # should end with "=== Setup finished ==="
psql -d site_a -f queries/baseline.sql               # Remote SQL without WHERE, ~1,000,000 rows
psql -d site_a -f queries/exact_keys.sql             # Remote SQL with = ANY ($1...), ~1,000 rows
psql -d site_a -f queries/param_nestloop.sql         # plan with use_remote_estimate
psql -d site_a -f queries/correctness_exact.sql      # both counts must be 0
psql -d site_a -v n=10000 -f queries/baseline.sql    # try other sizes with -v n=...
```

## Batch 2: the sjbloom extension with a Bloom filter

In this batch we built a PostgreSQL extension in C called `sjbloom`. It adds three SQL functions:

| Function | Runs at | What it does |
|---|---|---|
| `sjb_build(keys int4[], m int8, k int4) → bytea` | site A | Builds a Bloom filter with `m` bits from a list of join keys. |
| `sjb_contains(filter bytea, key int4) → bool` | site B | Says whether a key passes the filter. |
| `sjb_info(filter bytea) → text` | anywhere | Describes a filter in one line, for debugging. |

Only the BLOOM mode works so far. EXACT and PASS come in Batch 5; until then `sjb_contains` gives the error "filter mode ... is not supported yet" for them.

### What is a Bloom filter?

A Bloom filter is a row of `m` bits, all 0 at the start.

- **Add a key:** compute `k` bit positions from the key and set those bits to 1.
- **Test a key:** compute the same `k` positions. If **any** of them is 0, the key was certainly never added. If **all** are 1, the key was probably added.

"Probably", because other keys may have set those same bits by chance. That is a **false positive**. A Bloom filter **never** gives a false negative: a key that was added always passes. For our join this is exactly what we need. Site B may send back a few extra rows of S, but never forgets a row that matches. The normal join at site A then throws the extra rows away.

### How one key becomes k bit positions

```
 key (int4), seed
        │
        ▼
 hashint4extended(key, seed)       PostgreSQL's own int4 hash, 64 bits
        │
        ▼
 ┌──────────── 64-bit hash ────────────┐
 │   high 32 bits    │   low 32 bits   │
 └─────────┬─────────┴────────┬────────┘
           ▼                  ▼
   h2 = high | 1          h1 = low       (h2 is made odd, so it is never 0)

 position_i = (h1 + i * h2) mod m        for i = 0, 1, ..., k-1
```

This trick is called **double hashing**: one hash call gives all `k` positions. Bit position `p` is stored in byte `p / 8`, at bit `p % 8` inside that byte.

Example with m = 64 and k = 3: if h1 = 10 and h2 = 7, the positions are 10, 17, 24. So bit 2 of byte 1, bit 1 of byte 2 and bit 0 of byte 3 are set.

We always use PostgreSQL's `hashint4extended` (never our own hash of raw bytes), so site A and site B compute exactly the same positions.

### Filter byte layout

A filter is a `bytea`. PostgreSQL puts its own length header in front, then come our 24-byte header and the payload:

```
 ┌─────────┬──────────────────────────────── SjbHeader (24 bytes) ───────────────────────────────┬────────────────────┐
 │ varlena │ version │ mode │    k    │  nkeys  │          m          │         seed          │  Bloom bits        │
 │ length  │ uint8   │ uint8│ uint16  │ uint32  │       uint64        │        uint64         │  ceil(m/8) bytes   │
 └─────────┴─────────┴──────┴─────────┴─────────┴─────────────────────┴───────────────────────┴────────────────────┘
  (PostgreSQL) offset 0  1      2         4         8                     16                     24
```

- `version` = 1, `mode` = 1 (BLOOM), `k` = positions per key, `nkeys` = how many (non-NULL) keys were added, `m` = number of bits, `seed` = hash seed (0x5EED5EED).
- The fields are ordered from small to large so the compiler adds no padding. The header is exactly 24 bytes, and a `StaticAssertDecl` makes the build fail if it ever is not.
- We always copy the header in and out with `memcpy`. Bytes inside a bytea may sit at any address, and reading a `uint64` from an unaligned address by casting a pointer can crash on some machines.

Example: `sjb_info(sjb_build(ARRAY[1,2,3], 64, 3))` gives
`mode=bloom nkeys=3 m=64 k=3 bits_set=12.50% bytes=32`. That is 24 header bytes + 8 bytes of bits; 3 keys × 3 positions = 9 positions, of which 8 distinct bits ended up set (12.5% of 64).

### Files

**`sjbloom/Makefile`** uses PGXS, PostgreSQL's build system for extensions. `pg_config` (from our install's `bin/`, which is on PATH) tells it where the headers are. `MODULE_big` + `OBJS` say "link sjbloom.o and bloom.o into sjbloom.so". `DATA` is the SQL install script, and `REGRESS` names the regression test.

**`sjbloom/sjbloom.control`** tells `CREATE EXTENSION` the default version (1.0) and where the library is (`$libdir/sjbloom`).

**`sjbloom/sjb_filter.h`** is shared by both C files. It holds the `SjbHeader` struct, the mode numbers, the seed, the allowed ranges for m (8 bits to 64 MB) and k (1 to 32), and the prototypes of the two functions in `bloom.c`.

**`sjbloom/bloom.c`** holds the pure Bloom filter logic, working on a plain array of bytes. It knows nothing about SQL.
- `compute_two_hashes` hashes a key and splits the result into h1 and h2.
- `bit_position` computes `(h1 + i*h2) mod m` in 64-bit arithmetic, so it cannot overflow.
- `set_bit` / `bit_is_set` implement the "byte p/8, bit p%8" rule.
- `sjb_bloom_add` sets the k bits of one key.
- `sjb_bloom_test` checks the k bits of one key and returns false at the first 0 bit.

**`sjbloom/sjbloom.c`** holds the three SQL functions, plus helpers that check inputs and filter headers:
- `sjb_build` checks m and k, unpacks the array, allocates a zero-filled bytea of the right size, adds every non-NULL key, then writes the header.
- `sjb_contains` copies the header out, checks the version, mode and payload size, then calls `sjb_bloom_test`. A damaged filter gives an error instead of reading past the end of the data.
- `sjb_info` does the same checks, counts the 1 bits with a simple double loop, and prints one line.

**`sjbloom/sjbloom--1.0.sql`** is run by `CREATE EXTENSION`. It declares the three functions. They are `IMMUTABLE` because postgres_fdw only ships a WHERE condition to the remote server if all its functions are immutable. Without this, site B would send all of S and the filter would be useless. They are `STRICT` so a NULL key automatically gives NULL, which drops the row, just like a NULL never matches in an inner join.

**`sjbloom/sql/sjbloom_basic.sql`** and **`sjbloom/expected/sjbloom_basic.out`** are the regression test and its expected output. It checks:
- no false negatives for 10000 keys;
- the false-positive rate is between 0.004 and 0.015 (theory: about 0.0082);
- an empty array gives nkeys = 0 and passes nothing;
- NULL array elements are skipped;
- a NULL key gives NULL;
- m = 8 works and m = 4 is an error.

**`queries/fp_rate.sql`** compares the measured false-positive rate with the theoretical rate `(1 - e^(-kn/m))^k` for 4, 8, 12, 16 and 22 bits per key, with `k = round(0.693 × b)`. It tests 10000 keys in the filter against 1,000,000 keys that are not in it.

A note on `WITH ... AS MATERIALIZED` in the SQL files: it forces PostgreSQL to build the filter once. Without it, the planner may copy the `sjb_build(...)` call into the WHERE clause and rebuild the filter for every row tested.

### PostgreSQL macros and functions we used

| Name | What it does |
|---|---|
| `PG_MODULE_MAGIC` | Puts a "magic block" in the .so so the server refuses a library built for a different major version. |
| `PG_FUNCTION_INFO_V1(name)` | Declares that C function `name` uses the standard calling convention for SQL functions. |
| `PG_FUNCTION_ARGS` | The standard parameter list of such a function (one pointer to the call info). |
| `PG_GETARG_INT32(n)`, `PG_GETARG_INT64(n)` | Read SQL argument number n as a C int32 / int64. |
| `PG_GETARG_ARRAYTYPE_P(n)` | Read argument n as an SQL array. |
| `PG_GETARG_BYTEA_PP(n)` | Read argument n as a bytea, decompressing / fetching it from TOAST if needed. It may keep a short 1-byte length header. |
| `deconstruct_array_builtin` | Unpack an array of a built-in type (here `INT4OID`) into a C array of Datums and a C array of null flags. |
| `Datum`, `Int32GetDatum`, `Int64GetDatum`, `DatumGetInt32`, `DatumGetUInt64` | A Datum is PostgreSQL's "any value" type; these macros wrap C values into Datums and unwrap them again. |
| `DirectFunctionCall2(f, a, b)` | Call an SQL-callable C function (here `hashint4extended`) directly from C with two arguments. |
| `palloc0(size)` | Allocate zero-filled memory in the current memory context. It is freed automatically at the end of the query. |
| `VARHDRSZ` | Size (4 bytes) of the length header in front of every bytea/text value. |
| `SET_VARSIZE(ptr, size)` | Write the total size into that length header. |
| `VARDATA(ptr)` | Pointer to the data just after a 4-byte length header (used on our own freshly built bytea). |
| `VARDATA_ANY(ptr)`, `VARSIZE_ANY_EXHDR(ptr)` | Pointer to the data and size of the data for a bytea with either a 1-byte or 4-byte length header. |
| `ereport(ERROR, (errcode(...), errmsg(...)))` | Raise an SQL error: stops the function and aborts the query. |
| `StaticAssertDecl(cond, msg)` | Check a condition at compile time. |
| `UINT64CONST(x)` | Write a 64-bit unsigned constant portably. |
| `psprintf` | Like sprintf, but returns a new palloc'd string. |
| `cstring_to_text` | Turn a C string into an SQL `text` value. |
| `PG_RETURN_BYTEA_P`, `PG_RETURN_BOOL`, `PG_RETURN_TEXT_P` | Return a value from the C function to SQL. |

### How to test it

```bash
cd sjbloom
make                 # compile (should print no warnings)
make install         # installs into ~/postgres/postgresql/install, no sudo needed
make installcheck    # runs sql/sjbloom_basic.sql, compares with expected/
```

`make installcheck` should end with `# All 1 tests passed.` If it fails, look at `sjbloom/regression.diffs`.

Then load the extension into both sites and run the false-positive experiment:

```bash
psql -d site_a -c "CREATE EXTENSION sjbloom;" -c "SELECT sjb_info(sjb_build(ARRAY[1,2,3], 64, 3));"
psql -d site_b -c "CREATE EXTENSION sjbloom;" -c "SELECT sjb_info(sjb_build(ARRAY[1,2,3], 64, 3));"
psql -d site_a -f queries/fp_rate.sql
```

Both sites should print `mode=bloom nkeys=3 m=64 k=3 bits_set=12.50% bytes=32`.

**Note:** `setup/setup_all.sh` drops and recreates both databases. Since Batch 3 it also creates sjbloom in both sites for you.

Results in site_a (the measured rates match the theory closely):

```
 bits_per_key |   m    | k  | measured_fp | predicted_fp
--------------+--------+----+-------------+--------------
            4 |  40000 |  3 |    0.145977 |     0.146892
            8 |  80000 |  6 |    0.021581 |     0.021577
           12 | 120000 |  8 |    0.003116 |     0.003142
           16 | 160000 | 11 |    0.000431 |     0.000459
           22 | 220000 | 15 |    0.000024 |     0.000026
```

Each extra 4 bits per key cuts the false-positive rate by roughly a factor of 7 to 10. This is the trade-off the cost model will use later: a bigger filter costs more bytes to send, but lets fewer useless rows of S come back.

After changing `sjbloom--1.0.sql`, reinstall and recreate the extension: `make install`, then `DROP EXTENSION sjbloom CASCADE; CREATE EXTENSION sjbloom;` in each database.

## Batch 3: the Bloom semi-join by hand, and cost model version 1

In this batch the Bloom filter crosses the network for the first time. We write the semi-join by hand in SQL (no changes to postgres_fdw yet), check that the result is exactly right, measure the bytes, and add a formula that picks the best filter size.

### Files

**`setup/enable_sjbloom.sql`** (run in site_a) makes sure sjbloom exists in site_b and site_a (`CREATE EXTENSION IF NOT EXISTS`, using `\connect` to switch databases), then runs:

```sql
ALTER SERVER site_b_srv OPTIONS (ADD extensions 'sjbloom');
```

postgres_fdw only sends a WHERE condition to site B if it is sure site B can run it. It trusts built-in functions, but for extension functions it only trusts the extensions named in the server option `extensions`. **Without this option**, sjb_contains stays at site A: B sends all 1,000,000 customers and A filters them (`Filter: sjb_contains(...)` above the Foreign Scan). The result would still be correct, but nothing would be saved on the network. Run the file only once: a second `ADD` fails because the option already exists.

**`setup/setup_all.sh`** now has 5 steps. After the 3 old ones it runs `CREATE EXTENSION sjbloom` in site_b and in site_a, then `enable_sjbloom.sql`. Before the very first run, install the extension once with `make -C sjbloom install`. During step 5 psql prints two `extension "sjbloom" already exists, skipping` notices. That is expected: step 4 already created it.

**`queries/bloom_manual.sql`** is the join with a Bloom semi-join, under `EXPLAIN (ANALYZE, VERBOSE)`. psql variables: `n` (orders kept, default 1000) and `b` (filter bits per key, default 10). The filter is built from the distinct customer_ids of the kept orders, with `m = max(8, b × keys)` bits and `k = max(1, round(0.693 × b))` positions per key. That k gives the lowest false-positive rate for b bits per key.

**`queries/correctness_bloom.sql`** runs **`queries/correctness_bloom_one.sql`** (with psql's `\ir`, "include a file from this folder") for n = 100, 1000 and 10000. Each run prints `baseline_except_bloom` and `bloom_except_baseline`, the two EXCEPT ALL counts. Both must be 0. You can change b with `-v b=4`.

**`queries/byte_estimate.sql`** estimates the network bytes for one n and b:
- `bytes_a_to_b` = `octet_length(filter::text)`: the filter as hex text, which is how postgres_fdw sends it (2 × binary size + 2 bytes for `\x`).
- `avg_row_width` = average of `octet_length(customer_id::text) + octet_length(name)` over all customers. Only these two columns travel, because the query only needs them. postgres_fdw runs this `avg(...)` at site B and gets back one number.
- `remote_rows` = how many customers pass the filter. We count them with the same pushed-down `sjb_contains` condition, so it equals the Foreign Scan's "actual rows" in `bloom_manual.sql` without copying numbers by hand.
- `bytes_b_to_a` = `remote_rows × avg_row_width`, and `baseline_bytes_b_to_a` = all 1,000,000 rows × width, for comparison.

Small protocol overheads (message headers, a few bytes per row) are ignored.

**`sjbloom/sjbloom--1.0.sql`** has a new SQL function **`sjb_best_bits(n, s_rows, matches, row_width, wire_factor DEFAULT 2)`**: cost model version 1 (see below). It is `LANGUAGE sql IMMUTABLE`, so no C code was needed. After `make install` the extension was recreated in both sites (here by rerunning `setup_all.sh`).

### Data flow of the manual Bloom query

```
 SITE A (site_a)                                          SITE B (site_b)
 ───────────────                                          ───────────────
 1. InitPlan 1 (runs once, before the join):
    SELECT sjb_build(array_agg(DISTINCT customer_id), m, k)
    FROM orders WHERE order_id <= n
      → ~1,000 distinct keys → bytea filter (1,273 bytes for n=1000, b=10)

 2. The filter becomes parameter $1.

 3. Foreign Scan sends the Remote SQL, with $1 as hex text ──────►
    SELECT customer_id, name FROM public.customers
    WHERE public.sjb_contains($1::bytea, customer_id)
                                                          4. B scans customers and keeps
                                                             only rows whose id passes the
                                                             filter: all ~1,000 real matches
                                                             + false positives.
                           ◄────── rows come back as text
 5. Hash Join (exact): orders ⋈ returned customers ON customer_id.
    False positives find no order and are dropped.
    → exactly the baseline result.
```

Step by step:
1. **InitPlan builds the filter.** The sub-SELECT inside `sjb_contains(...)` does not depend on the row of customers, so PostgreSQL runs it once as an *InitPlan*, before the join.
2. **The filter becomes `$1`.** The InitPlan's result is a query parameter.
3. **Remote SQL.** Because sjb_contains is IMMUTABLE, belongs to an extension listed in the server's `extensions` option, and its arguments are a parameter and a column, postgres_fdw puts the whole condition into the SQL it sends to B. The parameter travels as text, so the bytea filter is sent as hex.
4. **B filters.** Site B runs `sjb_contains` for each of its 1,000,000 rows and returns only the rows that pass, which is the approximate semi-join S ⋉ R'.
5. **Exact join at A.** The normal hash join on `customer_id` keeps only true matches, so Bloom false positives never reach the result.

### Results on our machine

`bloom_manual.sql` with n = 1000, b = 10 (shortened):

```
 Hash Join  (actual time=4.243..177.219 rows=1000.00 loops=1)
   Hash Cond: (c.customer_id = o.customer_id)
   InitPlan 1
     ->  Aggregate  (actual rows=1.00 loops=1)
           Output: sjb_build(array_agg(DISTINCT orders.customer_id), (GREATEST('8'::double precision, ceil(((count(DISTINCT orders.customer_id) * 10))::double precision)))::bigint, 7)
   ->  Foreign Scan on public.customers c  (cost=100.00..99756.93 rows=333333 width=15) (actual time=3.826..174.989 rows=9773.00 loops=1)
         Remote SQL: SELECT customer_id, name FROM public.customers WHERE (public.sjb_contains($1::bytea, customer_id))
 Execution Time: 179.901 ms
```

`sjb_contains($1::bytea, customer_id)` is inside the Remote SQL, and there is no `Filter:` line at A. Site B returned **9,773** rows instead of 1,000,000.

Why 9,773 and not "1,000 plus a few"? The false-positive rate (about 0.8% at b = 10) applies to **each of the 999,001 customers that do not match**. The filter had 50.69% of its bits set, so the predicted rate is 0.5069^7 ≈ 0.0086. 999,001 × 0.0086 ≈ 8,600 false positives, and we measured 8,774 (rate 0.0088). So b = 10 is too small for this data. That is exactly what the cost model below is for. With b = 20 only 80 false positives come back (see the table).

Also note the planner's guess `rows=333333`. It doesn't know what sjb_contains does, so it assumes a default selectivity of 1/3. Remember this for timing measurements later.

`correctness_bloom.sql` (b = 10): all six counts are 0.

```
   n   | baseline_except_bloom | bloom_except_baseline
-------+-----------------------+-----------------------
   100 |                     0 |                     0
  1000 |                     0 |                     0
 10000 |                     0 |                     0
```

`byte_estimate.sql` with n = 10000 (9,955 distinct keys; average row width 16.78 bytes as text):

```
  b | bytes_a_to_b | remote_rows | false_positives | bytes_b_to_a | total_bytes | baseline_bytes_b_to_a
----+--------------+-------------+-----------------+--------------+-------------+-----------------------
 10 |       24,938 |      17,921 |           7,966 |      300,675 |     325,613 |            16,777,792
 20 |       49,826 |      10,035 |              80 |      168,365 |     218,191 |            16,777,792
```

The `remote_rows` numbers match the Foreign Scan's actual rows in `bloom_manual.sql -v n=10000` (17,921 and 10,035). Doubling the filter costs 25 KB more A→B, but saves 132 KB B→A. Both are far below the 16.8 MB baseline.

### Cost model version 1: `sjb_best_bits`

For b bits per key, the bytes on the network are:

| Part | Bytes | Depends on b? |
|---|---|---|
| filter, A→B | n × b / 8 × wire_factor | grows with b |
| true-match rows, B→A | matches × row_width | no |
| false-positive rows, B→A | (s_rows − matches) × row_width × e^(−0.4805 b) | shrinks with b |

`e^(−0.4805 b)` is the Bloom false-positive rate with the best k (0.4805 = (ln 2)²). `wire_factor` = 2 because the filter travels as hex text. Setting the derivative of the total to 0 gives the b with the smallest total:

```
b* = ln( 8 × 0.4805 × (s_rows − matches) × row_width / (n × wire_factor) ) / 0.4805
```

If the value inside ln() is ≤ 1, even a tiny filter costs more than the false positives it would remove. The function then returns 0, meaning "Bloom is not worth it". It also returns 0 for n ≤ 0.

```
SELECT sjb_best_bits(10000, 1000000, 10000, 100, 1);   → 21.95
SELECT sjb_best_bits(10000, 1000000, 10000, 100, 2);   → 20.51
SELECT sjb_best_bits(1000000, 1000000, 900000, 1, 2);  → 0   (not worth it)
SELECT sjb_best_bits(9955, 1000000, 9955, 16.78);      → 16.80 (our real n=10000 query)
```

For our real query (rows are only ~17 bytes as text, since just customer_id and name travel), the best b is about 17. That is between the two runs above, and both are already close to the best total.

### How to test it

```bash
source setup/env.example.sh
make -C sjbloom install                  # after any change to the extension
setup/setup_all.sh                        # rebuilds both sites, sjbloom included (~5 s)
psql -d site_a -f queries/bloom_manual.sql                 # look for sjb_contains in Remote SQL
psql -d site_a -f queries/correctness_bloom.sql            # all counts must be 0
psql -d site_a -c "SELECT sjb_best_bits(10000, 1000000, 10000, 100, 1);"   # about 22
psql -d site_a -c "SELECT sjb_best_bits(10000, 1000000, 10000, 100, 2);"   # about 20.5
psql -d site_a -v n=10000 -v b=10 -f queries/byte_estimate.sql
psql -d site_a -v n=10000 -v b=20 -f queries/byte_estimate.sql
```

If the plan shows `Filter: sjb_contains(...)` *above* the Foreign Scan instead of inside the Remote SQL, check that (1) the `extensions 'sjbloom'` server option is set (`SELECT srvoptions FROM pg_foreign_server;`), (2) sjb_contains is IMMUTABLE, and (3) sjbloom is installed in schema public at site B.

## Batch 4: Python benchmarks and the mid-sem charts

In this batch we stop copying numbers out of psql by hand. Python scripts run every strategy for several values of n, measure rows, bytes and time, check correctness, and draw the charts.

### One-time setup: the Python environment

The scripts need two packages, listed in **`bench/requirements.txt`**: `psycopg[binary]` (psycopg 3, the PostgreSQL driver, with its own copy of libpq) and `matplotlib` (charts). They go into a *virtual environment* `.venv/` in the project folder (it is in `.gitignore`).

The usual commands are:

```bash
python3 -m venv .venv
.venv/bin/pip install -r bench/requirements.txt
```

On our WSL Ubuntu the first command failed with `ensurepip is not available`, because the Ubuntu package `python3.12-venv` is missing. There are two ways to fix it:

```bash
# Either install the missing package once (needs sudo), then use the commands above:
sudo apt install python3.12-venv

# or, without sudo: make the venv without pip, and use the system pip to fill it:
python3 -m venv --without-pip .venv
pip --python .venv/bin/python install -r bench/requirements.txt
```

We used the second way. Check it with `.venv/bin/python -c "import psycopg, matplotlib"`.

Connection settings come from the environment variables `PGHOST`, `PGPORT` and `PGUSER` (`source setup/env.example.sh`), and the password, if your server checks one, from `~/.pgpass`. libpq reads both by itself, so no password appears in the Python code.

### Files

**`bench/common.py`**: small helpers used by all scripts.
- `connect(dbname)` opens an autocommit connection to site_a or site_b.
- `explain_json(conn, sql)` runs `EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON)` and returns the plan as a Python dict. JSON is a tree we can walk, so we don't need to parse EXPLAIN text.
- `find_foreign_scan(plan)` walks the tree and returns the `Foreign Scan` node. `remote_rows(node)` = its actual rows × loops, `remote_sql(node)` = the SQL sent to site B, `total_time_ms(plan)` = planning time + execution time.
- `find_join_above_foreign_scan(plan)` / `join_type(plan)` find the join node above the Foreign Scan. That node is not always the direct parent: in a Hash Join the Foreign Scan sits under a `Hash` node. `is_nestloop_with_join_filter(plan)` is True for a Nested Loop that tests the join condition as a `Join Filter`, which compares every outer row with every inner row.
- `avg_row_width(width)` runs in site_b: the average text length of the columns that travel. narrow = `customer_id, name` (**16.78 bytes**), wide = `customer_id, name, padding` (**96.78 bytes**).
- `check_correct(conn, baseline_sql, new_sql)` is the project's correctness test: both `EXCEPT ALL` counts must be 0.
- `print_table(rows, columns)` prints the readable tables below.

**`bench/strategies.py`**: one function per strategy. Each returns `(sql_text, bytes_a_to_b_sql)`: the query, and a query that returns the bytes site A sends to B, measured as text because postgres_fdw sends parameters as text.

| Strategy | Extra WHERE condition | bytes A->B |
|---|---|---|
| `baseline(n, width)` | none | 0 |
| `exact_keys(n, width)` | `c.customer_id = ANY ((SELECT array_agg(DISTINCT ...))::int[])` | `octet_length(array::text)`, the `{12,345,...}` text postgres_fdw sends |
| `bloom_fixed10(n, width)` | `sjb_contains(filter, c.customer_id)`, b = 10 | `octet_length(filter::text)`, the hex text |
| `bloom_dynamic(n, b, width)` | same, with b from the cost model | same |

`width` picks the select list: "narrow" = `o.order_id, c.name`, "wide" also selects `c.padding`. `dynamic_bits(conn, n, row_width)` computes the b for bloom_dynamic *before* the query: `sjb_best_bits(n_distinct, s_rows, n_distinct, row_width, 2)`. Here `n_distinct` = distinct customer_ids of the kept orders, and `s_rows` = `pg_class.reltuples` of the foreign table at site_a (1,000,000). Matches = n_distinct, because every customer_id exists at B. Each width uses its own row width, so the wide variant gets a bigger b (bigger rows make false positives more expensive).

**`bench/run_experiments.py`**: for width in (narrow, wide), n in (100, 1000, 10000, 50000), and each strategy:
1. Run the query 3 times under EXPLAIN ANALYZE and keep the run with the **median** time.
2. From that run: `join_type` and `remote_rows`.
3. `bytes_a_to_b` from the strategy's bytes query. `bytes_b_to_a` = remote_rows × the row width **of this variant**. `total_bytes` = the sum.
4. `correct` = `check_correct(baseline, strategy)`.
5. If the join is a **Nested Loop with a Join Filter**, run the same case again after `SET enable_nestloop = off` and record that too (`nestloop_off = True`).

Before measuring, it runs one tiny query on the foreign table, so postgres_fdw's login to site B is not counted in the first measurement. Rows are **appended** to `results/results.csv`, with a `run_at` timestamp. The full table is printed at the end.

**`bench/b_sweep.py`**: n = 10000, b from 2 to 30, for both widths. It runs the Bloom query once per b and saves bytes A->B, bytes B->A and total bytes to `results/b_sweep.csv` (overwritten each run). It prints the predicted b* from `sjb_best_bits`, the measured minimum, and `model_total_bytes`, the total that cost model v1 predicts for each b.

**`bench/plot.py`**: draws the charts into `results/`. For each width: `total_bytes_vs_n_<width>.png`, `time_ms_vs_n_<width>.png`, `remote_rows_vs_n_<width>.png` and `total_bytes_vs_b_<width>.png` (vertical dashed line at the predicted b*). One more chart, `fp_rate.png`, comes from running `queries/fp_rate.sql` from Python (its rows are also saved to `results/fp_rate.csv`). Because results.csv is appended to, plot.py uses the **last** row of each case (strategy, width, n, nestloop_off). All charts use log axes, since the values span several powers of ten.

**`bench/run_all.sh`**: runs the three scripts in order (see "How to rerun" below).

**`bench/capture_bytes.sh`**: optional cross-check with a real packet capture. It runs nothing: it **prints** the `tcpdump` and `capinfos` commands (some need sudo) to capture one Bloom query. psql connects through the Unix socket (`PGHOST=/tmp`), so only postgres_fdw's TCP connection from site A to site B is captured. Expect the capture to be larger than our estimate, because our estimate ignores TCP/IP headers and PostgreSQL's per-row protocol overhead (about 15 bytes per row for 2 columns).

### Results on our machine

Every one of the 36 rows had `correct = True`. Times are in ms (median of 3); bytes are text bytes.

Narrow rows (row width 16.78 bytes):

```
     strategy |      n |     b |   join_type | nestloop_off | remote_rows | bytes_a_to_b | bytes_b_to_a | total_bytes |  time_ms
--------------+--------+-------+-------------+--------------+-------------+--------------+--------------+-------------+---------
     baseline |    100 |       |   Hash Join |        False |   1,000,000 |            0 |   16,777,792 |  16,777,792 | 2,079.15
   exact_keys |    100 |       | Nested Loop |        False |         100 |          693 |        1,678 |       2,371 |     2.83
   exact_keys |    100 |       |   Hash Join |         True |         100 |          693 |        1,678 |       2,371 |     1.69
bloom_fixed10 |    100 |    10 |   Hash Join |        False |      11,481 |          300 |      192,626 |     192,926 |   256.26
bloom_dynamic |    100 | 26.40 |   Hash Join |        False |         315 |          710 |        5,285 |       5,995 |   157.17
     baseline |  1,000 |       |   Hash Join |        False |   1,000,000 |            0 |   16,777,792 |  16,777,792 | 1,899.64
   exact_keys |  1,000 |       | Nested Loop |        False |         999 |        6,887 |       16,761 |      23,648 |   134.20
   exact_keys |  1,000 |       |   Hash Join |         True |         999 |        6,887 |       16,761 |      23,648 |     7.20
bloom_fixed10 |  1,000 |    10 |   Hash Join |        False |       9,773 |        2,548 |      163,969 |     166,517 |   211.68
bloom_dynamic |  1,000 | 21.60 |   Hash Join |        False |       1,077 |        5,446 |       18,070 |      23,516 |   154.59
     baseline | 10,000 |       |   Hash Join |        False |   1,000,000 |            0 |   16,777,792 |  16,777,792 | 1,924.10
   exact_keys | 10,000 |       |   Hash Join |        False |       9,955 |       68,605 |      167,023 |     235,628 |    65.67
bloom_fixed10 | 10,000 |    10 |   Hash Join |        False |      17,921 |       24,938 |      300,675 |     325,613 |   258.41
bloom_dynamic | 10,000 | 16.80 |   Hash Join |        False |      10,290 |       41,862 |      172,643 |     214,505 |   231.34
     baseline | 50,000 |       |   Hash Join |        False |   1,000,000 |            0 |   16,777,792 |  16,777,792 | 2,060.47
   exact_keys | 50,000 |       |   Hash Join |        False |      48,738 |      335,701 |      817,716 |   1,153,417 |   361.30
bloom_fixed10 | 50,000 |    10 |   Hash Join |        False |      56,495 |      121,896 |      947,861 |   1,069,757 |   388.57
bloom_dynamic | 50,000 | 13.41 |   Hash Join |        False |      50,269 |      163,446 |      843,403 |   1,006,849 |   362.33
```

Wide rows (row width 96.78 bytes): same remote rows for baseline, exact_keys and bloom_fixed10. bloom_dynamic picks bigger b values (30.04, 25.25, 20.45, 17.06). Total bytes at n = 10000: baseline 96,777,792; exact_keys 1,032,028; bloom_fixed10 1,759,293; bloom_dynamic 1,019,595.

What the numbers show:
- **Every filter beats the baseline by a lot.** At n = 10000 the baseline moves 16.8 MB (narrow) or 96.8 MB (wide); the filters move 0.2 to 1.8 MB.
- **A fixed b = 10 is a bad choice here.** S has 1,000,000 rows, so even a 0.8% false-positive rate lets about 8,000 extra rows through, more than the real matches for small n. bloom_dynamic uses more bits per key and comes back with close to the true number of rows.
- **Exact keys vs. Bloom.** For small n, the exact list is smallest (no false positives, and a short list). For large n with narrow rows, the dynamic Bloom filter wins: at n = 50000 it sends 163 KB instead of 336 KB, and its few false positives cost less than that difference. With wide rows, false positives are expensive, so the two are close.
- **Join method.** For exact_keys at n = 100 and 1000, the planner guessed only 10 rows from the Foreign Scan and chose a Nested Loop with a Join Filter. With `enable_nestloop = off` it uses a Hash Join and gets much faster (134 ms → 7 ms at n = 1000). The bytes do not change, only the time. The dashed line in `time_ms_vs_n_*.png` shows this.
- **Time.** The Bloom strategies take at least about 150 ms even for tiny n, because site B runs `sjb_contains` on all 1,000,000 rows. exact_keys with a Hash Join is faster, because `= ANY` is cheaper per row. All filter strategies are about 5 to 1,200 times faster than the baseline.

`b_sweep.py` (n = 10000):

```
narrow: measured minimum at b = 16 (214,124 bytes); predicted b* = 16.8
wide:   measured minimum at b = 20 (1,020,991 bytes); predicted b* = 20.45
```

The measured minimum lands right next to the predicted b* for both widths, and the cost model's predicted totals are within 1% of the measured ones for b ≥ 6. Near the minimum the curve is flat: at narrow width, b = 14 to 22 are all within 4% of the best.

`fp_rate.png`: the measured false-positive rates lie on the theoretical curve (same numbers as in Batch 2).

### How to rerun all experiments (one command)

```bash
source setup/env.example.sh
bench/run_all.sh
```

This needs the server running (`pg_ctl status`), both sites set up (`setup/setup_all.sh`), and the `.venv` from above. It takes about 5 minutes. It runs `run_experiments.py` (appends to `results/results.csv`), `b_sweep.py` (rewrites `results/b_sweep.csv`) and `plot.py` (rewrites the PNGs and `results/fp_rate.csv`). To start results.csv fresh, delete it first. The scripts can also be run one at a time, e.g. `.venv/bin/python bench/plot.py`.

`results/` is in `.gitignore`, so each run writes there without touching git. A frozen copy of the mid-sem CSVs and charts is committed in `docs/midsem/`.

## Between Batch 4 and Batch 5: enhanced double hashing

The full story, with tables and references, is in **[docs/fp_investigation.md](fp_investigation.md)** (written as presentation material). In short: for small filters we measured far more false positives than theory (215 instead of about 3 at n = 100). The cause was classic double hashing, position_i = (h1 + i·h2) mod m. Its evenly spaced positions let different keys share the same bits.

### What changed

**`sjbloom/bloom.c`**: `bit_position` was replaced by **`compute_positions`**, which uses *enhanced double hashing* (Dillinger & Manolios 2004, also used by PostgreSQL's own `bloomfilter.c`): start at `x = h1 mod m` with step `y = h2 mod m`; for each next position take the step (`x = x + y`) and then make the step bigger (`y = y + i`), all mod m. There is still one hash call per key. The description of `bit_position` in the Batch 2 section above is the old version 1.

**`sjbloom/sjb_filter.h`**: `SJB_VERSION` is now **2**. Site A and site B must compute the same positions; a filter from old code must never be read with the new positions, or matching rows could be dropped.

**`sjbloom/sjbloom.c`**: `read_header` now rejects a filter of another version with a clear message and a HINT ("Site A and site B must run the same sjbloom build...").

**`queries/fp_cases.sql`**: predicted vs measured false positives for the small filters of the benchmark, with synthetic keys and with the real order keys. Step 3 counts the non-members that get exactly the same positions as a member, which explains the gap that remains at very small m.

Since the bit positions changed, some exact numbers in earlier sections changed too, for example `sjb_info(sjb_build(ARRAY[1,2,3], 64, 3))` now prints `bits_set=14.06%` instead of `12.50%`. The Batch 4 charts made with version 1 are kept in `docs/midsem/v1/`; `docs/midsem/` has the new ones.

### How to test it

```bash
source setup/env.example.sh
make -C sjbloom && make -C sjbloom install && make -C sjbloom installcheck
psql -d site_b -c "DROP EXTENSION sjbloom CASCADE; CREATE EXTENSION sjbloom;"
psql -d site_a -c "DROP EXTENSION sjbloom CASCADE; CREATE EXTENSION sjbloom;"
psql -X -d site_a -f queries/fp_rate.sql
psql -X -d site_a -f queries/fp_cases.sql
# An old (version 1) filter must be refused:
psql -d site_a -c "SELECT sjb_contains(set_byte(sjb_build(ARRAY[1,2,3], 64, 3), 0, 1), 2);"
```

### Follow-up: early exit, and the step no longer forced odd

**Early exit (`sjbloom/bloom.c`).** `sjb_bloom_test` now computes the positions one at a time and tests each bit immediately, returning false at the first 0 bit. The positions are exactly the same as before, so the results are too: `fp_rate.sql` and `fp_cases.sql` gave identical output before and after. A new helper, `step_to_next_position`, holds the update rule (`x = x + y`, then `y = y + i`, mod m), and both `compute_positions` (used by add) and the test use it, so add and test can never disagree. Why it matters: site B runs the test on every one of its 1,000,000 rows, and most keys are rejected after 1 or 2 positions. The old code computed all k positions first.

**New script `bench/time_contains.py`.** It runs `SELECT count(*) FROM customers WHERE sjb_contains(filter, customer_id)` in site_b 5 times per filter and prints the median time. Before / after the early exit:

| filter | k | rows passed | median before | median after |
|---|---:|---:|---:|---:|
| n = 100, b = 26.4 | 18 | 129 | 102.7 ms | 69.2 ms |
| n = 10,000, b = 16.8 | 12 | 10,348 | 89.7 ms | 74.7 ms |

**Step not forced odd (`sjbloom/bloom.c`, `sjb_filter.h`).** `compute_two_hashes` no longer forces h2 odd (`| 1` removed), like PostgreSQL's `bloomfilter.c`. Enhanced double hashing does not need an odd step, and forcing it halved the number of usable (h1 mod m, h2 mod m) pairs when m is even, which doubled the extra false positives of small filters. The bit positions changed, so `SJB_VERSION` is now **3**. Results are in [docs/fp_investigation.md](fp_investigation.md).

**`sjbloom/Makefile`.** It has a new line, `$(OBJS): sjb_filter.h`. PGXS does not know that our .c files include the header, so after changing `SJB_VERSION`, `make` recompiled only `bloom.c` and `sjbloom.so` kept version 2 (we noticed because a version-2 filter was accepted). With this line, a header change recompiles both files.

How to test (after `make -C sjbloom install` and recreating the extension in both sites):

```bash
psql -d site_a -c "SELECT get_byte(sjb_build(ARRAY[1], 64, 1), 0);"   # 3
psql -d site_a -c "SELECT sjb_contains(set_byte(sjb_build(ARRAY[1,2,3], 64, 3), 0, 2), 2);"   # ERROR: version 2
psql -X -d site_a -f queries/fp_cases.sql
.venv/bin/python bench/time_contains.py
```

## Batch 5: calibration, a time-based cost model, and adaptive filter modes

Up to now the filter was always a Bloom filter whose size we chose by hand (or with `sjb_best_bits` in Python). In this batch the extension decides by itself, using **estimated time**:

```
time = bytes moved / bandwidth  +  work at site B
```

This is communication cost plus local processing cost, as in Özsu & Valduriez, *Principles of Distributed Database Systems*. There are two decisions.

**Which PATH (decided at plan time in Batch 6; this batch only builds and tests the function):**

| path | what site B gets | work at B | rows back |
|---|---|---|---|
| index | `customer_id = ANY(key list)` | n index lookups: `n * t_lookup_us` | M |
| filter | `sjb_contains(filter, customer_id)` | checks ALL rows: `s_rows * t_check_ns` | depends on the mode |

**Which MODE inside the filter path (decided at runtime by `sjb_build_auto`, when the exact number of distinct keys is known):**

| mode | filter on the wire (hex text) | rows back |
|---|---|---|
| BLOOM | `2 * (24 + ceil(m/8)) + 2` | M + (s_rows − M) · fp |
| EXACT (sorted key list) | `2 * (24 + 4n) + 2` | M |
| PASS (header only) | `2 * 24 + 2` | s_rows |

Here M = min(s_rows, n · rows_per_key) is the number of rows that really match, and fp = (1 − e^(−kn/m))^k (Broder & Mitzenmacher 2004). The index path's key list costs `n * 7 + 2` bytes (about 6 digits and a comma per key, plus the braces). Rows back cost `row_width` bytes each. **row_width is the width on the wire**: the text of the values + 7 bytes per row + 4 bytes per column (PostgreSQL's DataRow message). Bandwidth in bytes per second is `network_mbit * 125000`.

The Bloom size comes from **b\***, the bits per key that minimise the total bytes (same formula as Batch 3's `sjb_best_bits`). With the best k, fp(b) = e^(−0.4805·b), where 0.4805 = (ln 2)². The total bytes are

```
total(b) = W·n·b/8  +  M·w  +  (S − M)·w·e^(−0.4805·b)       (W = wire factor 2, w = row width, S = s_rows)
```

Setting the derivative to 0 gives `b* = ln(3.844 · (S − M) · w / (W · n)) / 0.4805` (3.844 = 8 · 0.4805). If the value inside ln() is ≤ 1, Bloom is not worth it and b* = 0. Then m = max(8, ceil(n · b*)) and k = round(0.693 · b*), between 1 and 32.

### Files

**`sjbloom/costmodel.h`, `sjbloom/costmodel.c`** (new): the cost model as plain arithmetic on doubles, with no SQL values. One small function per formula, each with a comment naming its source: `sjb_fp_rate`, `sjb_best_bits`, `sjb_bloom_bits_for` (m), `sjb_bloom_hashes_for` (k), `sjb_matching_rows` (M), `sjb_bloom_filter_bytes`, `sjb_exact_filter_bytes`, `sjb_pass_filter_bytes`, `sjb_key_list_bytes`, `sjb_bandwidth_bytes_per_sec`. All the inputs travel in one struct, `SjbCostInputs`. **`sjb_estimate_all`** fills in four `SjbCostEstimate`s (index, bloom, exact, pass), each with its bytes, rows back, network time, work at B and total, and then makes both decisions:
- `best_filter_mode`: the allowed mode with the smallest time. BLOOM is skipped when b\* = 0 or its filter is bigger than `max_filter_bytes`; EXACT is skipped when it is bigger than `max_filter_bytes`. PASS is never skipped (24 bytes), so there is always a mode. On a tie the simpler mode wins: PASS, then EXACT, then BLOOM.
- `index_path_wins`: index path time ≤ the best filter mode's time.

`max_filter_bytes` limits the filter value itself (header included), not its hex text.

**`sjbloom/sjbloom.c`**: the file is now in five parts: checking a filter, the EXACT payload, building filters, reading cost model arguments, and the SQL functions.
- `sjb_build_auto(keys, s_rows, rows_per_key, row_width, network_mbit, t_check_ns, max_filter_bytes DEFAULT 1048576, wire_factor DEFAULT 2)`: `collect_unique_keys` copies the non-NULL keys into a C array, sorts it with `qsort` + `compare_int32`, and removes repeats (`remove_duplicates`) to get n. With n = 0 it returns an EXACT filter with no keys: nothing passes, and an empty R' gives an empty join, so that is correct. Otherwise it calls `sjb_estimate_all` and builds the winner with `build_bloom_filter`, `build_exact_filter` (keys copied one by one with memcpy) or `build_pass_filter`. The header's nkeys is n in every mode.
- `sjb_contains` now branches on the mode: BLOOM as before, EXACT = `exact_filter_contains`, a hand-written binary search that reads each key with memcpy (`read_exact_key`; the bytes inside a bytea may be unaligned), PASS = true. Each mode first checks that its payload has the right size (`check_exact_filter`, `check_pass_filter`).
- `sjb_info` prints mode-specific details, e.g. `mode=exact nkeys=3 min_key=1 max_key=3 bytes=36` or `mode=pass nkeys=5000 bytes=24`.
- `sjb_choice_report(n, s_rows, rows_per_key, row_width, network_mbit, t_check_ns, t_lookup_us)` returns a readable table: one line for the index path and one per filter mode, then the Bloom size, the best filter mode and the winner. `sjb_plan_choice(same arguments)` returns just `'index'` or `'filter'`; Batch 6's planner hook will call it. Both read their arguments with the same helper (`read_report_arguments`) and use the default max_filter_bytes and wire_factor, so they always agree with `sjb_build_auto`.
- `sjb_build` now uses the same `new_filter` / `write_header` / `build_bloom_filter` helpers; its output is unchanged.
- All settings are **arguments**, never read from configuration settings (GUCs). So the functions really are IMMUTABLE (same inputs, same output), which postgres_fdw needs before it ships `sjb_contains` to site B.

New PostgreSQL helpers used: `PG_GETARG_FLOAT8` (read a float8 argument), `StringInfo` / `initStringInfo` / `appendStringInfo` (a growing text buffer for the report), `palloc` (PostgreSQL's malloc).

`SJB_VERSION` stays 3: BLOOM filters are unchanged, and an older build rejects modes 2 and 3 with a clear error instead of misreading them.

**`sjbloom/sjbloom--1.0.sql`**: the three new functions, all `IMMUTABLE STRICT PARALLEL SAFE`. **`sjbloom/Makefile`**: `costmodel.o` added to OBJS, and `$(OBJS): sjb_filter.h costmodel.h`, so changing either header rebuilds everything.

**`sjbloom/sql/sjbloom_basic.sql`**: Steps 9–18. Zero keys (or only NULLs) give `mode=exact nkeys=0` and nothing passes. Repeats and NULLs are removed. For keys 1..5000 each mode wins in the right case:

| s_rows | mode | why |
|---|---|---|
| 1,000,000 | BLOOM | b* ≈ 19.5 bits per key, smaller than EXACT's 32 |
| 10^12 | EXACT | false positives so costly that b* ≈ 48 > 32 |
| 5,000 (= n) | PASS | every row matches anyway |
| 10^12, max_filter_bytes = 10000 | PASS | EXACT and BLOOM are both too big |

There are no false negatives in any mode, and no false positives for EXACT (1 million non-member keys tested). The binary search handles −2147483648, 2147483647 and negative keys. One full `sjb_choice_report` is printed. `sjb_plan_choice` says index on a fast network and filter on a slow one with many keys. Invalid arguments and an unknown mode give errors.

**`bench/calibrate.py`** (new): measures the two machine constants directly in site_b. See "Calibration" below.

**`bench/validate_model.py`** (new): predicted vs measured time. See "Does the model predict real times?" below.

**`queries/catalog_stats.sql`** (new): reads `s_rows` (`pg_class.reltuples`), `rows_per_key` (reltuples / distinct values; `pg_stats.n_distinct` < 0 means a fraction of the rows, −1 = all distinct) and `row_width` (sum of `pg_stats.avg_width` for customer_id and name, + 7 + 4·2) from **site_a's** catalog into psql variables with `\gset`. On our data: 1,000,000, 1, 30. (avg_width is the stored width: 4 bytes for an int4 whose text has 6–7 digits, so 30 is a bit below the measured 31.78.)

**`queries/auto_manual.sql`** + **`queries/auto_manual_one.sql`** (new): the real join with `sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), ...) FROM orders WHERE order_id <= :n), c.customer_id)`. It shows the plan for n = 10000, then sjb_info and both EXCEPT ALL counts for n = 100, 10000 and 150000. No `DISTINCT` is needed in `array_agg`, because `sjb_build_auto` removes repeats itself.

**`queries/choice_demo.sql`** (new): the presentation demo, `sjb_choice_report` for n = 10000 and 50000 at 1000 and 1 Mbit/s.

### Calibration (`bench/calibrate.py`)

- **t_check_ns**: build a Bloom filter of 10,000 random existing keys (b = 16, k = 11). Run `EXPLAIN (ANALYZE, FORMAT JSON) SELECT customer_id, name FROM customers WHERE sjb_contains(filter, customer_id)` 5 times (after one untimed warm-up run). Median execution time / 1,000,000 rows. The same is done with an EXACT and a PASS filter of the same keys.
- **t_lookup_us**: for n = 1000, 5000, 10000, 20000 random existing keys, the median of 5 runs of `... WHERE customer_id = ANY(keys)`. Then a straight line `time = a + n · slope` is fitted by least squares (the formula is written out in `fit_line`). t_lookup_us = the slope.
- **Parallel workers off.** postgres_fdw runs the remote query through a cursor, and PostgreSQL never uses parallel workers for a cursor. So every measurement uses `SET max_parallel_workers_per_gather = 0`. For comparison, everything is measured a second time with the default (2), and both are saved in `results/calibration.json`.

Our results (`results/calibration.json`):

```
mode  | ms_no_parallel | t_check_ns | ms_parallel | plan with parallel allowed
bloom |         182.62 |     182.62 |      177.29 | Seq Scan
exact |         153.62 |     153.62 |      153.26 | Seq Scan
pass  |         205.69 |     205.69 |      205.42 | Seq Scan
reference, SELECT count(*) with the Bloom filter: 141.03 ms (Index Only Scan) vs
           54.85 ms (Gather, Index Only Scan, 2 workers), speedup 2.57

    n | ms_no_parallel | ms_parallel | plan
 1000 |           3.64 |        2.72 | Index Scan
 5000 |          14.19 |       13.63 | Index Scan
10000 |          23.51 |       22.89 | Index Scan
20000 |          36.05 |       34.56 | Index Scan
fitted line (no parallel): time_ms = 4.456 + n * 0.001655  ->  t_lookup_us = 1.6547

Suggested: t_check_ns = 182.62, t_lookup_us = 1.6547
```

What we learned:
- **With and without parallel workers the times are the same, because the planner never chose a parallel plan for these queries.** For `WHERE sjb_contains(...)` it guesses that 1/3 of the rows pass (333,333 rows). Sending that many rows through a Gather node costs more than the workers would save. The 2x speed-up only shows up for `SELECT count(*)`, where each worker counts its part and sends back one number: 141 → 55 ms. That is also why Batch 4's `time_contains.py` (which uses `count(*)`) reported about 75 ms. Through postgres_fdw site B really needs about 180 ms.
- **One constant is roughly enough.** EXACT (154 ns) is cheaper than BLOOM (183 ns): a binary search over 10,000 keys is about 14 comparisons, while BLOOM computes a hash. PASS (206 ns) is the most expensive, because all 1,000,000 rows leave the scan node. The spread is about ±15% around the BLOOM value, which is the one we use. Inside the filter path all modes use the same t_check_ns, so the mode choice depends only on bytes.
- **The lookup line is not quite straight.** The cost per extra key falls from about 2.6 µs (1000 → 5000) to 1.25 µs (10000 → 20000), because neighbouring keys share index pages. The fitted line has a 4.5 ms intercept, which the model (n · t_lookup_us) leaves out.
- **WSL timing is noisy.** Three runs gave t_check_ns = 150, 188 and 183 ns. Rerun `calibrate.py` on your own machine and pass your values with `-v t_check_ns=... -v t_lookup_us=...`.
- A side discovery: when the filter is stored in a table (for example a temp table) and read back with `(SELECT filter FROM t)`, the 20 KB value is TOASTed. `sjb_contains` then fetches it again for every row: 5.5 s instead of 0.18 s. Through postgres_fdw this does not happen, because the filter arrives as a query parameter, but test queries should build the filter inline or in a CTE, not store it in a table.

### Results: which mode `sjb_build_auto` chose (`auto_manual.sql`)

```
     n | filter_info                                                         | missing | extra
   100 | mode=bloom nkeys=100 m=2761 k=19 bits_set=49.58% bytes=370          |       0 |     0
 10000 | mode=bloom nkeys=9955 m=179299 k=12 bits_set=48.70% bytes=22437     |       0 |     0
150000 | mode=bloom nkeys=139294 m=1703339 k=8 bits_set=48.03% bytes=212942  |       0 |     0
```

The plan shows `Remote SQL: SELECT customer_id, name FROM public.customers WHERE (public.sjb_contains($1::bytea, customer_id))`, with sjb_build_auto in an InitPlan.

Why BLOOM every time (numbers from `sjb_choice_report` with the same inputs, at 1000 Mbit/s): in the filter path site B checks all 1,000,000 rows in every mode (182.6 ms), so only the bytes differ.
- **n = 100**: BLOOM's filter is 742 bytes on the wire (27.6 bits per key) and lets about 2 extra rows through. EXACT's is 850 bytes. BLOOM wins by about 100 bytes (0.001 ms), so this is practically a tie. PASS would send all 1,000,000 rows back (30 MB, 240 ms).
- **n = 10000**: BLOOM 44.9 KB + 174 false-positive rows (5 KB), EXACT 79.7 KB. BLOOM wins by about 30 KB (0.24 ms).
- **n = 150000**: BLOOM 426 KB (12.2 bits per key) + 2,432 false positives (73 KB), EXACT 1.11 MB. BLOOM wins by about 600 KB (4.9 ms).

EXACT would win only if Bloom needed more than about 32 bits per key, for example with a much bigger S or much wider rows (test Step 11, s_rows = 10^12). PASS would win only if almost every customer matched (n close to s_rows). In our data, b* is between 12 and 28, so Bloom is always smaller. The index path would be much faster at n = 100 and 10000 (0.2 and 19 ms instead of about 183 ms), but `auto_manual.sql` always uses the filter path; choosing the path is Batch 6's job.

### Results: the demo (`choice_demo.sql`)

| n | network | index path | best filter (bloom) | winner |
|---|---|---:|---:|---|
| 10000 | 1000 Mbit/s | 19.5 ms | 185.4 ms | index |
| 10000 | 1 Mbit/s | 2,976.6 ms | 2,984.9 ms | index (by 8 ms) |
| 50000 | 1000 Mbit/s | 97.5 ms | 196.3 ms | index |
| 50000 | 1 Mbit/s | 14,882.8 ms | 13,847.9 ms | **filter** |

- On the fast network the bytes take only a few ms, so the work at site B decides. 10,000 or 50,000 index lookups (16.5 or 82.7 ms) are cheaper than checking 1,000,000 rows (182.6 ms).
- On the slow network every byte costs 8 µs, so the bytes decide. The rows that come back are about the same for both paths (M rows of 30 bytes). The difference is what A sends: the key list takes about 7 bytes per key, while the Bloom filter takes about 2·b/8 bytes per key as hex (4.5 bytes at b = 18, 3.6 bytes at b = 14.6). At n = 10000 that saves 25 KB = 0.2 s, almost exactly the extra 166 ms of site B's full scan, so the two paths nearly tie. At n = 50000 it saves 168 KB = 1.3 s, much more than the extra 100 ms of the full scan, so the filter path wins.

### Does the model predict real times? (`bench/validate_model.py`)

Narrow rows, network_mbit = 1000, the calibrated constants, and the wire row width 16.78 + 7 + 8 = 31.78. "measured" is the end-to-end time at site A (planning + execution, median of 3). "fscan" is the time of the Foreign Scan node alone (waiting for B, receiving and converting the rows). Ratio = measured / predicted.

```
     n | strategy                  | pred_network | pred_work_B | predicted | measured | ratio | fscan_ms | fscan_ratio | pred_rows | remote_rows
   100 | exact_keys (nestloop off) |         0.03 |        0.17 |      0.20 |     1.50 |  7.61 |     1.00 |        5.09 |       100 |         100
   100 | bloom_dynamic (b=27.73)   |         0.03 |      182.62 |    182.65 |   178.92 |  0.98 |   178.26 |        0.98 |       102 |         109
  1000 | exact_keys (nestloop off) |         0.31 |        1.65 |      1.96 |     6.97 |  3.55 |     5.88 |        3.00 |       999 |         999
  1000 | bloom_dynamic (b=22.93)   |         0.30 |      182.62 |    182.92 |   188.53 |  1.03 |   187.07 |        1.02 |     1,015 |       1,016
 10000 | exact_keys (nestloop off) |         3.09 |       16.47 |     19.56 |    58.16 |  2.97 |    49.69 |        2.54 |     9,955 |       9,955
 10000 | bloom_dynamic (b=18.13)   |         2.93 |      182.62 |    185.55 |   227.73 |  1.23 |   218.18 |        1.18 |    10,119 |      10,108
 50000 | exact_keys (nestloop off) |        15.12 |       80.65 |     95.77 |   293.52 |  3.06 |   259.07 |        2.71 |    48,738 |      48,738
 50000 | bloom_dynamic (b=14.74)   |        14.03 |      182.62 |    196.65 |   324.98 |  1.65 |   278.51 |        1.42 |    49,537 |      49,480
```

What it shows:
- **The row counts are right.** Predicted rows back match the real remote rows almost exactly (the false-positive formula and M are correct).
- **The filter path is right when few rows come back** (ratio 0.98 and 1.03 at n = 100 and 1000): then the full scan at B is nearly all the time, and t_check_ns measures exactly that.
- **The error grows with the number of returned rows.** For bloom_dynamic, the extra time over the prediction is about 42 ms for 10,000 rows and 128 ms for 49,500 rows. For exact_keys it is about 39 ms for 9,955 rows and 198 ms for 48,738 rows. That is roughly **3–4 µs per returned row** end to end; about 1.7–3.4 µs of it is already inside the Foreign Scan. The model has no term for this.
- **The index path's ratio is about 3 for every n ≥ 1000.** Its predicted time is small, so the same missing per-row cost is a big fraction of it. Per returned row, the extra is about the same for both strategies at n = 10000 (about 4 µs), but at n = 50000 it is bigger for exact_keys (4.1 µs) than for Bloom (2.6 µs). That hints at extra per-KEY work on the index path: turning the key list into text at A, parsing it at B (`array_in`), B planning a query with a 48,000-element array, and sorting the array keys before the index scan. The calibration timed only the index scan itself, with the array already parsed and the plan already made. For exact_keys, keys and returned rows are the same number, so these two costs cannot be told apart from these runs. Also, the Bloom full scan alone varies by ±20 ms between runs, so per-row figures from n = 10000 are rough.

What is missing from the model (reported, NOT added, as agreed):
1. **A cost per returned row**: B turns each row into text, postgres_fdw fetches 100 rows per round trip (`fetch_size`), and A parses the text back into values. About 2–4 µs per row here. With 1000 Mbit/s the model's network term is only about 0.3 µs per row, so the real per-row cost is CPU, not the wire. (On localhost the real "network" is much faster than 1000 Mbit/s.)
2. **Probably, extra per-key work on the index path** beyond the index lookup (key list text, array parsing and planning at B): at n = 50000 about 1.5 µs per key more than the filter path pays per row; at n = 10000 not visible within the noise.
3. **Work at site A**: scanning orders, the `DISTINCT` sorts that build the key list or filter, and the hash join (measured − fscan: 34 ms for exact_keys and 47 ms for Bloom at n = 50000).
4. **A fixed cost per query** of about 1 ms (planning, opening and closing the remote cursor, round trips). It only matters for tiny n (ratio 7.6 at n = 100, but only 1.3 ms in absolute terms).

Does it change decisions? On the fast network, the model puts the break-even between the index path and the Bloom filter path near 110,000 keys (182.6 ms / about 1.7 µs per key). In the measurements, exact_keys costs about 6 µs per key at both n = 10000 and 50000, and Bloom costs about 183 ms + 2.9 µs per row at n = 50000. Straight lines through these points cross near 60,000 keys. With only these few noisy points that is a rough guess, but it suggests the model is biased towards the index path for large n: between roughly 60,000 and 110,000 keys it could pick the index path when the filter path is really faster. Items 1 and 2 are what we would add. Both can be calibrated the same way: time a query that returns R rows through postgres_fdw, and the same query directly at B. That is for us to decide before Batch 6.

### How to test it

```bash
source setup/env.example.sh
make -C sjbloom && make -C sjbloom install
# recreate the extension in both sites (it stays version 1.0, no upgrade script):
for db in site_a site_b; do psql -d $db -c "DROP EXTENSION sjbloom CASCADE" -c "CREATE EXTENSION sjbloom"; done
make -C sjbloom installcheck                         # 1 test, ok
psql -d site_a -c "SELECT get_byte(sjb_build(ARRAY[1], 64, 1), 0);"   # 3

.venv/bin/python bench/calibrate.py                  # writes results/calibration.json (about 10 s)
psql -X -d site_a -f queries/auto_manual.sql         # modes + correctness (all counts 0)
psql -X -d site_a -f queries/choice_demo.sql         # the four reports
.venv/bin/python bench/validate_model.py             # predicted vs measured (about 5 s)
```

To use your own calibrated values in the SQL files: `psql -X -d site_a -v t_check_ns=150 -v t_lookup_us=1.5 -f queries/choice_demo.sql`. `DROP EXTENSION ... CASCADE` does not remove the `extensions 'sjbloom'` option of the foreign server; that is only text, so `enable_sjbloom.sql` does not need to be run again.

## Batch 5b: the complete Özsu–Valduriez cost model

Batch 5's validation showed that the model missed a cost for every returned row and part of the index path's work. In Batch 5b the model gets the full total-time formula of Özsu & Valduriez (*Principles of Distributed Database Systems*, chapter on query optimization, cost functions):

```
time = T_CPU * #instructions  +  T_MSG * #messages  +  T_TR * #bytes
```

In plain words, a distributed query costs three kinds of time. Computers do **work** (T_CPU). Every **message** sent between the sites has a fixed cost no matter how small it is (T_MSG). Every **byte** has to travel over the network (T_TR = 1 / bandwidth). (The book's I/O term is part of our CPU work, because our tables are cached in memory.) For our two paths:

| term | index path | filter path (each mode) |
|---|---|---|
| T_TR · bytes | (key list + M rows) / bandwidth | (filter + rows back) / bandwidth |
| T_MSG · messages | (1 + ceil(M / fetch_size)) · per_message_ms | (1 + ceil(rows back / fetch_size)) · per_message_ms |
| T_CPU · returned rows | M · per_row_us | rows back · per_row_us |
| T_CPU · work at B | n · t_lookup_us | s_rows · t_check_ns |
| T_CPU · work at A | — | n · t_build_us |

### The new constants, in plain words

- **per_message_ms**: the cost of one round trip between site A and site B. postgres_fdw sends the query, then asks for rows with `FETCH 100`, waits for them, asks again, and so on: 1 + ceil(rows / fetch_size) messages. Each round trip has a fixed cost (system calls, waking up the other process, protocol messages) however many rows it carries. **0.062 ms** on our machine.
- **per_row_us**: the CPU cost of one returned row on top of finding it. Site B turns the row into text and site A turns the text back into values and builds a tuple. **0.55 µs**.
- **t_lookup_us**: the index path's cost per key, measured end to end through postgres_fdw: the key list turned into text at A, parsed and planned at B, and the index lookup itself. The message and row costs are measured separately and taken out. **4.80 µs** (in Batch 5 we measured only the index lookup inside B: 1.65 µs).
- **t_build_us**: site A's cost per key to build the filter with `sjb_build_auto` (sort, remove repeats, hash or copy). Only the filter path pays it. **0.245 µs**.
- **fetch_size**: rows per FETCH. Not measured: it is read from the foreign table's or server's options, and postgres_fdw's default is 100.
- **t_check_ns** is unchanged in how it is measured (a full scan with sjb_contains in site_b). This run gave **158.19 ns**; earlier runs gave 150–188.

### Files

**`sjbloom/costmodel.h`, `costmodel.c`**: `SjbCostInputs` has four new fields (per_message_ms, per_row_us, t_build_us, fetch_size), and `SjbCostEstimate` has messages, message_seconds, row_seconds and work_at_a_seconds. New formula functions, each commented with its source: `sjb_message_count` (1 + ceil(rows / fetch_size)), `sjb_message_seconds`, `sjb_returned_row_seconds`, `sjb_index_lookup_seconds`, `sjb_filter_build_seconds`. `finish_estimate` now adds all five parts. `set_filter_path_work` sets the work at B and at A for the three filter modes.

**`sjbloom/sjbloom.c`, `sjbloom--1.0.sql`**: `sjb_build_auto`, `sjb_choice_report` and `sjb_plan_choice` have four new arguments at the end: `per_message_ms`, `per_row_us`, `t_build_us` (defaults = our calibrated values 0.06194, 0.54819, 0.245) and `fetch_size int4 DEFAULT 100`. They are still arguments, not GUCs, so the functions stay IMMUTABLE. Old calls keep working, because the new arguments have defaults. The report has new columns: messages, messages_ms, rows_ms and work_A_ms.

**`sjbloom/sql/sjbloom_basic.sql`**: Steps 9–16 now pass the new arguments explicitly as 0 (and fetch_size 100), so they still test the bytes-only choices and do not change when the calibrated defaults change. All their results are unchanged. Step 15 shows the wider report, Step 17 tests `fetch_size = 0`, and the new Step 19 shows that a cost per returned row turns BLOOM into EXACT: at 10 µs per row, Bloom's ~85 false positives cost more than its smaller filter saves.

**`bench/calibrate.py`**: three new measurements, each on its own workload: ranges of customer_ids and random existing keys from a fixed seed, never the benchmark queries or the orders keys. Parallel workers are off at both sites.

**`bench/common.py`**: `fetch_size(conn)` reads fetch_size from the foreign table or server options (default 100). `initplan_ms(plan)` adds up the InitPlan times.

**`bench/validate_model.py`**: uses all the calibrated constants, and reads the report by its header line (so new columns do not break it).

**`bench/strategies.py`**: `auto_filter(...)`, the filter path as the system really runs it: `sjb_contains(sjb_build_auto(array_agg(customer_id), ...), c.customer_id)`.

**`bench/break_even.py`** (new): the break-even n, predicted and measured.

**`queries/choice_demo.sql`**: new Step 5 finds the break-even n with `sjb_plan_choice`.

### How each constant is measured (`bench/calibrate.py`)

**per_message_ms and per_row_us.** `SELECT customer_id, name FROM customers WHERE customer_id <= R` for R = 1000, 10000, 100000, 300000, 1000000:
- `fdw_ms`: the Foreign Scan node's time at site_a.
- `local_ms`: the same query in site_b, run as `EXPLAIN ANALYZE DECLARE ... CURSOR FOR ...`, so B uses the same cursor plan postgres_fdw gets.
- `extra_ms = fdw_ms − local_ms` is what postgres_fdw adds on top of B's own work. We fit `extra_ms = per_message_ms · messages + per_row_us · R` with plain least squares (`fit_two_terms`, two normal equations solved with Cramer's rule).

The first try used only fetch_size 100, and it **failed**. With one fetch size, messages = 1 + R/100 is just R divided by 100, so the two terms grow together, and least squares cannot tell which one the time belongs to:

| run | per_message_ms | per_row_us | R² |
|---|---:|---:|---:|
| 1 | −82.7 | 829 | 0.981 |
| 2 | 8.8 | −86.7 | 0.9997 |

Both runs fit the data (high R²) with meaningless, even negative, constants. The only thing they agreed on was the combined cost per row, about 1.1–2.0 µs. The fix: run the same R values with **fetch_size 100, 1000 and 10000**, on a throwaway foreign table `calib_tmp.customers` (same remote table; dropped at the end, even on error), so the same R comes with very different numbers of messages:

```
fetch_size |         R | messages |   fdw_ms | local_ms | extra_ms |   fit_ms | local_plan
       100 |     1,000 |       11 |     2.14 |     0.30 |     1.84 |     1.23 | Index Scan
       100 |    10,000 |      101 |    13.53 |     2.57 |    10.96 |    11.74 | Index Scan
       100 |   100,000 |    1,001 |   141.05 |    26.02 |   115.03 |   116.82 | Index Scan
       100 |   300,000 |    3,001 |   433.15 |    74.66 |   358.49 |   350.33 | Index Scan
       100 | 1,000,000 |   10,001 | 1,403.06 |   234.63 | 1,168.43 | 1,167.64 |   Seq Scan
     1,000 |     1,000 |        2 |     1.18 |     0.30 |     0.88 |     0.67 | Index Scan
     1,000 |    10,000 |       11 |     8.97 |     2.57 |     6.40 |     6.16 | Index Scan
     1,000 |   100,000 |      101 |    79.66 |    26.02 |    53.63 |    61.08 | Index Scan
     1,000 |   300,000 |      301 |   252.21 |    74.66 |   177.55 |   183.10 | Index Scan
     1,000 | 1,000,000 |    1,001 |   813.67 |   234.63 |   579.04 |   610.19 |   Seq Scan
    10,000 |     1,000 |        2 |     1.14 |     0.30 |     0.84 |     0.67 | Index Scan
    10,000 |    10,000 |        2 |     7.69 |     2.57 |     5.12 |     5.61 | Index Scan
    10,000 |   100,000 |       11 |    74.22 |    26.02 |    48.20 |    55.50 | Index Scan
    10,000 |   300,000 |       31 |   239.93 |    74.66 |   165.27 |   166.38 | Index Scan
    10,000 | 1,000,000 |      101 |   820.65 |   234.63 |   586.01 |   554.44 |   Seq Scan
fit: extra_ms = 0.06194 ms * messages + 0.54819 us * rows, R^2 = 0.99858
```

The model still uses the **real** fetch_size (100) when it predicts. At fetch_size 100 the two constants add up to 0.062/100 ms + 0.55 µs ≈ 1.17 µs per row, inside the range the failed fits agreed on.

**t_lookup_us.** Through postgres_fdw, `SELECT customer_id, name FROM customers WHERE customer_id = ANY ((SELECT keys)::int4[])` for n = 1000, 5000, 20000, 50000 random existing keys. The sub-SELECT makes postgres_fdw send the keys as a parameter (`Remote SQL: ... WHERE ((customer_id = ANY ($1::integer[])))`), like the benchmark. From the Foreign Scan time we subtract messages · per_message_ms and n · per_row_us, then fit a straight line:

```
     n | fdw_ms | messages | message_ms | row_ms | per_key_work_ms
 1,000 |   6.61 |       11 |       0.68 |   0.55 |            5.38
 5,000 |  29.35 |       51 |       3.16 |   2.74 |           23.45
20,000 |  80.80 |      201 |      12.45 |  10.96 |           57.38
50,000 | 299.56 |      501 |      31.03 |  27.41 |          241.12
fitted line: per_key_work_ms = -9.284 + n * 0.004796  ->  t_lookup_us = 4.7956, R^2 = 0.968
```

The points are not on a straight line: the work per key is about 5 µs at 1000, 2.9 µs at 20000 and 4.8 µs at 50000. B's own plan stays an Index Scan for every n. B's own time per key also jumps at 50000 (38 ms for 20000 keys, 158 ms for 50000), probably because 50,000 random rows touch more table pages than stay in shared_buffers. The fitted slope is an average over this range.

**t_build_us.** At site_a, `SELECT sjb_build_auto((SELECT keys), ...)` for n = 1000, 10000, 50000, 150000 random existing keys. The keys are passed through a sub-SELECT: with a constant array, PostgreSQL would compute the IMMUTABLE function once while *planning*, and the execution time would be 0. The cost model's inputs are realistic (customers statistics + the constants measured just before), so it builds the mode the real filter path would build:

```
      n | median_ms |  mode
  1,000 |      0.11 | exact
 10,000 |      2.50 | bloom
 50,000 |     13.98 | bloom
150,000 |     36.74 | bloom
fitted line: time_ms = 0.411 + n * 0.000245  ->  t_build_us = 0.245, R^2 = 0.997
```

All of it is saved in `results/calibration.json`: every point, every fitted line with its R², and a `"suggested"` block:

```json
"suggested": {
  "t_check_ns": 158.19, "t_lookup_us": 4.7956, "per_message_ms": 0.06194,
  "per_row_us": 0.54819, "t_build_us": 0.245, "fetch_size": 100
}
```

The site B measurements with and without parallel workers are still there. This time the parallel-allowed runs were slower (Bloom 231 vs 158 ms) with the **same** serial plan, so that difference is noise. `count(*)` still goes parallel (144 → 69 ms).

### Validation (`bench/validate_model.py`)

The comparison is now against the **parts the model covers**: the Foreign Scan node's time. The batch asked for "Foreign Scan time plus InitPlan time", but the InitPlan (which builds the key list or filter) already runs **inside** the Foreign Scan's time. postgres_fdw computes the parameter when it opens the remote cursor. We checked this with a `pg_sleep` in the InitPlan: the InitPlan took 1108 ms, and the Foreign Scan's first row came at 1114 ms. Adding the InitPlan again would count it twice, so it is printed as its own column. End to end (planning + whole query) is printed for reference. The constants were not changed to fit these numbers.

```
     n | n_keys |                  strategy | predicted_ms | covered_ms | ratio | initplan_ms | end_to_end_ms | e2e_ratio | pred_rows | remote_rows
   100 |    100 | exact_keys (nestloop off) |         0.69 |       1.09 |  1.58 |        0.08 |          1.72 |      2.49 |       100 |         100
   100 |    100 |   bloom_dynamic (b=27.73) |       158.49 |     152.53 |  0.96 |        0.08 |        153.40 |      0.97 |       102 |         109
 1,000 |    999 | exact_keys (nestloop off) |         6.33 |       6.23 |  0.98 |        0.47 |          7.49 |      1.18 |       999 |         999
 1,000 |    999 |   bloom_dynamic (b=22.93) |       160.04 |     151.60 |  0.95 |        0.64 |        153.03 |      0.96 |     1,015 |       1,016
10,000 |  9,955 | exact_keys (nestloop off) |        62.54 |      52.91 |  0.85 |        4.76 |         62.29 |      1.00 |     9,955 |       9,955
10,000 |  9,955 |   bloom_dynamic (b=18.13) |       175.49 |     202.76 |  1.16 |        7.04 |        212.10 |      1.21 |    10,119 |      10,108
50,000 | 48,738 | exact_keys (nestloop off) |       305.85 |     311.52 |  1.02 |       26.11 |        346.97 |      1.13 |    48,738 |      48,738
50,000 | 48,738 |   bloom_dynamic (b=14.74) |       242.10 |     311.17 |  1.29 |       32.12 |        359.93 |      1.49 |    49,537 |      49,480
```

Two more runs gave ratios of 0.76–1.08 for exact_keys (n ≥ 1000), 0.89–1.08 for Bloom at n ≤ 1000, 1.16–1.32 at n = 10000 and 1.19–1.29 at n = 50000.

Compared with Batch 5 (index path ≈ 3× too optimistic, Bloom up to 1.65), the model now predicts the **index path** well. The 1.58 at n = 100 is only 0.4 ms. The **filter path at larger n** is still 20–30% too optimistic, for these reasons:
- **The InitPlan does more than the model counts.** bloom_dynamic's InitPlan scans orders and sorts the keys twice (`array_agg(DISTINCT)` and `count(DISTINCT)`): 32 ms at n = 50000, while the model counts only `sjb_build_auto`'s 12 ms. The index path's InitPlan (26 ms, scanning orders and building the key list) is not in the model either; its ratio near 1 at n = 50000 is within the run-to-run noise, so we cannot tell from these runs what absorbs it.
- **t_check_ns depends a little on the filter size.** A larger filter fits worse in the CPU cache: we measured 160–168 ns for 3–90 KB filters and 184 ns for a 223 KB one, against the calibrated 158 ns.
- **t_check_ns itself varies by ±15% between runs** (150–188 ns), which is ±25 ms on 1,000,000 rows. That is the same size as the remaining gap.

We do not tune t_check_ns to this. A larger, averaged calibration of t_check_ns (several filter sizes, more runs) would be the honest fix.

### Break-even: from how many keys is the filter path faster?

`queries/choice_demo.sql` Step 5 and `bench/break_even.py`. Predicted = the smallest n for which `sjb_plan_choice` says `filter` (steps of 500; the choice switches only once on both networks). Measured = exact_keys (nestloop off) against the real filter path (`auto_filter`, `sjb_build_auto`, which built BLOOM at every point) for 10,000–200,000 orders, with the crossing found by a straight line between the two points around it. 1 Mbit/s cannot be measured on localhost (that needs sudo to slow down the network), so it is **simulated**: measured time + measured bytes / 125,000 bytes per second.

| network | predicted | measured (Foreign Scan) | measured (end to end) |
|---|---:|---:|---:|
| 1000 Mbit/s | 35,000 keys | 65,263 keys | 74,772 keys |
| 1 Mbit/s (simulated) | 8,500 keys | 11,837 keys | 11,964 keys |

```
1000 Mbit/s:  n_keys | pred_index | pred_filter | index_covered | filter_covered
               9,955 |      62.54 |      175.49 |         44.57 |         231.65
              48,738 |     305.85 |      242.10 |        270.05 |         303.42
              72,226 |     453.21 |      282.38 |        348.23 |         334.17
             139,294 |     873.91 |      397.14 |        585.29 |         456.59
             181,329 |   1,137.66 |      469.02 |        848.30 |         556.21
```

- **1 Mbit/s:** the bytes dominate, and the model is close (8,500 vs about 11,800 keys).
- **1000 Mbit/s:** the CPU terms decide, and the model now switches to the filter path **too early** (35,000 vs about 65,000). Two errors push the same way. The filter path is predicted 20–30% too low (see above). And the straight t_lookup line, fitted on 1,000–50,000 keys where the last step is steep, overestimates the index path beyond 50,000 keys (1138 ms predicted vs 848 ms at 181,329 keys).
- **Compared with Batch 5:** the old model put the break-even near 110,000 keys against about 60,000 measured, so it erred the other way and by a similar factor. Now the network term is right, and the error comes from the fact that CPU time per key and per row is not a single constant on this machine.

### How to test it

```bash
source setup/env.example.sh
make -C sjbloom && make -C sjbloom install
for db in site_a site_b; do psql -d $db -c "DROP EXTENSION sjbloom CASCADE" -c "CREATE EXTENSION sjbloom"; done
make -C sjbloom installcheck                       # 1 test, ok
.venv/bin/python bench/calibrate.py                # about 50 s, writes results/calibration.json
.venv/bin/python bench/validate_model.py           # about 5 s
.venv/bin/python bench/break_even.py               # about 30 s
psql -X -d site_a -f queries/choice_demo.sql       # four reports + break-even
psql -X -d site_a -f queries/auto_manual.sql       # correctness, all counts 0
```

After calibrating on another machine, pass your values to the functions (the last four arguments), or change the defaults in `sjbloom--1.0.sql` and recreate the extension.

Side effect worth knowing: with the new costs, `auto_manual.sql` at n = 100 now builds **EXACT** instead of BLOOM. Bloom's ~2 false positives make 102 rows back, which needs a third FETCH round trip (0.062 ms). That costs more than the 108 bytes Bloom saves (about 1 µs at 1000 Mbit/s). At n = 10000 and 150000 it still builds BLOOM.

## Batch 6: automatic two-path semijoin rewrite inside postgres_fdw

Until Batch 5 the filter had to be written by hand into every query (`queries/bloom_manual.sql`, `auto_manual.sql`). In Batch 6 **postgres_fdw does it by itself**. You write the plain join:

```sql
SELECT o.order_id, c.name
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
WHERE o.order_id <= 1000;
```

A **planner hook** inside postgres_fdw sees this query before PostgreSQL plans it, estimates how many distinct keys the local rows will have, asks sjbloom's cost model (`sjb_plan_choice`) for the cheaper path, and adds ONE condition on the foreign key to `WHERE`. The join itself is not touched. The new condition only removes customers that cannot join, so the result is always exactly the same.

### Where the code is, and how a teammate gets it

- The change lives in the PostgreSQL source tree `~/postgres/postgresql`, on branch **`cs631-semijoin`**. Only files under `contrib/postgres_fdw` are changed.
- **Base commit: `a85b5523e6`** on `REL_18_STABLE`, which is REL_18_6 plus 156 commits. Since the 18.6 tag, the only change to postgres_fdw is a 2-line typo fix (`3a09e91a49`, in `postgresAcquireSampleRowsFunc`), far away from our code.
- The whole change is exported as **`patches/postgres_fdw-semijoin.patch`**. We checked that it applies cleanly to plain 18.6 (`git apply --check` in a worktree at `REL_18_6`: no errors).

How a teammate applies it to their own PostgreSQL 18 source tree, which must be the one their server was built from:

```bash
cd ~/postgres/postgresql                      # your source tree
git switch -c cs631-semijoin                  # optional: keep it on its own branch
git apply --check /path/to/postgre-project/patches/postgres_fdw-semijoin.patch   # dry run: no output = ok
git apply /path/to/postgre-project/patches/postgres_fdw-semijoin.patch
make -C contrib/postgres_fdw && make -C contrib/postgres_fdw install            # no sudo if installed in $HOME
```

Then run `setup/setup_all.sh` (it now has a step 6, see below), or, on an existing setup, run `setup/enable_semijoin.sql` once in site_a and reconnect.

To get the original postgres_fdw back, do one of these:
- `git switch REL_18_STABLE` in the source tree, then rebuild and install;
- or copy back the backup of the original library: `cp ~/postgres_fdw.so.orig ~/postgres/postgresql/install/lib/postgres_fdw.so`. With the original library, also remove the option: `ALTER SERVER site_b_srv OPTIONS (DROP semijoin_filter)`.

### Files

**`contrib/postgres_fdw/option.c`** (changed):
- Ten new settings (GUCs), all `PGC_USERSET`, so every user can `SET` them in their session:

  | GUC | default | meaning |
  |---|---|---|
  | `postgres_fdw.enable_semijoin` | on | the whole feature on/off |
  | `postgres_fdw.semijoin_force_path` | auto | `auto` (cost model), `index` or `filter`; for tests and demos |
  | `postgres_fdw.semijoin_verbose` | off | a NOTICE that explains every decision |
  | `postgres_fdw.semijoin_network_mbit` | 1000 | bandwidth between the sites |
  | `postgres_fdw.semijoin_max_filter_kb` | 1024 (kB) | largest filter that `sjb_build_auto` may send |
  | `postgres_fdw.semijoin_t_check_ns` | 158.19 | from results/calibration.json |
  | `postgres_fdw.semijoin_t_lookup_us` | 4.7956 | from results/calibration.json |
  | `postgres_fdw.semijoin_per_message_ms` | 0.06194 | from results/calibration.json |
  | `postgres_fdw.semijoin_per_row_us` | 0.54819 | from results/calibration.json |
  | `postgres_fdw.semijoin_t_build_us` | 0.245 | from results/calibration.json |

- A new boolean option **`semijoin_filter`** for a server or a foreign table, default false (opt-in). As with `use_remote_estimate`, a value set on the table wins over the server's value.
- At the end of `_PG_init`, the old `planner_hook` is saved in `prev_planner_hook` and ours is installed.

**`contrib/postgres_fdw/postgres_fdw.h`** (changed): `extern` declarations of the GUC variables, the three `SEMIJOIN_PATH_*` values, `prev_planner_hook` and `pgfdw_semijoin_planner`. The hook's signature is copied from `planner_hook_type` in `src/include/optimizer/planner.h`.

**`contrib/postgres_fdw/semijoin.c`** (new): the hook and all its helpers (see the next section).

**`contrib/postgres_fdw/Makefile`, `meson.build`**: `semijoin.o` / `semijoin.c` added.

**`setup/enable_semijoin.sql`** (new) and **`setup/setup_all.sh`** (new step 6): `ALTER DATABASE site_a SET session_preload_libraries = 'postgres_fdw'` and `ALTER SERVER site_b_srv OPTIONS (ADD semijoin_filter 'true')`. Before this, `setup_all.sh` dropped site_a and lost both settings.

**`queries/correctness_auto.sql`, `correctness_auto_one.sql`** (new): the correctness test of the automatic rewrite.

**`patches/postgres_fdw-semijoin.patch`** (new): the exported change.

### The hook, step by step (`semijoin.c`)

`pgfdw_semijoin_planner` is called for **every** query that PostgreSQL plans. A static flag `in_semijoin_hook` makes sure it never starts its own work again while it is already running. At the end it always calls `prev_planner_hook` (if another module had one) or `standard_planner`.

1. **`find_candidate`**: is this query safe to rewrite? Everything below must hold, otherwise nothing changes:
   - a plain `SELECT`: no UNION/INTERSECT/EXCEPT, no `FOR UPDATE`, no data-modifying `WITH`;
   - the range table has exactly one local table (`RELKIND_RELATION`, without row-level security or TABLESAMPLE) and one foreign table. JOIN entries and PostgreSQL 18's GROUP entry are ignored, so `GROUP BY` works;
   - the foreign table uses postgres_fdw, `semijoin_filter = true`, the server's `extensions` option lists `sjbloom`, and sjbloom is installed in site_a;
   - only INNER joins; exactly one condition `local int4 = foreign int4`; no other condition uses both tables; no `sjb_contains` yet;
   - the conditions on the local table contain no volatile function (such as `random()`), no subquery and no parameter (`$1`). **Why the volatile check matters:** the rewrite reads the local table a second time, in the sub-SELECT that collects the keys. A volatile condition can pick different rows on each read. The key list could then miss a key of a row that the main query keeps, and that result row would be lost. TABLESAMPLE is rejected for the same reason.
2. **`collect_remote_stats`**: everything comes from site A's own statistics of the foreign table, which `ANALYZE customers` at site_a fetched from site B, so no network call is needed:
   - `s_rows` = `reltuples` of the foreign table. If it is -1 (never analyzed), there is no rewrite, and a NOTICE "run ANALYZE customers at site_a" is printed even with verbose off;
   - `D_remote` = `n_distinct` of the foreign join column (a negative value means a fraction of `reltuples`), and `rows_per_key = s_rows / D_remote`;
   - `row_width` on the wire = the text widths of the foreign columns the query uses (SELECT list, conditions, GROUP BY, HAVING, plus the join column), + 7 + 4 · number of columns. An int4 counts as 7, because ids travel as text; other types use `pg_stats.avg_width`;
   - `fetch_size` from the table or server option (default 100).
3. **`estimate_local_keys`**: builds the SQL text `SELECT o.customer_id FROM public.orders o WHERE o.customer_id IS NOT NULL AND (<local conditions>)`. The local conditions are turned back into text with `deparse_expression` after `ChangeVarNodes`. The text is parsed with `raw_parser` + `parse_analyze_fixedparams` and planned with **`standard_planner` directly** (`planner()` would call our hook again). `r` = `plan_rows` of that plan. Then Cardenas' formula (below) turns r into n_est.
4. **`decide_path`**: if `semijoin_force_path` is `index` or `filter`, that path is used. Otherwise `sjb_plan_choice(n_est, s_rows, rows_per_key, row_width, network_mbit, t_check_ns, t_lookup_us, per_message_ms, per_row_us, t_build_us, fetch_size)` is called directly through fmgr (`LookupFuncName` + a `FunctionCallInfo`, no SPI). The decision lives only in sjbloom's `costmodel.c`.
5. **The new condition**: the sub-SELECT is built as SQL text, parsed, and wrapped in a `SubLink` of type `EXPR_SUBLINK`. Then it is added with `make_and_qual` to `WHERE`, and `hasSubLinks` is set:
   - index path: a `ScalarArrayOpExpr` (`useOr = true`, operator int4 `=`), meaning `c.customer_id = ANY ((SELECT array_agg(DISTINCT o.customer_id) FROM ...))`;
   - filter path: a call `sjb_contains((SELECT public.sjb_build_auto(array_agg(o.customer_id), <s_rows>, ...) FROM ...), c.customer_id)`. There is no DISTINCT, because `sjb_build_auto` sorts and removes repeats itself. The numbers are printed into the SQL text with `%.17g`, so nothing is rounded.

   The sub-SELECT uses nothing from the outer query, so the planner makes it an **InitPlan** that runs once. postgres_fdw sends its result to site B as the parameter `$1`.

Steps 2–5 run inside a **subtransaction** (`try_plan_semijoin`). If anything goes wrong there, the error is caught, the subtransaction is rolled back and the query is planned unchanged. Our feature never makes a query fail. Only a user's cancel (Ctrl-C) is passed on.

**Why `session_preload_libraries`?** postgres_fdw.so is normally loaded lazily, in the middle of planning the first query that touches a foreign table. Our hook is installed when the library is loaded, which for that first query is too late. With `session_preload_libraries = 'postgres_fdw'`, the library is loaded when the session starts, before any query is planned.

### Before and after

The plain query above at n = 1000, with `SET postgres_fdw.enable_semijoin = off` (the original postgres_fdw):

```
Hash Join
  Hash Cond: (c.customer_id = o.customer_id)
  ->  Foreign Scan on public.customers c          actual rows=1000000
        Remote SQL: SELECT customer_id, name FROM public.customers
  ...
Execution Time: 1464.644 ms
```

The same query with the rewrite on (the default). The NOTICE comes from `SET postgres_fdw.semijoin_verbose = on`:

```
NOTICE:  postgres_fdw semijoin: index path (cost model): n_est=949.3 (r=952, D_local=168392), s_rows=1000000, rows_per_key=1.000, row_width=33.00 (2 columns), fetch_size=100
DETAIL:  cost model winner: index path (6.058 ms <= 159.931 ms for filter path with bloom)
sub-SELECT: SELECT array_agg(DISTINCT o.customer_id) FROM public.orders o WHERE o.customer_id IS NOT NULL AND ((o.order_id <= 1000))

Nested Loop                                        actual rows=1000
  Join Filter: (o.customer_id = c.customer_id)
  InitPlan 1
    ->  Aggregate  Output: array_agg(DISTINCT o_1.customer_id) ...
  ->  Index Scan using orders_pkey on public.orders o
  ->  Materialize
        ->  Foreign Scan on public.customers c   rows=10 (estimate)  actual rows=999
              Remote SQL: SELECT customer_id, name FROM public.customers WHERE ((customer_id = ANY ($1::integer[])))
Execution Time: 263.485 ms
```

At n = 100000 the cost model picks the filter path, and `sjb_build_auto` builds a Bloom filter when the query runs:

```
NOTICE:  postgres_fdw semijoin: filter path (cost model): n_est=75930.5 (r=100951, D_local=168392), ...
DETAIL:  cost model winner: filter path with bloom (289.421 ms < 477.191 ms for index path)
  ->  Foreign Scan on public.customers c   actual rows=96509
        Remote SQL: SELECT customer_id, name FROM public.customers WHERE (public.sjb_contains($1::bytea, customer_id))
```

### Cardenas' formula: how many distinct keys will we send?

*(Batch 6b replaced this formula with PostgreSQL's own, see below. This section shows what Batch 6 did.)*

The planner only estimates **rows** (r), but the filter and the key list depend on **distinct keys** (n). Cardenas (A. F. Cardenas, "Analysis and Performance of Inverted Data Base Structures", CACM 18(5), 1975) gives the expected number of distinct values when r rows are picked from a column with D distinct values:

```
n = D * (1 - (1 - 1/D)^r)
```

In plain words: one picked row misses a given value with probability (1 - 1/D), so all r rows miss it with probability (1 - 1/D)^r. Every one of the D values is found with probability 1 - (1 - 1/D)^r.

**Worked example (n = 1000):** `D_local` = n_distinct of orders.customer_id = 168,392 (ANALYZE's estimate), and `r` = 952 (the planner's estimate for `order_id <= 1000`).
- (1 - 1/168392)^952 = e^(952 · ln(1 - 1/168392)) ≈ e^(-0.0056535) ≈ 0.994363
- n_est = 168392 · (1 - 0.994363) = 168392 · 0.005637 ≈ **949.3**. The true number is 999.

The same formula is a rougher estimate for big n:

| n (order_id <=) | r (planner) | n_est (Cardenas) | true distinct keys | path (auto) | join above the Foreign Scan |
|---|---|---|---|---|---|
| 100 | 95 | 95.0 | 100 | index | Nested Loop + Join Filter |
| 1000 | 952 | 949.3 | 999 | index | Nested Loop + Join Filter |
| 10000 | 10351 | 10039.3 | 9955 | index | Hash Join |
| 100000 | 100951 | 75930.5 | 95017 | filter (bloom) | Hash Join |
| 150000 | 150166 | 99362.9 | 139294 | filter (bloom) | Hash Join |

At n = 150000 the estimate is 29% too low, for two reasons. ANALYZE's D (168,392) is below the true 181,329. And Cardenas assumes the r rows pick values "with replacement", whereas we take r of only 200,000 rows, most of which have their own key. Near the whole table this finds almost all keys, while the formula says only 1 - e^(-r/D) of them. Here the error does not change the decision (the filter path wins either way), and it never affects correctness. Batch 7 can look at a formula for picking without replacement (e.g. Yao, 1977).

**Nested Loop at n ≤ 1000:** the planner guesses 10 rows for `customer_id = ANY ($1)` (the real number is 999), so it puts a Nested Loop with a Join Filter above the Foreign Scan (1000 × 999 comparisons). This is the same estimate problem as in Batch 4. It is recorded here, and Batch 7 will fix the estimates.

### Known limitations (version 1)

- Queries with parameters in local conditions (`$1` of a prepared statement, PL/pgSQL variables) are not rewritten.
- ~~`sjb_plan_choice` has no max_filter_bytes argument and always assumes 1 MB.~~ Fixed in Batch 6b.
- A foreign column that is used only in a condition shipped to site B (e.g. `c.city = 'X'`) is counted in `row_width`, although postgres_fdw does not fetch it. The width is then a little too high.

### How to test it

```bash
source setup/env.example.sh                     # PGUSER=postgres: the user mapping is for postgres
make -C ~/postgres/postgresql/contrib/postgres_fdw && make -C ~/postgres/postgresql/contrib/postgres_fdw install
psql -X -d site_a -f setup/enable_semijoin.sql  # only on an existing setup; setup_all.sh does it as step 6
psql -X -d site_a -f queries/correctness_auto.sql   # about 50 s; every off_except_on / on_except_off = 0
```

`correctness_auto.sql` runs a join query and a GROUP BY query (`c.city, count(*), sum(o.amount)`) for n = 100, 10000 and 150000, each with `semijoin_force_path` = auto, index and filter. Each query runs into a temp table with the rewrite off and again with it on, and the two tables are compared with EXCEPT ALL in both directions. (A single `query EXCEPT ALL query` is a set operation, which the hook never rewrites.) All 18 comparisons gave 0 and 0.

To see a decision, run `SET postgres_fdw.semijoin_verbose = on;` and then `EXPLAIN (VERBOSE)` of the plain query. To compare with the original behaviour, run `SET postgres_fdw.enable_semijoin = off;`.

## Batch 6b: safety fixes and a better key estimate

Four small fixes to `contrib/postgres_fdw/semijoin.c` (branch `cs631-semijoin`, commit `2c357700f4`) and one to sjbloom. After that, postgres_fdw's own regression suite was run.

### 1. The query is never left half-rewritten

The hook catches errors (see Batch 6). That is only correct if a caught error leaves the user's query **truly unchanged**. A query changed halfway, for example with the condition added but `hasSubLinks` not set, could get a wrong plan or crash the planner. We checked every place that writes to the Query:

- steps 2–5 only **read** it. The local conditions are copied with `copyObject` before their Vars are renumbered (`ChangeVarNodes` works on the copy). The foreign Var is copied into the new condition. The sub-SELECTs are new Query trees parsed from text;
- the only writes are in the new function **`attach_condition`**, the very last step. It runs only after `try_plan_semijoin` has finished successfully. It first builds the new WHERE completely (`make_and_qual`) and then stores it with two plain assignments (`quals` and `hasSubLinks = true`), which cannot fail.

So the code already followed rule (b): attaching is the last step and nothing after it can fail. Batch 6b makes this explicit with its own function and a comment that explains why.

### 2. Parallel mode

The hook now does nothing when `IsInParallelMode()`, for example in a parallel worker that plans a query inside a function, so it never starts a subtransaction there. Before, the check was inside `try_plan_semijoin`. A detail: PostgreSQL 18 does not forbid every subtransaction during a parallel operation. `BeginInternalSubTransaction` (src/backend/access/transam/xact.c) says: *"It's permissible to start and end "internal" subtransactions while in parallel mode, so long as no new XIDs or command IDs are assigned."* We cannot promise that for parse analysis and planning, and breaking the rule would be an error we could not hide, so we skip the rewrite.

### 3. The distinct-key estimate: PostgreSQL's own formula

Cardenas' formula (Batch 6) was 29% too low at n = 150000. Now `estimate_local_keys` uses the formula from `estimate_num_groups()` in `src/backend/utils/adt/selfuncs.c`, the one PostgreSQL uses for its own GROUP BY / DISTINCT estimates:

```
n_est = D * (1 - ((N - r) / N) ^ (N / D))
N = local reltuples, r = estimated rows after WHERE, D = local n_distinct
```

This is the explanation from selfuncs.c's comment, quoted exactly (their n is our D, their p is our r):

> Given a table containing N rows with n distinct values in a uniform distribution, if we select p rows at random then the expected number of distinct values selected is
>
>     n * (1 - product((N-N/n-i)/(N-i), i=0..p-1))
>
>     = n * (1 - (N-N/n)! / (N-N/n-p)! * (N-p)! / N!)
>
> See "Approximating block accesses in database organizations", S. B. Yao, Communications of the ACM, Volume 20 Issue 4, April 1977 Pages 260-261.
>
> Alternatively, re-arranging the terms from the factorials, this may be written as
>
>     n * (1 - product((N-p-i)/(N-i), i=0..N/n-1))
>
> This form of the formula is more efficient to compute in the common case where p is larger than N/n.  Additionally, as pointed out by Dell'Era, if i << N for all terms in the product, it can be approximated by
>
>     n * (1 - ((N-p)/N)^(N/n))
>
> See "Expected distinct values when selecting from a bag without replacement", Alberto Dell'Era, http://www.adellera.it/investigations/distinct_balls/.
>
> The condition i << N is equivalent to n >> 1, so this is a good approximation when the number of distinct values in the table is large.  It turns out that this formula also works well even when n is small.

In plain words: each of the D values occurs about N/D times in the table. We pick r of the N rows **without replacement**. A value is missed only if all of its N/D rows are among the N − r rows we did not pick, which happens with probability about ((N − r)/N)^(N/D). Cardenas' formula assumed picking *with* replacement from D equally likely values. That is wrong when r is a large part of the table: when r = N, Cardenas still says only 1 − e^(−N/D) of the values, while this formula gives all D. Like PostgreSQL, we clamp D to N and use D itself when r ≥ N.

**Worked example (n = 150000):** N = 200,000, r = 150,166, D = 168,392.
- (N − r)/N = 49,834 / 200,000 = 0.24917, and N/D = 1.18771
- 0.24917^1.18771 = e^(1.18771 · ln 0.24917) = e^(1.18771 · (−1.38962)) = e^(−1.65047) ≈ 0.19196
- n_est = 168,392 · (1 − 0.19196) ≈ **136,067**. The true number is 139,294.

Old vs new vs true (r from the planner, N and D from the statistics of orders):

| n (order_id <=) | r | Cardenas (Batch 6) | PostgreSQL formula (6b) | true distinct keys |
|---|---|---|---|---|
| 100 | 95 | 95.0 | 95.0 | 100 |
| 1000 | 952 | 949.3 | 951.6 | 999 |
| 10000 | 10351 | 10039.3 | 10300.0 | 9955 |
| 100000 | 100951 | 75930.5 | 95302.0 | 95017 |
| 150000 | 150166 | 99362.9 | 136067.2 | 139294 |

The remaining error comes mostly from r (the planner's row estimate) and from D: ANALYZE's n_distinct is 168,392, against 181,329 true. The paths chosen did not change: index for n ≤ 10000, filter (Bloom) for n ≥ 100000.

### 4. One filter-size limit for both decisions

`sjb_plan_choice` and `sjb_choice_report` (sjbloom) have a new last argument **`max_filter_bytes int8 DEFAULT 1048576`**, the same limit `sjb_build_auto` already had. Existing calls keep working because of the default. The hook passes `postgres_fdw.semijoin_max_filter_kb * 1024` to both the plan-time choice and the run-time `sjb_build_auto` (one helper, `get_max_filter_bytes`). The report's inputs line now shows `max_filter_bytes`. sjbloom's regression test has a new Step 20: 200,000 keys at 1 Mbit/s give `filter` with the default limit and `index` with a 64 KB limit. sjbloom must be recreated in both sites (`DROP EXTENSION sjbloom CASCADE; CREATE EXTENSION sjbloom;`). With an old sjbloom, the hook cannot find the 12-argument function and leaves the query unchanged.

A case where the limit changes the path (n = 100000):

```
SET postgres_fdw.semijoin_max_filter_kb = 1024;   -- default
NOTICE:  ... filter path (cost model): n_est=95302.0 ..., max_filter_bytes=1048576
DETAIL:  cost model winner: filter path with bloom (322.786 ms < 598.923 ms for index path)
         Remote SQL: SELECT customer_id, name FROM public.customers WHERE (public.sjb_contains($1::bytea, customer_id))

SET postgres_fdw.semijoin_max_filter_kb = 64;
NOTICE:  ... index path (cost model): n_est=95302.0 ..., max_filter_bytes=65536
DETAIL:  cost model winner: index path (598.923 ms <= 1613.191 ms for filter path with pass)
         Remote SQL: SELECT customer_id, name FROM public.customers WHERE ((customer_id = ANY ($1::integer[])))
```

The Bloom filter for about 95,000 keys is larger than 64 KB, and so is the EXACT list. Only PASS is left on the filter path, and PASS returns all 1,000,000 rows.

### 5. postgres_fdw's own regression suite

```
make -C ~/postgres/postgresql/contrib/postgres_fdw installcheck
ok 1         - postgres_fdw                             3297 ms
ok 2         - query_cancel                               22 ms
# All 2 tests passed.
ok 1         - eval_plan_qual                            109 ms
# All 1 tests passed.
TAP tests not enabled. Try configuring with --enable-tap-tests
```

All original tests pass with our hook installed and the feature on by default, because the test servers do not set `semijoin_filter`. The expected files were not touched. The TAP tests did not run, because this source tree was configured without `--enable-tap-tests`. installcheck leaves its database `contrib_regression` on the server; it can be dropped.

### How to test it

```bash
source setup/env.example.sh
make -C sjbloom && make -C sjbloom install
for db in site_a site_b; do psql -X -d $db -c "DROP EXTENSION sjbloom CASCADE" -c "CREATE EXTENSION sjbloom"; done
make -C sjbloom installcheck                                          # 1 test, ok
make -C ~/postgres/postgresql/contrib/postgres_fdw && make -C ~/postgres/postgresql/contrib/postgres_fdw install
make -C ~/postgres/postgresql/contrib/postgres_fdw installcheck       # 3 tests, ok
psql -X -d site_a -f queries/correctness_auto.sql                     # 18 x (0, 0)
```

The patch was re-exported (`patches/postgres_fdw-semijoin.patch`, base `a85b5523e6`). `git apply --check` against plain `REL_18_6` again reports no errors.

## Where the code lives (Batch 6 and later)

- **postgres_fdw changes:** `~/postgres/postgresql/contrib/postgres_fdw`, on branch **`cs631-semijoin`** of the PostgreSQL source tree, which branches off at base commit `a85b5523e6`. The changes are **only visible on that branch**. On `REL_18_STABLE` the same folder holds the original code. The changed files are `semijoin.c` (new), `option.c`, `postgres_fdw.c`, `postgres_fdw.h`, `Makefile` and `meson.build`, plus the test files `sql/semijoin.sql` and `expected/semijoin.out`.
- **The installed library:** `~/postgres/postgresql/install/lib/postgres_fdw.so`. It is built from that branch. The original library is backed up at **`~/postgres_fdw.so.orig`**.
- **The project repo** (this repository) holds the whole change as one file, `patches/postgres_fdw-semijoin.patch`. The sjbloom functions it calls are in `sjbloom/`.

View the code:

```bash
cd ~/postgres/postgresql
git switch cs631-semijoin                                   # the branch with our changes
git log --oneline a85b5523e6..cs631-semijoin                # our commits
git diff a85b5523e6..cs631-semijoin --stat -- contrib/postgres_fdw
git diff a85b5523e6..cs631-semijoin -- contrib/postgres_fdw  # the full change (= the patch file)
less contrib/postgres_fdw/semijoin.c
```

Restore the original postgres_fdw:

```bash
# quickest: copy the backup back (the server loads it in new sessions)
cp ~/postgres_fdw.so.orig ~/postgres/postgresql/install/lib/postgres_fdw.so

# or rebuild the original from source
cd ~/postgres/postgresql && git switch REL_18_STABLE
make -C contrib/postgres_fdw clean && make -C contrib/postgres_fdw && make -C contrib/postgres_fdw install
```

The original postgres_fdw does not know the option `semijoin_filter`, so remove it as well: `psql -d site_a -c "ALTER SERVER site_b_srv OPTIONS (DROP semijoin_filter)"`. To go back to our version, `git switch cs631-semijoin` and rebuild and install.

## Batch 7: row estimates, EXPLAIN output, binary filter, regression test

Batch 6 rewrote the query, but the planner still did not know what the rewrite does. It guessed **10 rows** for the Foreign Scan with `customer_id = ANY ($1)`, and 1/3 of the table (333,333) for `sjb_contains($1, customer_id)`. At n ≤ 1000 it therefore picked a Nested Loop with a Join Filter. Batch 7 passes the cost model's knowledge on to the planner and to EXPLAIN. It also adds an optional binary filter and our own regression test. Commit `2c34d26d89` on `cs631-semijoin`.

### How the hint travels: from the hook to the row estimate and to EXPLAIN

```
pgfdw_semijoin_planner (our hook)
 |  1. rewrite the query (Batch 6), then fill_hint():
 |       expected_rows = sjb_expected_remote_rows(..., path)
 |       index_ms, filter_ms = sjb_predicted_ms(..., 'index' / 'filter')
 |  2. pgfdw_semijoin_hint = &my_hint        (only now)
 |  3. standard_planner(query)
 |       |-- postgresGetForeignRelSize
 |       |     set_baserel_size_estimates()   -> planner guess: 10 rows
 |       |     pgfdw_apply_semijoin_hint()     -> baserel->rows = expected rows
 |       |     estimate_path_cost_size()       -> fpinfo->rows, retrieved_rows, costs
 |       |                                        all follow from baserel->rows
 |       |-- join planning: now sees 952 rows, picks a Hash Join
 |       '-- postgresGetForeignPlan
 |             pgfdw_semijoin_plan_info()     -> copies path, n_est, both times and
 |                                               the parameter number into the plan
 |                                               (fdw_private position 4)
 |  4. PG_FINALLY: pgfdw_semijoin_hint = previous value (NULL)
 v
executor:  postgresBeginForeignScan  reads fdw_private[4]
           create_cursor             after the parameters are computed:
                                     pgfdw_semijoin_decode_param() reads the
                                     filter header / counts the keys sent
EXPLAIN:   postgresExplainForeignScan -> pgfdw_semijoin_explain()
```

- **The hint** (`SemijoinHint` in postgres_fdw.h) holds the foreign table's OID, its join column, sjb_contains's OID, the path, n_est, the expected rows and both predicted times. The global pointer `pgfdw_semijoin_hint` points to it **only while that one query is planned**. The hook sets it just before calling the planner and puts the old value back in `PG_FINALLY`, also after an error. A query planned inside another one, or the next query, never sees it. A query that is not rewritten sets it to NULL.
- **The row estimate.** `pgfdw_apply_semijoin_hint` is one new line in `postgresGetForeignRelSize`, right after PostgreSQL's own estimate. It does something only if the hint's OID matches, `root->query_level == 1` (the top-level query, never a subquery), and our own condition is found among the table's conditions. It sets `baserel->rows = expected_rows × selectivity of the other conditions`. For example, `c.city = 'X'` still lowers the estimate. postgres_fdw derives everything else (fpinfo->rows, retrieved_rows, per-row costs) from baserel->rows. The `use_remote_estimate` branch is not changed. With verbose on, it prints `semijoin: foreign scan rows 10 -> 952`.
- **sjbloom** has two new functions with the same 12 arguments as `sjb_plan_choice` plus a path: `sjb_expected_remote_rows` (index: M; filter: the rows back of the mode the model would pick) and `sjb_predicted_ms`. Both use `sjb_estimate_all`, so the logic stays in one place (regression Step 21).
- **The plan.** EXPLAIN runs after planning, when the hint is gone. So `postgresGetForeignPlan` copies the values into the ForeignScan's `fdw_private` as a new item, `FdwScanPrivateSemijoin` (position 4). A plain table scan has no Relations item at position 3, so a NULL placeholder goes there and no existing position moves. EXPLAIN used to treat "more than 3 items" as a join. That one line now also checks that position 3 is not NULL.
- **What was sent.** The parameter values are built in short-lived memory, so `create_cursor` decodes ours right away, into a fixed-size buffer in `PgFdwScanState`. For a filter it reads the 24-byte header (version, mode, k, nkeys, m) from the hex text with memcpy. For a key list it counts the elements of `{...}`. If anything looks wrong it shows "unavailable"; it never raises an error.

### Results: row estimate before and after

| n | join before | join after | Foreign Scan est. before → after | actual rows | time before | time after |
|---|---|---|---|---|---|---|
| 100 | Nested Loop | Nested Loop | 10 → 95 | 100 | 2.2 ms | 2.5 ms |
| 1000 | Nested Loop | **Hash Join** | 10 → 952 | 999 | **117.4 ms** | **6.7 ms** |
| 10000 | Hash Join | Hash Join | 10 → 10300 | 9955 | 55.2 ms | 60.1 ms |
| 100000 | Hash Join | Hash Join | 333333 → 96807 | 96509 | 504 ms | 515–539 ms (5 runs; same plan) |

At n = 100 the planner still picks a Nested Loop. The reason is the **join** estimate (rows=1 vs 100), which comes from PostgreSQL's own join selectivity (n_distinct of customers.customer_id = 1M). With `enable_nestloop = off` a Hash Join is just as fast there (about 5 ms each), so it does no harm. Changing join estimates would need another hook; it is not done.

**Known limitation:** the path decision (index vs filter) ignores extra conditions on the foreign table alone, such as `c.city = 'X'`. Only the row estimate accounts for them. Both paths are affected about equally, so the choice rarely changes.

### EXPLAIN output

Plain EXPLAIN, index path (n = 1000):

```
   ->  Foreign Scan on customers c  (cost=100.00..39956.92 rows=952 width=15)
         Semijoin Path: index
         Semijoin Estimated Keys: 952
         Semijoin Predicted Time: index=6.071 ms filter=159.934 ms
```

EXPLAIN ANALYZE adds what was really sent:

```
         Remote SQL: SELECT customer_id, name FROM public.customers WHERE ((customer_id = ANY ($1::integer[])))
         Semijoin Keys Sent: 999 bytes_sent=6887

         Remote SQL: SELECT customer_id, name FROM public.customers WHERE (public.sjb_contains($1::bytea, customer_id))
         Semijoin Path: filter
         Semijoin Estimated Keys: 95302
         Semijoin Predicted Time: index=598.923 ms filter=322.786 ms
         Semijoin Filter: mode=bloom keys=95017 m=1266314 k=9 bytes_sent=316630
```

The filter really holds 95,017 keys (the true number; the estimate was 95,302). Its size checks out: 2 · (24 + ⌈1266314/8⌉) + 2 = 316,630 hex characters.

### Binary filter (`postgres_fdw.semijoin_binary_filter`, default off)

postgres_fdw normally sends every parameter as text, so a bytea filter travels as hex (2 characters per byte). With this setting on, every **bytea** parameter of a foreign scan is sent in binary instead: format code 1, the raw bytes and their length, passed to `PQsendQueryParams` through the new arrays `param_formats` and `param_lengths` in `PgFdwScanState`. Everything else is unchanged; with the setting off both arrays are NULL, as before. The hook then passes `wire_factor = 1` to the cost functions and to `sjb_build_auto`. All four report-style sjbloom functions have a new last argument, `wire_factor float8 DEFAULT 2` (regression Step 22).

At n = 100000: hex text 316,630 bytes, binary **175,447 bytes** (55%). It is not exactly half because with wire_factor 1 the cost model picks a slightly larger Bloom filter (m = 1,403,381, k = 10, fewer false positives). The results are identical (EXCEPT ALL 0/0), and postgres_fdw's own suite passes with the setting at its default.

### Our regression test: `contrib/postgres_fdw/sql/semijoin.sql`

A loopback server to the test database plays site B (the same style as postgres_fdw's own test). It uses 2,000 "remote" customers and 300 local orders, some with NULL keys and some with keys that have no customer. Step 0 SETs every cost constant and the network speed to fixed values, so the expected output never depends on a future recalibration. The test covers:
- the rewrite on and off in `EXPLAIN (VERBOSE, COSTS OFF)`;
- `force_path` index and filter;
- EXPLAIN ANALYZE of what was sent, text and binary, filtered to the Foreign Scan lines so memory sizes cannot change the output;
- correctness with the temp-table method (`sj_compare`) for auto, index, filter and binary;
- NULL-only keys and an empty local result;
- GROUP BY;
- the negative cases: LEFT JOIN, `random()`, `semijoin_filter = false` on the table, and an invalid option value.

It needs sjbloom, which plain PostgreSQL does not have, so it is **not** in the default `REGRESS` list (a comment in the Makefile says so). Run it explicitly:

```bash
make -C ~/postgres/postgresql/contrib/postgres_fdw installcheck REGRESS=semijoin
```

### How to test it

```bash
source setup/env.example.sh
make -C sjbloom && make -C sjbloom install
for db in site_a site_b; do psql -X -d $db -c "DROP EXTENSION sjbloom CASCADE" -c "CREATE EXTENSION sjbloom"; done
make -C sjbloom installcheck                                                         # ok
make -C ~/postgres/postgresql/contrib/postgres_fdw clean all install                 # full rebuild: the header changed
make -C ~/postgres/postgresql/contrib/postgres_fdw installcheck                      # postgres_fdw's own tests: ok
make -C ~/postgres/postgresql/contrib/postgres_fdw installcheck REGRESS=semijoin     # our test: ok
psql -X -d site_a -f queries/correctness_auto.sql                                    # 18 x (0, 0)
```

Note: like sjbloom's header, `postgres_fdw.h` is not tracked by this tree's Makefiles. After changing it, run `make clean` first, or files that include it keep the old struct layout.

## Batch 8: the final evaluation

Batch 8 adds no C code. It measures the finished system against plain postgres_fdw in 38 configurations, checks every result for correctness, and turns the numbers into the final charts and a summary. The results, written for the presentation, are in **`docs/final/summary.md`**. The charts and tables are in `docs/final/` (copied from `results/final/`, which stays out of git).

### Files

| File | What it does |
|---|---|
| `setup/make_data.sh` | `make_data.sh S_ROWS MATCH_FRACTION uniform\|skewed` regenerates the data of both sites for one experiment, and leaves extensions, server options, user mapping and the foreign table alone. `make_data.sh 1000000 1.0 uniform` makes exactly the rows of `setup_all.sh` (we checked with a checksum of both tables). |
| `setup/data_customers.sql` | site B: `customers` with ids 1..s_rows. The primary key is added after the rows, which is faster for 3,000,000 rows. Then `VACUUM (ANALYZE)`, so autovacuum stays quiet during measurements. |
| `setup/data_orders.sql` | site A: 200,000 orders with keys from 1..round(s_rows / match_fraction): uniform `(random() * (max_key - 1))::int + 1`, or skewed `1 + floor(max_key * random()^3)`. Then `VACUUM (ANALYZE) orders` and `ANALYZE customers`. Our planner hook reads the **foreign** table's statistics, so they must be refreshed whenever site B's data changes. |
| `bench/final_eval.py` | the measurements (details below). It writes one CSV per experiment: `results/final/e1.csv`, ..., `e5_100.csv`, `e5_10.csv`. |
| `bench/final_report.py` | reads those CSVs and writes `decision_quality.csv/.md`, `headline.md` and all charts. |

### How one strategy is measured (`final_eval.py`)

The five strategies:

| strategy | settings |
|---|---|
| original | `postgres_fdw.enable_semijoin = off` (the baseline) |
| fdw_param | `enable_semijoin = off`, plus the table option `use_remote_estimate 'true'`. It is added just before the run and dropped right after, also after an error. Only for n ≤ 10,000. |
| auto | `enable_semijoin = on`, `semijoin_force_path = auto` |
| index / filter | `semijoin_force_path = index` / `filter` |

For each one:

1. **A fresh connection** to site_a, with the strategy's SET commands.
2. **One warm-up run:** `EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) CREATE UNLOGGED TABLE final_chk_<strategy> AS <query>`. This one run does three jobs:
   - it opens postgres_fdw's connection to site B and warms the caches;
   - its plan gives the join type, the Foreign Scan's actual rows and our Semijoin lines (path, predicted time of both paths, and the mode, m and k actually sent);
   - the stored table is used for the correctness check.
   With `semijoin_verbose` on, the hook's NOTICE also gives the cost model's inputs (n_est, s_rows, rows_per_key, row_width). We pass them to `sjb_choice_report` to get **our estimated bytes**, so this formula is still computed only in costmodel.c.
3. **Three timed runs** of the plain SELECT; we keep the median. The time is wall-clock time in Python around `cursor.execute()`, which returns when the whole result has arrived. So it covers planning, execution and sending all rows: it is **end to end**. We do not turn the rows into Python objects, which would only add Python's own time.
4. **Correctness:** `final_chk_original EXCEPT ALL final_chk_<strategy>` and the reverse must both be empty, and the timed runs must return as many rows as the original. This is the temp-table method of `correctness_auto_one.sql`, with unlogged tables instead of temp tables because each strategy runs in its own session.

### Real network bytes without sudo: `/proc/net/dev`

Linux keeps a byte counter for every network interface, and any user can read it in `/proc/net/dev`:

```
    lo: 32414724263 13643705 0 0 0 0 0 0 32414724263 13643705 0 0 ...
         ^ bytes received                ^ bytes sent
```

`read_loopback_bytes()` reads the `lo` line just before and just after each timed run, in the same Python process, and the difference is `measured_bytes`. Three details make this number mean "postgres_fdw's traffic":

- **Our own client does not use `lo`.** `final_eval.py` connects to site_a through the **Unix socket** (`host=/tmp`). postgres_fdw connects from site_a to site_b by TCP to `localhost`, so it does use `lo`. The only traffic counted is A ↔ B: the filter or key list, the rows, postgres_fdw's protocol messages (START TRANSACTION, DECLARE, FETCH, CLOSE, COMMIT) and the TCP/IP headers.
- **Received = sent.** On loopback every packet sent is also received by the same interface, so the two counters always grew by the same amount. We report one of them.
- **Background noise.** Other programs (for example VS Code's server) also use `lo`. Before each run we read the counters 10 times, 1 second apart, while sending no query. The result was a median of 1.2 KB/s (0.5–1.9 KB/s in the E5 runs) and at most 42 KB/s. A query moves 5 KB to 119 MB, so the noise is far below 1%.

Measured and estimated bytes agree within ±6% for every transfer above 1 MB. The exception is E3: the model assumes every key exists at B, and only 50% or 10% did.

### Decision quality: why regret, and not only "% correct"

For each configuration:

- **best** = the fastest of original, index and filter (measured);
- **regret** = auto's time / best's time. 1.00 means auto was as fast as the best strategy; 1.30 means 30% slower;
- **correct_choice** = auto picked the strategy that was actually fastest;
- **speedup** = original's time / auto's time.

"% correct" counts every mistake the same. Most mistakes happen near the **break-even point**, where the two paths take almost the same time. There a "wrong" choice costs a few percent: E2 with n = 10,000 was wrong, but index took 34.6 ms and filter 38.7 ms, so the regret is only 1.08. A wrong choice far from the break-even point (E3 with n = 50,000: 80 ms vs 190 ms) gives a large regret. So regret measures what the mistakes actually cost, and a good cost model should keep it close to 1 even where it cannot be exactly right.

Auto and the forced path run the same plan but in separate sessions, so their times differ by noise: up to ±30% for runs under 50 ms on this machine. That is why some regrets are below 1.00. `path_regret` in `decision_quality.csv` compares the forced run of the path auto chose with the best instead, which removes this noise.

### E5: real network emulation with tc netem

`tc qdisc add dev lo root netem rate 100mbit delay 1ms` (run by the user with sudo, and later `change ... rate 10mbit`) slows down `lo` itself: every packet waits 1 ms, and the bandwidth is capped. Before measuring, `final_eval.py --e5 <mbit>` checks with `tc qdisc show dev lo`, which needs no sudo, that netem is really active with that rate. It then tells the cost model about the network:

- `postgres_fdw.semijoin_network_mbit` = the netem rate;
- `postgres_fdw.semijoin_per_message_ms` = one round trip through postgres_fdw, **measured on this network** by `measure_fdw_round_trip()`. The same 4,950-row foreign scan runs with `fetch_size` 100 and with 10000. The rows, the bytes and site B's work are the same; only the number of round trips differs (51 vs 2 in the model's count). So one round trip = (time₁₀₀ − time₁₀₀₀₀) / 49. Result: **2.26 ms** at 100 Mbit and **2.42 ms** at 10 Mbit, against 0.062 ms on plain localhost.

  A tiny one-row remote query minus its execution time at B gives 11–12 ms. That is **not** one round trip: postgres_fdw sends START TRANSACTION, DECLARE, FETCH, CLOSE and COMMIT, about 5 round trips. We record it only as a cross-check.

At 10 Mbit, `auto_loopback` runs auto once more with the calibrated loopback numbers (1000 Mbit, 0.062 ms). This shows what happens when the cost model's network assumptions are wrong. The values the model was given are in every CSV row: `model_network_mbit` and `model_per_message_ms`.

`final_eval.py --e5 <mbit> --simulated` is the fallback for machines without netem: measured time + measured bytes / bandwidth, labelled "simulated" in the CSV and the charts. It was not needed here.

### Results in short (details: `docs/final/summary.md`)

- **Correctness:** 180 of 180 strategy runs correct. After restoring the default data, `queries/correctness_auto.sql` gives 18 × (0, 0).
- **Speedup** of auto over plain postgres_fdw: geometric mean **36×**, from 1.35× (100,000 remote rows, n = 50,000) to 2,597× (10 Mbit, n = 100). Auto was never slower.
- **Bytes:** 96.6% less over all configurations.
- **Correct choices:** 34 of 38 (89%). Regret: mean 1.07, median 1.00, worst 2.38.
- **The losses:**
  - E3, n = 50,000 (regret 1.82, 2.38): the model assumes every key exists at B, and lookups of missing keys are much cheaper;
  - E4 skewed, n = 50,000 (1.27): clustered keys make the index path cache-friendly;
  - E2, 100,000 rows, n = 10,000 (1.08): a near tie.
- **E5:** with the measured network numbers, all 10 choices were right and the predictions were within 0.96–1.05 of the measured time for n ≥ 10,000. With the loopback numbers the choices were the same, but the predicted times were 10–20× too low.

### Measurement notes

- **The machine's state.** On the first try the Windows host was in a power-saving state. A `SELECT 1` round trip took about 175 µs instead of about 50 µs, and fetching 1M rows through postgres_fdw took 3.3 s instead of 1.4 s (the calibration's value). We measured only after the host was set to Best performance and the round trip was back at about 50 µs. **Check this before trusting any timing on WSL.**
- **Run time:** E1, E6, E2, E3 and E4 took 6½ minutes; E5 took 13 minutes at 100 Mbit and 26 minutes at 10 Mbit. Under netem, plain postgres_fdw needs 10,001 FETCH round trips of about 2.3 ms each.
- **The order of the experiments** keeps data regenerations to a minimum: default data (E1, E6), then 100k and 3M rows (E2), match fraction 0.5 and 0.1 (E3), skewed (E4), then the default data is restored.

### How to rerun it

```bash
source setup/env.example.sh
.venv/bin/python bench/final_eval.py          # E1, E6, E2, E3, E4; restores the data; runs correctness_auto.sql (~7 min)
# E5, with someone who can run sudo (it slows down ALL local traffic until removed):
sudo tc qdisc add dev lo root netem rate 100mbit delay 1ms
.venv/bin/python bench/final_eval.py --e5 100 # ~13 min
sudo tc qdisc change dev lo root netem rate 10mbit delay 1ms
.venv/bin/python bench/final_eval.py --e5 10  # ~26 min
sudo tc qdisc del dev lo root
.venv/bin/python bench/final_report.py        # table, headline, charts in results/final/
cp results/final/*.png results/final/*.md results/final/*.csv docs/final/
```

To follow a run: `tail -f results/final/run_*.log` (if you redirect the output there, as we did). Each line shows one strategy: median time, measured bytes, rows sent by B, join type, path/mode and `correct=`.

## Batch 8b: does the adaptive choice beat a simple rule?

No new measurements. `bench/final_report.py` now also compares four **policies**: original, always-index, always-filter and auto. It uses only the **forced-path** times, so the noise between an auto session and the forced session of the same plan does not count. Auto's time is the forced time of the path it chose. Each policy's regret is its time / the fastest of original, index and filter.

- `policy_regrets()` and `summarize_policies()` compute the regret per configuration, then the mean, median, worst (with its configuration) and the number of configurations within 5% of the best.
- They write `results/final/policy_comparison.csv` (one row per configuration) and `policy_comparison.md` (the summary table), and draw `policy_regret.png` (mean and worst regret per policy, log scale).
- `regret.png` now shows `path_regret`, the forced time of the chosen path. So no bar falls below 1.0 because of session noise.

| policy | mean regret | median | worst | within 5% of the best |
|---|---:|---:|---|---:|
| original | 231.20 | 24.81 | 2,609.97 (E5 n=100 10Mbit) | 0 of 38 |
| always index | 1.03 | 1.00 | 1.22 (E1 n=150000 narrow) | 32 of 38 |
| always filter | 8.08 | 2.49 | 50.16 (E2 n=1000 s=3M) | 11 of 38 |
| auto | 1.08 | 1.00 | 2.51 (E3 n=50000 match=0.1) | 34 of 38 |

In plain words:
- The semijoin is the big win over plain postgres_fdw.
- With compact int4 keys, "always index" is a strong simple rule, slightly better than auto on average. The filter path wins only by 2–22% (large n), and on localhost that win comes from site B's work (one scan instead of many lookups), not from the bytes.
- Auto's E3/E4 mistakes (regret 2.09, 2.51, 1.34) cost more than its wins. Without E3/E4, auto beats always-index: mean 1.004 vs 1.037.

`docs/final/summary.md`, section "Does the adaptive choice beat a simple rule?", gives the numbers behind each statement. It also has the calculation, not a measurement, of why wide keys such as UUIDs would make the Bloom filter matter much more.

Rerun: `.venv/bin/python bench/final_report.py`, then copy `results/final/` to `docs/final/` as in Batch 8.
