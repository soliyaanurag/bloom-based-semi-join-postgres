"""
b_sweep.py: how does the Bloom filter size b (bits per key) change the
total network bytes, and does the cost model pick a good b?

For n = 10000 and each b in B_VALUES, for both width variants, it runs the
Bloom query once under EXPLAIN ANALYZE and records:
  bytes_a_to_b  the filter as hex text
  bytes_b_to_a  remote_rows * measured average row width (this variant)
  total_bytes   the sum
It also prints the b* predicted by sjb_best_bits for each variant and the
total bytes the cost model predicts for each b, so plot.py can draw both.

Results go to results/b_sweep.csv (overwritten on every run).

Run from the project folder:
    source setup/env.example.sh
    .venv/bin/python bench/b_sweep.py
"""

import csv
import math
import os

import common
import strategies


N = 10000
B_VALUES = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 30]
WIDTHS = ["narrow", "wide"]

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
SWEEP_CSV = os.path.join(RESULTS_DIR, "b_sweep.csv")

CSV_COLUMNS = ["width", "n", "b", "remote_rows", "bytes_a_to_b", "bytes_b_to_a",
               "total_bytes", "model_total_bytes", "predicted_b"]


def model_total_bytes(n_distinct, s_rows, row_width, b):
    """
    Total bytes predicted by cost model version 1 for b bits per key. This
    is the same formula that sjb_best_bits minimises (see
    sjbloom/sjbloom--1.0.sql):
        filter (A to B)            n * b / 8 * 2            (hex text)
      + true-match rows (B to A)   matches * row_width
      + false positives (B to A)   (s_rows - matches) * row_width * e^(-0.4805 b)
    with matches = n_distinct (every key exists at site B).

    Input:  n_distinct, s_rows, row_width, b.
    Output: the predicted total bytes, as a float.
    """
    filter_bytes = n_distinct * b / 8 * 2
    match_bytes = n_distinct * row_width
    false_positive_bytes = (s_rows - n_distinct) * row_width * math.exp(-0.4805 * b)
    return filter_bytes + match_bytes + false_positive_bytes


def main():
    conn = common.connect("site_a")
    n_distinct = strategies.distinct_keys(conn, N)
    s_rows = strategies.foreign_table_rows(conn)
    print(f"n = {N}: {n_distinct} distinct keys, s_rows = {s_rows:,.0f}")

    all_rows = []
    for width in WIDTHS:
        row_width = common.avg_row_width(width)
        predicted_b = strategies.dynamic_bits(conn, N, row_width)
        print(f"{width}: row width {row_width:.2f} bytes, predicted b* = {predicted_b}")

        for b in B_VALUES:
            sql_text, bytes_sql = strategies.bloom_manual(N, b, width)
            plan = common.explain_json(conn, sql_text)
            rows_from_b = common.remote_rows(common.find_foreign_scan(plan))
            bytes_a_to_b = conn.execute(bytes_sql).fetchone()[0]
            bytes_b_to_a = round(rows_from_b * row_width)
            all_rows.append({
                "width": width,
                "n": N,
                "b": b,
                "remote_rows": rows_from_b,
                "bytes_a_to_b": bytes_a_to_b,
                "bytes_b_to_a": bytes_b_to_a,
                "total_bytes": bytes_a_to_b + bytes_b_to_a,
                "model_total_bytes": round(model_total_bytes(n_distinct, s_rows, row_width, b)),
                "predicted_b": predicted_b,
            })
    conn.close()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(SWEEP_CSV, "w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

    print()
    common.print_table(all_rows, CSV_COLUMNS)
    print()

    # For each variant: which measured b had the smallest total?
    for width in WIDTHS:
        width_rows = [row for row in all_rows if row["width"] == width]
        best_row = min(width_rows, key=lambda row: row["total_bytes"])
        print(f"{width}: measured minimum at b = {best_row['b']} "
              f"({best_row['total_bytes']:,} bytes); predicted b* = {best_row['predicted_b']}")
    print(f"Saved {os.path.normpath(SWEEP_CSV)}")


if __name__ == "__main__":
    main()
