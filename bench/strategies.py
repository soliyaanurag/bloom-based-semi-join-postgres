"""
strategies.py: the SQL text of each join strategy we compare.

Every strategy computes the same join, orders ⋈ customers, for the orders
with order_id <= n. They differ only in what site A sends to site B to cut
down the customers that come back:

  baseline       nothing: plain postgres_fdw, B sends all 1,000,000
                 customers
  bloom_fixed10  a Bloom filter with b = 10 bits per key (sjb_build)
  bloom_auto     a Bloom filter sized by the cost model at run time
                 (sjb_build_auto: sizing rules a-d, see sjbloom/costmodel.c)

Each strategy function returns two SQL strings:
  sql_text           the query itself (without EXPLAIN)
  bytes_a_to_b_sql   a query that returns how many bytes site A sends to B
                     (the filter as TEXT, because postgres_fdw sends query
                     parameters as text: a bytea becomes hex)

The "width" argument picks the select list: "narrow" selects
o.order_id, c.name; "wide" also selects c.padding (about 100 bytes per row).

The SQL is built with Python f-strings. That is safe here because n, b and
the statistics are numbers produced by our own scripts (we force them
through int() and float()), never text typed by a user.
"""

import common


# The select list for each width variant.
SELECT_LISTS = {
    "narrow": "o.order_id, c.name",
    "wide": "o.order_id, c.name, c.padding",
}

# customer_id is the primary key of customers: one row per key value.
ROWS_PER_KEY = 1.0


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


def bloom_filter_sql(n, b):
    """
    A sub-SELECT that builds the Bloom filter with a chosen b, the same way
    as queries/bloom_manual.sql:
      m = b bits per distinct key (at least 8 bits)
      k = round(0.693 * b) hash positions per key (at least 1)

    Input:  n, keep orders with order_id <= n; b, bits per key.
    Output: the SQL text, in parentheses.
    """
    return (f"(SELECT sjb_build(array_agg(DISTINCT customer_id), "
            f"greatest(8, ceil(count(DISTINCT customer_id) * {float(b)}))::int8, "
            f"greatest(1, round(0.693 * {float(b)}))::int4) "
            f"FROM orders WHERE order_id <= {int(n)})")


def auto_filter_sql(n, s_rows, row_width):
    """
    A sub-SELECT that builds the Bloom filter with sjb_build_auto, the same
    way as queries/auto_manual.sql. No DISTINCT is needed: sjb_build_auto
    removes NULLs and repeated keys itself. The message, row and build
    costs use the function's defaults (our calibrated values).

    Input:  n; s_rows, rows of customers; row_width, bytes of one returned
            row ON THE WIRE (common.wire_row_width).
    Output: the SQL text, in parentheses.
    """
    return (f"(SELECT sjb_build_auto(array_agg(customer_id), {float(s_rows)}, "
            f"{ROWS_PER_KEY}, {float(row_width)}, {float(common.NETWORK_MBIT)}, "
            f"{float(common.T_CHECK_NS)}) "
            f"FROM orders WHERE order_id <= {int(n)})")


def baseline(n, width):
    """
    The plain join, with no semi-join filter. Site A sends nothing extra.

    Input:  n; width, "narrow" or "wide".
    Output: (sql_text, bytes_a_to_b_sql).
    """
    return join_sql(n, width), "SELECT 0"


def bloom_query(n, width, filter_sql):
    """
    The join with "sjb_contains(filter, c.customer_id)". postgres_fdw sends
    the filter to site B as a parameter, as hex text.

    Input:  n; width; filter_sql, a sub-SELECT that builds the filter.
    Output: (sql_text, bytes_a_to_b_sql). The bytes are the octet_length of
            the filter's text (hex) form.
    """
    sql_text = join_sql(n, width, f"AND sjb_contains({filter_sql}, c.customer_id)")
    bytes_sql = f"SELECT octet_length({filter_sql}::text)"
    return sql_text, bytes_sql


def bloom_manual(n, b, width):
    """
    The Bloom join with a chosen b (used by bloom_fixed10 and b_sweep.py).

    Input:  n; b, bits per key; width, "narrow" or "wide".
    Output: (sql_text, bytes_a_to_b_sql).
    """
    return bloom_query(n, width, bloom_filter_sql(n, b))


def bloom_fixed10(n, width):
    """
    Bloom filter with a fixed b = 10 bits per key (about 0.8% false
    positives), the usual textbook choice.

    Input:  n; width, "narrow" or "wide".
    Output: (sql_text, bytes_a_to_b_sql).
    """
    return bloom_manual(n, 10, width)


def bloom_auto(n, width, s_rows, row_width):
    """
    Bloom filter sized by sjb_build_auto, as the system runs it.

    Input:  n; width; s_rows, rows of customers; row_width, bytes of one
            returned row on the wire for this width.
    Output: (sql_text, bytes_a_to_b_sql).
    """
    return bloom_query(n, width, auto_filter_sql(n, s_rows, row_width))


def auto_bits_per_key(conn, n, s_rows, row_width):
    """
    The bits per key (m / n) of the filter bloom_auto builds for this n.
    sjb_bloom_estimate runs the same C code as sjb_build_auto, so this is
    exactly the filter's size, without building it.

    Input:  conn, a connection to site_a; n; s_rows; row_width (wire).
    Output: b, rounded to 2 decimals.
    """
    n_distinct = distinct_keys(conn, n)
    row = conn.execute("SELECT bits_per_key FROM sjb_bloom_estimate(%s, %s, %s, %s, %s, %s)",
                       (n_distinct, s_rows, ROWS_PER_KEY, row_width,
                        common.NETWORK_MBIT, common.T_CHECK_NS)).fetchone()
    return round(row[0], 2)


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


def all_strategies(conn, n, width, s_rows, row_width):
    """
    The three strategies for one n and width, in the order we report them.

    Input:  conn, a connection to site_a; n; width; s_rows; row_width, the
            wire width of a returned row for this width.
    Output: a list of (name, b, sql_text, bytes_a_to_b_sql). b is None for
            the baseline, which has no Bloom filter.
    """
    baseline_sql, baseline_bytes = baseline(n, width)
    fixed_sql, fixed_bytes = bloom_fixed10(n, width)
    auto_sql, auto_bytes = bloom_auto(n, width, s_rows, row_width)
    auto_b = auto_bits_per_key(conn, n, s_rows, row_width)
    return [
        ("baseline", None, baseline_sql, baseline_bytes),
        ("bloom_fixed10", 10, fixed_sql, fixed_bytes),
        ("bloom_auto", auto_b, auto_sql, auto_bytes),
    ]
