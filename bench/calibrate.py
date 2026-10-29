"""
calibrate.py: measures the constants of the time-based cost model.

The cost model (sjbloom/costmodel.c) uses the total-time formula of Ozsu &
Valduriez:

    time = T_CPU * work + T_MSG * messages + T_TR * bytes

and needs these machine-dependent numbers:

  t_check_ns      filter path: ns site B needs to scan ONE row of customers
                  and run sjb_contains on it                  (Batch 5)
  per_message_ms  one message round trip between A and B      (Batch 5b)
  per_row_us      CPU per returned row: B turns it into text, A turns the
                  text back into values                       (Batch 5b)
  t_lookup_us     index path, per key, END TO END through postgres_fdw:
                  key list to text, parsing and planning at B, the index
                  lookup; messages and returned rows taken out (Batch 5b)
  t_build_us      filter path, site A's time per key to build the filter
                  with sjb_build_auto                         (Batch 5b)

Every constant is measured on its OWN calibration workload: random
existing customer_ids from a fixed seed, or "customer_id <= R" ranges.
Never on the benchmark queries or the orders keys, so that
validate_model.py can test the model on data it was not fitted to.

Parallel workers: postgres_fdw runs the remote query through a CURSOR, and
PostgreSQL never uses parallel workers for a cursor. So every measurement
uses SET max_parallel_workers_per_gather = 0. For comparison, the site B
measurements (t_check_ns and the direct index lookup) are taken a second
time with the server's default setting, and both are printed and saved.

Method (each point: one untimed warm-up run, then the median of 5):
  t_check_ns:  a Bloom filter of 10,000 random existing keys (b = 16),
               then  SELECT customer_id, name FROM customers
                     WHERE sjb_contains(filter, customer_id)
               in site_b; median execution time / rows of customers.
               The same is done with an EXACT and a PASS filter of the same
               keys, to see whether one constant is enough for all modes.
               For reference only, the Bloom scan is also timed as
               SELECT count(*) ...: that form CAN use parallel workers,
               while the row-returning form above does not (see
               docs/WALKTHROUGH.md, Batch 5).
  direct lookup (reference only): customer_id = ANY(keys) run directly in
               site_b for n = 1000 .. 20000; the slope of the fitted line.
               This was Batch 5's t_lookup_us.
  per_message_ms and per_row_us:
               SELECT customer_id, name FROM customers WHERE customer_id <= R
               for R = 1000 .. 1,000,000:
                 fdw_ms   = the Foreign Scan node's time at site_a
                 local_ms = the same query's execution time in site_b, run
                            as EXPLAIN ANALYZE DECLARE ... CURSOR, so B
                            uses the same (cursor) plan postgres_fdw gets
               and fits  fdw_ms - local_ms = per_message_ms * messages
                                             + per_row_us * R
               with messages = 1 + ceil(R / fetch_size). This is done for
               fetch_size = 100, 1000 and 10000 (on a throwaway foreign
               table calib_tmp.customers, dropped at the end): with one
               fetch size, messages would just be R / 100 and the two
               constants could not be told apart.
               The model itself uses the REAL fetch_size of
               public.customers (100 unless set in the options).
  t_lookup_us: through postgres_fdw at site_a, for n = 1000 .. 50000 random
               existing keys:
                 SELECT customer_id, name FROM customers
                 WHERE customer_id = ANY ((SELECT keys)::int4[])
               (the sub-SELECT makes postgres_fdw send the keys as a
               parameter, like the benchmark's exact_keys; the outer cast
               is needed, see CLAUDE.md). From the Foreign
               Scan time we subtract messages * per_message_ms and
               n * per_row_us, then fit a straight line; t_lookup_us is
               its slope.
  t_build_us:  at site_a, SELECT sjb_build_auto((SELECT keys), ...) for
               n = 1000 .. 150000 random existing keys; a straight line
               through the execution times; t_build_us is its slope.

Every fitted line is saved with its R^2 (1 = the line explains all the
variation, 0 = it explains none).

Results go to results/calibration.json. "suggested" holds the values to
use (the ones without parallel workers).

Run from the project folder:
    source setup/env.example.sh
    .venv/bin/python bench/calibrate.py
"""

import json
import math
import os
import statistics
import time

import common


