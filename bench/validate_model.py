"""
validate_model.py: does the time-based cost model predict real times?

For narrow rows and n in N_VALUES, it compares the cost model's predicted
time of the Bloom semi-join with the measured time of the bloom_auto
strategy of the benchmark (bench/strategies.py):

  predicted_ms  total_ms of sjb_bloom_estimate (the same C code as
                sjb_build_auto, so the same filter), with ALL the
                calibrated constants (results/calibration.json) and
                network_mbit = 1000
  covered_ms    the measured time of the parts the model covers: the
                Foreign Scan node's time at site A. It includes sending the
                query, site B's work, every FETCH round trip, turning the
                rows back into values, and also the InitPlan that builds
                the filter: postgres_fdw computes that parameter when it
                opens the remote cursor, so the InitPlan runs INSIDE the
                Foreign Scan's time (checked with a pg_sleep in the
                InitPlan). Adding the InitPlan time again would count it
                twice. initplan_ms is printed separately so you can see it.
  ratio         covered_ms / predicted_ms
  end_to_end_ms planning + execution of the whole query (median of 3),
                for reference: it also has the local scan of orders for the
                join, the hash join and planning, which the model leaves
                out.

The constants were calibrated on separate workloads (bench/calibrate.py),
never on these queries, and are NOT adjusted to fit these numbers.

Needs results/calibration.json (run bench/calibrate.py first).

Run from the project folder:
    source setup/env.example.sh
    .venv/bin/python bench/validate_model.py
"""

import json
import os

import common
import run_experiments
import strategies


N_VALUES = [100, 1000, 10000, 50000]
WIDTH = "narrow"

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
CALIBRATION_JSON = os.path.join(RESULTS_DIR, "calibration.json")


def load_calibration():
    """
    Read the suggested constants from results/calibration.json.

    Input:  none.
    Output: a dict with t_check_ns, per_message_ms, per_row_us, t_build_us
            and fetch_size.
    """
    with open(CALIBRATION_JSON) as json_file:
        calibration = json.load(json_file)
    return calibration["suggested"]


def bloom_estimate(conn, n_keys, s_rows, row_width, constants):
    """
    Call sjb_bloom_estimate in site_a with all the calibrated constants.

    Input:  conn, a connection to site_a; n_keys, s_rows, row_width (on
            the wire); constants, the dict from load_calibration.
    Output: a dict with the columns of sjb_bloom_estimate (size_rule, m, k,
            rows_back, network_ms, messages_ms, rows_ms, work_a_ms,
            work_b_ms, total_ms, ...).
    """
    sql = ("SELECT * FROM sjb_bloom_estimate(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    params = (n_keys, s_rows, strategies.ROWS_PER_KEY, row_width, common.NETWORK_MBIT,
              constants["t_check_ns"], constants["per_message_ms"], constants["per_row_us"],
              constants["t_build_us"], constants["fetch_size"])
    cursor = conn.execute(sql, params)
    values = cursor.fetchone()
    column_names = [column.name for column in cursor.description]
    return dict(zip(column_names, values))


def measure(conn, sql_text):
    """
    Measure one query like run_experiments.py does (median of 3 runs).

    Input:  conn, a connection to site_a; sql_text.
    Output: a dict with end_to_end_ms, covered_ms (the Foreign Scan's
            time), initplan_ms, remote_rows.
    """
    plan = run_experiments.run_median(conn, sql_text)
    foreign_scan = common.find_foreign_scan(plan)
    return {
        "end_to_end_ms": common.total_time_ms(plan),
        "covered_ms": foreign_scan["Actual Total Time"] * foreign_scan["Actual Loops"],
        "initplan_ms": common.initplan_ms(plan),
        "remote_rows": common.remote_rows(foreign_scan),
    }


def main():
    constants = load_calibration()
    conn = common.connect("site_a")
    run_experiments.warm_up(conn)

    text_width = common.avg_row_width(WIDTH)
    row_width = common.wire_row_width(text_width, WIDTH)
    s_rows = strategies.foreign_table_rows(conn)
    print("constants: " + ", ".join(f"{name} = {value}" for name, value in constants.items()))
    print(f"network_mbit = {common.NETWORK_MBIT}, s_rows = {s_rows:,.0f}, row width: text "
          f"{text_width:.2f} + 7 + 4*{len(common.WIDTH_COLUMNS[WIDTH])} = {row_width:.2f} "
          f"bytes on the wire")
    print()

    rows = []
    for n in N_VALUES:
        n_keys = strategies.distinct_keys(conn, n)
        model = bloom_estimate(conn, n_keys, s_rows, row_width, constants)
        sql_text, unused_bytes_sql = strategies.bloom_auto(n, WIDTH, s_rows, row_width)
        result = measure(conn, sql_text)

        row = {
            "n": n,
            "n_keys": n_keys,
            "rule": model["size_rule"],
            "m": model["m"],
            "k": model["k"],
            "predicted_ms": round(model["total_ms"], 2),
            "covered_ms": round(result["covered_ms"], 2),
            "ratio": round(result["covered_ms"] / model["total_ms"], 2),
            "initplan_ms": round(result["initplan_ms"], 2),
            "end_to_end_ms": round(result["end_to_end_ms"], 2),
            "e2e_ratio": round(result["end_to_end_ms"] / model["total_ms"], 2),
            "pred_rows": round(model["rows_back"]),
            "remote_rows": result["remote_rows"],
        }
        rows.append(row)
        print(f"  n={n:<6} bloom_auto (m={row['m']}, k={row['k']})  "
              f"predicted={row['predicted_ms']:9.2f} ms  covered={row['covered_ms']:9.2f} ms  "
              f"ratio={row['ratio']}")

        # The model's parts, to see which one is off.
        parts = []
        for column in ["network_ms", "messages_ms", "rows_ms", "work_a_ms", "work_b_ms"]:
            parts.append(f"{column}={model[column]:.2f}")
        print("      predicted parts: " + ", ".join(parts))

    conn.close()
    print()
    common.print_table(rows, ["n", "n_keys", "rule", "m", "k", "predicted_ms", "covered_ms",
                              "ratio", "initplan_ms", "end_to_end_ms", "e2e_ratio", "pred_rows",
                              "remote_rows"])


if __name__ == "__main__":
    main()
