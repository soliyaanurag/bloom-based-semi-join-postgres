/*-------------------------------------------------------------------------
 *
 * sjbloom.c
 *
 * The SQL-callable functions of the sjbloom extension:
 *
 *   sjb_build(keys int4[], m int8, k int4) RETURNS bytea
 *       Site A: build a Bloom filter with a given size.
 *   sjb_build_auto(keys int4[], s_rows, rows_per_key, row_width,
 *                  network_mbit, t_check_ns,
 *                  max_filter_bytes, wire_factor,
 *                  per_message_ms, per_row_us, t_build_us,
 *                  fetch_size) RETURNS bytea
 *       Site A: let the cost model size the Bloom filter (sizing rules
 *       a-d in costmodel.c), then build it.
 *   sjb_contains(filter bytea, key int4) RETURNS bool
 *       Site B: does this key pass the filter?
 *   sjb_info(filter bytea) RETURNS text
 *       Debugging: describe a filter in one line of text.
 *   sjb_bloom_report(n, s_rows, rows_per_key, row_width, network_mbit,
 *                    t_check_ns, per_message_ms, per_row_us, t_build_us,
 *                    fetch_size, max_filter_bytes, wire_factor) RETURNS text
 *       A readable report of the cost model for n keys.
 *   sjb_bloom_estimate(the same arguments) RETURNS record
 *       The same numbers as columns, for the benchmark scripts.
 *
 * This file turns SQL values (arrays, bytea, float8) into plain C data and
 * back. The Bloom filter logic is in bloom.c, the cost model in
 * costmodel.c, and the EXPLAIN lines in sjb_explain.c.
 *
 * Filter layout (see sjb_filter.h):
 *   [varlena length header][24-byte SjbHeader][ceil(m/8) bytes of bits]
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <math.h>
#include <stdlib.h>

#include "access/htup_details.h"
#include "catalog/pg_type.h"
#include "fmgr.h"
#include "funcapi.h"
#include "lib/stringinfo.h"
#include "utils/array.h"
#include "utils/builtins.h"

#include "sjb_filter.h"
#include "costmodel.h"

/*
 * PG_MODULE_MAGIC puts a small "magic block" into sjbloom.so. The server
 * checks it when loading the library, to refuse a library that was built
 * for a different PostgreSQL major version. (The library's _PG_init, which
 * runs when it is loaded, is in sjb_explain.c.)
 */
PG_MODULE_MAGIC;


/* ------------------------------------------------------------------------
 * Part 1: checking a filter
 * ------------------------------------------------------------------------
 */

/*
 * check_bloom_parameters
 *
 * What it does: raises an error if m or k is outside the allowed range.
 *
 * Inputs:  m - the number of bits in the Bloom filter
 *          k - the number of hash positions per key
 * Output:  none; returns normally only if both values are valid
 *
 * Why it exists: sjb_build uses it to check the user's arguments, and
 * read_filter uses it to report bad values stored in a filter header.
 * A value of m = 0 would cause a division by zero, and a huge m would try
 * to allocate too much memory.
 */
static void
check_bloom_parameters(int64 m, int32 k)
{
	/*
	 * ereport(ERROR, ...) is how PostgreSQL raises an SQL error. It stops
	 * this function, aborts the current query and shows the message to the
	 * user. errcode sets the SQLSTATE error code and errmsg the text.
	 */
	if (m < SJB_MIN_BITS || m > SJB_MAX_BITS)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("sjbloom: m must be between %d and %d bits, got %lld",
						SJB_MIN_BITS, SJB_MAX_BITS, (long long) m)));

	if (k < SJB_MIN_K || k > SJB_MAX_K)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("sjbloom: k must be between %d and %d, got %d",
						SJB_MIN_K, SJB_MAX_K, k)));
}

/*
 * read_filter
 *
 * What it does: copies the 24-byte header out of a filter and checks the
 * whole filter with sjb_check_filter (bloom.c). If anything is wrong, it
 * raises a clear error saying what.
 *
 * Inputs:  filter_data - pointer to the first byte after the varlena
 *                        length header (the start of SjbHeader)
 *          filter_size - number of bytes at filter_data
 * Output:  *header is filled in; returns normally only for a valid Bloom
 *          filter of our version
 *
 * Why it exists: every function that reads a filter must first make sure
 * the bytes really are a Bloom filter of our version, so it never reads
 * one with the wrong layout or outside the payload.
 */