RUNS = 5
CHECK_NUM_KEYS = 10000
CHECK_BITS_PER_KEY = 16
LOOKUP_N_VALUES = [1000, 5000, 10000, 20000]
# Batch 5b workloads.
RANGE_R_VALUES = [1000, 10000, 100000, 300000, 1000000]
CALIBRATION_FETCH_SIZES = [100, 1000, 10000]
FDW_LOOKUP_N_VALUES = [1000, 5000, 20000, 50000]
BUILD_N_VALUES = [1000, 10000, 50000, 150000]
# The cost model's inputs used while timing sjb_build_auto (they only
# decide which mode is built; see measure_build).
BUILD_NETWORK_MBIT = 1000
# Seed for PostgreSQL's random(), so every run picks the same keys.
RANDOM_SEED = 0.42

# The filter modes, as stored in byte 1 of a filter (see sjb_filter.h).
MODE_NUMBERS = {"bloom": 1, "exact": 2, "pass": 3}

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
CALIBRATION_JSON = os.path.join(RESULTS_DIR, "calibration.json")


def set_parallel(conn, parallel_allowed):
    """
    Turn parallel workers off (like postgres_fdw's cursor) or back to the
    server's default.

    Input:  conn, a connection to site_b; parallel_allowed, True or False.
    Output: the value of max_parallel_workers_per_gather now in effect.
    """
    if parallel_allowed:
        conn.execute("RESET max_parallel_workers_per_gather")
    else:
        conn.execute("SET max_parallel_workers_per_gather = 0")
    return conn.execute("SHOW max_parallel_workers_per_gather").fetchone()[0]


def random_keys(conn, n):
    """
    n random customer_ids that exist in site_b. setseed makes random()
    repeatable, so every run of this script uses the same keys.

    Input:  conn, a connection to site_b; n.
    Output: a Python list of n ints.
    """
    conn.execute("SELECT setseed(%s)", (RANDOM_SEED,))
    rows = conn.execute("SELECT customer_id FROM customers ORDER BY random() LIMIT %s",
                        (n,)).fetchall()
    return [row[0] for row in rows]


def count_customers(conn):
    """
    The exact number of rows of customers in site_b.

    Input:  conn, a connection to site_b.
    Output: the count, as an int.
    """
    return conn.execute("SELECT count(*) FROM customers").fetchone()[0]


def build_filters(conn, keys):
    """
    Build one filter of each mode from the same keys.

      bloom: sjb_build with m = n * 16 bits and k = round(0.693 * 16) = 11
      exact: sjb_build_auto with s_rows = 10^12. False positives would be so
             expensive that Bloom needs more than 32 bits per key, so the
             cost model picks EXACT.
      pass:  sjb_build_auto with s_rows = number of keys: every row of S
             matches, so the cost model picks PASS.
    The message, row and build costs are passed as 0, so only bytes decide
    and the modes do not depend on earlier calibration results.

    Input:  conn, a connection to site_b; keys, a list of ints.
    Output: a dict {mode name: filter as Python bytes}. Stops with an error
            if a filter does not have the expected mode.
    """
    m = math.ceil(len(keys) * CHECK_BITS_PER_KEY)
    k = round(0.693 * CHECK_BITS_PER_KEY)
    filters = {
        "bloom": conn.execute("SELECT sjb_build(%s::int4[], %s::int8, %s::int4)",
                              (keys, m, k)).fetchone()[0],
        "exact": conn.execute("SELECT sjb_build_auto(%s::int4[], 1e12, 1, 30, 1000, 0, "
                              "1048576, 2, 0, 0, 0, 100)",
                              (keys,)).fetchone()[0],
        "pass": conn.execute("SELECT sjb_build_auto(%s::int4[], %s, 1, 30, 1000, 0, "
                             "1048576, 2, 0, 0, 0, 100)",
                             (keys, len(keys))).fetchone()[0],
    }
    for mode_name, filter_bytes in filters.items():
        # Byte 0 is the version, byte 1 the mode.
        if filter_bytes[1] != MODE_NUMBERS[mode_name]:
            raise RuntimeError(f"expected a {mode_name} filter, got mode {filter_bytes[1]}")
    return filters


