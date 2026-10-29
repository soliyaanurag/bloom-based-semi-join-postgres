# Bloom Filter Semi-Join for postgres_fdw

CS631 course project (mid-semester version), IIT Bombay.

**Team:** Anurag Soliya, Aman Patel, Dhaivat Patel, Nitin Saini

## The idea

When a query at site A joins a local table R (`orders`) with a remote table S (`customers`) at site B through postgres_fdw, PostgreSQL normally pulls **all** of S across the network, even if only a few of its rows will match.

With a Bloom filter semi-join:

1. Site A puts the distinct join keys of the rows it needs into a **Bloom filter**, a compact bit array.
2. Site A sends the filter to site B as a query parameter.
3. Site B returns only the rows of S whose key passes the filter: all real matches, plus a few false positives.
4. Site A runs the normal join, which removes the false positives, so the answer is always correct.

The semi-join is written by hand in SQL and runs on plain PostgreSQL 18 with the original postgres_fdw:

```sql
SELECT o.order_id, c.name
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
WHERE o.order_id <= 10000
  AND sjb_contains((SELECT sjb_build_auto(array_agg(customer_id), 1000000, 1, 30, 1000, 158.19)
                    FROM orders WHERE order_id <= 10000),
                   c.customer_id);
```

Our C extension **`sjbloom`** provides the filter functions, a **cost model** that sizes the filter for each query (bits per key b\* from the bytes on the wire, plus an Özsu–Valduriez time estimate), and **Bloom details in `EXPLAIN ANALYZE`** (keys, m, k, predicted false-positive rate, bytes sent).

## Results in one paragraph

On 1,000,000 customers and n = 100 to 50,000 orders, the cost-model-sized Bloom filter (`bloom_auto`) sent 0.01% to 5.5% of the bytes of plain postgres_fdw and was 4.6× to 12× faster. All 24 benchmark runs returned the same rows as the plain join. A fixed 10 bits per key let about 8,000 false positives through at every n; the sized filter brought back close to the true number of rows. The measured byte minimum of a b sweep sits on the predicted b\* (18 vs 18.13), and the time model is within 10% of the measured remote scan. Details: [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md), charts and CSVs in [docs/midsem/](docs/midsem/).

## What is in this repository

| Folder / file | What it is |
|---|---|
| `sjbloom/` | **The C extension**: `sjb_build`, `sjb_build_auto`, `sjb_contains`, `sjb_info`, `sjb_best_bits`, `sjb_bloom_report`, `sjb_bloom_estimate`, the cost model (`costmodel.c`) and the EXPLAIN lines (`sjb_explain.c`). Built with PGXS, with its own regression test. |
| `setup/` | Creates the two sites (databases `site_a` and `site_b`), loads the data and connects them with postgres_fdw. |
| `queries/` | SQL demos and checks: the plain join, the hand-written Bloom semi-join, correctness checks, edge cases, false-positive experiments, the cost model demo and the EXPLAIN demo. |
| `bench/` | Python scripts: benchmark, charts, calibration of the cost model, validation. |
| `docs/WALKTHROUGH.md` | Step-by-step explanation of everything, with our results. |
| `docs/manual_test_guide.md` | Commands with their expected output. |
| `docs/midsem/` | Benchmark CSVs and charts. |
| `docs/fp_investigation.md` | Why small Bloom filters had too many false positives, and how the hashing was fixed. |
| `CLAUDE.md` | The project's fixed design decisions, environment notes and lessons. |

## How to reproduce everything from a fresh clone

You need **PostgreSQL 18 or newer** (the EXPLAIN lines use a hook that is new in 18), with **postgres_fdw** and the **server development files** (`pg_config` and the headers). Any PostgreSQL 18 works, built from source or installed from packages. postgres_fdw is used as it is; nothing in PostgreSQL is changed.

### 1. PostgreSQL 18

