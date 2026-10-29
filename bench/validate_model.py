"""
validate_model.py: does the time-based cost model predict real times?

For narrow rows and n in N_VALUES, it compares, for two strategies of the
benchmark (bench/strategies.py):

  model line         measured strategy
  -----------------  ------------------------------------------------
  index path         exact_keys with SET enable_nestloop = off
  filter path BLOOM  bloom_dynamic (b* from sjb_best_bits)

  predicted_ms  the "total_ms" of that line of sjb_choice_report, with ALL
                the calibrated constants (results/calibration.json) and
                network_mbit = 1000
  covered_ms    the measured time of the parts the model covers: the
                Foreign Scan node's time at site A. It includes sending the
                query, site B's work, every FETCH round trip, turning the
                rows back into values, and also the InitPlan that builds
                the key list or filter: postgres_fdw computes that
                parameter when it opens the remote cursor, so the InitPlan
                runs INSIDE the Foreign Scan's time (checked in Batch 5b
                with a pg_sleep in the InitPlan). Adding the InitPlan time
                again would count it twice. initplan_ms is printed
                separately so you can see it.
  ratio         covered_ms / predicted_ms
  end_to_end_ms planning + execution of the whole query (median of 3),
                for reference: it also has the local scan of orders for the
                join, the hash join and planning, which the model leaves
                out because they are the same for both paths.

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
NETWORK_MBIT = 1000
# customer_id is the primary key of customers: one row per key value.
ROWS_PER_KEY = 1.0
# Columns in the Remote SQL for the narrow width: customer_id, name.
NUM_REMOTE_COLUMNS = 2

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
CALIBRATION_JSON = os.path.join(RESULTS_DIR, "calibration.json")


def load_calibration():
    """
    Read the suggested constants from results/calibration.json.

    Input:  none.
    Output: a dict with t_check_ns, t_lookup_us, per_message_ms,
            per_row_us, t_build_us and fetch_size.
    """
    with open(CALIBRATION_JSON) as json_file:
        calibration = json.load(json_file)
    return calibration["suggested"]


def wire_row_width(text_width):
    """
    The width of one returned row ON THE WIRE: the text of the values plus
    PostgreSQL's DataRow message overhead, 7 bytes per row + 4 bytes per
    column (see CLAUDE.md).

    Input:  text_width, the average text width of the row's values.
    Output: the wire width in bytes.
    """
    return text_width + 7 + 4 * NUM_REMOTE_COLUMNS


def parse_report(report):
    """
    Read the table lines of sjb_choice_report into a dict.

    The table starts with a header line ("path mode bits/key ... total_ms").
    We take the column names from it, so this keeps working if columns are
    added. A table line looks like
      "filter  bloom  18.14  45390  10163  325223  103  2.965 ..."
    The index line has mode "-". Skipped modes
    ("filter  bloom  skipped: ...") are left out.

    Input:  report, the text returned by sjb_choice_report.
    Output: {"index": {...}, "bloom": {...}, ...}; each inner dict maps a
            column name (for example "total_ms") to its number.
    """
    column_names = None
    lines = {}
    for line in report.splitlines():
        parts = line.split()
        if len(parts) > 0 and parts[0] == "path":
            column_names = parts
            continue
        if column_names is None or len(parts) < 3 or parts[0] not in ("index", "filter"):
            continue
        if parts[2] == "skipped:":
            continue
        if parts[0] == "index":
            name = "index"
        else:
            name = parts[1]
        values = {}
        # Columns 0 and 1 are path and mode (text); the rest are numbers.
        for index in range(2, len(column_names)):
            if parts[index] == "-":
                values[column_names[index]] = None
            else:
                values[column_names[index]] = float(parts[index])
        lines[name] = values
    return lines


def choice_report(conn, n_keys, s_rows, row_width, network_mbit, constants):
    """
    Call sjb_choice_report in site_a with all the calibrated constants.

    Input:  conn, a connection to site_a; n_keys, s_rows, row_width,
            network_mbit; constants, the dict from load_calibration.
    Output: the report text.
    """
    sql = "SELECT sjb_choice_report(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    params = (n_keys, s_rows, ROWS_PER_KEY, row_width, network_mbit,
              constants["t_check_ns"], constants["t_lookup_us"],
              constants["per_message_ms"], constants["per_row_us"],
              constants["t_build_us"], constants["fetch_size"])
    return conn.execute(sql, params).fetchone()[0]


def measure(conn, sql_text, nestloop_off):
    """
    Measure one query like run_experiments.py does (median of 3 runs).

    Input:  conn, a connection to site_a; sql_text; nestloop_off, True to
            run with SET enable_nestloop = off.
    Output: a dict with end_to_end_ms, covered_ms (the Foreign Scan's
            time), initplan_ms, remote_rows, join_type.
    """
    if nestloop_off:
        conn.execute("SET enable_nestloop = off")
    plan = run_experiments.run_median(conn, sql_text)
    if nestloop_off:
        conn.execute("RESET enable_nestloop")

    foreign_scan = common.find_foreign_scan(plan)
    return {
        "end_to_end_ms": common.total_time_ms(plan),
        "covered_ms": foreign_scan["Actual Total Time"] * foreign_scan["Actual Loops"],
        "initplan_ms": common.initplan_ms(plan),
        "remote_rows": common.remote_rows(foreign_scan),
        "join_type": common.join_type(plan),
    }


def main():
    constants = load_calibration()
    conn = common.connect("site_a")
    run_experiments.warm_up(conn)

    text_width = common.avg_row_width(WIDTH)
    row_width = wire_row_width(text_width)
    s_rows = strategies.foreign_table_rows(conn)
    print("constants: " + ", ".join(f"{name} = {value}" for name, value in constants.items()))
    print(f"network_mbit = {NETWORK_MBIT}, s_rows = {s_rows:,.0f}, row width: text "
          f"{text_width:.2f} + 7 + 4*{NUM_REMOTE_COLUMNS} = {row_width:.2f} bytes on the wire")
    print()

    rows = []
    for n in N_VALUES:
        n_keys = strategies.distinct_keys(conn, n)
        report = choice_report(conn, n_keys, s_rows, row_width, NETWORK_MBIT, constants)
        predicted = parse_report(report)

        # index path <-> exact_keys with nested loops off
        exact_sql, unused_bytes_sql = strategies.exact_keys(n, WIDTH)
        cases = [("index", "exact_keys (nestloop off)", exact_sql, True)]

        # filter path BLOOM <-> bloom_dynamic, with b* for the wire width
        if "bloom" in predicted:
            b = strategies.dynamic_bits(conn, n, row_width)
            bloom_sql, unused_bytes_sql = strategies.bloom_dynamic(n, b, WIDTH)
            cases.append(("bloom", f"bloom_dynamic (b={b})", bloom_sql, False))

        for model_line, strategy_name, sql_text, nestloop_off in cases:
            result = measure(conn, sql_text, nestloop_off)
            model = predicted[model_line]
            row = {
                "n": n,
                "n_keys": n_keys,
                "strategy": strategy_name,
                "predicted_ms": model["total_ms"],
                "covered_ms": round(result["covered_ms"], 2),
                "ratio": round(result["covered_ms"] / model["total_ms"], 2),
                "initplan_ms": round(result["initplan_ms"], 2),
                "end_to_end_ms": round(result["end_to_end_ms"], 2),
                "e2e_ratio": round(result["end_to_end_ms"] / model["total_ms"], 2),
                "pred_rows": round(model["rows_back"]),
                "remote_rows": result["remote_rows"],
            }
            rows.append(row)
            print(f"  n={n:<6} {strategy_name:28} predicted={row['predicted_ms']:9.2f} ms  "
                  f"covered={row['covered_ms']:9.2f} ms  ratio={row['ratio']}")

            # The model's parts for this line, to see which one is off.
            parts = []
            for column in ["network_ms", "messages_ms", "rows_ms", "work_A_ms", "work_B_ms"]:
                parts.append(f"{column}={model[column]:.2f}")
            print("      predicted parts: " + ", ".join(parts))

    conn.close()
    print()
    common.print_table(rows, ["n", "n_keys", "strategy", "predicted_ms", "covered_ms", "ratio",
                              "initplan_ms", "end_to_end_ms", "e2e_ratio", "pred_rows",
                              "remote_rows"])


if __name__ == "__main__":
    main()