def describe_plan(plan):
    """
    A short text for the plan: the node types from the top down, plus the
    number of parallel workers launched, for example
    "Gather > Seq Scan (2 workers)" or "Index Scan".

    Input:  plan, the dict from EXPLAIN (FORMAT JSON).
    Output: the text.
    """
    node_types = []
    workers = 0
    node = plan["Plan"]
    while node is not None:
        node_types.append(node["Node Type"])
        workers = workers + node.get("Workers Launched", 0)
        children = node.get("Plans", [])
        if children:
            node = children[0]
        else:
            node = None
    text = " > ".join(node_types)
    if workers > 0:
        text = text + f" ({workers} workers)"
    return text


def median_execution_ms(conn, sql, params):
    """
    Run a query under EXPLAIN (ANALYZE, FORMAT JSON): once untimed to warm
    the cache, then RUNS times.

    Input:  conn, a connection to site_b; sql, the query; params, its
            parameters.
    Output: (median "Execution Time" in ms, plan description of the last
            run).
    """
    explain_sql = "EXPLAIN (ANALYZE, FORMAT JSON) " + sql
    conn.execute(explain_sql, params).fetchone()

    times = []
    plan = None
    for run_number in range(RUNS):
        plan = conn.execute(explain_sql, params).fetchone()[0][0]
        times.append(plan["Execution Time"])
    return statistics.median(times), describe_plan(plan)


def fit_line(x_values, y_values):
    """
    Least-squares straight line y = intercept + slope * x, written out by
    hand:
        slope     = (N * sum(x*y) - sum(x) * sum(y)) / (N * sum(x*x) - sum(x)^2)
        intercept = (sum(y) - slope * sum(x)) / N

    Input:  x_values, y_values, two lists of the same length (at least 2,
            with different x values).
    Output: (intercept, slope).
    """
    count = len(x_values)
    sum_x = 0.0
    sum_y = 0.0
    sum_xy = 0.0
    sum_xx = 0.0
    for index in range(count):
        x = x_values[index]
        y = y_values[index]
        sum_x = sum_x + x
        sum_y = sum_y + y
        sum_xy = sum_xy + x * y
        sum_xx = sum_xx + x * x

    slope = (count * sum_xy - sum_x * sum_y) / (count * sum_xx - sum_x * sum_x)
    intercept = (sum_y - slope * sum_x) / count
    return intercept, slope


def fit_two_terms(x1_values, x2_values, y_values):
    """
    Least squares for  y = a * x1 + b * x2  (two terms, no constant),
    written out by hand. Setting the derivatives of
    sum((y - a*x1 - b*x2)^2) by a and by b to 0 gives two equations:
        a * sum(x1*x1) + b * sum(x1*x2) = sum(x1*y)
        a * sum(x1*x2) + b * sum(x2*x2) = sum(x2*y)
    which we solve with Cramer's rule (det = the determinant).

    Input:  x1_values, x2_values, y_values, three lists of the same length.
    Output: (a, b).
    """
    sum_11 = 0.0
    sum_12 = 0.0
    sum_22 = 0.0
    sum_1y = 0.0
    sum_2y = 0.0
    for index in range(len(y_values)):
        x1 = x1_values[index]
        x2 = x2_values[index]
        y = y_values[index]
        sum_11 = sum_11 + x1 * x1
        sum_12 = sum_12 + x1 * x2
        sum_22 = sum_22 + x2 * x2
        sum_1y = sum_1y + x1 * y
        sum_2y = sum_2y + x2 * y

    det = sum_11 * sum_22 - sum_12 * sum_12
    a = (sum_1y * sum_22 - sum_12 * sum_2y) / det
    b = (sum_11 * sum_2y - sum_12 * sum_1y) / det
    return a, b


def r_squared(y_values, predicted_values):
    """
    R^2 of a fit: 1 - (sum of squared errors) / (sum of squared distances
    from the mean). 1 means the fit explains all the variation in y.

    Input:  y_values, the measured values; predicted_values, the fit's
            values at the same points.
    Output: R^2, a float.
    """
    mean_y = sum(y_values) / len(y_values)
    squared_errors = 0.0
    squared_spread = 0.0
    for index in range(len(y_values)):
        squared_errors = squared_errors + (y_values[index] - predicted_values[index]) ** 2
        squared_spread = squared_spread + (y_values[index] - mean_y) ** 2
    return 1.0 - squared_errors / squared_spread


def line_r_squared(x_values, y_values, intercept, slope):
    """
    R^2 of a straight line y = intercept + slope * x.

    Input:  x_values, y_values, intercept, slope.
    Output: R^2.
    """
    predicted = [intercept + slope * x for x in x_values]
    return r_squared(y_values, predicted)


