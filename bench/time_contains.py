"""
time_contains.py: how long does site B need to test all its rows against
a Bloom filter?

This is the work site B does for every Bloom semi-join: it runs
sjb_contains(filter, customer_id) on each of the 1,000,000 customers.
The script runs, directly in site_b:

    SELECT count(*) FROM customers WHERE sjb_contains(<filter>, customer_id)

for two filters (the sizes bloom_dynamic uses for n = 100 and n = 10000)
and prints the median wall-clock time of 5 runs. It is used to compare two
versions of the C code (for example before and after a speed-up): the
counts must stay identical, only the times may change.

The filter is built first (keys 1 .. n, m = ceil(n*b), k = round(0.693*b),
as in queries/fp_cases.sql) and sent as a query parameter, so only the
scan is timed, not the building of the filter.

Run from the project folder:
    source setup/env.example.sh
    .venv/bin/python bench/time_contains.py
"""

import statistics
import time

import common


# (n, b) of the filters to time.
CASES = [(100, 26.4), (10000, 16.8)]
RUNS = 5


def build_filter(conn, n, b):
    """
    Build the Bloom filter for keys 1 .. n with b bits per key.

    Input:  conn, a connection to site_b; n; b.
    Output: (filter as Python bytes, m, k).
    """
    sql = ("SELECT sjb_build(ARRAY(SELECT generate_series(1, %s)), m, k), m, k "
           "FROM (SELECT ceil(%s * %s)::int8 AS m, round(0.693 * %s)::int4 AS k) AS sizes")
    return conn.execute(sql, (n, n, b, b)).fetchone()


def time_one_scan(conn, filter_bytes):
    """
    Run one full scan of customers with sjb_contains and time it.

    Input:  conn, a connection to site_b; filter_bytes, the filter.
    Output: (time in ms, number of rows that passed).
    """
    start = time.perf_counter()
    row = conn.execute("SELECT count(*) FROM customers WHERE sjb_contains(%s::bytea, customer_id)",
                       (filter_bytes,)).fetchone()
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms, row[0]


def main():
    conn = common.connect("site_b")
    results = []
    for n, b in CASES:
        filter_bytes, m, k = build_filter(conn, n, b)
        times = []
        counts = set()
        for run_number in range(RUNS):
            elapsed_ms, passed = time_one_scan(conn, filter_bytes)
            times.append(elapsed_ms)
            counts.add(passed)
        results.append({
            "n": n,
            "b": b,
            "m": m,
            "k": k,
            "rows_passed": ", ".join(str(count) for count in sorted(counts)),
            "median_ms": round(statistics.median(times), 1),
            "all_runs_ms": " ".join(f"{t:.0f}" for t in times),
        })
    conn.close()
    common.print_table(results, ["n", "b", "m", "k", "rows_passed", "median_ms", "all_runs_ms"])


if __name__ == "__main__":
    main()