static void
read_filter(const char *filter_data, Size filter_size, SjbHeader *header)
{
	int			problem = sjb_check_filter(filter_data, filter_size, header);

	if (problem == SJB_FILTER_OK)
		return;

	if (problem == SJB_FILTER_TOO_SMALL)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("sjbloom: filter is too small (%zu bytes, need at least %zu)",
						filter_size, sizeof(SjbHeader))));

	/*
	 * A filter of another version was built with a different algorithm
	 * (for example version 1 = classic double hashing). Reading it with our
	 * bit positions could drop rows that really match, so we refuse it.
	 * This happens when site A and site B run different builds of sjbloom.
	 * errhint adds a "HINT:" line that tells the user how to fix it.
	 */
	if (problem == SJB_FILTER_WRONG_VERSION)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("sjbloom: filter has version %d, but this sjbloom understands only version %d",
						header->version, SJB_VERSION),
				 errhint("Site A and site B must run the same sjbloom build. "
						 "Install it on both sites and rebuild the filter.")));

	/* Only the Bloom mode exists in this build. */
	if (problem == SJB_FILTER_WRONG_MODE)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("sjbloom: filter has mode %d, but this sjbloom understands only mode %d (Bloom)",
						header->mode, SJB_MODE_BLOOM),
				 errhint("Site A and site B must run the same sjbloom build.")));

	if (problem == SJB_FILTER_BAD_PARAMETERS)
		check_bloom_parameters((int64) header->m, (int32) header->k);

	if (problem == SJB_FILTER_WRONG_SIZE)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("sjbloom: Bloom payload is %zu bytes, expected %llu",
						filter_size - sizeof(SjbHeader),
						(unsigned long long) sjb_bloom_payload_bytes(header->m))));

	/* Not reached: every problem code is handled above. */
	ereport(ERROR,
			(errcode(ERRCODE_DATA_CORRUPTED),
			 errmsg("sjbloom: damaged filter (problem %d)", problem)));
}


/* ------------------------------------------------------------------------
 * Part 2: building a filter
 * ------------------------------------------------------------------------
 */

/*
 * new_filter
 *
 * What it does: allocates a new filter bytea with room for the 24-byte
 * header and payload_bytes bytes of payload, all set to 0.
 *
 * Input:  payload_bytes - size of the payload
 * Output: the new bytea (its header must still be written)
 *
 * Why it exists: keeps the allocation in one place.
 */
static bytea *
new_filter(Size payload_bytes)
{
	Size		total_size = VARHDRSZ + sizeof(SjbHeader) + payload_bytes;
	bytea	   *filter;

	/*
	 * A bytea is a "varlena": a 4-byte length header (VARHDRSZ bytes)
	 * followed by the data. palloc0 allocates memory in the current
	 * PostgreSQL memory context and fills it with zeros, so all Bloom bits
	 * start at 0. (palloc memory is freed automatically at the end of the
	 * query, so we never call free.) SET_VARSIZE writes the total size
	 * into the length header.
	 */
	filter = (bytea *) palloc0(total_size);
	SET_VARSIZE(filter, total_size);
	return filter;
}

/*
 * write_header
 *
 * What it does: fills in an SjbHeader (mode BLOOM) and copies it to the
 * start of the filter's data.
 *
 * Inputs:  filter - a bytea from new_filter
 *          k, m   - Bloom parameters
 *          nkeys  - number of keys the filter was built from
 * Output:  none (writes into the filter)
 *
 * Why it exists: memcpy is used because the data inside a bytea may not
 * be aligned for 8-byte writes.
 */
static void
write_header(bytea *filter, int k, uint64 m, uint32 nkeys)
{
	SjbHeader	header;

	memset(&header, 0, sizeof(header));
	header.version = SJB_VERSION;
	header.mode = SJB_MODE_BLOOM;
	header.k = (uint16) k;
	header.nkeys = nkeys;
	header.m = m;
	header.seed = SJB_SEED;

	/* VARDATA gives a pointer to the first byte after the length header. */
	memcpy(VARDATA(filter), &header, sizeof(SjbHeader));
}

/*
 * build_bloom_filter
 *
 * What it does: builds a Bloom filter with m bits and k positions per key
 * and adds every key of the C array to it.
 *
 * Inputs:  keys, num_keys - the keys (no NULLs; num_keys may be 0)
 *          m, k           - filter size and positions per key (valid)
 * Output:  the new filter
 *
 * Why it exists: used by sjb_build and sjb_build_auto.
 */
