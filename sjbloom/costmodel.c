/*-------------------------------------------------------------------------
 *
 * costmodel.c
 *
 * The cost model of the Bloom filter semi-join (see costmodel.h for the
 * overview).
 *
 * The query it models: site A sends a Bloom filter (as hex text) to
 * site B; B checks all |S| rows with sjb_contains and sends back the M
 * matching rows plus the false positives; A builds the filter first.
 *
 *   time = T_CPU * work  +  T_MSG * messages  +  T_TR * bytes
 *
 * the total-time formula of Ozsu & Valduriez, "Principles of Distributed
 * Database Systems" (chapter on query optimization, cost functions).
 * Our terms:
 *   T_TR * bytes      (bytes A->B + bytes B->A) / bandwidth
 *   T_MSG * messages  messages * per_message_ms
 *   T_CPU * work      rows_back * per_row_us          (returned rows)
 *                     + s_rows * t_check_ns           (at site B)
 *                     + n * t_build_us                (at site A)
 * The constants are measured by bench/calibrate.py.
 *
 * The filter size (m bits, k hash positions per key) is chosen by the
 * sizing rules a) to d) in sjb_estimate_bloom. Every rule gives a CORRECT
 * join: a Bloom filter never drops a matching row, and the final join at
 * site A removes the false positives. The rules only change how many
 * bytes travel.
 *
 * Every function here is plain arithmetic on doubles: no SQL values, no
 * memory allocation. sjb_build_auto and sjb_bloom_report (sjbloom.c) both
 * call sjb_estimate_bloom, so the filter that is built and the filter the
 * report describes are always the same.
 *
 * What the model does NOT contain: the rest of site A's work (scanning
 * orders, collecting the keys, the final join) and planning time.
 * bench/validate_model.py compares the model with measured times.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <math.h>

#include "sjb_filter.h"
#include "costmodel.h"


/*
 * sjb_fp_rate
 *
 * What it does: the predicted false-positive rate of a Bloom filter with
 * m bits, k hash positions per key and n keys added:
 *
 *   fp = (1 - e^(-k*n/m))^k
 *
 * Source: Broder & Mitzenmacher, "Network Applications of Bloom Filters:
 * A Survey" (2004). This is the ONLY false-positive formula we use.
 *
 * Inputs:  n - number of keys in the filter
 *          m - number of bits
 *          k - number of hash positions per key
 * Output:  the probability (0 .. 1) that a key that was NOT added passes
 *
 * Why it exists: false positives are extra rows that site B sends back
 * for nothing, so the estimate needs to know how many there are. The
 * EXPLAIN hook (sjb_explain.c) prints it too.
 */
double
sjb_fp_rate(double n, double m, double k)
{
	if (n <= 0 || m <= 0 || k <= 0)
		return 0.0;				/* an empty filter lets nothing through */

	return pow(1.0 - exp(-k * n / m), k);
}

/*
 * sjb_best_bits
 *
 * What it does: the number of bits per key b that makes the total bytes
 * of the Bloom filter semi-join as small as possible:
 *
 *   b* = ln( 3.844 * (s_rows - matches) * row_width / (wire_factor * n) )
 *        / 0.4805
 *
 * How it was derived (see docs/WALKTHROUGH.md, "The cost model"):
 * with the best k, fp(b) = e^(-0.4805 * b), where 0.4805 = (ln 2)^2.
 * Total bytes = filter (wire_factor * n * b / 8) + true matches
 * (matches * row_width, does not depend on b) + false positives
 * ((s_rows - matches) * row_width * fp(b)). Setting the derivative to 0
 * gives b*. 3.844 = 8 * 0.4805. It is the same formula as the SQL function
 * sjb_best_bits in sjbloom--1.0.sql.
 *
 * Inputs:  n           - number of keys in the filter
 *          s_rows      - rows of S at site B
 *          matches     - rows of S that really match (they come back anyway)
 *          row_width   - bytes of one returned row
 *          wire_factor - wire bytes per filter byte (2 = hex text)
 * Output:  b*, or 0 when Bloom is not worth it: the value inside ln() is
 *          1 or less, so even the smallest filter costs more than the
 *          false positives it saves. Also 0 when n <= 0.
 *
 * Why it exists: the filter is sized with it (dynamic sizing, rule b).
 */
double
sjb_best_bits(double n, double s_rows, double matches,
			  double row_width, double wire_factor)
{
	double		inside_ln;

	if (n <= 0 || wire_factor <= 0)
		return 0.0;

	inside_ln = 3.844 * (s_rows - matches) * row_width / (wire_factor * n);
	if (inside_ln <= 1.0)
		return 0.0;

	return log(inside_ln) / 0.4805;
}

