#!/usr/bin/env bash
#
# capture_bytes.sh: OPTIONAL cross-check of our byte estimates with a real
# packet capture. This script runs NOTHING itself: it only PRINTS the
# commands for you to run (some need sudo).
#
# Idea: capture every TCP packet on port 5432 of the loopback interface
# while ONE Bloom query runs, then let capinfos add up the bytes.
#
# Trick: psql connects to site_a through the Unix socket (PGHOST=/tmp), so
# its own traffic does NOT go over TCP. postgres_fdw connects from site_a
# to site_b with host=localhost, so the capture contains only the traffic
# between the two sites.
#
# Usage:  bench/capture_bytes.sh            (n = 10000, b = 17)
#         bench/capture_bytes.sh 1000 20    (n = 1000, b = 20)

N="${1:-10000}"
B="${2:-17}"
CAPTURE_FILE="/tmp/sjb_capture.pcap"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cat <<EOF
Packet capture cross-check for the Bloom query, n = $N, b = $B.
Run these commands yourself (this script does not run them).

0. Once: install the tools (needs sudo).
       sudo apt install tcpdump wireshark-common

1. Terminal 1: start capturing site A <-> site B traffic (needs sudo).
       sudo tcpdump -i lo -s 0 -w $CAPTURE_FILE 'tcp port 5432'

2. Terminal 2: run ONE query. PGHOST=/tmp makes psql use the Unix socket,
   so only postgres_fdw's TCP connection to site_b is captured.
       cd $PROJECT_DIR
       PGHOST=/tmp psql -X -d site_a -v n=$N -v b=$B -f queries/bloom_manual.sql

3. Terminal 1: stop tcpdump with Ctrl+C.

4. Count the bytes ("Data size" line = all captured bytes, both directions).
       capinfos $CAPTURE_FILE

5. Compare with our estimate (bytes_a_to_b + bytes_b_to_a = total_bytes).
       psql -X -d site_a -v n=$N -v b=$B -f queries/byte_estimate.sql

Expect the capture to be LARGER than our estimate. Our estimate counts
only the filter text and the row values. The capture also contains:
  - TCP/IP headers of every packet, plus the login handshake of the new
    postgres_fdw connection,
  - PostgreSQL protocol overhead: each returned row is a DataRow message
    with a 7-byte header plus a 4-byte length per column (15 bytes per row
    for 2 columns), and postgres_fdw fetches rows in batches of 100 with
    a cursor (DECLARE / FETCH 100 / CLOSE), so there are extra round trips.
With narrow rows (about 17 bytes) that overhead is roughly as big as the
data itself, so a factor of about 2 is normal.
EOF
