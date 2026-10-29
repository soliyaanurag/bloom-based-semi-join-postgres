# Cost-Based Adaptive Semi-Join Filtering for postgres_fdw

CS631 course project, IIT Bombay.

**Team:** Anurag Soliya, Aman Patel, Dhaivat Patel, Nitin Saini

**Status:** complete (Batches 0–8b). Final results: [docs/final/summary.md](docs/final/summary.md).

## The idea

When a query at site A joins a local table R (orders) with a remote table S (customers) at site B through postgres_fdw, PostgreSQL normally pulls all of S across the network, even if only a few of its rows will match.

In our project:

1. Site A collects the distinct join keys of the filtered local rows and packs them into a compact filter: a Bloom filter, an exact sorted key list, or a "pass everything" filter.
2. Site A sends this filter to site B as a query parameter.
3. Site B returns only the rows of S whose key passes the filter (a semijoin).
4. Site A runs the normal join, which removes any Bloom false positives, so the answer is always exactly correct.

A cost model picks the path, the filter type and the Bloom filter size separately for each query. The user's SQL does not change: a planner hook inside postgres_fdw adds the filter.

## Results in one paragraph

We measured 38 configurations with 5 strategies each. All 180 runs returned exactly the original rows. Our system was 36× faster than plain postgres_fdw on average (from 1.35× to 2,597×) and sent 96.6% fewer bytes. The cost model picked the fastest path in 34 of 38 configurations. Details, charts, where it loses, and the limitations are in [docs/final/](docs/final/).

## What is in this repository

| Folder / file | What it is |
|---|---|
| `sjbloom/` | **Our C extension**: the filter (Bloom / EXACT / PASS), `sjb_contains`, `sjb_build_auto`, and the whole cost model (`costmodel.c`). Built with PGXS, with its own regression test. |
| `patches/postgres_fdw-semijoin.patch` | **Our change to postgres_fdw**: the planner hook, row estimates, EXPLAIN lines, binary filter option and a regression test. It is applied to the PostgreSQL 18.6 source tree (see step 2 below). On GitHub you can read it directly as a diff. |
| `setup/` | Creates the two sites (databases `site_a` and `site_b`), loads the data, connects them with postgres_fdw and turns the rewrite on. `make_data.sh` regenerates the data for experiments. |
| `queries/` | SQL demos and correctness checks, for example `correctness_auto.sql` and `choice_demo.sql`. |
| `bench/` | Python scripts: calibration of the cost model, validation, and the final evaluation (`final_eval.py`, `final_report.py`). |
| `docs/WALKTHROUGH.md` | Step-by-step explanation of every batch: what was built, file by file, and how to test it. |
| `docs/final/` | Final evaluation: `summary.md`, charts, decision-quality tables and the raw CSVs. |
| `docs/midsem/`, `docs/fp_investigation.md` | Mid-semester results and the Bloom false-positive study. |
| `CLAUDE.md` | The project's fixed design decisions, environment notes and lessons learned. |

## How to reproduce everything from a fresh clone

Tested on Ubuntu 24.04 under WSL2. Everything is installed inside your home folder; only step 1 needs sudo.

### 1. System packages (once)

```bash
sudo apt update
sudo apt install -y build-essential git flex bison libreadline-dev zlib1g-dev pkg-config python3 python3-pip
```

### 2. PostgreSQL 18.6 from source, with our postgres_fdw patch

```bash
git clone https://github.com/soliyaanurag/Bloom-Filter-based-semi-join.git ~/postgre-project

mkdir -p ~/postgres
git clone --branch REL_18_6 --depth 1 https://git.postgresql.org/git/postgresql.git ~/postgres/postgresql
cd ~/postgres/postgresql

# apply our change to contrib/postgres_fdw (no output from --check = ok)
git apply --check ~/postgre-project/patches/postgres_fdw-semijoin.patch
git apply ~/postgre-project/patches/postgres_fdw-semijoin.patch

# build and install PostgreSQL into ~/postgres/postgresql/install
./configure --prefix=$HOME/postgres/postgresql/install --enable-debug --without-icu
make -j"$(nproc)"
make install
make -C contrib/postgres_fdw && make -C contrib/postgres_fdw install   # contrib is not built by the top-level make
```

