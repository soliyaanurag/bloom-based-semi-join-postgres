/*-------------------------------------------------------------------------
 *
 * sjb_explain.c
 *
 * Bloom filter details in EXPLAIN ANALYZE.
 *
 * When a hand-written Bloom semi-join runs, for example
 *
 *   ... WHERE sjb_contains((SELECT sjb_build_auto(...) FROM orders ...),
 *                          c.customer_id)
 *
 * the sub-SELECT runs once at site A and its result, the filter, is sent
 * to site B as a query parameter ($1). This file adds lines like these
 * under the Foreign Scan in EXPLAIN (ANALYZE):
 *
 *   Semijoin Filter: Bloom (sjbloom)
 *   Bloom Keys: 9946
 *   Bloom Size: m=179156 bits, k=12, 18.01 bits/key, 49.87% bits set
 *   Bloom Predicted FP Rate: 0.000237
 *   Bloom Bytes Sent: 44840 bytes
 *
 * How: PostgreSQL 18 has a hook, explain_per_node_hook, that EXPLAIN
 * calls for every plan node after printing that node's own lines. Our
 * function looks at the Foreign Scan's parameters, finds the filter and
 * prints its header values. It needs NO change to postgres_fdw.
 *
 * Only with ANALYZE: the filter is computed when the query runs, so plain
 * EXPLAIN (which does not run the query) has no filter to show.
 *
 * The hook must NEVER raise an error (an error would abort the user's
 * EXPLAIN): it checks everything first and prints nothing if anything
 * looks wrong.
 *
 * This file also holds the library's _PG_init, which installs the hook.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "catalog/pg_type.h"
#include "commands/explain.h"
#include "commands/explain_format.h"
#include "commands/explain_state.h"
#include "fmgr.h"
#include "nodes/execnodes.h"
#include "nodes/params.h"
#include "nodes/pg_list.h"
#include "nodes/plannodes.h"
#include "nodes/primnodes.h"

#include "sjb_filter.h"
#include "costmodel.h"


/*
 * The hook that was installed before ours (by another extension), or
 * NULL. We call it first, so both extensions keep working.
 */
static explain_per_node_hook_type previous_explain_per_node_hook = NULL;


/*
 * parameter_filter_value
 *
 * What it does: if an expression of the Foreign Scan is an executor
 * parameter of type bytea whose value has been computed, returns that
 * value; otherwise returns NULL.
 *
 * Inputs:  expression - one entry of the Foreign Scan's fdw_exprs (the
 *                       expressions postgres_fdw sends to site B as
 *                       parameters $1, $2, ...)
 *          estate     - the executor state of the query
 * Output:  the bytea value, or NULL
 *
 * Why it exists: our filter reaches site B as such a parameter. A
 * sub-SELECT that runs once (an InitPlan) stores its result in
 * estate->es_param_exec_vals[paramid], and that is where we read it.
 */
static bytea *
parameter_filter_value(Node *expression, EState *estate)
{
	Param	   *param;
	ParamExecData *param_data;
	int			num_params;

	/*
	 * IsA checks the node type of a planner node; a Param is a parameter
	 * like $1. PARAM_EXEC means "computed inside this query" (for example
	 * by an InitPlan), and BYTEAOID is the type OID of bytea.
	 */
	if (expression == NULL || !IsA(expression, Param))
		return NULL;
	param = (Param *) expression;
	if (param->paramkind != PARAM_EXEC || param->paramtype != BYTEAOID)
		return NULL;

	/* The array has one entry per executor parameter of the plan. */
	if (estate == NULL || estate->es_param_exec_vals == NULL ||
		estate->es_plannedstmt == NULL)
		return NULL;
	num_params = list_length(estate->es_plannedstmt->paramExecTypes);
	if (param->paramid < 0 || param->paramid >= num_params)
		return NULL;

	/*
	 * execPlan != NULL means the InitPlan has not run yet, so there is no
	 * value (for example when the Foreign Scan was never started).
	 */
	param_data = &estate->es_param_exec_vals[param->paramid];
	if (param_data->execPlan != NULL || param_data->isnull)
		return NULL;

	/*
	 * DatumGetByteaPP unpacks the value into a bytea. It may keep a short
	 * 1-byte length header (small filters, for example m = 8 bits, can
	 * have one), so the caller must use VARDATA_ANY / VARSIZE_ANY_EXHDR.
	 */
	return DatumGetByteaPP(param_data->value);
}

/*
 * explain_filter
 *
 * What it does: prints the five Bloom lines for one filter, if it is a
 * valid sjbloom Bloom filter (sjb_check_filter in bloom.c):
 *
 *   Semijoin Filter          always "Bloom (sjbloom)"
 *   Bloom Keys               nkeys from the header
 *   Bloom Size               m, k, m / nkeys bits per key (left out when
 *                            nkeys = 0) and the share of bits set to 1
 *   Bloom Predicted FP Rate  sjb_fp_rate(nkeys, m, k)
 *   Bloom Bytes Sent         2 * (24 + ceil(m/8)) + 2: postgres_fdw sends
 *                            a bytea parameter as hex text, 2 characters
 *                            per byte plus "\x" (sjb_bloom_filter_bytes)
 *
 * Inputs:  filter - the bytea value of the parameter
 *          es     - the EXPLAIN state (output format and buffer)
 * Output:  none (prints into the EXPLAIN output, or prints nothing)
 *
 * Why it exists: lets us see, for a real query, how big the filter was
 * and how many false positives to expect, next to the Foreign Scan's
 * actual row count.
 */