static bytea *
build_bloom_filter(const int32 *keys, int num_keys, uint64 m, int k)
{
	bytea	   *filter = new_filter(sjb_bloom_payload_bytes(m));
	uint8	   *bits = (uint8 *) (VARDATA(filter) + sizeof(SjbHeader));
	int			i;

	for (i = 0; i < num_keys; i++)
		sjb_bloom_add(bits, m, k, SJB_SEED, keys[i]);

	write_header(filter, k, m, (uint32) num_keys);
	return filter;
}

/*
 * compare_int32
 *
 * What it does: the comparison function qsort needs: negative if the
 * first key is smaller, positive if it is bigger, 0 if equal.
 * Inputs:  a, b - pointers to two int32 values
 * Output:  -1, 0 or 1
 * Why it exists: qsort sorts any kind of data, so we must tell it how to
 * compare two keys. We compare with < and > instead of returning
 * (x - y), because x - y can overflow for very large or very negative
 * keys and then give the wrong sign.
 */
static int
compare_int32(const void *a, const void *b)
{
	int32		first = *(const int32 *) a;
	int32		second = *(const int32 *) b;

	if (first < second)
		return -1;
	if (first > second)
		return 1;
	return 0;
}

/*
 * remove_duplicates
 *
 * What it does: removes repeated keys from a SORTED array, in place.
 * Equal keys are next to each other after sorting, so we keep a key only
 * if it differs from the last key we kept.
 *
 * Inputs:  keys, num_keys - a sorted array
 * Output:  the new number of keys (the first ones in the array)
 *
 * Why it exists: n must count DISTINCT keys.
 */
static int
remove_duplicates(int32 *keys, int num_keys)
{
	int			num_unique = 0;
	int			i;

	for (i = 0; i < num_keys; i++)
	{
		if (num_unique == 0 || keys[i] != keys[num_unique - 1])
		{
			keys[num_unique] = keys[i];
			num_unique = num_unique + 1;
		}
	}
	return num_unique;
}

/*
 * collect_unique_keys
 *
 * What it does: copies the non-NULL elements of an int4[] into a C array,
 * sorts it and removes duplicates.
 *
 * Inputs:  keys_array - the SQL array
 * Output:  the palloc'd C array; *num_keys_out = number of unique keys
 *
 * Why it exists: sjb_build_auto needs the true number of distinct keys
 * (the n of the cost model).
 */
static int32 *
collect_unique_keys(ArrayType *keys_array, int *num_keys_out)
{
	Datum	   *elements;
	bool	   *nulls;
	int			num_elements;
	int32	   *keys;
	int			num_keys = 0;
	int			i;

	/*
	 * deconstruct_array_builtin unpacks an SQL array of a built-in type
	 * (here INT4OID = int4) into two C arrays: elements[i] holds the value
	 * and nulls[i] says whether that element is NULL.
	 */
	deconstruct_array_builtin(keys_array, INT4OID,
							  &elements, &nulls, &num_elements);

	/* palloc is PostgreSQL's malloc (freed at the end of the query). */
	keys = (int32 *) palloc(sizeof(int32) * (num_elements + 1));

	for (i = 0; i < num_elements; i++)
	{
		if (nulls[i])
			continue;			/* a NULL key never matches in a join */

		/* DatumGetInt32 unwraps a Datum back into a C int32. */
		keys[num_keys] = DatumGetInt32(elements[i]);
		num_keys = num_keys + 1;
	}

	qsort(keys, num_keys, sizeof(int32), compare_int32);
	*num_keys_out = remove_duplicates(keys, num_keys);
	return keys;
}


/* ------------------------------------------------------------------------
 * Part 3: reading cost model arguments
 * ------------------------------------------------------------------------
 */

/*
 * check_cost_argument
 *
 * What it does: raises an error if a cost model argument is not a normal
 * number, is negative, or is 0 when 0 is not allowed.
 *
 * Inputs:  name         - the argument name, for the error message
 *          value        - its value
 *          zero_allowed - false for values we divide by (network_mbit,
 *                         wire_factor, fetch_size)
 * Output:  none; returns normally only if the value is valid
 *
 * Why it exists: a NaN, an infinity or a negative count would make the
 * estimates meaningless, and a zero bandwidth would divide by zero.
 */
