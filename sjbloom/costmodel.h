/*-------------------------------------------------------------------------
 *
 * costmodel.h
 *
 * The cost model of the Bloom filter semi-join (sjbloom extension).
 *
 * Site A sends a Bloom filter to site B, B checks ALL rows of S with
 * "sjb_contains(filter, customer_id)" and sends back the rows that pass:
 * the M rows that really match, plus the false positives.
 *
 * The model does two jobs:
 *
 *   1. SIZE the filter: m bits and k hash positions per key, chosen to
 *      make the total bytes (filter A->B + rows B->A) as small as possible
 *      (sjb_best_bits), with the sizing rules a) to d) of
 *      sjb_estimate_bloom in costmodel.c.
 *
 *   2. ESTIMATE the TIME of the remote part of the join, with the
 *      total-time formula of Ozsu & Valduriez ("Principles of Distributed
 *      Database Systems", chapter on query optimization, cost functions):
 *
 *        time = T_CPU * work  +  T_MSG * messages  +  T_TR * bytes
 *
 *        T_CPU * work     work at site B (checking every row of S), work
 *                         at site A (building the filter), and the work
 *                         per returned row (text out at B, text in at A)
 *        T_MSG * messages one cost per message round trip: postgres_fdw
 *                         sends the query, then fetches fetch_size rows
 *                         per FETCH
 *        T_TR * bytes     bytes moved / bandwidth
 *
 * costmodel.c only does arithmetic on doubles. It knows nothing about SQL
 * values; sjbloom.c turns SQL arguments into an SjbCostInputs and calls it.
 *
 * Every .c file must include "postgres.h" BEFORE this file.
 *
 *-------------------------------------------------------------------------
 */
#ifndef SJB_COSTMODEL_H
#define SJB_COSTMODEL_H

/*
 * Default settings. The SQL functions use the same numbers as defaults.
 *
 * WIRE_FACTOR: postgres_fdw sends a bytea parameter as hex text, so every
 *   filter byte becomes 2 bytes on the wire.
 * MAX_FILTER_BYTES: the largest filter (header included) sjb_build_auto
 *   builds (1 MB); a bigger one is capped to this size (rule d).
 */
#define SJB_DEFAULT_WIRE_FACTOR 2.0
#define SJB_DEFAULT_MAX_FILTER_BYTES 1048576.0

/*
 * The sizing rule sjb_estimate_bloom used (see costmodel.c):
 *   a) no keys at all
 *   b) the normal case: b* bits per key from sjb_best_bits
 *   c) b* = 0: a Bloom filter cannot save bytes
 *   d) the b* filter is too big, so m is capped
 */
#define SJB_RULE_NO_KEYS 'a'
#define SJB_RULE_BEST_BITS 'b'
#define SJB_RULE_NOT_WORTH_IT 'c'
#define SJB_RULE_CAPPED 'd'

/*
 * Everything the cost model needs to know. All values are doubles, so the
 * formulas can be written exactly as on paper.
 */
typedef struct SjbCostInputs
{
	double		num_keys;		/* n: distinct join keys at site A */
	double		s_rows;			/* number of rows of S at site B */
	double		rows_per_key;	/* rows of S per distinct key value */
	double		row_width;		/* bytes of one returned row ON THE WIRE */
	double		network_mbit;	/* network speed in megabits per second */
	double		t_check_ns;		/* site B: ns to scan + check one row */
	double		max_filter_bytes;	/* largest filter allowed (header included) */
	double		wire_factor;	/* wire bytes per filter byte (2 = hex) */
	double		per_message_ms;	/* T_MSG: ms per message round trip */
	double		per_row_us;		/* us per returned row (text out + in) */
	double		t_build_us;		/* site A: us per key to build the filter */
	double		fetch_size;		/* rows per postgres_fdw FETCH (100) */
} SjbCostInputs;

/* The size of the Bloom filter and the estimate of the query with it. */
typedef struct SjbBloomEstimate
{
	char		size_rule;		/* SJB_RULE_...: 'a', 'b', 'c' or 'd' */
	double		matching_rows;	/* M = rows of S that really match */
	double		best_bits;		/* b* from sjb_best_bits (0 = not worth it) */
	double		bits_per_key;	/* m / n of the filter (0 when n = 0) */
	double		m;				/* number of bits */
	double		k;				/* hash positions per key */
	double		fp_rate;		/* predicted false-positive rate */
	double		filter_size;	/* the filter value itself (header included) */
	double		bytes_a_to_b;	/* the filter, as hex text on the wire */
	double		rows_back;		/* rows site B sends back */
	double		bytes_b_to_a;	/* rows_back * row_width */
	double		network_seconds;	/* (bytes_a_to_b + bytes_b_to_a) / bandwidth */
	double		messages;		/* 1 + ceil(rows_back / fetch_size) */
	double		message_seconds;	/* messages * per_message_ms */
	double		row_seconds;	/* rows_back * per_row_us */
	double		work_at_a_seconds;	/* building the filter at site A */
	double		work_at_b_seconds;	/* checking every row of S at site B */
	double		total_seconds;	/* the sum of the five times above */
} SjbBloomEstimate;

/* Single formulas (costmodel.c explains each one). */
extern double sjb_fp_rate(double n, double m, double k);
extern double sjb_best_bits(double n, double s_rows, double matches,
							double row_width, double wire_factor);
extern double sjb_bloom_bits_for(double n, double bits_per_key);
extern double sjb_bloom_hashes_for(double bits_per_key);
extern double sjb_matching_rows(double n, double s_rows, double rows_per_key);
extern double sjb_bloom_filter_bytes(double m, double wire_factor);
extern double sjb_bandwidth_bytes_per_sec(double network_mbit);
extern double sjb_message_count(double rows_back, double fetch_size);
extern double sjb_message_seconds(double messages, double per_message_ms);
extern double sjb_returned_row_seconds(double rows_back, double per_row_us);
extern double sjb_filter_build_seconds(double n, double t_build_us);

/*
 * The whole model: sizes the filter (rules a-d) and fills in the
 * estimate. sjb_build_auto and sjb_bloom_report both call it.
 */
extern void sjb_estimate_bloom(const SjbCostInputs *inputs,
							   SjbBloomEstimate *estimate);

#endif							/* SJB_COSTMODEL_H */