def measure_check(conn, filters, num_customers):
    """
    t_check_ns for each filter mode: median time of the full scan with
    sjb_contains, divided by the number of rows of customers.

    Input:  conn, a connection to site_b; filters, from build_filters;
            num_customers.
    Output: a dict {mode name: {"median_ms", "t_check_ns", "plan"}}.
    """
    sql = "SELECT customer_id, name FROM customers WHERE sjb_contains(%s::bytea, customer_id)"
    results = {}
    for mode_name, filter_bytes in filters.items():
        median_ms, plan_text = median_execution_ms(conn, sql, (filter_bytes,))
        results[mode_name] = {
            "median_ms": round(median_ms, 3),
            "t_check_ns": round(median_ms * 1e6 / num_customers, 2),
            "plan": plan_text,
        }
    return results


def measure_count_star(conn, filter_bytes):
    """
    For reference only: the same Bloom scan written as SELECT count(*).
    With parallel workers allowed, PostgreSQL runs this form in parallel
    (each worker counts part of the table), while the row-returning form
    in measure_check stays serial. This shows where the speed-up of
    parallel workers would come from.

    Input:  conn, a connection to site_b; filter_bytes, the Bloom filter.
    Output: a dict {"median_ms", "plan"}.
    """
    sql = "SELECT count(*) FROM customers WHERE sjb_contains(%s::bytea, customer_id)"
    median_ms, plan_text = median_execution_ms(conn, sql, (filter_bytes,))
    return {"median_ms": round(median_ms, 3), "plan": plan_text}


def measure_lookup(conn, keys_by_n):
    """
    Direct index lookup (reference only; Batch 5's t_lookup_us): time of
    "customer_id = ANY(keys)" run directly in site_b for each n, and the
    straight line through those points.

    Input:  conn, a connection to site_b; keys_by_n, {n: list of n keys}.
    Output: a dict with the points, the intercept (ms), the slope (ms per
            key) and t_lookup_us (= slope * 1000).
    """
    sql = "SELECT customer_id, name FROM customers WHERE customer_id = ANY(%s::int4[])"
    points = []
    for n in LOOKUP_N_VALUES:
        median_ms, plan_text = median_execution_ms(conn, sql, (keys_by_n[n],))
        points.append({"n": n, "median_ms": round(median_ms, 3), "plan": plan_text})

    n_values = [point["n"] for point in points]
    times = [point["median_ms"] for point in points]
    intercept_ms, slope_ms = fit_line(n_values, times)
    return {
        "points": points,
        "intercept_ms": round(intercept_ms, 3),
        "slope_ms_per_key": slope_ms,
        "t_lookup_us": round(slope_ms * 1000, 4),
        "r_squared": round(line_r_squared(n_values, times, intercept_ms, slope_ms), 4),
    }


def median_foreign_scan_ms(conn, sql, params):
    """
    Run a query at site_a under EXPLAIN (ANALYZE, FORMAT JSON): once
    untimed, then RUNS times, and take the Foreign Scan node's actual total
    time. That time covers sending the query to site B, B's work, all the
    FETCH round trips and turning the rows back into values at A (and any
    InitPlan whose result is sent as a parameter).

    Input:  conn, a connection to site_a; sql; params.
    Output: (median Foreign Scan time in ms, rows it returned, its Remote
            SQL).
    """
    explain_sql = "EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + sql
    conn.execute(explain_sql, params).fetchone()

    times = []
    node = None
    for run_number in range(RUNS):
        plan = conn.execute(explain_sql, params).fetchone()[0][0]
        node = common.find_foreign_scan(plan)
        times.append(node["Actual Total Time"] * node["Actual Loops"])
    return statistics.median(times), common.remote_rows(node), common.remote_sql(node)


def median_cursor_execution_ms(conn, sql):
    """
    Run a query directly in site_b the way postgres_fdw runs it: as a
    cursor. EXPLAIN ANALYZE DECLARE plans the query with the cursor
    settings (like postgres_fdw's "DECLARE c1 CURSOR FOR ...") and runs it
    to the end. A cursor only lives inside a transaction, so each run is
    wrapped in one (conn.transaction() sends BEGIN and COMMIT).

    Input:  conn, a connection to site_b; sql, the query (no parameters).
    Output: (median execution time in ms, plan description).
    """
    explain_sql = "EXPLAIN (ANALYZE, FORMAT JSON) DECLARE calibrate_cursor CURSOR FOR " + sql
    with conn.transaction():
        conn.execute(explain_sql).fetchone()

    times = []
    plan = None
    for run_number in range(RUNS):
        with conn.transaction():
            plan = conn.execute(explain_sql).fetchone()[0][0]
        times.append(plan["Execution Time"])
    return statistics.median(times), describe_plan(plan)


