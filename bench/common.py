"""
common.py: small helper functions shared by all benchmark scripts.

The benchmark scripts (run_experiments.py, b_sweep.py, plot.py) all need to
connect to the two sites, run EXPLAIN ANALYZE, find the Foreign Scan in the
plan, and read numbers out of it. Those jobs live here, one function each,
so the scripts themselves stay short.

Connection settings come from the usual PostgreSQL environment variables
(PGHOST, PGPORT, PGUSER; load them with `source setup/env.example.sh`).
The password, if your server checks one, comes from ~/.pgpass. libpq (the
PostgreSQL client library psycopg uses) reads both automatically, so no
password ever appears in this code.
"""

import psycopg


# The columns of customers that travel from site B to site A in each
# "width" variant. customer_id always travels, because site A needs it for
# the join. The wide variant also selects c.padding (about 80 bytes).
WIDTH_COLUMNS = {
    "narrow": ["customer_id", "name"],
    "wide": ["customer_id", "name", "padding"],
}


def connect(dbname):
    """
    Open a connection to one database (site_a or site_b).

    Input:  dbname, the database name.
    Output: a psycopg connection in autocommit mode, so every statement runs
            on its own and SET commands stay in effect for the session.
    """
    return psycopg.connect(dbname=dbname, autocommit=True)