static void
explain_filter(bytea *filter, ExplainState *es)
{
	const char *filter_data = VARDATA_ANY(filter);
	Size		filter_size = VARSIZE_ANY_EXHDR(filter);
	SjbHeader	header;
	const uint8 *bits;
	uint64		num_bits_set;
	double		percent_set;
	double		fp_rate;
	double		bytes_sent;
	char	   *size_text;

	/* Not one of our filters, another version or mode, or damaged. */
	if (sjb_check_filter(filter_data, filter_size, &header) != SJB_FILTER_OK)
		return;

	bits = (const uint8 *) (filter_data + sizeof(SjbHeader));
	num_bits_set = sjb_count_set_bits(bits, sjb_bloom_payload_bytes(header.m));
	percent_set = 100.0 * (double) num_bits_set / (double) header.m;
	fp_rate = sjb_fp_rate((double) header.nkeys, (double) header.m, (double) header.k);
	bytes_sent = sjb_bloom_filter_bytes((double) header.m, SJB_DEFAULT_WIRE_FACTOR);

	/* psprintf is like sprintf, but returns a new palloc'd string. */
	if (header.nkeys > 0)
		size_text = psprintf("m=%llu bits, k=%d, %.2f bits/key, %.2f%% bits set",
							 (unsigned long long) header.m, (int) header.k,
							 (double) header.m / (double) header.nkeys,
							 percent_set);
	else
		size_text = psprintf("m=%llu bits, k=%d, %.2f%% bits set",
							 (unsigned long long) header.m, (int) header.k,
							 percent_set);

	/*
	 * ExplainPropertyText and ExplainPropertyInteger print one "Label:
	 * value" line in TEXT format, or one field of the node in JSON, XML
	 * and YAML format. The second argument of ExplainPropertyInteger is
	 * the unit printed after the number in TEXT format (NULL = none).
	 */
	ExplainPropertyText("Semijoin Filter", "Bloom (sjbloom)", es);
	ExplainPropertyInteger("Bloom Keys", NULL, (int64) header.nkeys, es);
	ExplainPropertyText("Bloom Size", size_text, es);
	ExplainPropertyText("Bloom Predicted FP Rate", psprintf("%.6g", fp_rate), es);
	ExplainPropertyInteger("Bloom Bytes Sent", "bytes", (int64) bytes_sent, es);
}

/*
 * sjb_explain_per_node
 *
 * What it does: our explain_per_node_hook. EXPLAIN calls it once for
 * every plan node. It first calls the previous hook (if any). Then, only
 * for EXPLAIN ANALYZE and only for a Foreign Scan, it looks at every
 * expression postgres_fdw sends to site B as a parameter, and prints the
 * Bloom lines for each one that is a computed bytea parameter holding a
 * valid sjbloom filter.
 *
 * Inputs:  planstate - the node being printed (plan + executor state)
 *          ancestors, relationship, plan_name - not used here
 *          es        - the EXPLAIN state (es->analyze is true for ANALYZE)
 * Output:  none (may print into the EXPLAIN output)
 *
 * Why it exists: it is the entry point PostgreSQL calls.
 */
static void
sjb_explain_per_node(PlanState *planstate, List *ancestors,
					 const char *relationship, const char *plan_name,
					 ExplainState *es)
{
	ForeignScan *foreign_scan;
	ListCell   *cell;

	if (previous_explain_per_node_hook != NULL)
		previous_explain_per_node_hook(planstate, ancestors, relationship,
									   plan_name, es);

	if (!es->analyze)
		return;
	if (planstate == NULL || planstate->plan == NULL ||
		!IsA(planstate->plan, ForeignScan))
		return;

	/*
	 * fdw_exprs is a List (PostgreSQL's linked list). foreach walks over
	 * it, and lfirst(cell) gives the current element.
	 */
	foreign_scan = (ForeignScan *) planstate->plan;
	foreach(cell, foreign_scan->fdw_exprs)
	{
		bytea	   *filter = parameter_filter_value((Node *) lfirst(cell),
													planstate->state);

		if (filter != NULL)
			explain_filter(filter, es);
	}
}

/*
 * _PG_init
 *
 * What it does: PostgreSQL calls this function once, when sjbloom.so is
 * loaded into a session (the first time one of our SQL functions is
 * used). It installs our EXPLAIN hook, after saving the hook that was
 * there before.
 *
 * Inputs:  none
 * Output:  none
 *
 * Why it exists: a hook only works after it has been installed. In a
 * Bloom query sjb_build or sjb_build_auto runs before EXPLAIN prints, so
 * the library (and the hook) is always loaded in time.
 */
void
_PG_init(void)
{
	previous_explain_per_node_hook = explain_per_node_hook;
	explain_per_node_hook = sjb_explain_per_node;
}