def message_count(rows, fetch_size):
    """
    Messages for one remote query: 1 (send the query) + one FETCH round
    trip per fetch_size rows. The same formula as sjb_message_count in
    costmodel.c.

    Input:  rows, fetch_size.
    Output: the number of messages.
    """
    return 1 + math.ceil(rows / fetch_size)


def create_scratch_foreign_table(conn_a, fetch_size_value):
    """
    (Re)create the throwaway foreign table calib_tmp.customers: the same
    remote table as public.customers, but with its own fetch_size option.

    Input:  conn_a, a connection to site_a; fetch_size_value.
    Output: nothing.
    """
    server_name = conn_a.execute(
        "SELECT s.srvname FROM pg_foreign_table t JOIN pg_foreign_server s ON s.oid = t.ftserver "
        "WHERE t.ftrelid = 'public.customers'::regclass").fetchone()[0]
    conn_a.execute("DROP FOREIGN TABLE IF EXISTS calib_tmp.customers")
    conn_a.execute(
        f"CREATE FOREIGN TABLE calib_tmp.customers (customer_id int, name text) "
        f"SERVER {server_name} "
        f"OPTIONS (schema_name 'public', table_name 'customers', fetch_size '{int(fetch_size_value)}')")


def measure_messages_and_rows(conn_a, conn_b):
    """
    per_message_ms and per_row_us from "customer_id <= R" ranges.

    For each R: fdw_ms (Foreign Scan at site_a) minus local_ms (the same
    query as a cursor in site_b) is the time postgres_fdw adds on top of
    B's own work: messages and returned rows. Then
        extra_ms = per_message_ms * messages + per_row_ms * R
    is fitted with fit_two_terms.

    Why several fetch sizes: with ONE fetch size, messages = 1 + R/100 is
    just R divided by 100, so the two terms grow together and least
    squares cannot tell them apart (in Batch 5b this gave a negative
    per_message_ms on one run and a negative per_row_us on the next). With
    fetch_size 100, 1000 and 10000, the same R comes with very different
    numbers of messages, so both constants can be measured. The fetch size
    is set on a throwaway foreign table calib_tmp.customers (same remote
    table), so public.customers is never changed. The schema is dropped at
    the end, even if a measurement fails.

    Input:  conn_a, conn_b.
    Output: a dict with the points, per_message_ms, per_row_us and R^2.
    """
    # B's own time does not depend on the fetch size: measure it once per R.
    local_by_r = {}
    for r in RANGE_R_VALUES:
        sql = f"SELECT customer_id, name FROM customers WHERE customer_id <= {int(r)}"
        local_by_r[r] = median_cursor_execution_ms(conn_b, sql)

    points = []
    conn_a.execute("DROP SCHEMA IF EXISTS calib_tmp CASCADE")
    conn_a.execute("CREATE SCHEMA calib_tmp")
    try:
        for fetch_size_value in CALIBRATION_FETCH_SIZES:
            create_scratch_foreign_table(conn_a, fetch_size_value)
            for r in RANGE_R_VALUES:
                sql = f"SELECT customer_id, name FROM calib_tmp.customers WHERE customer_id <= {int(r)}"
                fdw_ms, rows, unused_remote_sql = median_foreign_scan_ms(conn_a, sql, None)
                local_ms, local_plan = local_by_r[r]
                points.append({
                    "fetch_size": fetch_size_value,
                    "R": r,
                    "rows": rows,
                    "messages": message_count(rows, fetch_size_value),
                    "fdw_ms": round(fdw_ms, 3),
                    "local_ms": round(local_ms, 3),
                    "extra_ms": round(fdw_ms - local_ms, 3),
                    "local_plan": local_plan,
                })
    finally:
        conn_a.execute("DROP SCHEMA IF EXISTS calib_tmp CASCADE")

    messages = [point["messages"] for point in points]
    rows = [point["rows"] for point in points]
    extra = [point["extra_ms"] for point in points]
    per_message_ms, per_row_ms = fit_two_terms(messages, rows, extra)
    predicted = [per_message_ms * messages[i] + per_row_ms * rows[i] for i in range(len(points))]
    for index, point in enumerate(points):
        point["fit_ms"] = round(predicted[index], 3)
    return {
        "points": points,
        "per_message_ms": round(per_message_ms, 5),
        "per_row_us": round(per_row_ms * 1000, 5),
        "r_squared": round(r_squared(extra, predicted), 5),
    }


