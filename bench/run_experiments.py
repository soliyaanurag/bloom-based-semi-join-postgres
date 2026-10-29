"""
run_experiments.py: the main benchmark.

For each width variant (narrow, wide), each n in N_VALUES and each strategy
(baseline, exact_keys, bloom_fixed10, bloom_dynamic) it:
  1. runs the query 3 times under EXPLAIN ANALYZE and keeps the run with
     the median time,
  2. reads from that run: the join type above the Foreign Scan and the
     number of rows site B sent back (remote_rows),
  3. computes the network bytes:
       bytes_a_to_b = the filter / key list as text (query from strategies.py)
       bytes_b_to_a = remote_rows * measured average row width (this variant)
  4. checks that the result equals the baseline (EXCEPT ALL both ways).
If the join is a Nested Loop with a Join Filter, the same case is run again
with SET enable_nestloop = off, and both rows are recorded (column
nestloop_off).

The rows are APPENDED to results/results.csv (with a run_at timestamp), and
a readable table is printed at the end. plot.py uses the latest row for
each case.

Run from the project folder:
    source setup/env.example.sh
    .venv/bin/python bench/run_experiments.py
"""

import csv
import os
import statistics
import time

import common
import strategies


N_VALUES = [100, 1000, 10000, 50000]
WIDTHS = ["narrow", "wide"]
RUNS_PER_CASE = 3

# results/ is next to bench/, at the top of the project.
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
RESULTS_CSV = os.path.join(RESULTS_DIR, "results.csv")

CSV_COLUMNS = ["run_at", "strategy", "width", "n", "b", "join_type", "nestloop_off",
               "remote_rows", "bytes_a_to_b", "bytes_b_to_a", "total_bytes",
               "time_ms", "correct"]


def warm_up(conn):
    """
    Run one tiny query on the foreign table, so that postgres_fdw opens its
    connection to site B now. Otherwise the first measured query would also
    pay for logging in to site B.

    Input:  conn, a connection to site_a.
    Output: nothing.
    """
    conn.execute("SELECT customer_id FROM customers LIMIT 1").fetchall()


def run_median(conn, sql_text):
    """
    Run a query RUNS_PER_CASE times under EXPLAIN ANALYZE and return the
    plan of the run with the median total time.

    Input:  conn, a connection to site_a; sql_text, the query.
    Output: the plan dict of the median run.
    """
    plans = []
    for run_number in range(RUNS_PER_CASE):
        plans.append(common.explain_json(conn, sql_text))

    times = [common.total_time_ms(plan) for plan in plans]
    median_time = statistics.median(times)
    # With 3 runs the median is one of the measured times, so we can find
    # the plan that produced it.
    return plans[times.index(median_time)]


def measure_case(conn, strategy_name, width, n, b, sql_text, bytes_sql,
                 baseline_sql, row_width, nestloop_off):
    """
    Measure one strategy for one n and width, and build its result row.

    Input:  conn, a connection to site_a; strategy_name, width, n, b (None
            when there is no Bloom filter); sql_text and bytes_sql from
            strategies.py; baseline_sql for the correctness check;
            row_width, the measured row width of this variant;
            nestloop_off, True when enable_nestloop is off for this run.
    Output: (row dict with the CSV_COLUMNS keys, the median plan).
    """
    plan = run_median(conn, sql_text)
    foreign_scan = common.find_foreign_scan(plan)
    rows_from_b = common.remote_rows(foreign_scan)

    bytes_a_to_b = conn.execute(bytes_sql).fetchone()[0]
    bytes_b_to_a = round(rows_from_b * row_width)

    row = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_name,
        "width": width,
        "n": n,
        "b": b,
        "join_type": common.join_type(plan),
        "nestloop_off": nestloop_off,
        "remote_rows": rows_from_b,
        "bytes_a_to_b": bytes_a_to_b,
        "bytes_b_to_a": bytes_b_to_a,
        "total_bytes": bytes_a_to_b + bytes_b_to_a,
        "time_ms": round(common.total_time_ms(plan), 2),
        "correct": common.check_correct(conn, baseline_sql, sql_text),
    }
    return row, plan


def print_progress(row, plan):
    """
    Print one short line about a finished case, plus the Remote SQL for the
    smallest n so we can see what was sent to site B.

    Input:  row, a result row; plan, its plan dict.
    Output: nothing.
    """
    print(f"  {row['width']:6} n={row['n']:<6} {row['strategy']:14} "
          f"join={row['join_type']:11} nestloop_off={row['nestloop_off']!s:5} "
          f"remote_rows={row['remote_rows']:<8} time={row['time_ms']:.1f} ms "
          f"correct={row['correct']}")
    if row["n"] == N_VALUES[0] and not row["nestloop_off"]:
        print("      Remote SQL: " + common.remote_sql(common.find_foreign_scan(plan)))


def append_to_csv(rows):
    """
    Append result rows to results/results.csv. Writes the header line first
    if the file does not exist yet.

    Input:  rows, a list of row dicts.
    Output: nothing.
    """
    os.makedirs(RESULTS_DIR, exist_ok=True)
    file_is_new = not os.path.exists(RESULTS_CSV)
    with open(RESULTS_CSV, "a", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        if file_is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    conn = common.connect("site_a")
    warm_up(conn)

    # Measure the row width of each variant once, in site_b.
    row_widths = {}
    for width in WIDTHS:
        row_widths[width] = common.avg_row_width(width)
        print(f"Average row width ({width}): {row_widths[width]:.2f} bytes as text")

    all_rows = []
    for width in WIDTHS:
        for n in N_VALUES:
            # The cost model picks b for this n and this variant's row width.
            dynamic_b = strategies.dynamic_bits(conn, n, row_widths[width])
            baseline_sql, unused_bytes_sql = strategies.baseline(n, width)

            for name, b, sql_text, bytes_sql in strategies.all_strategies(n, width, dynamic_b):
                row, plan = measure_case(conn, name, width, n, b, sql_text, bytes_sql,
                                         baseline_sql, row_widths[width], False)
                all_rows.append(row)
                print_progress(row, plan)

                # A Nested Loop with a Join Filter compares every pair of
                # rows. Measure the same case again without nested loops.
                if common.is_nestloop_with_join_filter(plan):
                    conn.execute("SET enable_nestloop = off")
                    row, plan = measure_case(conn, name, width, n, b, sql_text, bytes_sql,
                                             baseline_sql, row_widths[width], True)
                    conn.execute("RESET enable_nestloop")
                    all_rows.append(row)
                    print_progress(row, plan)

    conn.close()
    append_to_csv(all_rows)

    print()
    common.print_table(all_rows, CSV_COLUMNS[1:])
    print()
    print(f"Appended {len(all_rows)} rows to {os.path.normpath(RESULTS_CSV)}")
    if all(row["correct"] for row in all_rows):
        print("All rows correct.")
    else:
        print("WARNING: some rows are NOT correct!")


if __name__ == "__main__":
    main()
