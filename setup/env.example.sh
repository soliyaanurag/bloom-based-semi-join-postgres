#!/usr/bin/env bash
#
# env.example.sh: connection settings for the CS631 project.
#
# Load these settings into your shell before running any project script:
#
#     source setup/env.example.sh
#
# (If your setup is different, copy this file to setup/env.sh, change the
# copy, and source that instead.)
#
# PASSWORD: never write your password into this file or any other file in
# the repository. Instead:
#
#   1. Create ~/.pgpass with one line (host:port:database:user:password):
#          localhost:5432:*:postgres:YOUR_PASSWORD
#      and protect it, otherwise psql ignores it:
#          chmod 600 ~/.pgpass
#      psql then finds your password automatically.
#
#   2. setup_all.sh also needs the password once, to store it in the
#      postgres_fdw user mapping (site_a uses it to log in to site_b).
#      Give it only for that one command, for example:
#          read -s -p "Password: " PGPASSWORD; export PGPASSWORD
#          setup/setup_all.sh
#      If your server uses "trust" authentication (no password is checked),
#      you can skip this step.

export PGHOST=localhost
export PGPORT=5432
export PGUSER=postgres