def measure_lookup_end_to_end(conn_a, keys_by_n, per_message_ms, per_row_us, fetch_size):
    """
    t_lookup_us through postgres_fdw: the index path's time per key.

    For each n, the Foreign Scan time of
    "customer_id = ANY ((SELECT keys)::int4[])"
    minus the message cost and the returned-row cost (from
    measure_messages_and_rows) leaves the per-key work: the key list as
    text at A, parsing and planning at B, and the index lookups. A
    straight line through those leftovers gives t_lookup_us (its slope).

    Input:  conn_a; keys_by_n, {n: list of n existing keys};
            per_message_ms, per_row_us, fetch_size.
    Output: a dict with the points, the line, t_lookup_us and R^2.
    """
    sql = ("SELECT customer_id, name FROM customers "
           "WHERE customer_id = ANY ((SELECT %s::int4[])::int4[])")
    points = []
    remote_sql_text = ""
    for n in FDW_LOOKUP_N_VALUES:
        fdw_ms, rows, remote_sql_text = median_foreign_scan_ms(conn_a, sql, (keys_by_n[n],))
        messages = message_count(rows, fetch_size)
        message_ms = messages * per_message_ms
        row_ms = rows * per_row_us / 1000
        points.append({
            "n": n,
            "rows": rows,
            "fdw_ms": round(fdw_ms, 3),
            "messages": messages,
            "message_ms": round(message_ms, 3),
            "row_ms": round(row_ms, 3),
            "per_key_work_ms": round(fdw_ms - message_ms - row_ms, 3),
        })

    n_values = [point["n"] for point in points]
    work = [point["per_key_work_ms"] for point in points]
    intercept_ms, slope_ms = fit_line(n_values, work)
    return {
        "remote_sql": remote_sql_text,
        "points": points,
        "intercept_ms": round(intercept_ms, 3),
        "t_lookup_us": round(slope_ms * 1000, 4),
        "r_squared": round(line_r_squared(n_values, work, intercept_ms, slope_ms), 5),
    }


def measure_build(conn_a, keys_by_n, model_inputs):
    """
    t_build_us: site A's time per key for sjb_build_auto.

    The keys are passed as (SELECT keys): otherwise PostgreSQL would see
    a call of an IMMUTABLE function with constant arguments and compute it
    once while PLANNING, and the execution time would be 0.

    sjb_build_auto also needs the cost model's inputs, which decide which
    mode it builds. We pass realistic ones (the customers statistics and
    the constants measured above), so it builds the mode the real filter
    path would build. The mode is printed for each n.

    Input:  conn_a; keys_by_n; model_inputs, a dict with s_rows,
            row_width, t_check_ns, per_message_ms, per_row_us, fetch_size.
    Output: a dict with the points, the line, t_build_us and R^2.
    """
    arguments_sql = ("%s, 1, %s, %s, %s, 1048576, 2, %s, %s, 0, %s")
    sql = "SELECT sjb_build_auto((SELECT %s::int4[]), " + arguments_sql + ")"
    mode_sql = "SELECT get_byte(sjb_build_auto(%s::int4[], " + arguments_sql + "), 1)"
    mode_names = {1: "bloom", 2: "exact", 3: "pass"}
    points = []
    for n in BUILD_N_VALUES:
        params = (keys_by_n[n], model_inputs["s_rows"], model_inputs["row_width"],
                  BUILD_NETWORK_MBIT, model_inputs["t_check_ns"],
                  model_inputs["per_message_ms"], model_inputs["per_row_us"],
                  model_inputs["fetch_size"])
        median_ms, unused_plan = median_execution_ms(conn_a, sql, params)
        mode_number = conn_a.execute(mode_sql, params).fetchone()[0]
        points.append({"n": n, "median_ms": round(median_ms, 3), "mode": mode_names[mode_number]})

    n_values = [point["n"] for point in points]
    times = [point["median_ms"] for point in points]
    intercept_ms, slope_ms = fit_line(n_values, times)
    return {
        "points": points,
        "intercept_ms": round(intercept_ms, 3),
        "t_build_us": round(slope_ms * 1000, 4),
        "r_squared": round(line_r_squared(n_values, times, intercept_ms, slope_ms), 5),
    }


