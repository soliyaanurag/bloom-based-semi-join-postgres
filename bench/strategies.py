"""
strategies.py: the SQL text of each join strategy we compare.

Every strategy computes the same join, orders ⋈ customers, for the orders
with order_id <= n. They differ only in what site A sends to site B to cut
down the customers that come back:

  baseline       nothing: B sends all 1,000,000 customers
  exact_keys     the exact list of needed customer_ids (an int4[] array)
  bloom_fixed10  a Bloom filter with b = 10 bits per key
  bloom_dynamic  a Bloom filter whose b comes from the cost model
                 sjb_best_bits (computed in Python before the query)

Each strategy function returns two SQL strings:
  sql_text           the query itself (without EXPLAIN)
  bytes_a_to_b_sql   a query that returns how many bytes site A sends to B
                     (the filter or key list as TEXT, because postgres_fdw
                     sends query parameters as text)

The "width" argument picks the select list: "narrow" selects
o.order_id, c.name; "wide" also selects c.padding (about 100 bytes per row).

The SQL is built with Python f-strings. That is safe here because n and b
are numbers produced by our own scripts (we force them through int() and
float()), never text typed by a user.
"""


# The select list for each width variant.
SELECT_LISTS = {
    "narrow": "o.order_id, c.name",
    "wide": "o.order_id, c.name, c.padding",
}


def join_sql(n, width, extra_condition=""):
    """
    The join query shared by every strategy.

    Input:  n, keep orders with order_id <= n; width, "narrow" or "wide";
            extra_condition, an optional extra WHERE condition on c (the
            semi-join filter), starting with "AND".
    Output: the SQL text.
    """
    return (f"SELECT {SELECT_LISTS[width]} "
            f"FROM orders o "
            f"JOIN customers c ON c.customer_id = o.customer_id "
            f"WHERE o.order_id <= {int(n)} {extra_condition}")


def key_array_sql(n):
    """
    A sub-SELECT that returns the distinct customer_ids of the kept orders
    as one int4[] array. The ::int[] cast is needed so that "= ANY (...)"
    treats it as an array value, not as a row set (see CLAUDE.md).

    Input:  n, keep orders with order_id <= n.
    Output: the SQL text, in parentheses.
    """
    return (f"((SELECT array_agg(DISTINCT customer_id) "
            f"FROM orders WHERE order_id <= {int(n)})::int[])")


def bloom_filter_sql(n, b):
    """
    A sub-SELECT that builds the Bloom filter, the same way as
    queries/bloom_manual.sql:
      m = b bits per distinct key (at least 8 bits)
      k = round(0.693 * b) hash positions per key (at least 1)

    Input:  n, keep orders with order_id <= n; b, bits per key.
    Output: the SQL text, in parentheses.
    """
    return (f"(SELECT sjb_build(array_agg(DISTINCT customer_id), "
            f"greatest(8, ceil(count(DISTINCT customer_id) * {float(b)}))::int8, "
            f"greatest(1, round(0.693 * {float(b)}))::int4) "
            f"FROM orders WHERE order_id <= {int(n)})")


def baseline(n, width):
    """
    The plain join, with no semi-join filter. Site A sends nothing extra.

    Input:  n; width, "narrow" or "wide".
    Output: (sql_text, bytes_a_to_b_sql).
    """
    return join_sql(n, width), "SELECT 0"


def exact_keys(n, width):
    """
    The join with "c.customer_id = ANY (array of needed ids)". postgres_fdw
    sends the array to site B as a parameter, as text like {12,345,...}.

    Input:  n; width, "narrow" or "wide".
    Output: (sql_text, bytes_a_to_b_sql). The bytes are the octet_length of
            the array's text form, exactly what postgres_fdw sends.
    """
    sql_text = join_sql(n, width, f"AND c.customer_id = ANY {key_array_sql(n)}")
    bytes_sql = f"SELECT octet_length({key_array_sql(n)}::text)"
    return sql_text, bytes_sql


def bloom_manual(n, b, width):
    """
    The join with "sjb_contains(bloom filter, c.customer_id)". postgres_fdw
    sends the filter to site B as a parameter, as hex text.

    Input:  n; b, bits per key; width, "narrow" or "wide".
    Output: (sql_text, bytes_a_to_b_sql). The bytes are the octet_length of
            the filter's text (hex) form.
    """
    filter_sql = bloom_filter_sql(n, b)
    sql_text = join_sql(n, width, f"AND sjb_contains({filter_sql}, c.customer_id)")
    bytes_sql = f"SELECT octet_length({filter_sql}::text)"
    return sql_text, bytes_sql