/*
 * sjb_bloom_bits_for
 *
 * What it does: the filter size m for n keys and b bits per key:
 *   m = max(8, ceil(n * b))
 * Inputs:  n, bits_per_key
 * Output:  m (a whole number, as a double)
 * Why it exists: rule b of sjb_estimate_bloom.
 */
double
sjb_bloom_bits_for(double n, double bits_per_key)
{
	double		m = ceil(n * bits_per_key);

	if (m < SJB_MIN_BITS)
		m = SJB_MIN_BITS;
	return m;
}

/*
 * sjb_bloom_hashes_for
 *
 * What it does: the number of hash positions per key for b bits per key:
 *   k = round(0.693 * b), at least 1 and at most 32
 * 0.693 = ln 2; k = b * ln 2 gives the lowest false-positive rate for a
 * given b (Broder & Mitzenmacher 2004).
 * Input:   bits_per_key
 * Output:  k (a whole number, as a double)
 * Why it exists: rules b and d of sjb_estimate_bloom.
 */
double
sjb_bloom_hashes_for(double bits_per_key)
{
	double		k = round(0.693 * bits_per_key);

	if (k < SJB_MIN_K)
		k = SJB_MIN_K;
	if (k > SJB_MAX_K)
		k = SJB_MAX_K;
	return k;
}

/*
 * sjb_matching_rows
 *
 * What it does: M, the number of rows of S that really match one of the
 * n keys:  M = min(s_rows, n * rows_per_key)
 * Inputs:  n, s_rows, rows_per_key (rows of S per key value; 1 when the
 *          join column is unique at site B)
 * Output:  M
 * Why it exists: these rows always come back, whatever the filter size.
 */
double
sjb_matching_rows(double n, double s_rows, double rows_per_key)
{
	double		matches = n * rows_per_key;

	if (matches > s_rows)
		matches = s_rows;
	return matches;
}

/*
 * sjb_bloom_filter_bytes
 *
 * What it does: bytes on the wire for a Bloom filter with m bits:
 *   wire_factor * (24 + ceil(m/8)) + 2
 * 24 = header, ceil(m/8) = bits, and + 2 for the "\x" in front of the
 * hex text, because postgres_fdw sends the bytea as hex text.
 * Inputs:  m, wire_factor
 * Output:  bytes A->B
 * Why it exists: the network part of the estimate; the EXPLAIN hook
 * prints it as "Bloom Bytes Sent".
 */
double
sjb_bloom_filter_bytes(double m, double wire_factor)
{
	return wire_factor * (sizeof(SjbHeader) + ceil(m / 8.0)) + 2.0;
}

/*
 * sjb_bandwidth_bytes_per_sec
 *
 * What it does: turns megabits per second into bytes per second:
 *   network_mbit * 1,000,000 / 8 = network_mbit * 125,000
 * Input:   network_mbit
 * Output:  bytes per second
 * Why it exists: network time = bytes / bandwidth.
 */
double
sjb_bandwidth_bytes_per_sec(double network_mbit)
{
	return network_mbit * 125000.0;
}

/*
 * sjb_message_count
 *
 * What it does: the number of message round trips between site A and
 * site B for one remote query:
 *   messages = 1 + ceil(rows_back / fetch_size)
 * postgres_fdw first sends the query (opening a cursor), then fetches the
 * rows fetch_size at a time (default 100), one round trip per FETCH.
 * Source: the T_MSG * #messages term of Ozsu & Valduriez; the message
 * pattern is postgres_fdw's (contrib/postgres_fdw, fetch_more_data).
 *
 * Inputs:  rows_back  - rows site B sends back
 *          fetch_size - rows per FETCH (at least 1)
 * Output:  the number of messages
 * Why it exists: many returned rows also mean many round trips.
 */
double
sjb_message_count(double rows_back, double fetch_size)
{
	return 1.0 + ceil(rows_back / fetch_size);
}

/*
 * sjb_message_seconds
 *
 * What it does: the time spent on messages:
 *   messages * per_message_ms * 1e-3      (ms -> seconds)
 * Source: T_MSG * #messages (Ozsu & Valduriez). per_message_ms is
 * measured by bench/calibrate.py.
 * Inputs:  messages, per_message_ms
 * Output:  seconds
 * Why it exists: one part of the total time.
 */
double
sjb_message_seconds(double messages, double per_message_ms)
{
	return messages * per_message_ms * 1e-3;
}