**Option A: from packages** (Ubuntu/Debian with the PostgreSQL apt repository, see https://www.postgresql.org/download/linux/ubuntu/):

```bash
sudo apt install postgresql-18 postgresql-server-dev-18 build-essential python3 python3-pip
export PATH=/usr/lib/postgresql/18/bin:$PATH        # so that pg_config is the one of version 18
```

The packaged server runs as the `postgres` operating-system user and usually checks passwords for TCP connections. Give the database role `postgres` a password once (`sudo -u postgres psql -c "\password postgres"`) and put it in `~/.pgpass` (see `setup/env.example.sh`). Installing the extension into the system folders needs sudo (step 3).

**Option B: from source** (everything inside your home folder, no sudo after the system packages):

```bash
sudo apt install -y build-essential git flex bison libreadline-dev zlib1g-dev pkg-config python3 python3-pip
git clone --branch REL_18_6 --depth 1 https://git.postgresql.org/git/postgresql.git ~/postgres/postgresql
cd ~/postgres/postgresql
./configure --prefix=$HOME/postgres/postgresql/install --without-icu
make -j"$(nproc)" && make install
make -C contrib/postgres_fdw && make -C contrib/postgres_fdw install   # contrib is not built by the top-level make

# add these two lines to ~/.bashrc as well
export PATH=$HOME/postgres/postgresql/install/bin:$PATH
export PGDATA=$HOME/postgres/postgresql/install/data

initdb                          # local connections use "trust"
pg_ctl -l ~/pg.log start        # WSL does not start it automatically; check with: pg_ctl status
psql -d postgres -c "CREATE ROLE postgres SUPERUSER LOGIN"   # our scripts connect as "postgres" (skip if it exists)
```

Check with `pg_config --version` (18.x) and `psql -d postgres -c "SELECT version()"`.

### 2. Get the code

```bash
git clone <this repository> ~/bloom-semijoin-midsem
cd ~/bloom-semijoin-midsem
```

### 3. Build and install sjbloom

```bash
make -C sjbloom && make -C sjbloom install    # uses the pg_config on your PATH
make -C sjbloom installcheck                  # regression test: "# All 1 tests passed."
```

With packages, `make install` writes into `/usr/lib/postgresql/18/...` and `/usr/share/postgresql/18/...`, so run it as `sudo make -C sjbloom install PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config`. With a source build in your home folder no sudo is needed.

### 4. Create the two sites

```bash
source setup/env.example.sh       # PGHOST=localhost, PGPORT=5432, PGUSER=postgres
setup/setup_all.sh                # drops and recreates site_a and site_b (about 10 s)
```

If your server checks passwords, see the comments in `setup/env.example.sh`. The password is only ever passed in as a variable, never written into a file.

### 5. Check that it works

```bash
source setup/env.example.sh
psql -X -d site_a -f queries/baseline.sql          # plain postgres_fdw: 1,000,000 rows from site B
psql -X -d site_a -f queries/bloom_manual.sql      # Bloom semi-join: sjb_contains in the Remote SQL,
                                                   # "Bloom ..." lines under the Foreign Scan
psql -X -d site_a -f queries/correctness_bloom.sql # every count must be 0
psql -X -d site_a -f queries/edge_cases.sql        # every count must be 0
psql -X -d site_a -f queries/explain_demo.sql      # plain vs Bloom, with the EXPLAIN checks
psql -X -d site_a -f queries/bloom_cost_demo.sql   # what the cost model predicts
```

Expected outputs: [docs/manual_test_guide.md](docs/manual_test_guide.md).

### 6. Benchmarks (optional)

```bash
python3 -m venv .venv && .venv/bin/pip install -r bench/requirements.txt
# if "ensurepip is not available": python3 -m venv --without-pip .venv
#                                  pip --python .venv/bin/python install -r bench/requirements.txt

source setup/env.example.sh
.venv/bin/python bench/calibrate.py        # measures the cost model's constants on YOUR machine
bench/run_all.sh                           # benchmark + charts into results/ (about 3 minutes)
.venv/bin/python bench/validate_model.py   # predicted vs measured time
```

Before timing anything on a laptop, plug in the charger and use the best-performance power mode: in power-saving mode we measured every network round trip about 3× slower. Your numbers will differ from ours on a faster or slower machine.

## Switching between this repository and another sjbloom build

Another version of this project (for example the full CS631 version) installs an extension with the same name, `sjbloom`, into the same PostgreSQL folders. Only one build can be installed at a time: the last `make install` wins. To switch:

```bash
cd <the repository you want>
make -C sjbloom install
source setup/env.example.sh && setup/setup_all.sh     # recreates both sites with that build's SQL functions
```

`setup_all.sh` is needed because a database keeps the SQL function definitions of the build that was installed when the extension was created. Follow that repository's own README for anything else it needs.

## Undo

- **Test databases:** `psql -d postgres -c "DROP DATABASE site_a WITH (FORCE)" -c "DROP DATABASE site_b WITH (FORCE)"`.
- **The extension files:** `make -C sjbloom uninstall`.