def measure_all(conn, parallel_allowed, filters, keys_by_n, num_customers):
    """
    One full calibration (check + lookup) with parallel workers off or on.

    Input:  conn; parallel_allowed; filters; keys_by_n; num_customers.
    Output: a dict with the setting used and both measurements.
    """
    setting = set_parallel(conn, parallel_allowed)
    print(f"Measuring with max_parallel_workers_per_gather = {setting} ...")
    return {
        "max_parallel_workers_per_gather": setting,
        "check": measure_check(conn, filters, num_customers),
        "count_star_bloom": measure_count_star(conn, filters["bloom"]),
        "lookup_direct": measure_lookup(conn, keys_by_n),
    }


def print_results(calibration):
    """
    Print both calibrations side by side.

    Input:  calibration, the dict that is also saved as JSON.
    Output: nothing (prints).
    """
    no_parallel = calibration["no_parallel"]
    with_parallel = calibration["with_parallel"]

    print()
    print(f"t_check_ns: full scan of {calibration['customers_rows']:,} rows with sjb_contains "
          f"({CHECK_NUM_KEYS} keys)")
    rows = []
    for mode_name in ["bloom", "exact", "pass"]:
        off = no_parallel["check"][mode_name]
        on = with_parallel["check"][mode_name]
        rows.append({
            "mode": mode_name,
            "ms_no_parallel": off["median_ms"],
            "t_check_ns_no_parallel": off["t_check_ns"],
            "ms_parallel": on["median_ms"],
            "t_check_ns_parallel": on["t_check_ns"],
            "speedup": round(off["median_ms"] / on["median_ms"], 2),
            "plan_parallel": on["plan"],
        })
    common.print_table(rows, ["mode", "ms_no_parallel", "t_check_ns_no_parallel",
                              "ms_parallel", "t_check_ns_parallel", "speedup", "plan_parallel"])
    off = no_parallel["count_star_bloom"]
    on = with_parallel["count_star_bloom"]
    print(f"  reference, SELECT count(*) with the Bloom filter: {off['median_ms']:.2f} ms "
          f"({off['plan']}) vs {on['median_ms']:.2f} ms ({on['plan']}), "
          f"speedup {off['median_ms'] / on['median_ms']:.2f}")

    print()
    print("direct lookup in site_b (reference only): customer_id = ANY(keys)")
    rows = []
    for index, n in enumerate(LOOKUP_N_VALUES):
        off = no_parallel["lookup_direct"]["points"][index]
        on = with_parallel["lookup_direct"]["points"][index]
        rows.append({
            "n": n,
            "ms_no_parallel": off["median_ms"],
            "plan_no_parallel": off["plan"],
            "ms_parallel": on["median_ms"],
            "plan_parallel": on["plan"],
        })
    common.print_table(rows, ["n", "ms_no_parallel", "plan_no_parallel",
                              "ms_parallel", "plan_parallel"])
    for label, result in [("no parallel", no_parallel), ("parallel", with_parallel)]:
        lookup = result["lookup_direct"]
        print(f"  fitted line ({label}): time_ms = {lookup['intercept_ms']:.3f} + n * "
              f"{lookup['slope_ms_per_key']:.6f}  ->  {lookup['t_lookup_us']:.4f} us per key, "
              f"R^2 = {lookup['r_squared']}")

    fdw = calibration["fdw"]
    print()
    print("per_message_ms and per_row_us: customer_id <= R through postgres_fdw, "
          "fetch_size 100 / 1000 / 10000 (scratch table calib_tmp.customers)")
    common.print_table(fdw["messages_and_rows"]["points"],
                       ["fetch_size", "R", "rows", "messages", "fdw_ms", "local_ms", "extra_ms",
                        "fit_ms", "local_plan"])
    result = fdw["messages_and_rows"]
    print(f"  fit: extra_ms = {result['per_message_ms']} ms * messages + "
          f"{result['per_row_us']} us * rows, R^2 = {result['r_squared']}")

    print()
    print("t_lookup_us: customer_id = ANY ((SELECT keys)::int4[]) through postgres_fdw")
    print("  Remote SQL: " + fdw["lookup_end_to_end"]["remote_sql"])
    common.print_table(fdw["lookup_end_to_end"]["points"],
                       ["n", "rows", "fdw_ms", "messages", "message_ms", "row_ms",
                        "per_key_work_ms"])
    result = fdw["lookup_end_to_end"]
    print(f"  fitted line: per_key_work_ms = {result['intercept_ms']} + n * "
          f"{result['t_lookup_us'] / 1000:.6f}  ->  t_lookup_us = {result['t_lookup_us']}, "
          f"R^2 = {result['r_squared']}")

    print()
    print("t_build_us: sjb_build_auto at site_a")
    common.print_table(fdw["build"]["points"], ["n", "median_ms", "mode"])
    result = fdw["build"]
    print(f"  fitted line: time_ms = {result['intercept_ms']} + n * "
          f"{result['t_build_us'] / 1000:.6f}  ->  t_build_us = {result['t_build_us']}, "
          f"R^2 = {result['r_squared']}")

    print()
    print("Suggested values (no parallel workers, as postgres_fdw's cursor runs):")
    for name, value in calibration["suggested"].items():
        print(f"  {name:15} = {value}")