static void
check_cost_argument(const char *name, double value, bool zero_allowed)
{
	bool		is_valid = true;

	if (isnan(value) || isinf(value) || value < 0)
		is_valid = false;
	if (value == 0 && !zero_allowed)
		is_valid = false;

	if (!is_valid)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("sjbloom: %s must be a number %s 0, got %g",
						name, zero_allowed ? ">=" : ">", value)));
}

/*
 * check_cost_inputs
 *
 * What it does: checks every field of an SjbCostInputs with
 * check_cost_argument.
 * Input:  inputs
 * Output: none; returns normally only if all values are valid
 * Why it exists: every SQL function that uses the cost model needs the
 * same checks.
 */
static void
check_cost_inputs(const SjbCostInputs *inputs)
{
	check_cost_argument("n", inputs->num_keys, true);
	check_cost_argument("s_rows", inputs->s_rows, true);
	check_cost_argument("rows_per_key", inputs->rows_per_key, true);
	check_cost_argument("row_width", inputs->row_width, true);
	check_cost_argument("network_mbit", inputs->network_mbit, false);
	check_cost_argument("t_check_ns", inputs->t_check_ns, true);
	check_cost_argument("max_filter_bytes", inputs->max_filter_bytes, true);
	check_cost_argument("wire_factor", inputs->wire_factor, false);
	check_cost_argument("per_message_ms", inputs->per_message_ms, true);
	check_cost_argument("per_row_us", inputs->per_row_us, true);
	check_cost_argument("t_build_us", inputs->t_build_us, true);
	check_cost_argument("fetch_size", inputs->fetch_size, false);
}

/*
 * read_report_arguments
 *
 * What it does: reads the 12 arguments of sjb_bloom_report and
 * sjb_bloom_estimate (n, s_rows, rows_per_key, row_width, network_mbit,
 * t_check_ns, per_message_ms, per_row_us, t_build_us, fetch_size,
 * max_filter_bytes, wire_factor) into an SjbCostInputs and checks them.
 *
 * Inputs:  fcinfo - the function call info of the SQL function (it holds
 *                   the arguments; the PG_GETARG_ macros read from it)
 * Output:  *inputs is filled in
 *
 * Why it exists: both SQL functions take the same arguments, and they
 * must give the cost model exactly the same inputs.
 */
static void
read_report_arguments(FunctionCallInfo fcinfo, SjbCostInputs *inputs)
{
	/* PG_GETARG_FLOAT8(i) reads argument number i as a C double. */
	inputs->num_keys = PG_GETARG_FLOAT8(0);
	inputs->s_rows = PG_GETARG_FLOAT8(1);
	inputs->rows_per_key = PG_GETARG_FLOAT8(2);
	inputs->row_width = PG_GETARG_FLOAT8(3);
	inputs->network_mbit = PG_GETARG_FLOAT8(4);
	inputs->t_check_ns = PG_GETARG_FLOAT8(5);
	inputs->per_message_ms = PG_GETARG_FLOAT8(6);
	inputs->per_row_us = PG_GETARG_FLOAT8(7);
	inputs->t_build_us = PG_GETARG_FLOAT8(8);
	inputs->fetch_size = PG_GETARG_INT32(9);
	inputs->max_filter_bytes = (double) PG_GETARG_INT64(10);
	inputs->wire_factor = PG_GETARG_FLOAT8(11);

	check_cost_inputs(inputs);
}

/*
 * size_rule_text
 *
 * What it does: a one-line description of a sizing rule (a-d).
 * Input:  rule - SJB_RULE_NO_KEYS, _BEST_BITS, _NOT_WORTH_IT or _CAPPED
 * Output: the description
 * Why it exists: sjb_bloom_report says which rule applied.
 */
static const char *
size_rule_text(char rule)
{
	if (rule == SJB_RULE_NO_KEYS)
		return "a) no keys: m = 8, k = 1, no bits set (nothing passes)";
	if (rule == SJB_RULE_NOT_WORTH_IT)
		return "c) b* = 0, Bloom cannot save bytes: smallest filter m = 8, k = 1 (almost everything passes)";
	if (rule == SJB_RULE_CAPPED)
		return "d) the b* filter is too big: m capped to the largest allowed size, k = round(0.693 * m / n)";
	return "b) normal: m = max(8, ceil(n * b*)), k = round(0.693 * b*)";
}


