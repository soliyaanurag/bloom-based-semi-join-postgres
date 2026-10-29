/*
 * sjbloom--1.0.sql
 *
 * Creates the SQL functions of the sjbloom extension. PostgreSQL runs this
 * file when you type CREATE EXTENSION sjbloom; in both site_a and site_b.
 *
 * Why IMMUTABLE matters: postgres_fdw only sends a WHERE condition to the
 * remote server (site B) if every function in it is IMMUTABLE. If
 * sjb_contains were not IMMUTABLE, site B would send ALL rows of S back
 * and site A would filter them itself, which defeats the whole project.
 *
 * Why STRICT matters: a STRICT function returns NULL whenever any argument
 * is NULL, without running the C code. So sjb_contains(filter, NULL) is
 * NULL, and "WHERE NULL" drops the row, exactly like a NULL key never
 * matching in an inner join. The C code never has to check for NULLs.
 */

-- Stop if someone runs this file directly with psql instead of CREATE EXTENSION.
\echo Use "CREATE EXTENSION sjbloom" to load this file. \quit

-- Step 1: sjb_build. Runs at site A: builds a Bloom filter (m bits, k
-- positions per key) from the join keys.
CREATE FUNCTION sjb_build(keys int4[], m int8, k int4)
RETURNS bytea
AS 'MODULE_PATHNAME', 'sjb_build'
LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;

-- Step 2: sjb_contains. Runs at site B inside the WHERE clause: true if the
-- key passes the filter (Bloom false positives are possible). A filter of
-- another version or mode, or a damaged one, gives an error.
CREATE FUNCTION sjb_contains(filter bytea, key int4)
RETURNS bool
AS 'MODULE_PATHNAME', 'sjb_contains'
LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;

-- Step 3: sjb_info. A debugging helper: describes a filter in one line
-- (nkeys, m, k, share of bits set, size).
CREATE FUNCTION sjb_info(filter bytea)
RETURNS text
AS 'MODULE_PATHNAME', 'sjb_info'
LANGUAGE C IMMUTABLE STRICT;

/*
 * Step 4: sjb_best_bits. How many filter bits per
 * key (b) make the total network traffic as small as possible?
 *
 * Inputs:
 *   n           number of distinct join keys at site A (keys in the filter)
 *   s_rows      number of rows of S (customers) at site B
 *   matches     rows of S that really match a key (they come back anyway)
 *   row_width   bytes of one returned row, as text
 *   wire_factor how many bytes on the wire per filter byte. Default 2,
 *               because postgres_fdw sends the bytea filter as hex text
 *               (every byte becomes 2 hex characters).
 *
 * The total bytes we send and receive, for b bits per key, are:
 *
 *   filter bytes (A to B)       n * b / 8 * wire_factor
 * + true-match rows (B to A)    matches * row_width
 * + false-positive rows (B to A)(s_rows - matches) * row_width * fp(b)
 *
 * With the best number of hash functions (k = 0.693 * b), the Bloom
 * false-positive rate is fp(b) = e^(-0.4805 * b), where 0.4805 = (ln 2)^2.
 *
 * A bigger b makes the filter cost more but lets fewer false positives
 * through. The true-match rows do not depend on b. Setting the derivative
 * of the total to 0 and solving for b gives:
 *
 *   b* = ln( 8 * 0.4805 * (s_rows - matches) * row_width
 *            / (n * wire_factor) ) / 0.4805
 *
 * If the value inside ln() is 1 or less, b* would be 0 or negative: even
 * the smallest filter costs more than the false-positive rows it removes,
 * so a Bloom filter is not worth it and the function returns 0. It also
 * returns 0 when n <= 0 (no keys, so nothing to divide by).
 *
 * Example: sjb_best_bits(10000, 1000000, 10000, 100, 2) is about 20.5.
 */
CREATE FUNCTION sjb_best_bits(n float8,
                              s_rows float8,
                              matches float8,
                              row_width float8,
                              wire_factor float8 DEFAULT 2)
RETURNS float8
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE
        -- No keys: there is no filter to size.
        WHEN n <= 0 THEN 0
        -- Value inside ln() is <= 1: Bloom is not worth it.
        WHEN 8 * 0.4805 * (s_rows - matches) * row_width / (n * wire_factor) <= 1 THEN 0
        -- Otherwise: the formula above.
        ELSE ln(8 * 0.4805 * (s_rows - matches) * row_width / (n * wire_factor)) / 0.4805
    END
$$;