def main():
    conn = common.connect("site_b")
    conn_a = common.connect("site_a")
    num_customers = count_customers(conn)
    fetch_size = common.fetch_size(conn_a)

    # Part 1 (Batch 5): site B's own work, parallel workers off and on.
    # The same keys for both.
    check_keys = random_keys(conn, CHECK_NUM_KEYS)
    filters = build_filters(conn, check_keys)
    keys_by_n = {}
    for n in LOOKUP_N_VALUES:
        keys_by_n[n] = random_keys(conn, n)

    no_parallel = measure_all(conn, False, filters, keys_by_n, num_customers)
    with_parallel = measure_all(conn, True, filters, keys_by_n, num_customers)

    # Part 2 (Batch 5b): through postgres_fdw, parallel workers off at both
    # sites.
    set_parallel(conn, False)
    set_parallel(conn_a, False)
    print("Measuring through postgres_fdw ...")
    messages_and_rows = measure_messages_and_rows(conn_a, conn)

    fdw_keys_by_n = {}
    for n in FDW_LOOKUP_N_VALUES:
        fdw_keys_by_n[n] = random_keys(conn, n)
    lookup_end_to_end = measure_lookup_end_to_end(conn_a, fdw_keys_by_n,
                                                  messages_and_rows["per_message_ms"],
                                                  messages_and_rows["per_row_us"],
                                                  fetch_size)

    build_keys_by_n = {}
    for n in BUILD_N_VALUES:
        build_keys_by_n[n] = random_keys(conn, n)
    model_inputs = {
        "s_rows": num_customers,
        "row_width": common.avg_row_width("narrow") + 7 + 4 * 2,
        "t_check_ns": no_parallel["check"]["bloom"]["t_check_ns"],
        "per_message_ms": messages_and_rows["per_message_ms"],
        "per_row_us": messages_and_rows["per_row_us"],
        "fetch_size": fetch_size,
    }
    build = measure_build(conn_a, build_keys_by_n, model_inputs)
    conn.close()
    conn_a.close()

    calibration = {
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "customers_rows": num_customers,
        "runs_per_measurement": RUNS,
        "check_filter": {"num_keys": CHECK_NUM_KEYS, "bits_per_key": CHECK_BITS_PER_KEY},
        "fetch_size": fetch_size,
        "no_parallel": no_parallel,
        "with_parallel": with_parallel,
        "fdw": {
            "messages_and_rows": messages_and_rows,
            "lookup_end_to_end": lookup_end_to_end,
            "build": build,
        },
        "suggested": {
            "t_check_ns": no_parallel["check"]["bloom"]["t_check_ns"],
            "t_lookup_us": lookup_end_to_end["t_lookup_us"],
            "per_message_ms": messages_and_rows["per_message_ms"],
            "per_row_us": messages_and_rows["per_row_us"],
            "t_build_us": build["t_build_us"],
            "fetch_size": fetch_size,
        },
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(CALIBRATION_JSON, "w") as json_file:
        json.dump(calibration, json_file, indent=2)

    print_results(calibration)
    print()
    print(f"Saved {os.path.normpath(CALIBRATION_JSON)}")


if __name__ == "__main__":
    main()