/* ------------------------------------------------------------------------
 * Part 4: the SQL functions
 * ------------------------------------------------------------------------
 */

/*
 * PG_FUNCTION_INFO_V1 tells PostgreSQL that the C function with this name
 * uses the standard ("version 1") calling convention for SQL functions:
 * it is declared as  Datum name(PG_FUNCTION_ARGS)  and reads its arguments
 * with the PG_GETARG_... macros.
 */
PG_FUNCTION_INFO_V1(sjb_build);

/*
 * sjb_build(keys int4[], m int8, k int4) RETURNS bytea
 *
 * What it does: builds a Bloom filter with m bits and k positions per key
 * and adds every non-NULL key of the array to it.
 *
 * Inputs:  keys - array of join keys (NULL elements are skipped)
 *          m    - number of bits (8 .. 8*64*1024*1024)
 *          k    - number of positions per key (1 .. 32)
 * Output:  a bytea: 24-byte header (mode BLOOM) followed by ceil(m/8)
 *          bytes of bits. header.nkeys = number of keys added (repeated
 *          keys are counted each time).
 *
 * Why it exists: this runs at site A; the result is sent to site B. It is
 * the "manual" builder where the caller chooses m and k.
 */
Datum
sjb_build(PG_FUNCTION_ARGS)
{
	/*
	 * PG_GETARG_ARRAYTYPE_P, PG_GETARG_INT64 and PG_GETARG_INT32 read
	 * argument number 0, 1, 2 of the SQL call as C values.
	 */
	ArrayType  *keys_array = PG_GETARG_ARRAYTYPE_P(0);
	int64		m = PG_GETARG_INT64(1);
	int32		k = PG_GETARG_INT32(2);
	Datum	   *elements;
	bool	   *nulls;
	int			num_elements;
	int32	   *keys;
	int			num_keys = 0;
	int			i;

	check_bloom_parameters(m, k);

	deconstruct_array_builtin(keys_array, INT4OID,
							  &elements, &nulls, &num_elements);

	/* Copy the non-NULL keys into a plain C array (no sorting here). */
	keys = (int32 *) palloc(sizeof(int32) * (num_elements + 1));
	for (i = 0; i < num_elements; i++)
	{
		if (nulls[i])
			continue;			/* a NULL key never matches in a join */
		keys[num_keys] = DatumGetInt32(elements[i]);
		num_keys = num_keys + 1;
	}

	/* PG_RETURN_BYTEA_P returns the bytea to SQL. */
	PG_RETURN_BYTEA_P(build_bloom_filter(keys, num_keys, (uint64) m, k));
}


PG_FUNCTION_INFO_V1(sjb_build_auto);

/*
 * sjb_build_auto(keys int4[], s_rows float8, rows_per_key float8,
 *                row_width float8, network_mbit float8, t_check_ns float8,
 *                max_filter_bytes int8, wire_factor float8,
 *                per_message_ms float8, per_row_us float8,
 *                t_build_us float8, fetch_size int4) RETURNS bytea
 *
 * What it does: ALWAYS builds a Bloom filter, sized by the cost model:
 *   1. collect the non-NULL keys, sort them, remove duplicates -> n
 *   2. sjb_estimate_bloom (costmodel.c) picks m and k with the sizing
 *      rules:
 *        a) n = 0: m = 8, k = 1, no bits set (nothing passes)
 *        b) normal: m = max(8, ceil(n*b*)), k = round(0.693*b*)
 *        c) b* = 0: m = 8, k = 1 (almost everything passes)
 *        d) too big for max_filter_bytes or SJB_MAX_BITS: m capped,
 *           k recomputed for m / n
 *   3. build that filter; header nkeys = n
 * Every rule gives a correct join, because the final join at site A
 * removes the false positives.
 *
 * Inputs:  keys          - site A's join keys (NULLs and repeats allowed)
 *          s_rows        - rows of S at site B (from the catalog)
 *          rows_per_key  - rows of S per key value (from the catalog)
 *          row_width     - bytes per returned row on the wire
 *          network_mbit  - network speed
 *          t_check_ns    - site B's time to scan + check one row
 *          max_filter_bytes - largest filter allowed (header included)
 *          wire_factor   - wire bytes per filter byte (2 = hex text)
 *          per_message_ms, per_row_us, t_build_us - calibrated costs
 *          fetch_size    - rows per postgres_fdw FETCH
 * Output:  the filter (bytea)
 *
 * The time arguments do not change the filter (the size only depends on
 * bytes); they are checked and kept so that the same arguments describe
 * the same estimate as sjb_bloom_report.
 *
 * Why it exists: the filter is sized at RUNTIME, when the true number
 * of distinct keys is known, not guessed by the planner.
 */