/*
 * Step 5: sjb_build_auto. Runs at site A: the cost model sizes the Bloom
 * filter at RUNTIME, when the exact number of distinct keys n is known,
 * then builds it. It ALWAYS builds a Bloom filter, with these sizing rules
 * (costmodel.c, choose_filter_size):
 *
 *   a) n = 0 (no keys): m = 8, k = 1, no bits set. Nothing passes, and the
 *      join with an empty R' is empty, so this is correct.
 *   b) normal: b* = sjb_best_bits(...), m = max(8, ceil(n * b*)),
 *      k = round(0.693 * b*), at least 1 and at most 32.
 *   c) b* = 0 (a Bloom filter cannot save bytes, because almost every row
 *      of S matches): the smallest filter, m = 8, k = 1. Almost everything
 *      passes, like plain postgres_fdw.
 *   d) the filter of rule b would be bigger than max_filter_bytes (header
 *      included) or need more than 64 MB of bits: m is capped to the
 *      largest allowed size and k is recomputed for the capped bits per
 *      key, k = round(0.693 * m / n).
 *
 * Every rule is correct: a Bloom filter never drops a matching row, and
 * the final join at site A removes the false positives.
 *
 * Inputs:
 *   keys             site A's join keys (NULLs and repeats are fine)
 *   s_rows           rows of S (customers) at site B
 *   rows_per_key     rows of S per key value (1 for a unique column)
 *   row_width        bytes of one returned row ON THE WIRE
 *                    (text width + 7 + 4 * number of columns)
 *   network_mbit     network speed in megabits per second
 *   t_check_ns       site B's time to scan + check one row (calibrated)
 *   max_filter_bytes the largest filter allowed (rule d)
 *   wire_factor      wire bytes per filter byte (2: hex text)
 *   per_message_ms   cost of one message round trip A <-> B
 *   per_row_us       CPU cost of one returned row (text out at B, text
 *                    in at A)
 *   t_build_us       site A's time per key to build the filter
 *   fetch_size       rows per postgres_fdw FETCH (postgres_fdw default 100)
 *
 * Only s_rows, rows_per_key, row_width, max_filter_bytes and wire_factor
 * change the filter (its size depends only on bytes). The time arguments
 * are checked and belong to the same estimate that sjb_bloom_report shows.
 *
 * The defaults of per_message_ms, per_row_us and t_build_us are the
 * values bench/calibrate.py measured on OUR machine
 * (results/calibration.json); pass your own values after calibrating.
 *
 * All settings are ARGUMENTS, never read from configuration settings, so
 * the function really is IMMUTABLE: the same inputs always give the same
 * filter. The formulas are in costmodel.c.
 */
CREATE FUNCTION sjb_build_auto(keys int4[],
                               s_rows float8,
                               rows_per_key float8,
                               row_width float8,
                               network_mbit float8,
                               t_check_ns float8,
                               max_filter_bytes int8 DEFAULT 1048576,
                               wire_factor float8 DEFAULT 2,
                               per_message_ms float8 DEFAULT 0.06194,
                               per_row_us float8 DEFAULT 0.54819,
                               t_build_us float8 DEFAULT 0.245,
                               fetch_size int4 DEFAULT 100)
RETURNS bytea
AS 'MODULE_PATHNAME', 'sjb_build_auto'
LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;

/*
 * Step 6: sjb_bloom_report. A readable multi-line report of the cost model
 * for n distinct keys. It runs the SAME C code as sjb_build_auto
 * (sjb_estimate_bloom in costmodel.c), so it describes exactly the filter
 * sjb_build_auto would build for n keys: the inputs, M (rows of S that
 * really match), b*, which sizing rule (a-d) applied, m, k, the predicted
 * false-positive rate, bytes each way, rows back, messages, and the time
 * in ms (Ozsu & Valduriez):
 *
 *   T_TR * bytes      (bytes A->B + bytes B->A) / bandwidth
 * + T_MSG * messages  (1 + ceil(rows back / fetch_size)) * per_message_ms
 * + T_CPU * work      rows back * per_row_us
 *                     + n * t_build_us        (build the filter at A)
 *                     + s_rows * t_check_ns   (check every row at B)
 *
 * The arguments are those of sjb_build_auto, with n instead of the keys
 * (and in a different order: max_filter_bytes and wire_factor last).
 */
CREATE FUNCTION sjb_bloom_report(n float8,
                                 s_rows float8,
                                 rows_per_key float8,
                                 row_width float8,
                                 network_mbit float8,
                                 t_check_ns float8,
                                 per_message_ms float8 DEFAULT 0.06194,
                                 per_row_us float8 DEFAULT 0.54819,
                                 t_build_us float8 DEFAULT 0.245,
                                 fetch_size int4 DEFAULT 100,
                                 max_filter_bytes int8 DEFAULT 1048576,
                                 wire_factor float8 DEFAULT 2)
RETURNS text
AS 'MODULE_PATHNAME', 'sjb_bloom_report'
LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;

/*
 * Step 7: sjb_bloom_estimate. The same numbers as sjb_bloom_report, as one
 * row with one column each, so scripts (bench/validate_model.py) can read
 * them without parsing text. size_rule is 'a', 'b', 'c' or 'd'; the
 * *_ms columns are the parts of the time and their sum total_ms.
 */
CREATE FUNCTION sjb_bloom_estimate(n float8,
                                   s_rows float8,
                                   rows_per_key float8,
                                   row_width float8,
                                   network_mbit float8,
                                   t_check_ns float8,
                                   per_message_ms float8 DEFAULT 0.06194,
                                   per_row_us float8 DEFAULT 0.54819,
                                   t_build_us float8 DEFAULT 0.245,
                                   fetch_size int4 DEFAULT 100,
                                   max_filter_bytes int8 DEFAULT 1048576,
                                   wire_factor float8 DEFAULT 2,
                                   OUT size_rule text,
                                   OUT matching_rows float8,
                                   OUT best_bits float8,
                                   OUT bits_per_key float8,
                                   OUT m int8,
                                   OUT k int4,
                                   OUT fp_rate float8,
                                   OUT bytes_a_to_b float8,
                                   OUT rows_back float8,
                                   OUT bytes_b_to_a float8,
                                   OUT messages float8,
                                   OUT network_ms float8,
                                   OUT messages_ms float8,
                                   OUT rows_ms float8,
                                   OUT work_a_ms float8,
                                   OUT work_b_ms float8,
                                   OUT total_ms float8)
RETURNS record
AS 'MODULE_PATHNAME', 'sjb_bloom_estimate'
LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
