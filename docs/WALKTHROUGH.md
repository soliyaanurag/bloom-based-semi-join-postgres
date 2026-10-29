<!-- Walkthrough of the Bloom filter semi-join: sites and data, filter format, hashing, false positives, cost model, sizing rules, EXPLAIN lines, benchmarks, and how to test each part. -->

# Walkthrough: the Bloom filter semi-join for postgres_fdw

This document explains the whole project step by step, file by file, and how to test each part. Every number in it was measured on our machine (WSL2 Ubuntu, PostgreSQL 18.6, laptop on charger in best-performance mode).

Contents:

1. [The idea](#1-the-idea)
2. [The two sites and the data](#2-the-two-sites-and-the-data)
3. [The sjbloom extension](#3-the-sjbloom-extension)
4. [The hand-written Bloom semi-join](#4-the-hand-written-bloom-semi-join)
5. [False positives](#5-false-positives)
6. [The cost model](#6-the-cost-model)
7. [Bloom details in EXPLAIN ANALYZE](#7-bloom-details-in-explain-analyze)
8. [Benchmarks](#8-benchmarks)
9. [How to test each part](#9-how-to-test-each-part)
10. [PostgreSQL macros and functions we use](#10-postgresql-macros-and-functions-we-use)
11. [Where the code lives](#11-where-the-code-lives)

## 1. The idea

Site A has a table R (`orders`). Site B has a table S (`customers`), which site A sees as a postgres_fdw *foreign table*. For a join R ⋈ S, plain postgres_fdw fetches **all** of S from site B, even when only a few rows of S match.

With a **Bloom filter semi-join**:

1. Site A collects the distinct join keys of the rows of R it needs (R′) and puts them into a Bloom filter: a compact bit array.
2. Site A sends the filter to site B as a query parameter.
3. Site B returns only the rows of S whose key passes the filter. These are all the real matches, plus a few *false positives*.
4. Site A runs the normal join, which drops the false positives. The result is **always correct**.

The semi-join is written **by hand** in SQL (`sjb_contains((SELECT sjb_build_auto(...) ...), c.customer_id)`). It runs on plain PostgreSQL 18 with the original, unmodified postgres_fdw. Our C extension `sjbloom` provides the filter functions, a cost model that sizes the filter, and extra Bloom lines in `EXPLAIN ANALYZE`.

```
 SITE A (site_a)                                          SITE B (site_b)
 ───────────────                                          ───────────────
 1. InitPlan (runs once, before the join):
    SELECT sjb_build_auto(array_agg(customer_id), ...)
    FROM orders WHERE order_id <= n
      → n distinct keys → bytea filter

 2. The filter becomes parameter $1.

 3. Foreign Scan sends the Remote SQL, $1 as hex text ────►
    SELECT customer_id, name FROM public.customers
    WHERE public.sjb_contains($1::bytea, customer_id)
                                                          4. B scans customers and keeps
                                                             only rows whose key passes:
                                                             all real matches + a few
                                                             false positives.
                           ◄────── rows come back as text
 5. Hash Join: orders ⋈ returned customers ON customer_id.
    False positives find no order and are dropped.
    → the same rows as the plain join.
```

## 2. The two sites and the data

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

Both "sites" are databases on the same PostgreSQL server. postgres_fdw still connects from site_a to site_b over a real TCP connection (localhost:5432), so every row of customers that site A reads really goes through a network socket.

### Files (`setup/`)

- **`env.example.sh`** sets `PGHOST=localhost`, `PGPORT=5432`, `PGUSER=postgres`. Load it with `source setup/env.example.sh`. Passwords never go in this file: put yours in `~/.pgpass` (`chmod 600`), and give it to `setup_all.sh` once through `PGPASSWORD` (see the comments in the file). On our machine the server uses `trust` authentication, so no password is checked.
- **`create_sites.sql`** drops site_a and site_b if they exist (`WITH (FORCE)` disconnects old connections), then creates them empty.
- **`site_b_data.sql`** creates `customers(customer_id, name, city, padding)` in site_b with 1,000,000 rows (ids 1..1,000,000). The `padding` column (80 × `'x'`) makes each row about 100 bytes.
- **`site_a_setup.sql`** runs in site_a:
  - **orders**: 200,000 orders, each pointing at a random customer between 1 and 1,000,000. `setseed(0.42)` makes the "random" data the same on every run.
  - **`CREATE EXTENSION postgres_fdw`** installs the *foreign data wrapper*: code that reads tables on another PostgreSQL server by sending it SQL.
  - **`CREATE SERVER site_b_srv`** records *where* the other site is (host, port, database).
  - **`CREATE USER MAPPING`** records *who* to log in as at site B. The password comes in as the psql variable `pgpass`, never from the file.
  - **`IMPORT FOREIGN SCHEMA ... LIMIT TO (customers)`** creates the local foreign table `customers`. It stores no data: every query on it becomes SQL sent to site B.
  - **`ANALYZE orders; ANALYZE customers;`** collects statistics. For the foreign table, site A fetches a sample from site B, so its planner knows the remote table has about 1,000,000 rows.
- **`enable_sjbloom.sql`** makes sure sjbloom exists in both sites, then runs `ALTER SERVER site_b_srv OPTIONS (ADD extensions 'sjbloom')`. postgres_fdw only sends a WHERE condition to site B if it is sure site B can run it. It trusts built-in functions, but for extension functions it only trusts the extensions named in the server option `extensions`. **Without this option**, `sjb_contains` stays at site A: B sends all 1,000,000 customers and A filters them (a `Filter: sjb_contains(...)` line above the Foreign Scan). The result is still correct, but nothing is saved on the network.
- **`setup_all.sh`** runs everything in 5 steps: create the sites, load customers, load orders + postgres_fdw, `CREATE EXTENSION sjbloom` in both sites, `enable_sjbloom.sql`. `set -e` stops the script at the first failing command, and `psql -v ON_ERROR_STOP=1` stops each SQL file at its first error. **It drops and recreates both databases.** Step 5 prints two `extension "sjbloom" already exists, skipping` notices; that is expected.

Nothing in the setup changes postgres_fdw's behaviour: no library is preloaded, and the server only gets the standard options `host`, `port`, `dbname` and `extensions`.

### How to read the EXPLAIN output

`EXPLAIN (ANALYZE, VERBOSE)` runs the query and prints the plan as a tree. Each `->` is a child node that feeds rows to the node above it.

```
Foreign Scan on public.customers c  (cost=100.00..237257.00 rows=1000000 width=15) (actual time=0.517..1624.927 rows=1000000.00 loops=1)
                                     └──────── planner's GUESS ───────────────┘  └──────────── what REALLY happened ─────────────────┘
```

- **cost / rows / width** (first parentheses): the planner's estimate before running.
- **actual time / rows / loops** (second parentheses): measured. Total rows = rows × loops.
- **Foreign Scan**: the node that reads the foreign table. Its rows come from site B over the network. **This is the number the Bloom filter makes small.**
- **Remote SQL:** (only with `VERBOSE`): the SQL text site A sent to site B. If a condition appears in its `WHERE`, site B filters the rows.
- **InitPlan**: a sub-query that does not depend on the outer rows, so it runs **once**, before the main plan. Its result is passed to the rest of the plan as a parameter (`$1`, ...). postgres_fdw can send such a parameter to site B.

### The baseline: plain postgres_fdw

`queries/baseline.sql` (n = 1000):

```
 Hash Join  (actual time=2.029..1773.941 rows=1000.00 loops=1)
   ->  Foreign Scan on public.customers c  (actual time=0.517..1624.927 rows=1000000.00 loops=1)
         Remote SQL: SELECT customer_id, name FROM public.customers
 Execution Time: 1774.539 ms
```

The Remote SQL has **no WHERE clause**, so site B sends all **1,000,000** rows to answer a query with 1000 result rows. postgres_fdw cannot push the join condition down, because the other side of the join (orders) lives at site A.

## 3. The sjbloom extension

`sjbloom/` is a C extension built with PGXS. Its SQL functions:

| Function | Runs at | What it does |
|---|---|---|
| `sjb_build(keys int4[], m int8, k int4) → bytea` | site A | Builds a Bloom filter with `m` bits and `k` positions per key. |
| `sjb_build_auto(keys int4[], s_rows, rows_per_key, row_width, network_mbit, t_check_ns, max_filter_bytes DEFAULT 1048576, wire_factor DEFAULT 2, per_message_ms DEFAULT 0.06194, per_row_us DEFAULT 0.54819, t_build_us DEFAULT 0.245, fetch_size DEFAULT 100) → bytea` | site A | Removes NULLs and repeated keys, sizes the filter with the cost model (rules a–d, section 6), and builds it. Always a Bloom filter. |
| `sjb_contains(filter bytea, key int4) → bool` | site B | Does this key pass the filter? |
| `sjb_info(filter bytea) → text` | anywhere | One line: `mode=bloom nkeys=... m=... k=... bits_set=...% bytes=...`. |
| `sjb_best_bits(n, s_rows, matches, row_width, wire_factor DEFAULT 2) → float8` | anywhere | b\*, the bits per key that minimise the bytes (section 6.2). Written in SQL. |
| `sjb_bloom_report(n, s_rows, rows_per_key, row_width, network_mbit, t_check_ns, per_message_ms, per_row_us, t_build_us, fetch_size, max_filter_bytes, wire_factor) → text` | anywhere | A readable report of the cost model for n keys. The same C code as `sjb_build_auto`. |
| `sjb_bloom_estimate(same arguments) → record` | anywhere | The same numbers as columns (`size_rule, matching_rows, best_bits, bits_per_key, m, k, fp_rate, bytes_a_to_b, rows_back, bytes_b_to_a, messages, network_ms, messages_ms, rows_ms, work_a_ms, work_b_ms, total_ms`), for scripts. |

All functions are `IMMUTABLE` (and the C ones `STRICT PARALLEL SAFE`):

- **IMMUTABLE**: postgres_fdw only sends a WHERE condition to the remote server if all its functions are immutable. If `sjb_contains` were not, site B would send all of S. This is also why every cost model setting is a function **argument**, never a configuration setting: the same arguments must always give the same filter.
- **STRICT**: a NULL argument gives NULL without running the C code. `sjb_contains(filter, NULL)` is NULL, and `WHERE NULL` drops the row, just like a NULL key never matches in an inner join.

### What is a Bloom filter?

A Bloom filter is a row of `m` bits, all 0 at the start.

- **Add a key:** compute `k` bit positions from the key and set those bits to 1.
- **Test a key:** compute the same `k` positions. If **any** of them is 0, the key was certainly never added. If **all** are 1, the key was probably added.

"Probably", because other keys may have set those same bits by chance: a **false positive**. A Bloom filter **never** gives a false negative: a key that was added always passes. For the join this is what we need. Site B may send a few extra rows, but never forgets a row that matches, and the join at site A throws the extra rows away.

### Filter format

A filter is a `bytea`. PostgreSQL puts its own length header in front, then come our 24-byte header and the bits:

```
 ┌─────────┬──────────────────────────────── SjbHeader (24 bytes) ───────────────────────────────┬────────────────────┐
 │ varlena │ version │ mode │    k    │  nkeys  │          m          │         seed          │  Bloom bits        │
 │ length  │ uint8   │ uint8│ uint16  │ uint32  │       uint64        │        uint64         │  ceil(m/8) bytes   │
 └─────────┴─────────┴──────┴─────────┴─────────┴─────────────────────┴───────────────────────┴────────────────────┘
  (PostgreSQL) offset 0  1      2         4         8                     16                     24
```

- `version` = 3 (`SJB_VERSION`), `mode` = 1 (`SJB_MODE_BLOOM`), `k` = positions per key, `nkeys` = keys added, `m` = number of bits, `seed` = hash seed 0x5EED5EED. Mode numbers 2 and 3 are reserved for other filter kinds; this build rejects them.
- The fields are ordered from small to large, so the compiler adds no padding and the header is 24 bytes; a `StaticAssertDecl` makes the build fail if it ever is not.
- The header is always copied in and out with `memcpy`. Bytes inside a bytea may sit at any address, and reading a `uint64` from an unaligned address by casting a pointer can crash on some machines.
- Bit position `p` lives in byte `p / 8`, at bit `p % 8` inside that byte.

Example: `sjb_info(sjb_build(ARRAY[1,2,3], 64, 3))` gives `mode=bloom nkeys=3 m=64 k=3 bits_set=14.06% bytes=32` (24 header bytes + 8 bytes of bits).

### Hashing: how one key becomes k bit positions

Each key is hashed **once**, with PostgreSQL's own `hashint4extended(key, seed)` (called from C with `DirectFunctionCall2`). Site A and site B therefore compute the same hash on any machine. The 64-bit result is split into two 32-bit numbers:

```
 h1 = low 32 bits,  h2 = high 32 bits
```

The k positions come from **enhanced double hashing** (Dillinger & Manolios, FMCAD 2004; the same method as PostgreSQL's own `src/backend/lib/bloomfilter.c`):

```
x = h1 mod m             (where we are)
y = h2 mod m             (how far the next step goes)
position_0 = x
for i = 1 .. k-1:
    x = (x + y) mod m    take the step
    y = (y + i) mod m    the next step is i bigger
    position_i = x
```

The steps grow by 1, 2, 3, ..., so the positions are not evenly spaced. Classic double hashing, `(h1 + i·h2) mod m`, spaces them evenly; then two keys with the same step and nearby starting points share most of their bits, which gave far too many false positives for small filters. h2 is **not** forced odd (as in PostgreSQL): enhanced double hashing does not need an odd step, and forcing it halved the usable (h1 mod m, h2 mod m) pairs when m is even. The full story is in [fp_investigation.md](fp_investigation.md).

`sjb_bloom_test` computes the positions one at a time and stops at the **first 0 bit**. In a well-sized filter about half the bits are 1, so most non-members are rejected after 1 or 2 positions instead of all k. Adding and testing use the same helper (`step_to_next_position`), so they can never disagree.

**Why the version number matters.** Site A builds the filter and site B tests it, so both must compute the same positions. If they ran different builds, B could look at the wrong bits and drop matching rows (false negatives), and the result would silently be wrong. So the header carries `SJB_VERSION`, and a filter of another version is refused:

```
ERROR:  sjbloom: filter has version 2, but this sjbloom understands only version 3
HINT:  Site A and site B must run the same sjbloom build. Install it on both sites and rebuild the filter.
```

### Checking a filter

Before any bit is read, `sjb_check_filter` (`bloom.c`) checks, in this order: at least 24 bytes; version 3; mode 1 (Bloom); m between 8 and 2^29 bits (64 MB) and k between 1 and 32; and exactly `ceil(m/8)` bytes of bits after the header. It returns a problem code and **never raises an error itself**. `sjb_contains` and `sjb_info` (`read_filter` in `sjbloom.c`) turn a problem into a clear error; the EXPLAIN hook (section 7) prints nothing instead. Examples from the regression test:

```
ERROR:  sjbloom: filter has mode 2, but this sjbloom understands only mode 1 (Bloom)
ERROR:  sjbloom: filter is too small (2 bytes, need at least 24)
ERROR:  sjbloom: k must be between 1 and 32, got 0
ERROR:  sjbloom: Bloom payload is 9 bytes, expected 8
```

### Files (`sjbloom/`)

| File | What is in it |
|---|---|
| `Makefile` | PGXS build: `MODULE_big = sjbloom`, `OBJS = sjbloom.o bloom.o costmodel.o sjb_explain.o`, `REGRESS = sjbloom_basic`. The line `$(OBJS): sjb_filter.h costmodel.h` makes a header change recompile every file (PGXS does not track headers; without it a changed `SJB_VERSION` once silently stayed old in sjbloom.so). |
| `sjbloom.control` | Extension name, default version 1.0, library path. |
| `sjb_filter.h` | `SjbHeader`, `SJB_VERSION`, `SJB_MODE_BLOOM`, the seed, the m and k ranges, the `SJB_FILTER_...` check codes, and the prototypes of `bloom.c`. |
| `bloom.c` | Part 1: hashing, `sjb_bloom_add`, `sjb_bloom_test`. Part 2: `sjb_bloom_payload_bytes`, `sjb_count_set_bits`, `sjb_check_filter` (no errors raised). |
| `costmodel.h`, `costmodel.c` | The cost model: one small function per formula, the sizing rules a–d and `sjb_estimate_bloom` (section 6). Plain arithmetic on doubles, no SQL values. |
| `sjbloom.c` | The SQL functions: checking arguments and filters (errors), building filters, `collect_unique_keys` (qsort + remove repeats), the report. |
| `sjb_explain.c` | The EXPLAIN hook and the library's `_PG_init` (section 7). |
| `sjbloom--1.0.sql` | `CREATE FUNCTION` for every SQL function, with comments. |
| `sql/sjbloom_basic.sql`, `expected/sjbloom_basic.out` | The regression test (section 9). |

After changing `sjbloom--1.0.sql`, reinstall and recreate the extension: `make -C sjbloom install`, then `DROP EXTENSION sjbloom CASCADE; CREATE EXTENSION sjbloom;` in each database (or rerun `setup/setup_all.sh`). The extension stays at version 1.0; there are no upgrade scripts.

## 4. The hand-written Bloom semi-join

**`queries/bloom_manual.sql`** (psql variables `n`, default 1000, and `b`, default 10):

```sql
EXPLAIN (ANALYZE, VERBOSE)
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
        c.customer_id);
```

Step by step:

1. **The InitPlan builds the filter.** The sub-SELECT inside `sjb_contains(...)` does not depend on the row of customers, so PostgreSQL runs it once as an *InitPlan*: `m = b` bits per distinct key (at least 8), `k = round(0.693 × b)` (at least 1). That k gives the lowest false-positive rate for b bits per key.
2. **The filter becomes `$1`.**
3. **Remote SQL.** `sjb_contains` is IMMUTABLE, belongs to an extension listed in the server's `extensions` option, and its arguments are a parameter and a column, so postgres_fdw puts the whole condition into the SQL it sends to B: `WHERE (public.sjb_contains($1::bytea, customer_id))`. The parameter travels as text, so the bytea filter is sent as hex: 2 × (filter bytes) + 2 bytes for `\x`.
4. **B filters.** Site B runs `sjb_contains` on each of its 1,000,000 rows and returns only the rows that pass. This is the semi-join S ⋉ R′, plus false positives.
5. **The join at A.** The normal hash join on `customer_id` keeps only true matches.

**`queries/auto_manual.sql`** is the same query with `sjb_build_auto(array_agg(customer_id), :s_rows, :rows_per_key, :row_width, :network_mbit, :t_check_ns)`: the filter is sized by the cost model at run time, from the true number of distinct keys. No `DISTINCT` is needed, because `sjb_build_auto` removes NULLs and repeats itself. The statistics come from site_a's catalog (`queries/catalog_stats.sql`, section 6.1).

**An empty R′.** `array_agg` over no rows returns **NULL**, not an empty array. Then the filter is NULL, `sjb_contains(NULL, key)` is NULL for every row (STRICT), nothing passes, and the join is empty: the result is correct. But there is no filter value, so EXPLAIN ANALYZE shows **no Bloom lines** for that query. `ARRAY(SELECT customer_id FROM ...)` returns `'{}'` instead, and `sjb_build_auto` then builds the rule-a filter (m = 8, k = 1, no bit set; section 6.3), which also lets nothing pass and does show its Bloom lines. `queries/edge_cases.sql` checks both (E2 and E3).

**Correctness.** The test for every query is: `(baseline EXCEPT ALL bloom)` and `(bloom EXCEPT ALL baseline)` must both have 0 rows. `EXCEPT ALL` keeps duplicates, so both 0 means both queries return the same rows the same number of times. `queries/correctness_bloom.sql` (via `correctness_bloom_one.sql`) does it for n = 100, 1000, 10000; `auto_manual.sql` for n = 100, 10000, 150000; `queries/edge_cases.sql` for 11 unusual cases (section 9). All counts are 0.

Two practical rules for test queries:

- Build a filter **inline or in a `WITH ... AS MATERIALIZED` CTE**, so it is built once. Without `MATERIALIZED`, the planner may copy the `sjb_build(...)` call into the WHERE clause and rebuild the filter for every row.
- Never store a big filter in a table and read it back with `(SELECT filter FROM t)`: the value is TOASTed, and `sjb_contains` fetches it again for every row (5.5 s instead of 0.18 s in one test). Through postgres_fdw this does not happen, because the filter arrives as a query parameter.

## 5. False positives

The standard false-positive formula (Broder & Mitzenmacher, "Network Applications of Bloom Filters: A Survey", 2004) is the **only** one we use:

```
fp = (1 − e^(−k·n/m))^k        n = keys in the filter, m = bits, k = positions per key
```

With the best k (k = b · ln 2, b = m/n bits per key) it becomes fp(b) = e^(−(ln 2)²·b) = e^(−0.4805·b): each extra bit per key cuts the rate by a factor of about 1.6.

`queries/fp_rate.sql` (10,000 keys in the filter, 1,000,000 non-members tested; `docs/midsem/fp_rate.csv`, `fp_rate.png`):

| b | m | k | measured | predicted |
|---:|---:|---:|---:|---:|
| 4 | 40,000 | 3 | 0.145933 | 0.146892 |
| 8 | 80,000 | 6 | 0.021308 | 0.021577 |
| 12 | 120,000 | 8 | 0.003179 | 0.003142 |
| 16 | 160,000 | 11 | 0.000445 | 0.000459 |
| 22 | 220,000 | 15 | 0.000020 | 0.000026 |

**The false positives scale with S, not with R′.** At b = 10 (fp ≈ 0.8%) about 8,000 of the 999,000 non-matching customers pass, more than the real matches for small n. That is why the size must depend on s_rows (section 6).

**Small filters.** For very small m the measured rate is above the formula: at n = 100, m = 2,640 we measure 14 false positives per million where the formula says 3. All k positions come from the pair (h1 mod m, h2 mod m); there are only m² pairs, and a non-member with a member's pair is always a false positive (expected n/m² per key tested). This is a known property of double hashing (Kirsch & Mitzenmacher, "Less Hashing, Same Performance", 2006) and only matters when the formula predicts very few false positives. It is documented, **not modelled**. `queries/fp_cases.sql` and [fp_investigation.md](fp_investigation.md) have the details.

## 6. The cost model

All formulas live in `sjbloom/costmodel.c`, one small function each. `sjb_build_auto`, `sjb_bloom_report` and `sjb_bloom_estimate` all call **one** function, `sjb_estimate_bloom`, so the filter that is built and the filter a report describes are always the same.

The inputs:

| input | meaning | our value |
|---|---|---|
| n | distinct join keys at site A | from the query |
| s_rows | rows of S at site B | 1,000,000 (`pg_class.reltuples` of the foreign table) |
| rows_per_key | rows of S per key value | 1 (customer_id is the primary key) |
| row_width | bytes of one returned row **on the wire** | 31.78 measured (30 from the catalog) |
| network_mbit | network speed | 1000 (we treat localhost as 1 Gbit/s) |
| t_check_ns | site B: time to scan and check one row | calibrated |
| per_message_ms, per_row_us, t_build_us, fetch_size | time constants and rows per FETCH | calibrated; 100 |
| max_filter_bytes | largest filter (header included) | 1 MB |
| wire_factor | wire bytes per filter byte | 2 (hex text) |

M = min(s_rows, n · rows_per_key) is the number of rows of S that really match. They come back whatever the filter size.

### 6.1 The row width on the wire

postgres_fdw receives rows as **text**, and every row is a PostgreSQL **DataRow** message: a 1-byte message type, a 4-byte message length, a 2-byte column count (**7 bytes per row**), then for every column a 4-byte value length (**4 bytes per column**) and the value's text. So the bytes of one returned row on the wire are

```
row_width = (text of the values) + 7 + 4 × (number of columns in the Remote SQL)
```

For our narrow rows (`customer_id, name`) the values are 16.78 bytes of text on average, so row_width = 16.78 + 7 + 8 = **31.78** bytes; for wide rows (`customer_id, name, padding`) 96.78 + 7 + 12 = **115.78**. The protocol overhead is about as big as a narrow row's values, so it must be counted. TCP/IP headers and the few other protocol messages (DECLARE, FETCH, CLOSE) are not counted, so all our byte numbers are **estimated bytes on the wire**.

`bench/common.py` measures the text width in site_b and adds the overhead (`wire_row_width`). `queries/catalog_stats.sql` gets row_width from site_a's catalog instead (sum of `pg_stats.avg_width` + 7 + 4·2 = 30): `avg_width` is the *stored* width, 4 bytes for an int4 whose text has 6–7 digits, so it is a little low. The benchmark uses the measured 31.78; the SQL demos use the catalog's 30.

### 6.2 The best filter size b\*

For b bits per key, the bytes on the wire are:

| part | bytes | depends on b? |
|---|---|---|
| filter, A→B | wire_factor · n · b / 8 | grows with b |
| true matches, B→A | M · row_width | no |
| false positives, B→A | (s_rows − M) · row_width · e^(−0.4805·b) | shrinks with b |

Write W = wire_factor, w = row_width, S = s_rows:

```
total(b)  = W·n·b/8  +  M·w  +  (S − M)·w·e^(−0.4805·b)
total'(b) = W·n/8  −  0.4805·(S − M)·w·e^(−0.4805·b)  = 0
⇒  e^(−0.4805·b) = W·n / (8 · 0.4805 · (S − M) · w)
⇒  b* = ln( 3.844 · (S − M) · w / (W · n) ) / 0.4805        (3.844 = 8 · 0.4805)
```

The second derivative is positive, so this is a minimum. If the value inside ln() is ≤ 1, even the smallest filter costs more than the false positives it removes, and b\* = 0 ("Bloom cannot save bytes"). b\* also is 0 for n ≤ 0. Then

```
m = max(8, ceil(n · b*))        k = round(0.693 · b*), between 1 and 32
```

0.693 = ln 2: k = b·ln 2 gives the lowest false-positive rate for b bits per key. The same formula is the SQL function `sjb_best_bits` and the C function `sjb_best_bits`; the regression test checks they agree (20.5071 for n = 10,000, S = 1,000,000, w = 100).

b\* grows slowly (logarithmically) with S/n and with the row width: wide rows make false positives more expensive, so they get more bits. For our data at n = 10,000: narrow 18.13 bits per key, wide 20.82.

Note that b\* minimises **bytes**, not time. On a fast network the time hardly depends on b near b\* (section 8).

### 6.3 The sizing rules a–d (`choose_filter_size` in `costmodel.c`)

`sjb_build_auto` **always** builds a Bloom filter. Its size follows four rules:

| rule | when | m | k | what happens |
|---|---|---|---|---|
| **a** | n = 0 (no keys, or only NULLs) | 8 | 1 | no bit is set, nothing passes. The join with an empty R′ is empty, so that is correct. |
| **b** | normal case | max(8, ceil(n·b\*)) | round(0.693·b\*), 1..32 | the size of section 6.2 |
| **c** | b\* = 0: almost every row of S matches, so a filter cannot save bytes | 8 | 1 | the smallest filter. With n keys almost all 8 bits are set, so almost every row passes, like plain postgres_fdw. |
| **d** | the rule-b filter is bigger than max_filter_bytes (header included) or needs more than 2^29 bits (64 MB) | min(2^29, (max_filter_bytes − 24) · 8), but at least 8 | round(0.693 · m/n), 1..32 | the filter is capped to the largest allowed size; k is recomputed for the capped bits per key. If max_filter_bytes < 25, even the 8-bit filter does not fit; the 8-bit filter is still built (a filter must be sent). |

**All four rules give a correct join**: a Bloom filter never drops a key that was added, and the join at site A removes the false positives. The rules only change how many bytes travel. Examples (regression test, keys 1..5000, rows of 30 bytes):

```
rule a: mode=bloom nkeys=0 m=8 k=1 bits_set=0.00% bytes=25
rule b: mode=bloom nkeys=5000 m=97273 k=13 bits_set=48.84% bytes=12184      (s_rows = 1,000,000: b* = 19.45)
rule c: mode=bloom nkeys=5000 m=8 k=1 bits_set=100.00% bytes=25            (s_rows = 5000: every row matches)
rule d: mode=bloom nkeys=5000 m=7808 k=1 bits_set=47.48% bytes=1000        (max_filter_bytes = 1000)
```

### 6.4 The time model

The model estimates the time of the remote part of the join with the total-time formula of Özsu & Valduriez (*Principles of Distributed Database Systems*, chapter on query optimization, cost functions):

```
time = T_CPU · #instructions  +  T_MSG · #messages  +  T_TR · #bytes
```

In plain words: computers do **work**; every **message** between the sites has a fixed cost no matter how small it is; every **byte** has to travel. (The book's I/O term is part of our CPU work, because our tables are cached in memory.) Our terms (`sjb_estimate_bloom`):

| term | formula | what it is |
|---|---|---|
| network (T_TR · bytes) | (bytes A→B + bytes B→A) / (network_mbit · 125,000) | bytes A→B = 2·(24 + ceil(m/8)) + 2; bytes B→A = rows back · row_width |
| messages (T_MSG · #messages) | (1 + ceil(rows back / fetch_size)) · per_message_ms | postgres_fdw sends the query, then fetches fetch_size rows per round trip |
| returned rows (T_CPU) | rows back · per_row_us | B turns each row into text, A turns it back into values |
| work at A (T_CPU) | n · t_build_us | building the filter (sort, remove repeats, hash) |
| work at B (T_CPU) | s_rows · t_check_ns | `sjb_contains` on **every** row of S: site B cannot use an index for it |

with rows back = M + (s_rows − M) · fp, since every non-matching row passes with probability fp.

What the model leaves out: the rest of site A's work (scanning orders, the final join), planning time, and about 4 fixed round trips per remote query (START TRANSACTION, DECLARE, CLOSE, COMMIT besides the FETCHes). These are the same for any filter size.

The size rules use only bytes; the time constants do not change the filter. The time model explains where the time goes and how a slower network changes it.

### 6.5 The constants (`bench/calibrate.py`)

Each constant is measured on its **own** workload, never on the benchmark queries, so the validation below is honest. Every measurement uses `SET max_parallel_workers_per_gather = 0`, because postgres_fdw runs the remote query through a cursor and PostgreSQL never uses parallel workers for a cursor. One untimed warm-up run, then the median of 5.

- **t_check_ns**: a Bloom filter of 10,000 random existing keys (b = 16) and `SELECT customer_id, name FROM customers WHERE sjb_contains(filter, customer_id)` in site_b; median time / 1,000,000 rows. (For reference, `SELECT count(*)` with the same filter does go parallel: 134 → 61 ms. The row-returning form never does, because the planner guesses 1/3 of the rows pass and Gather would be too expensive.)
- **per_message_ms and per_row_us**: `SELECT customer_id, name FROM customers WHERE customer_id <= R` for R = 1,000 .. 1,000,000, through postgres_fdw (the Foreign Scan's time) and directly at B as a cursor (`EXPLAIN ANALYZE DECLARE ... CURSOR`, the plan postgres_fdw gets). The difference is fitted to `per_message_ms · messages + per_row_us · R`. With one fetch size, messages = 1 + R/100 grows together with R and the two cannot be told apart, so this is done for fetch_size 100, 1000 and 10000 on a throwaway foreign table `calib_tmp.customers`.
- **t_build_us**: `sjb_build_auto((SELECT keys), ...)` at site A for n = 1,000 .. 150,000; the slope of a straight line. The keys are passed as `(SELECT ...)`: otherwise PostgreSQL computes an IMMUTABLE function with constant arguments once while planning, and the execution time would be 0.

Our results (`docs/midsem/calibration.json`):

| constant | value | fit |
|---|---:|---|
| t_check_ns | 145.2 | median of 5 (parallel off; 144.7 with parallel allowed: same plan) |
| per_message_ms | 0.06115 | R² = 0.9988 over 15 points |
| per_row_us | 0.56177 | (same fit) |
| t_build_us | 0.2424 | R² = 0.9996 |

The SQL defaults of `sjb_build_auto` (0.06194, 0.54819, 0.245) come from an earlier calibration on the same machine and are within 4% of these. WSL timing is noisy: t_check_ns has measured between 145 and 188 ns across runs. Rerun `calibrate.py` on your machine and pass your values (`-v t_check_ns=...` for the SQL demos).

### 6.6 The report (`sjb_bloom_report`, `queries/bloom_cost_demo.sql`)

`sjb_bloom_report(10000, 1e6, 1, 32, 1000, 150, 0.06, 0.5, 0.25, 100, 1048576, 2)` (from the regression test):

```
inputs: n=10000 s_rows=1000000 rows_per_key=1 row_width=32.00 network_mbit=1000 t_check_ns=150.00
        per_message_ms=0.06000 per_row_us=0.50000 t_build_us=0.2500 fetch_size=100 max_filter_bytes=1048576 wire_factor=2
matching rows M = 10000
best bits per key b* = 18.14
size rule: b) normal: m = max(8, ceil(n * b*)), k = round(0.693 * b*)
filter: m = 181358 bits, k = 13, 18.14 bits/key, 22694 bytes (header included)
predicted fp rate = 0.000165
bytes A->B = 45390 (the filter as hex text)
rows back = M + (s_rows - M) * fp = 10163.2
bytes B->A = rows back * row_width = 325223
messages = 1 + ceil(rows back / fetch_size) = 103
time in ms: network 2.965 + messages 6.180 + rows 5.082 + work at A 2.500 + work at B 150.000
total = 166.727 ms
```

`queries/bloom_cost_demo.sql` prints such reports for n = 100, 10,000 and 150,000 on a fast (1000 Mbit/s) and a slow (1 Mbit/s) network, using site_a's catalog statistics, then a summary table (rounded):

| n | b\* | m | k | bytes A→B | rows back | total at 1000 Mbit/s | total at 1 Mbit/s |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 27.61 | 2,761 | 19 | 742 | 102 | 158.5 ms | 188.8 ms |
| 1,000 | 22.81 | 22,813 | 16 | 5,754 | 1,017 | 160.0 ms | 449.9 ms |
| 10,000 | 18.00 | 180,015 | 12 | 45,054 | 10,174 | 175.4 ms | 2,974.9 ms |
| 50,000 | 14.57 | 728,305 | 10 | 182,128 | 50,868 | 243.6 ms | 13,895.2 ms |
| 150,000 | 12.05 | 1,807,232 | 8 | 451,858 | 152,611 | 413.5 ms | 40,614.8 ms |

On the fast network, site B's full scan (158 ms here) is most of the time for small n. On the slow network every byte costs 8 µs and the bytes decide. Plain postgres_fdw would send 1,000,000 rows × 30 bytes = 30 MB back in every case (240 s at 1 Mbit/s).

### 6.7 Does the model predict real times? (`bench/validate_model.py`)

For bloom_auto, narrow rows, network_mbit = 1000 and the calibrated constants, the prediction is compared with the time of the **Foreign Scan node** (sending the query, site B's work, every FETCH, turning rows back into values, and the InitPlan that builds the filter: postgres_fdw computes that parameter when it opens the remote cursor, so the InitPlan runs *inside* the Foreign Scan's time). `docs/midsem/validation.txt`:

| n | keys | m | k | predicted (ms) | Foreign Scan (ms) | ratio | rows predicted | rows measured |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 100 | 2,773 | 19 | 145.50 | 142.88 | 0.98 | 102 | 109 |
| 1,000 | 999 | 22,912 | 16 | 147.05 | 135.78 | 0.92 | 1,015 | 1,024 |
| 10,000 | 9,955 | 180,492 | 13 | 162.53 | 162.98 | 1.00 | 10,119 | 10,099 |
| 50,000 | 48,738 | 718,489 | 10 | 229.27 | 252.27 | 1.10 | 49,537 | 49,554 |

The model is within 10% of the measured Foreign Scan time, and the predicted rows back (M + false positives) match the measured rows. The end-to-end time (planning + execution) is 1.0–1.3× the prediction: the difference is site A's own work (scanning orders, the hash join), which the model leaves out.

## 7. Bloom details in EXPLAIN ANALYZE

`sjbloom/sjb_explain.c` adds five lines under the Foreign Scan of a Bloom semi-join (`queries/explain_demo.sql`, n = 10,000):

```
   ->  Foreign Scan on public.customers c (actual time=10.700..221.517 rows=10140.00 loops=1)
         Remote SQL: SELECT customer_id, name FROM public.customers WHERE (public.sjb_contains($1::bytea, customer_id))
         Semijoin Filter: Bloom (sjbloom)
         Bloom Keys: 9955
         Bloom Size: m=179299 bits, k=12, 18.01 bits/key, 48.70% bits set
         Bloom Predicted FP Rate: 0.000175248
         Bloom Bytes Sent: 44876 bytes
```

- **Bloom Keys**: nkeys from the filter header (for `sjb_build_auto`: the distinct non-NULL keys).
- **Bloom Size**: m, k, m / nkeys (left out when nkeys = 0) and the share of bits set to 1 (about 50% for a well-sized filter).
- **Bloom Predicted FP Rate**: `sjb_fp_rate(nkeys, m, k)`, the formula of section 5.
- **Bloom Bytes Sent**: 2 · (24 + ceil(m/8)) + 2, because postgres_fdw sends a bytea parameter as hex text (`sjb_bloom_filter_bytes`).

Here 10,140 − 9,955 = **185 false positives** came back; predicted: 0.000175 × (1,000,000 − 9,955) = 173.5.

**How it works.** PostgreSQL 18 has a hook, `explain_per_node_hook` (`commands/explain.h`), that EXPLAIN calls for every plan node after printing the node's own lines. The library's `_PG_init` saves the previous hook and installs ours; ours calls the previous one first. Then, only when `es->analyze` is true and the node is a ForeignScan, it looks at every expression in the node's `fdw_exprs` (the expressions postgres_fdw sends to site B as `$1, $2, ...`). For each one that is a `Param` of kind `PARAM_EXEC` and type bytea, it reads the value from `estate->es_param_exec_vals[paramid]`. It skips the parameter if the value is NULL or not computed yet (`execPlan != NULL`). The value is unpacked with `DatumGetByteaPP` and read with `VARDATA_ANY` / `VARSIZE_ANY_EXHDR`. Small filters (rules a and c, 25 bytes) are stored with a short 1-byte length header, and these macros handle both header sizes. It is printed **only** if `sjb_check_filter` says it is a valid Bloom filter of version 3. The lines are printed with `ExplainPropertyText` / `ExplainPropertyInteger` (`commands/explain_format.h`), so they also appear as fields in `FORMAT JSON`, XML and YAML.

**It never raises an error.** An error in the hook would abort the user's EXPLAIN, so the hook checks everything first and prints nothing if anything looks wrong. It reuses the extension's helpers (`sjb_check_filter`, `sjb_count_set_bits`, `sjb_fp_rate`, `sjb_bloom_filter_bytes`), so no formula is written twice. No change to postgres_fdw is needed.

**When the lines do not appear:**

- **Plain `EXPLAIN`** (without ANALYZE): the query does not run, so there is no filter yet.
- **A NULL filter**, for example `array_agg` over an empty R′ (section 4).
- **A Foreign Scan that never started**, for example when the other side of a join is empty and the executor skips it. Then the InitPlan never ran.
- **The plain join**: there is no filter parameter at all.

**Loading.** A hook works only after the library is loaded. sjbloom.so is loaded the first time one of its functions is used in a session. In a Bloom query, `sjb_build_auto` or `sjb_build` runs (as the InitPlan) before EXPLAIN prints, so the lines appear even when the EXPLAIN is the first statement of a fresh session. We checked this with a fresh `psql -c "EXPLAIN (ANALYZE, VERBOSE) ..."`. No `session_preload_libraries` setting is needed.

**Requires PostgreSQL 18 or newer** (`explain_per_node_hook` and `commands/explain_format.h` are new in 18).

**`queries/explain_demo.sql`** (run it in a fresh session) does, for n = 1,000, 10,000 and 150,000 (`explain_demo_one.sql`):

1. A warm-up run of the plain join and of the Bloom join (`\o /dev/null`).
2. The TEXT `EXPLAIN (ANALYZE, VERBOSE)` of the plain join and of the Bloom join, with their Execution Time lines.
3. A check row read from `EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON)`: Bloom Keys compared with `count(DISTINCT customer_id)`, measured false positives (Foreign Scan rows − keys), predicted ones (FP rate × (s_rows − keys)), and both execution times.

Then it checks the lines for the two smallest filters (rule a with `ARRAY(SELECT ...)` = `'{}'`, and `sjb_build(..., 8, 1)`). Our run:

| n | keys = Bloom Keys | m, k | measured FP | predicted FP | plain (ms) | Bloom (ms) |
|---:|---|---|---:|---:|---:|---:|
| 1,000 | 999 = 999 | 22,792, 16 | 20 | 17.3 | 1,838 | 177 |
| 10,000 | 9,955 = 9,955 | 179,299, 12 | 185 | 173.5 | 1,972 | 232 |
| 150,000 | 139,294 = 139,294 | 1,703,339, 8 | 2,476 | 2,431.5 | 2,157 | 679 |

## 8. Benchmarks

### Strategies (`bench/strategies.py`)

Every strategy computes the same join for the orders with `order_id <= n`:

| strategy | extra WHERE condition | filter size |
|---|---|---|
| `baseline` | none (plain postgres_fdw) | – |
| `bloom_fixed10` | `sjb_contains((SELECT sjb_build(...)), c.customer_id)` | b = 10, k = 7: the textbook choice (about 0.8% false positives) |
| `bloom_auto` | `sjb_contains((SELECT sjb_build_auto(...)), c.customer_id)` | rules a–d with s_rows = 1,000,000, rows_per_key = 1, the wire row width of the variant |

Two widths: narrow selects `o.order_id, c.name`; wide also selects `c.padding`.

### Method (`bench/run_experiments.py`)

For width in (narrow, wide), n in (100, 1000, 10000, 50000) and each strategy:

1. Run the query 3 times under `EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON)` and keep the run with the **median** time (planning + execution).
2. Read the rows from site B (the Foreign Scan's actual rows) and the join type.
3. bytes A→B = `octet_length(filter::text)` (the hex text postgres_fdw sends); bytes B→A = rows from B × the **wire row width** of the variant (31.78 or 115.78, section 6.1); total = the sum.
4. Correctness: both `EXCEPT ALL` counts against the baseline must be 0.
5. If the join is a Nested Loop with a Join Filter, rerun with `enable_nestloop = off` and record both. (This did not happen in our runs.)

Before measuring, one tiny query opens postgres_fdw's connection to site B, so the login is not counted. Rows are **appended** to `results/results.csv`; the scripts read EXPLAIN as JSON and look fields up by name, so the hook's extra Bloom fields do not disturb them.

### Results (`docs/midsem/results.csv`, all 24 rows correct)

Narrow rows (31.78 bytes per row on the wire):

| n | strategy | b | rows from B | bytes A→B | bytes B→A | total bytes | time (ms) |
|---:|---|---:|---:|---:|---:|---:|---:|
| 100 | baseline | – | 1,000,000 | 0 | 31,777,792 | 31,777,792 | 1,483.8 |
| 100 | bloom_fixed10 | 10 | 8,063 | 300 | 256,224 | 256,524 | 168.6 |
| 100 | bloom_auto | 27.73 | 109 | 744 | 3,464 | 4,208 | 135.7 |
| 1,000 | baseline | – | 1,000,000 | 0 | 31,777,792 | 31,777,792 | 1,487.0 |
| 1,000 | bloom_fixed10 | 10 | 9,205 | 2,548 | 292,515 | 295,063 | 172.8 |
| 1,000 | bloom_auto | 22.93 | 1,024 | 5,778 | 32,540 | 38,318 | 151.2 |
| 10,000 | baseline | – | 1,000,000 | 0 | 31,777,792 | 31,777,792 | 1,528.0 |
| 10,000 | bloom_fixed10 | 10 | 18,021 | 24,938 | 572,668 | 597,606 | 207.8 |
| 10,000 | bloom_auto | 18.13 | 10,099 | 45,174 | 320,924 | 366,098 | 171.6 |
| 50,000 | baseline | – | 1,000,000 | 0 | 31,777,792 | 31,777,792 | 1,558.0 |
| 50,000 | bloom_fixed10 | 10 | 56,469 | 121,896 | 1,794,460 | 1,916,356 | 330.2 |
| 50,000 | bloom_auto | 14.74 | 49,554 | 179,674 | 1,574,717 | 1,754,391 | 339.6 |

Wide rows (115.78 bytes per row on the wire):

| n | strategy | b | rows from B | bytes A→B | bytes B→A | total bytes | time (ms) |
|---:|---|---:|---:|---:|---:|---:|---:|
| 100 | baseline | – | 1,000,000 | 0 | 115,777,792 | 115,777,792 | 1,685.7 |
| 100 | bloom_fixed10 | 10 | 8,063 | 300 | 933,516 | 933,816 | 166.5 |
| 100 | bloom_auto | 30.42 | 108 | 812 | 12,504 | 13,316 | 140.1 |
| 1,000 | baseline | – | 1,000,000 | 0 | 115,777,792 | 115,777,792 | 1,561.3 |
| 1,000 | bloom_fixed10 | 10 | 9,205 | 2,548 | 1,065,735 | 1,068,283 | 167.5 |
| 1,000 | bloom_auto | 25.63 | 1,004 | 6,450 | 116,241 | 122,691 | 150.9 |
| 10,000 | baseline | – | 1,000,000 | 0 | 115,777,792 | 115,777,792 | 1,548.2 |
| 10,000 | bloom_fixed10 | 10 | 18,021 | 24,938 | 2,086,432 | 2,111,370 | 214.7 |
| 10,000 | bloom_auto | 20.82 | 10,003 | 51,870 | 1,158,125 | 1,209,995 | 182.0 |
| 50,000 | baseline | – | 1,000,000 | 0 | 115,777,792 | 115,777,792 | 1,788.9 |
| 50,000 | bloom_fixed10 | 10 | 56,469 | 121,896 | 6,537,856 | 6,659,752 | 350.2 |
| 50,000 | bloom_auto | 17.43 | 48,956 | 212,458 | 5,668,018 | 5,880,476 | 357.5 |

What the numbers show:

- **Both Bloom strategies beat plain postgres_fdw by far.** bloom_auto sends 0.01% (n = 100) to 5.5% (n = 50,000) of the baseline's bytes and is 4.6× to 12× faster. Every result was correct.
- **A fixed b = 10 is a poor choice for a large S.** Its 0.8% false-positive rate applies to the ~990,000 non-matching customers, so about 8,000 extra rows come back whatever n is: 80 times more rows than the 100 matches at n = 100. bloom_auto picks more bits per key and brings back close to the true number of rows (109 for 100 keys).
- **b\* follows the data.** bloom_auto uses fewer bits per key as n grows (27.7 → 14.7 narrow) and more for wide rows (30.4 → 17.4), because false positives are then more expensive.
- **Time.** Every Bloom query takes at least about 135 ms, even for tiny n, because site B runs `sjb_contains` on all 1,000,000 rows. At n = 50,000 bloom_auto sends 8% fewer bytes than bloom_fixed10 but is 3% slower (339.6 vs 330.2 ms). That difference is within run-to-run noise: b\* minimises **bytes**, and on a fast localhost network the 160 KB it saves are worth about 1 ms.

### The b sweep (`bench/b_sweep.py`, `docs/midsem/b_sweep.csv`)

n = 10,000 (9,955 distinct keys), b from 2 to 30, both widths, with the wire row width everywhere (measured bytes, model curve and b\*):

| width | measured minimum | b\* (= bloom_auto's b) |
|---|---|---:|
| narrow | b = 18 (366,407 bytes) | 18.13 |
| wide | b = 22 (1,210,266 bytes); b = 20: 1,210,730 (0.04% more) | 20.82 |

The measured minimum sits on b\*, and the model's predicted totals are within 1% of the measured ones for b ≥ 6. Near the minimum the curve is flat: for narrow rows b = 16 to 26 are all within 5% of the best.

### Charts (`docs/midsem/`)

| file | what it shows |
|---|---|
| `total_bytes_vs_n_<width>.png` | estimated bytes on the wire vs n, one line per strategy |
| `time_ms_vs_n_<width>.png` | query time vs n (median of 3) |
| `remote_rows_vs_n_<width>.png` | rows sent by site B vs n |
| `total_bytes_vs_b_<width>.png` | the b sweep: measured, model, and the b\* line |
| `fp_rate.png` | measured vs predicted false-positive rate (`queries/fp_rate.sql`) |

All axes with values over several powers of ten are logarithmic. The CSVs, `calibration.json` and `validation.txt` are next to the charts.

### How to rerun

```bash
source setup/env.example.sh
.venv/bin/python bench/calibrate.py        # about 30 s, writes results/calibration.json
bench/run_all.sh                           # about 3 minutes: run_experiments, b_sweep, plot
.venv/bin/python bench/validate_model.py   # about 10 s, needs results/calibration.json
```

`results/` is in `.gitignore`; copy what you want to keep into `docs/midsem/`. results.csv is appended to on every run (plot.py uses the last row of each case); delete it to start fresh.

**Before timing anything on a laptop**, plug in the charger and use the best-performance power mode: in power-saving mode we measured every network round trip about 3× slower (a `SELECT 1` round trip 175 µs instead of 50 µs). A plain fetch of all 1,000,000 customers takes about 1.4 s in best-performance mode; if you see 3 s, check the power mode.

## 9. How to test each part

Everything below assumes `source setup/env.example.sh` and a running server (`pg_ctl status`). Expected outputs are in [manual_test_guide.md](manual_test_guide.md).

| part | command | expected result |
|---|---|---|
| build + regression test | `make -C sjbloom && make -C sjbloom install && make -C sjbloom installcheck` | `# All 1 tests passed.` |
| the two sites | `setup/setup_all.sh` | ends with `=== Setup finished ===` |
| plain postgres_fdw | `psql -X -d site_a -c "EXPLAIN VERBOSE SELECT o.order_id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 1000"` | `Remote SQL: SELECT customer_id, name FROM public.customers`, no WHERE, no Bloom lines |
| Bloom semi-join by hand | `psql -X -d site_a -f queries/bloom_manual.sql` | `sjb_contains($1::bytea, customer_id)` in the Remote SQL, Bloom lines below it |
| correctness | `psql -X -d site_a -f queries/correctness_bloom.sql` and `queries/auto_manual.sql` | every count 0 |
| edge cases | `psql -X -d site_a -f queries/edge_cases.sql` | every missing_rows / extra_rows 0, both false-negative counts 0 |
| EXPLAIN lines | `psql -X -d site_a -f queries/explain_demo.sql` (fresh session) | `keys_ok = t` for all n; `lines_present = t` for both small filters |
| false positives | `psql -X -d site_a -f queries/fp_rate.sql`, `queries/fp_cases.sql` | measured ≈ predicted (except the small-m gap) |
| cost model | `psql -X -d site_a -f queries/bloom_cost_demo.sql` | six reports and a summary table |
| bytes by hand | `psql -X -d site_a -v n=10000 -v b=10 -f queries/byte_estimate.sql` | 18,021 rows from B, 597,606 bytes |
| benchmarks | section 8 | `All rows correct.` |

What the regression test (`sjbloom/sql/sjbloom_basic.sql`) covers: no false negatives; the false-positive rate; empty filters; NULL elements and NULL keys (STRICT); repeated, negative and int4-limit keys with `sjb_build` and `sjb_build_auto`; m and k range errors; sizing rules a, b, c, d (and d with max_filter_bytes = 10); no false negatives under rules b, c and d; `sjb_bloom_estimate` agrees with `sjb_build_auto`; `sjb_best_bits` in SQL agrees with C; errors for version 2, modes 2 and 9, too short, k = 0, and a payload one byte too long or short; `sjb_bloom_report` for rules a–d with explicit arguments (so the output does not depend on the machine); invalid arguments.

## 10. PostgreSQL macros and functions we use

| Name | What it does |
|---|---|
| `PG_MODULE_MAGIC` | Puts a "magic block" in the .so so the server refuses a library built for another major version. |
| `_PG_init` | Called once when the library is loaded into a session; we install the EXPLAIN hook there. |
| `PG_FUNCTION_INFO_V1(name)` | Declares that C function `name` uses the standard calling convention for SQL functions. |
| `PG_GETARG_INT32/INT64/FLOAT8(n)` | Read SQL argument number n as a C int32 / int64 / double. |
| `PG_GETARG_ARRAYTYPE_P(n)` | Read argument n as an SQL array. |
| `PG_GETARG_BYTEA_PP(n)`, `DatumGetByteaPP` | Get a bytea, decompressing / fetching it from TOAST if needed. It may keep a short 1-byte length header. |
| `VARDATA_ANY`, `VARSIZE_ANY_EXHDR` | Pointer to the data and its size, for a bytea with either a 1-byte or 4-byte length header. |
| `VARHDRSZ`, `SET_VARSIZE`, `VARDATA` | The 4-byte length header, writing it, and the data after it (for our own freshly built bytea). |
| `deconstruct_array_builtin` | Unpack an array of a built-in type (here `INT4OID`) into C arrays of values and NULL flags. |
| `Datum`, `Int32GetDatum`, `DatumGetInt32`, `Float8GetDatum`, ... | A Datum is PostgreSQL's "any value" type; these macros wrap C values into Datums and back. |
| `DirectFunctionCall2(f, a, b)` | Call an SQL-callable C function (here `hashint4extended`) directly from C. |
| `palloc`, `palloc0` | Allocate (zero-filled) memory in the current memory context; freed automatically at the end of the query. |
| `ereport(ERROR, (errcode(...), errmsg(...), errhint(...)))` | Raise an SQL error: stops the function and aborts the query. |
| `StaticAssertDecl(cond, msg)` | Check a condition at compile time. |
| `UINT64CONST(x)` | Write a 64-bit unsigned constant portably. |
| `psprintf`, `StringInfo` / `initStringInfo` / `appendStringInfo` | Formatted strings and a growing text buffer. |
| `cstring_to_text`, `CStringGetTextDatum` | Turn a C string into an SQL `text` value. |
| `get_call_result_type`, `BlessTupleDesc`, `heap_form_tuple`, `HeapTupleGetDatum` | Return a record (one row of OUT parameters) from a C function. |
| `explain_per_node_hook` | PostgreSQL 18 hook called by EXPLAIN for every plan node. |
| `IsA(node, Type)`, `foreach` / `lfirst` | Check a node's type; walk a PostgreSQL `List`. |
| `ExplainPropertyText`, `ExplainPropertyInteger` | Print one "Label: value" line in EXPLAIN (a field in JSON/XML/YAML). |
| `PG_RETURN_BYTEA_P`, `PG_RETURN_BOOL`, `PG_RETURN_TEXT_P`, `PG_RETURN_DATUM` | Return a value from the C function to SQL. |

## 11. Where the code lives

| Folder | Contents |
|---|---|
| `sjbloom/` | the C extension, its SQL file and the regression test (section 3) |
| `setup/` | the two sites (section 2) |
| `queries/` | SQL demos and checks (sections 4–7, 9) |
| `bench/` | Python benchmark, calibration and validation scripts (section 8) |
| `docs/midsem/` | the committed benchmark results and charts |
| `docs/fp_investigation.md` | the false-positive study (hashing versions 1 → 3) |
| `docs/manual_test_guide.md` | commands with their expected output |