Datum
sjb_build_auto(PG_FUNCTION_ARGS)
{
	ArrayType  *keys_array = PG_GETARG_ARRAYTYPE_P(0);
	SjbCostInputs inputs;
	SjbBloomEstimate estimate;
	int32	   *keys;
	int			num_keys;

	/* Step 1: the distinct keys. */
	keys = collect_unique_keys(keys_array, &num_keys);

	/* Read and check the cost model arguments. */
	inputs.num_keys = num_keys;
	inputs.s_rows = PG_GETARG_FLOAT8(1);
	inputs.rows_per_key = PG_GETARG_FLOAT8(2);
	inputs.row_width = PG_GETARG_FLOAT8(3);
	inputs.network_mbit = PG_GETARG_FLOAT8(4);
	inputs.t_check_ns = PG_GETARG_FLOAT8(5);
	inputs.max_filter_bytes = (double) PG_GETARG_INT64(6);
	inputs.wire_factor = PG_GETARG_FLOAT8(7);
	inputs.per_message_ms = PG_GETARG_FLOAT8(8);
	inputs.per_row_us = PG_GETARG_FLOAT8(9);
	inputs.t_build_us = PG_GETARG_FLOAT8(10);
	inputs.fetch_size = PG_GETARG_INT32(11);
	check_cost_inputs(&inputs);

	/* Step 2: the size (rules a-d). */
	sjb_estimate_bloom(&inputs, &estimate);

	/* Step 3: build it. */
	PG_RETURN_BYTEA_P(build_bloom_filter(keys, num_keys,
										 (uint64) estimate.m,
										 (int) estimate.k));
}


PG_FUNCTION_INFO_V1(sjb_contains);

/*
 * sjb_contains(filter bytea, key int4) RETURNS bool
 *
 * What it does: tests whether a key passes the Bloom filter: true if all
 * k bits of the key are 1 (this may be a false positive).
 *
 * Inputs:  filter - a filter made by sjb_build or sjb_build_auto
 *          key    - one join key from table S
 * Output:  true if the key passes, false if it is certainly not one of
 *          site A's keys. (Because the SQL function is STRICT, a NULL key
 *          gives NULL without this C code ever running.) A filter of
 *          another version or mode, or a damaged one, gives an error.
 *
 * Why it exists: this runs at site B inside the WHERE clause, so only
 * rows that pass are sent back to site A.
 */
Datum
sjb_contains(PG_FUNCTION_ARGS)
{
	/*
	 * PG_GETARG_BYTEA_PP gets the bytea argument, decompressing it or
	 * fetching it from TOAST storage if needed. It may still have a short
	 * 1-byte length header, so we must use VARDATA_ANY (pointer to the
	 * data) and VARSIZE_ANY_EXHDR (data size without the length header),
	 * which work with both header sizes.
	 */
	bytea	   *filter = PG_GETARG_BYTEA_PP(0);
	int32		key = PG_GETARG_INT32(1);
	const char *filter_data = VARDATA_ANY(filter);
	Size		filter_size = VARSIZE_ANY_EXHDR(filter);
	SjbHeader	header;
	const uint8 *bits;
	bool		passes;

	read_filter(filter_data, filter_size, &header);
	bits = (const uint8 *) (filter_data + sizeof(SjbHeader));

	passes = sjb_bloom_test(bits, header.m, header.k, header.seed, key);

	/* PG_RETURN_BOOL returns a C bool to SQL as a boolean. */
	PG_RETURN_BOOL(passes);
}


PG_FUNCTION_INFO_V1(sjb_info);

/*
 * sjb_info(filter bytea) RETURNS text
 *
 * What it does: describes a filter in one line, for example
 *   mode=bloom nkeys=10000 m=220000 k=15 bits_set=48.10% bytes=27524
 * "bytes" is the header plus the bits (without the varlena length).
 *
 * Input:  filter - a filter made by sjb_build or sjb_build_auto
 * Output: the description as text (an error for a damaged filter or one
 *         of another version or mode)
 *
 * Why it exists: lets us look inside a filter while testing and tuning.
 */