We developed on `REL_18_STABLE` at commit `a85b5523e6`. The patch also applies cleanly to the plain `REL_18_6` tag used above (checked with `git apply --check`).

### 3. Start a database server

```bash
# add these two lines to ~/.bashrc as well
export PATH=$HOME/postgres/postgresql/install/bin:$PATH
export PGDATA=$HOME/postgres/postgresql/install/data

initdb                          # creates the data directory (local connections use "trust")
pg_ctl -l ~/pg.log start        # WSL does not start it automatically; check with: pg_ctl status
psql -d postgres -c "CREATE ROLE postgres SUPERUSER LOGIN"   # our scripts connect as "postgres"
                                                            # (skip if the role already exists)
```

### 4. Build and install sjbloom

```bash
cd ~/postgre-project
make -C sjbloom && make -C sjbloom install    # uses pg_config from step 3's PATH
make -C sjbloom installcheck                  # regression test: should end with "All 1 tests passed" / ok
```

### 5. Create the two sites

```bash
source setup/env.example.sh       # PGHOST=localhost, PGPORT=5432, PGUSER=postgres
setup/setup_all.sh                # drops and recreates site_a and site_b (about 30 s)
```

If your server checks passwords, see the comments in `setup/env.example.sh`. The password is only ever passed in as a variable and never written into a file.

### 6. Check that it works

```bash
source setup/env.example.sh

# the rewrite is active: look for the "Semijoin ..." lines under the Foreign Scan
psql -d site_a -c "EXPLAIN VERBOSE SELECT o.order_id, c.name FROM orders o
                   JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id <= 1000"

# correctness: all 18 comparisons must print 0 and 0
psql -X -d site_a -f queries/correctness_auto.sql

# postgres_fdw's own tests, and ours (ours needs sjbloom, so it is run separately)
make -C ~/postgres/postgresql/contrib/postgres_fdw installcheck
make -C ~/postgres/postgresql/contrib/postgres_fdw installcheck REGRESS=semijoin
```

Things to try by hand in `psql -d site_a`:

```sql
SET postgres_fdw.semijoin_verbose = on;            -- explains every decision in a NOTICE
SET postgres_fdw.semijoin_force_path = 'filter';   -- or 'index', or 'auto' (the default)
SET postgres_fdw.enable_semijoin = off;            -- plain postgres_fdw, for comparison
EXPLAIN (ANALYZE, VERBOSE)
SELECT o.order_id, c.name
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
WHERE o.order_id <= 150000;                        -- shows the mode, m, k and bytes sent
```

### 7. Benchmarks and the final evaluation (optional)

```bash
cd ~/postgre-project
python3 -m venv .venv && .venv/bin/pip install -r bench/requirements.txt
# if "ensurepip is not available": python3 -m venv --without-pip .venv
#                                  pip --python .venv/bin/python install -r bench/requirements.txt

source setup/env.example.sh
.venv/bin/python bench/calibrate.py       # optional: measures the cost model's constants on YOUR machine
.venv/bin/python bench/final_eval.py      # E1, E2, E3, E4, E6: about 7 minutes
.venv/bin/python bench/final_report.py    # decision-quality table and charts in results/final/
```

Before timing anything on a laptop, plug in the charger and use the best-performance power mode. In power-saving mode we measured every network round trip 3× slower. The network experiment (E5) needs `sudo tc qdisc ...` and slows down all local traffic while active; see docs/WALKTHROUGH.md, Batch 8.

Your numbers will differ from ours if your machine is faster or slower. The cost model uses the constants we calibrated (see `CLAUDE.md`, "Calibrated on this machine"). To use your own, run `bench/calibrate.py` and `SET postgres_fdw.semijoin_t_check_ns = ...` (and the other `semijoin_*` settings) in your session.

## Undo

- **Original postgres_fdw:** `git checkout -- contrib/postgres_fdw && git clean -fd contrib/postgres_fdw` in `~/postgres/postgresql`, rebuild and install postgres_fdw, then `psql -d site_a -c "ALTER SERVER site_b_srv OPTIONS (DROP semijoin_filter)"`.
- **Test databases:** `psql -d postgres -c "DROP DATABASE site_a WITH (FORCE)" -c "DROP DATABASE site_b WITH (FORCE)"`.