/*
 * sjb_returned_row_seconds
 *
 * What it does: the CPU time per returned row, for all rows:
 *   rows_back * per_row_us * 1e-6      (us -> seconds)
 * Site B turns every returned row into text, and site A turns the text
 * back into values and builds a tuple. This work does not depend on the
 * network speed.
 * Source: a T_CPU * #instructions term of Ozsu & Valduriez, measured per
 * row by bench/calibrate.py.
 * Inputs:  rows_back, per_row_us
 * Output:  seconds
 * Why it exists: our measurements showed a cost per returned row that
 * the bytes / bandwidth term alone did not explain.
 */
double
sjb_returned_row_seconds(double rows_back, double per_row_us)
{
	return rows_back * per_row_us * 1e-6;
}

/*
 * sjb_filter_build_seconds
 *
 * What it does: the extra work at site A, building the filter from n keys
 * (sort, remove repeats, hash the keys):
 *   n * t_build_us * 1e-6      (us -> seconds)
 * Source: a T_CPU * #instructions term of Ozsu & Valduriez, measured per
 * key by bench/calibrate.py (timing sjb_build_auto at site A).
 * Inputs:  n, t_build_us
 * Output:  seconds
 * Why it exists: plain postgres_fdw does not build a filter, so this is
 * a real extra cost of the semi-join.
 */
double
sjb_filter_build_seconds(double n, double t_build_us)
{
	return n * t_build_us * 1e-6;
}

/*
 * largest_allowed_bits
 *
 * What it does: the largest m (in bits) whose filter, header included,
 * still fits in max_filter_bytes and in SJB_MAX_BITS:
 *   m_max = min(SJB_MAX_BITS, (max_filter_bytes - 24) * 8)
 * but never less than 8 bits (the smallest filter we can build).
 *
 * Input:   max_filter_bytes
 * Output:  m_max (a whole number, as a double)
 *
 * Why it exists: rule d of sjb_estimate_bloom. A payload of P bytes holds
 * 8 * P bits, so (max_filter_bytes - 24) * 8 bits fill the allowed size
 * exactly. If max_filter_bytes is below 25, not even the 8-bit filter
 * fits; we still return 8, because a filter of some size must be sent.
 */
static double
largest_allowed_bits(double max_filter_bytes)
{
	double		payload_bytes = floor(max_filter_bytes) - sizeof(SjbHeader);
	double		bits = payload_bytes * 8.0;

	if (bits > SJB_MAX_BITS)
		bits = SJB_MAX_BITS;
	if (bits < SJB_MIN_BITS)
		bits = SJB_MIN_BITS;
	return bits;
}

/*
 * choose_filter_size
 *
 * What it does: picks m and k with the four sizing rules, in this order:
 *
 *   a) n = 0 (no keys): m = 8, k = 1. No key is added, so no bit is set
 *      and nothing passes. The join with an empty R' is empty, so this is
 *      correct.
 *   c) b* = 0 (sjb_best_bits says a Bloom filter cannot save bytes,
 *      because almost every row of S matches anyway): the smallest
 *      filter, m = 8, k = 1. With n keys almost all 8 bits are set, so
 *      almost every row passes, like plain postgres_fdw.
 *   b) normal: m = max(8, ceil(n * b*)), k = clamp(round(0.693 * b*), 1, 32).
 *   d) the filter of rule b would be bigger than max_filter_bytes (header
 *      included) or need more than SJB_MAX_BITS bits: m is capped to the
 *      largest allowed size (largest_allowed_bits), and k is recomputed
 *      for the capped bits per key m / n:
 *      k = clamp(round(0.693 * m / n), 1, 32).
 *
 * All four are correct: a Bloom filter never drops a key that was added,
 * and the final join at site A removes the false positives.
 *
 * Inputs:  inputs, estimate (matching_rows already filled in)
 * Output:  none (fills size_rule, best_bits, bits_per_key, m, k,
 *          filter_size)
 *
 * Why it exists: sjb_build_auto must build exactly the filter that the
 * estimate (and sjb_bloom_report) describes, so the rules live in one
 * place.
 */