Datum
sjb_info(PG_FUNCTION_ARGS)
{
	bytea	   *filter = PG_GETARG_BYTEA_PP(0);
	const char *filter_data = VARDATA_ANY(filter);
	Size		filter_size = VARSIZE_ANY_EXHDR(filter);
	SjbHeader	header;
	const uint8 *bits;
	uint64		num_bits_set;
	double		percent_set;
	char	   *description;

	read_filter(filter_data, filter_size, &header);
	bits = (const uint8 *) (filter_data + sizeof(SjbHeader));

	num_bits_set = sjb_count_set_bits(bits, sjb_bloom_payload_bytes(header.m));
	percent_set = 100.0 * (double) num_bits_set / (double) header.m;

	/* psprintf is like sprintf, but returns a new palloc'd string. */
	description = psprintf("mode=bloom nkeys=%u m=%llu k=%d bits_set=%.2f%% bytes=%zu",
						   header.nkeys,
						   (unsigned long long) header.m,
						   (int) header.k,
						   percent_set,
						   filter_size);

	/*
	 * cstring_to_text turns a C string into an SQL text value, and
	 * PG_RETURN_TEXT_P returns it.
	 */
	PG_RETURN_TEXT_P(cstring_to_text(description));
}


PG_FUNCTION_INFO_V1(sjb_bloom_report);

/*
 * sjb_bloom_report(n, s_rows, rows_per_key, row_width, network_mbit,
 *                  t_check_ns, per_message_ms, per_row_us, t_build_us
 *                  float8, fetch_size int4, max_filter_bytes int8,
 *                  wire_factor float8) RETURNS text
 *
 * What it does: runs the cost model (sjb_estimate_bloom, the same code
 * sjb_build_auto uses) for n distinct keys and returns a readable
 * multi-line report: the inputs, M, b*, the sizing rule that applied,
 * m, k, the predicted fp rate, bytes each way, rows back, messages and
 * the time breakdown in ms.
 *
 * Inputs:  n            - number of distinct keys at site A
 *          the others   - as for sjb_build_auto
 * Output:  the report as text
 *
 * Why it exists: to explain and present the cost model's numbers.
 */
Datum
sjb_bloom_report(PG_FUNCTION_ARGS)
{
	SjbCostInputs inputs;
	SjbBloomEstimate estimate;
	StringInfoData report;

	read_report_arguments(fcinfo, &inputs);
	sjb_estimate_bloom(&inputs, &estimate);

	/*
	 * A StringInfo is PostgreSQL's growing text buffer: initStringInfo
	 * starts an empty one, and appendStringInfo adds printf-style text to
	 * the end of it.
	 */
	initStringInfo(&report);
	appendStringInfo(&report,
					 "inputs: n=%.0f s_rows=%.0f rows_per_key=%g row_width=%.2f "
					 "network_mbit=%g t_check_ns=%.2f\n",
					 inputs.num_keys, inputs.s_rows, inputs.rows_per_key,
					 inputs.row_width, inputs.network_mbit, inputs.t_check_ns);
	appendStringInfo(&report,
					 "        per_message_ms=%.5f per_row_us=%.5f t_build_us=%.4f "
					 "fetch_size=%.0f max_filter_bytes=%.0f wire_factor=%g\n",
					 inputs.per_message_ms, inputs.per_row_us, inputs.t_build_us,
					 inputs.fetch_size, inputs.max_filter_bytes, inputs.wire_factor);
	appendStringInfo(&report, "matching rows M = %.0f\n", estimate.matching_rows);
	appendStringInfo(&report, "best bits per key b* = %.2f\n", estimate.best_bits);
	appendStringInfo(&report, "size rule: %s\n", size_rule_text(estimate.size_rule));
	appendStringInfo(&report,
					 "filter: m = %.0f bits, k = %.0f, %.2f bits/key, %.0f bytes (header included)\n",
					 estimate.m, estimate.k, estimate.bits_per_key, estimate.filter_size);
	appendStringInfo(&report, "predicted fp rate = %.6f\n", estimate.fp_rate);
	appendStringInfo(&report, "bytes A->B = %.0f (the filter as hex text)\n",
					 estimate.bytes_a_to_b);
	appendStringInfo(&report, "rows back = M + (s_rows - M) * fp = %.1f\n",
					 estimate.rows_back);
	appendStringInfo(&report, "bytes B->A = rows back * row_width = %.0f\n",
					 estimate.bytes_b_to_a);
	appendStringInfo(&report, "messages = 1 + ceil(rows back / fetch_size) = %.0f\n",
					 estimate.messages);
	appendStringInfo(&report,
					 "time in ms: network %.3f + messages %.3f + rows %.3f + work at A %.3f + work at B %.3f\n",
					 estimate.network_seconds * 1000.0,
					 estimate.message_seconds * 1000.0,
					 estimate.row_seconds * 1000.0,
					 estimate.work_at_a_seconds * 1000.0,
					 estimate.work_at_b_seconds * 1000.0);
	appendStringInfo(&report, "total = %.3f ms", estimate.total_seconds * 1000.0);

	PG_RETURN_TEXT_P(cstring_to_text(report.data));
}