def explain_json(conn, sql):
    """
    Run a query under EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON).

    ANALYZE really runs the query and records real row counts and times.
    VERBOSE adds the "Remote SQL" that postgres_fdw sends to site B.
    FORMAT JSON gives a tree we can walk in Python instead of parsing text.

    Input:  conn, a connection to site_a; sql, the query without EXPLAIN.
    Output: the plan dict, with keys "Plan", "Planning Time" and
            "Execution Time". (psycopg turns the json column into Python
            lists and dicts for us; the result is a list with one dict.)
    """
    row = conn.execute("EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + sql).fetchone()
    return row[0][0]


def find_foreign_scan(plan):
    """
    Walk the plan tree and return the "Foreign Scan" node.

    Input:  plan, the dict returned by explain_json.
    Output: the first node whose "Node Type" is "Foreign Scan", or None.
    """
    nodes_to_visit = [plan["Plan"]]
    while nodes_to_visit:
        node = nodes_to_visit.pop(0)
        if node["Node Type"] == "Foreign Scan":
            return node
        for child in node.get("Plans", []):
            nodes_to_visit.append(child)
    return None


def find_join_above_foreign_scan(plan):
    """
    Return the join node that sits above the Foreign Scan.

    The Foreign Scan is not always the direct child of the join: in a Hash
    Join it sits under a "Hash" node, and in a Nested Loop it may sit under
    a "Materialize" node. So we remember the path from the root down to
    each node, and once we find the Foreign Scan we go back up that path to
    the closest node whose type ends in "Join" or is "Nested Loop".

    Input:  plan, the dict returned by explain_json.
    Output: the join node dict ("Hash Join", "Nested Loop" or
            "Merge Join"), or None if there is no join above it.
    """
    # Each entry is (node, list of the nodes above it).
    nodes_to_visit = [(plan["Plan"], [])]
    while nodes_to_visit:
        node, ancestors = nodes_to_visit.pop(0)
        if node["Node Type"] == "Foreign Scan":
            # Go back up, closest ancestor first.
            for ancestor in reversed(ancestors):
                node_type = ancestor["Node Type"]
                if node_type.endswith("Join") or node_type == "Nested Loop":
                    return ancestor
            return None
        for child in node.get("Plans", []):
            nodes_to_visit.append((child, ancestors + [node]))
    return None


def join_type(plan):
    """
    Name of the join node above the Foreign Scan, for example "Hash Join".

    Input:  plan, the dict returned by explain_json.
    Output: the node type as a string, or "none" if there is no join.
    """
    join_node = find_join_above_foreign_scan(plan)
    if join_node is None:
        return "none"
    return join_node["Node Type"]


def is_nestloop_with_join_filter(plan):
    """
    True when the join above the Foreign Scan is a Nested Loop that checks
    the join condition with a "Join Filter".

    Such a plan compares every outer row with every inner row, which gets
    very slow for big inputs. run_experiments.py reruns those cases with
    enable_nestloop = off to see the time without that plan choice.

    Input:  plan, the dict returned by explain_json.
    Output: True or False.
    """
    join_node = find_join_above_foreign_scan(plan)
    if join_node is None:
        return False
    return join_node["Node Type"] == "Nested Loop" and "Join Filter" in join_node


def remote_rows(node):
    """
    How many rows site B sent back for this Foreign Scan.

    "Actual Rows" is the average per loop, so we multiply by "Actual Loops"
    (normally 1). Since PostgreSQL 18 the average can have decimals, so we
    round to a whole number.

    Input:  node, the Foreign Scan node.
    Output: the number of rows, as an int.
    """
    return round(node["Actual Rows"] * node["Actual Loops"])


def initplan_ms(plan):
    """
    Total time of the InitPlans in a plan (sub-SELECTs that run once, for
    example the one that builds the filter or the key list). In JSON
    EXPLAIN they are nodes with "Parent Relationship": "InitPlan".

    Note: when an InitPlan's result is a parameter of the Foreign Scan,
    postgres_fdw computes it while opening the remote cursor, so this time
    is ALSO inside the Foreign Scan's time.

    Input:  plan, the dict returned by explain_json.
    Output: the time in ms (0 if there is no InitPlan).
    """
    total = 0.0
    nodes_to_visit = [plan["Plan"]]
    while nodes_to_visit:
        node = nodes_to_visit.pop(0)
        if node.get("Parent Relationship") == "InitPlan":
            total = total + node["Actual Total Time"] * node["Actual Loops"]
        for child in node.get("Plans", []):
            nodes_to_visit.append(child)
    return total


def total_time_ms(plan):
    """
    Total time of the query at site A, in milliseconds: planning time plus
    execution time, as reported by EXPLAIN ANALYZE.

    Input:  plan, the dict returned by explain_json.
    Output: the time in ms, as a float.
    """
    return plan["Planning Time"] + plan["Execution Time"]


def remote_sql(node):
    """
    The SQL text postgres_fdw sent to site B for this Foreign Scan.

    Input:  node, the Foreign Scan node.
    Output: the "Remote SQL" string.
    """
    return node["Remote SQL"]


def avg_row_width(width="narrow"):
    """
    Average size in bytes of one row that site B sends back, as text.

    postgres_fdw receives rows as text, so we measure the text length of
    each column that travels and average over all customers. This runs
    directly in site_b, once per width variant.

    Input:  width, "narrow" (customer_id, name) or "wide" (customer_id,
            name, padding).
    Output: the average row width in bytes, as a float.
    """
    # Build "octet_length(customer_id::text) + octet_length(name::text) ...".
    # The column names come from WIDTH_COLUMNS above, never from user input.
    parts = []
    for column in WIDTH_COLUMNS[width]:
        parts.append("octet_length(" + column + "::text)")
    width_expression = " + ".join(parts)

    conn = connect("site_b")
    row = conn.execute("SELECT avg(" + width_expression + ") FROM customers").fetchone()
    conn.close()
    return float(row[0])


def fetch_size(conn):
    """
    How many rows postgres_fdw fetches from site B per FETCH round trip.

    It can be set as an option of the foreign table (wins) or of the
    foreign server; if neither is set, postgres_fdw uses 100.
    pg_options_to_table turns an options array like {fetch_size=500} into
    (option_name, option_value) rows.

    Input:  conn, a connection to site_a.
    Output: the fetch size, as an int.
    """
    sql = """
        SELECT coalesce(
            (SELECT option_value
             FROM pg_foreign_table t, pg_options_to_table(t.ftoptions)
             WHERE t.ftrelid = 'public.customers'::regclass
               AND option_name = 'fetch_size'),
            (SELECT option_value
             FROM pg_foreign_table t
             JOIN pg_foreign_server s ON s.oid = t.ftserver,
                  pg_options_to_table(s.srvoptions)
             WHERE t.ftrelid = 'public.customers'::regclass
               AND option_name = 'fetch_size'),
            '100')::int
    """
    return conn.execute(sql).fetchone()[0]


def check_correct(conn, baseline_sql, new_sql):
    """
    The project's correctness test: the new query must return exactly the
    same rows as the baseline query, the same number of times.

    EXCEPT ALL keeps duplicates, so both counts below are 0 only when the
    two results are identical as multisets.

    Input:  conn, a connection to site_a; baseline_sql and new_sql, the two
            queries (plain SELECTs, no EXPLAIN).
    Output: True when (baseline EXCEPT ALL new) and (new EXCEPT ALL
            baseline) both have 0 rows, otherwise False.
    """
    missing_sql = ("SELECT count(*) FROM ((" + baseline_sql + ") EXCEPT ALL ("
                   + new_sql + ")) AS missing_rows")
    extra_sql = ("SELECT count(*) FROM ((" + new_sql + ") EXCEPT ALL ("
                 + baseline_sql + ")) AS extra_rows")
    missing_count = conn.execute(missing_sql).fetchone()[0]
    extra_count = conn.execute(extra_sql).fetchone()[0]
    return missing_count == 0 and extra_count == 0


def print_table(rows, columns):
    """
    Print a list of dicts as a simple text table with aligned columns.

    Input:  rows, a list of dicts; columns, the keys to print, in order.
    Output: nothing (prints to the screen).
    """
    # Turn every value into text first, so we can measure column widths.
    text_rows = []
    for row in rows:
        text_row = []
        for column in columns:
            value = row[column]
            if value is None:
                text_row.append("")
            elif isinstance(value, float):
                text_row.append(f"{value:,.2f}")
            elif isinstance(value, int) and not isinstance(value, bool):
                text_row.append(f"{value:,}")
            else:
                text_row.append(str(value))
        text_rows.append(text_row)

    # Each column is as wide as its longest value (or its header).
    widths = []
    for index, column in enumerate(columns):
        longest = len(column)
        for text_row in text_rows:
            longest = max(longest, len(text_row[index]))
        widths.append(longest)

    header = " | ".join(column.rjust(widths[i]) for i, column in enumerate(columns))
    print(header)
    print("-+-".join("-" * width for width in widths))
    for text_row in text_rows:
        print(" | ".join(text_row[i].rjust(widths[i]) for i in range(len(columns))))