static void
choose_filter_size(const SjbCostInputs *inputs, SjbBloomEstimate *estimate)
{
	double		n = inputs->num_keys;
	double		max_bits;

	estimate->best_bits = sjb_best_bits(n, inputs->s_rows,
										estimate->matching_rows,
										inputs->row_width,
										inputs->wire_factor);

	if (n <= 0)
	{
		/* Rule a: no keys. */
		estimate->size_rule = SJB_RULE_NO_KEYS;
		estimate->m = SJB_MIN_BITS;
		estimate->k = SJB_MIN_K;
	}
	else if (estimate->best_bits <= 0)
	{
		/* Rule c: Bloom cannot save bytes; the smallest filter. */
		estimate->size_rule = SJB_RULE_NOT_WORTH_IT;
		estimate->m = SJB_MIN_BITS;
		estimate->k = SJB_MIN_K;
	}
	else
	{
		/* Rule b: b* bits per key. */
		estimate->size_rule = SJB_RULE_BEST_BITS;
		estimate->m = sjb_bloom_bits_for(n, estimate->best_bits);
		estimate->k = sjb_bloom_hashes_for(estimate->best_bits);

		/* Rule d: too big, so cap m and recompute k. */
		max_bits = largest_allowed_bits(inputs->max_filter_bytes);
		if (estimate->m > max_bits)
		{
			estimate->size_rule = SJB_RULE_CAPPED;
			estimate->m = max_bits;
			estimate->k = sjb_bloom_hashes_for(max_bits / n);
		}
	}

	if (n > 0)
		estimate->bits_per_key = estimate->m / n;
	else
		estimate->bits_per_key = 0.0;
	estimate->filter_size = sizeof(SjbHeader) + ceil(estimate->m / 8.0);
}

/*
 * sjb_estimate_bloom
 *
 * What it does: sizes the filter (choose_filter_size, rules a-d) and
 * estimates the query with it:
 *   fp_rate         = (1 - e^(-k*n/m))^k                     (sjb_fp_rate)
 *   bytes_a_to_b    = wire_factor * (24 + ceil(m/8)) + 2
 *   rows_back       = M + (s_rows - M) * fp_rate
 *                     (every non-matching row passes with probability fp)
 *   bytes_b_to_a    = rows_back * row_width
 *   network_seconds = (bytes_a_to_b + bytes_b_to_a) / bandwidth   (T_TR)
 *   messages        = 1 + ceil(rows_back / fetch_size)
 *   message_seconds = messages * per_message_ms                  (T_MSG)
 *   row_seconds     = rows_back * per_row_us                     (T_CPU)
 *   work at A       = n * t_build_us        (build the filter)   (T_CPU)
 *   work at B       = s_rows * t_check_ns   (sjb_contains on ALL rows;
 *                     site B cannot use an index for it)         (T_CPU)
 *   total_seconds   = network + messages + rows + work at A + work at B
 *
 * Inputs:  inputs - the statistics and settings
 * Output:  *estimate is filled in
 *
 * Why it exists: this is the one place where the filter is sized and the
 * query is estimated. sjb_build_auto builds the m and k it chooses, and
 * sjb_bloom_report / sjb_bloom_estimate show the same numbers.
 */
void
sjb_estimate_bloom(const SjbCostInputs *inputs, SjbBloomEstimate *estimate)
{
	double		bandwidth = sjb_bandwidth_bytes_per_sec(inputs->network_mbit);
	double		non_matching_rows;

	memset(estimate, 0, sizeof(SjbBloomEstimate));
	estimate->matching_rows = sjb_matching_rows(inputs->num_keys, inputs->s_rows,
												inputs->rows_per_key);
	non_matching_rows = inputs->s_rows - estimate->matching_rows;

	choose_filter_size(inputs, estimate);

	estimate->fp_rate = sjb_fp_rate(inputs->num_keys, estimate->m, estimate->k);
	estimate->bytes_a_to_b = sjb_bloom_filter_bytes(estimate->m, inputs->wire_factor);
	estimate->rows_back = estimate->matching_rows + non_matching_rows * estimate->fp_rate;
	estimate->bytes_b_to_a = estimate->rows_back * inputs->row_width;

	estimate->network_seconds = (estimate->bytes_a_to_b + estimate->bytes_b_to_a) / bandwidth;
	estimate->messages = sjb_message_count(estimate->rows_back, inputs->fetch_size);
	estimate->message_seconds = sjb_message_seconds(estimate->messages, inputs->per_message_ms);
	estimate->row_seconds = sjb_returned_row_seconds(estimate->rows_back, inputs->per_row_us);
	estimate->work_at_a_seconds = sjb_filter_build_seconds(inputs->num_keys, inputs->t_build_us);
	estimate->work_at_b_seconds = inputs->s_rows * inputs->t_check_ns * 1e-9;

	estimate->total_seconds = estimate->network_seconds
		+ estimate->message_seconds
		+ estimate->row_seconds
		+ estimate->work_at_a_seconds
		+ estimate->work_at_b_seconds;
}
