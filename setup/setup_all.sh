#!/usr/bin/env bash
#
# setup_all.sh: builds the whole test environment from scratch.
#
#   1. create_sites.sql  - (re)create the databases site_a and site_b
#   2. site_b_data.sql   - create and fill customers (1,000,000 rows) at B
#   3. site_a_setup.sql  - create and fill orders (200,000 rows) at A and
#                          connect A to B with postgres_fdw
#   4. CREATE EXTENSION sjbloom in site_b and in site_a
#   5. enable_sjbloom.sql - let postgres_fdw send sjbloom functions to B
#
# It uses PostgreSQL's own, unmodified postgres_fdw. The Bloom semi-join is
# written by hand in the queries (see queries/bloom_manual.sql).
#
# Before the first run, build and install the extension once:
#     make -C sjbloom install
#
# Usage (from the project folder):
#     source setup/env.example.sh
#     setup/setup_all.sh
#
# WARNING: this drops site_a and site_b if they already exist.

# Stop at the first command that fails.
set -e

# The folder this script is in, so it works from any current directory.
SETUP_DIR="$(cd "$(dirname "$0")" && pwd)"

# psql options used for every step:
#   -X                    do not read ~/.psqlrc (so everyone gets the same behaviour)
#   -v ON_ERROR_STOP=1    stop the SQL file at the first error and exit with an error code
PSQL="psql -X -v ON_ERROR_STOP=1"

if [ -z "${PGPASSWORD:-}" ]; then
    echo "Note: PGPASSWORD is not set, so the user mapping gets an empty password."
    echo "      That is fine if your server uses trust authentication."
fi

echo "=== Step 1/5: create site_a and site_b ==="
$PSQL -d postgres -f "$SETUP_DIR/create_sites.sql"

echo "=== Step 2/5: load customers into site_b ==="
$PSQL -d site_b -f "$SETUP_DIR/site_b_data.sql"

echo "=== Step 3/5: load orders into site_a and set up postgres_fdw ==="
$PSQL -v pgpass="${PGPASSWORD:-}" -d site_a -f "$SETUP_DIR/site_a_setup.sql"

echo "=== Step 4/5: create the sjbloom extension in site_b and site_a ==="
$PSQL -d site_b -c "CREATE EXTENSION sjbloom;"
$PSQL -d site_a -c "CREATE EXTENSION sjbloom;"

echo "=== Step 5/5: let postgres_fdw send sjbloom functions to site_b ==="
$PSQL -d site_a -f "$SETUP_DIR/enable_sjbloom.sql"

echo "=== Setup finished ==="