PG_FUNCTION_INFO_V1(sjb_bloom_estimate);

/*
 * sjb_bloom_estimate(the same 12 arguments as sjb_bloom_report)
 *     RETURNS record (size_rule text, matching_rows, best_bits,
 *                     bits_per_key float8, m int8, k int4, fp_rate,
 *                     bytes_a_to_b, rows_back, bytes_b_to_a, messages,
 *                     network_ms, messages_ms, rows_ms, work_a_ms,
 *                     work_b_ms, total_ms float8)
 *
 * What it does: the same numbers as sjb_bloom_report, as one row with
 * one column each (the OUT parameters in sjbloom--1.0.sql).
 *
 * Inputs:  the same as sjb_bloom_report
 * Output:  one record
 *
 * Why it exists: bench/validate_model.py needs the predicted times, and
 * should not have to read them out of the report's text.
 */
Datum
sjb_bloom_estimate(PG_FUNCTION_ARGS)
{
	SjbCostInputs inputs;
	SjbBloomEstimate estimate;
	TupleDesc	result_type;
	Datum		values[17];
	bool		nulls[17];
	char		rule_name[2];
	HeapTuple	result_row;
	int			i;

	read_report_arguments(fcinfo, &inputs);
	sjb_estimate_bloom(&inputs, &estimate);

	/*
	 * get_call_result_type looks up the row type this call must return
	 * (the OUT parameters of the SQL declaration). BlessTupleDesc makes
	 * that row type usable for a returned record.
	 */
	if (get_call_result_type(fcinfo, NULL, &result_type) != TYPEFUNC_COMPOSITE)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("sjbloom: sjb_bloom_estimate must be called where a record is expected")));
	result_type = BlessTupleDesc(result_type);

	rule_name[0] = estimate.size_rule;
	rule_name[1] = '\0';

	/*
	 * Each column is a Datum: CStringGetTextDatum, Float8GetDatum,
	 * Int64GetDatum and Int32GetDatum wrap a C value into a Datum.
	 */
	values[0] = CStringGetTextDatum(rule_name);
	values[1] = Float8GetDatum(estimate.matching_rows);
	values[2] = Float8GetDatum(estimate.best_bits);
	values[3] = Float8GetDatum(estimate.bits_per_key);
	values[4] = Int64GetDatum((int64) estimate.m);
	values[5] = Int32GetDatum((int32) estimate.k);
	values[6] = Float8GetDatum(estimate.fp_rate);
	values[7] = Float8GetDatum(estimate.bytes_a_to_b);
	values[8] = Float8GetDatum(estimate.rows_back);
	values[9] = Float8GetDatum(estimate.bytes_b_to_a);
	values[10] = Float8GetDatum(estimate.messages);
	values[11] = Float8GetDatum(estimate.network_seconds * 1000.0);
	values[12] = Float8GetDatum(estimate.message_seconds * 1000.0);
	values[13] = Float8GetDatum(estimate.row_seconds * 1000.0);
	values[14] = Float8GetDatum(estimate.work_at_a_seconds * 1000.0);
	values[15] = Float8GetDatum(estimate.work_at_b_seconds * 1000.0);
	values[16] = Float8GetDatum(estimate.total_seconds * 1000.0);
	for (i = 0; i < 17; i++)
		nulls[i] = false;

	/*
	 * heap_form_tuple builds one row from the column values, and
	 * HeapTupleGetDatum turns it into the Datum we return.
	 */
	result_row = heap_form_tuple(result_type, values, nulls);
	PG_RETURN_DATUM(HeapTupleGetDatum(result_row));
}