def bloom_fixed10(n, width):
    """
    Bloom filter with a fixed b = 10 bits per key (about 0.8% false
    positives), the usual textbook choice.

    Input:  n; width, "narrow" or "wide".
    Output: (sql_text, bytes_a_to_b_sql).
    """
    return bloom_manual(n, 10, width)


def bloom_dynamic(n, b, width):
    """
    Bloom filter whose b was chosen by the cost model. The caller computes
    b first with dynamic_bits() below.

    Input:  n; b, the bits per key from dynamic_bits(); width.
    Output: (sql_text, bytes_a_to_b_sql).
    """
    return bloom_manual(n, b, width)


def auto_filter(n, width, s_rows, rows_per_key, row_width, network_mbit, t_check_ns):
    """
    The filter path as the system really runs it (Batch 5): the join with
    "sjb_contains(sjb_build_auto(keys, ...), c.customer_id)". sjb_build_auto
    picks the mode (BLOOM, EXACT or PASS) and the Bloom size. The message,
    row and build costs use the function's defaults (our calibrated
    values). No DISTINCT is needed: sjb_build_auto removes repeated keys.

    Input:  n; width; s_rows, rows_per_key, row_width (on the wire),
            network_mbit, t_check_ns: the cost model's inputs.
    Output: (sql_text, bytes_a_to_b_sql, filter_sql). filter_sql is the
            sub-SELECT that builds the filter, so the caller can also ask
            which mode was built.
    """
    filter_sql = (f"(SELECT sjb_build_auto(array_agg(customer_id), {float(s_rows)}, "
                  f"{float(rows_per_key)}, {float(row_width)}, {float(network_mbit)}, "
                  f"{float(t_check_ns)}) "
                  f"FROM orders WHERE order_id <= {int(n)})")
    sql_text = join_sql(n, width, f"AND sjb_contains({filter_sql}, c.customer_id)")
    bytes_sql = f"SELECT octet_length({filter_sql}::text)"
    return sql_text, bytes_sql, filter_sql


def distinct_keys(conn, n):
    """
    Number of distinct customer_ids among the kept orders (the keys that go
    into the filter).

    Input:  conn, a connection to site_a; n.
    Output: the count, as an int.
    """
    sql = f"SELECT count(DISTINCT customer_id) FROM orders WHERE order_id <= {int(n)}"
    return conn.execute(sql).fetchone()[0]


def foreign_table_rows(conn):
    """
    The planner's estimate of the number of rows of customers at site B:
    pg_class.reltuples of the foreign table at site_a (filled in by
    ANALYZE customers during setup).

    Input:  conn, a connection to site_a.
    Output: the row count, as a float.
    """
    sql = "SELECT reltuples FROM pg_class WHERE oid = 'public.customers'::regclass"
    return float(conn.execute(sql).fetchone()[0])


def dynamic_bits(conn, n, row_width):
    """
    Cost model version 1: ask sjb_best_bits for the best bits per key.

    We call it as sjb_best_bits(n_distinct, s_rows, n_distinct, row_width, 2):
      n          = number of distinct keys in the filter
      s_rows     = rows of customers (reltuples of the foreign table)
      matches    = n_distinct, because every customer_id in orders exists
                   at site B, so each distinct key matches exactly one row
      row_width  = measured text width of a returned row for this variant
      2          = the filter travels as hex text (2 bytes per byte)

    If it returns 0 ("Bloom is not worth it"), bloom_filter_sql still
    builds a tiny 8-bit filter that lets almost everything through, so the
    query stays correct.

    Input:  conn, a connection to site_a; n; row_width.
    Output: b, rounded to 2 decimals.
    """
    n_distinct = distinct_keys(conn, n)
    s_rows = foreign_table_rows(conn)
    row = conn.execute("SELECT sjb_best_bits(%s, %s, %s, %s, 2)",
                       (n_distinct, s_rows, n_distinct, row_width)).fetchone()
    return round(row[0], 2)


def all_strategies(n, width, dynamic_b):
    """
    The four strategies for one n and width, in the order we report them.

    Input:  n; width; dynamic_b, the b from dynamic_bits().
    Output: a list of (name, b, sql_text, bytes_a_to_b_sql). b is None for
            the strategies that have no Bloom filter.
    """
    baseline_sql, baseline_bytes = baseline(n, width)
    exact_sql, exact_bytes = exact_keys(n, width)
    fixed_sql, fixed_bytes = bloom_fixed10(n, width)
    dynamic_sql, dynamic_bytes = bloom_dynamic(n, dynamic_b, width)
    return [
        ("baseline", None, baseline_sql, baseline_bytes),
        ("exact_keys", None, exact_sql, exact_bytes),
        ("bloom_fixed10", 10, fixed_sql, fixed_bytes),
        ("bloom_dynamic", dynamic_b, dynamic_sql, dynamic_bytes),
    ]
