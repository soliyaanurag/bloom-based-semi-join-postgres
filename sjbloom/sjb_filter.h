/*-------------------------------------------------------------------------
 *
 * sjb_filter.h
 *
 * Shared definitions for the sjbloom extension.
 *
 * A "filter" is a bytea value that site A builds from its join keys and
 * sends to site B. Every filter starts with the same 24-byte header
 * (SjbHeader below), followed by the Bloom filter bits:
 *
 *   SJB_MODE_BLOOM : payload = ceil(m/8) bytes of Bloom filter bits
 *
 * This header also declares the functions in bloom.c: the pure Bloom
 * filter functions, and a check of a filter's header and size that never
 * raises an error (sjb_check_filter).
 *
 * Every .c file must include "postgres.h" BEFORE this file, because we use
 * PostgreSQL's integer types (uint8, uint16, uint32, uint64, int32) and
 * Size.
 *
 *-------------------------------------------------------------------------
 */
#ifndef SJB_FILTER_H
#define SJB_FILTER_H

/*
 * Version number stored in every filter header.
 *
 * Site A builds the filter and site B tests keys against it, so both must
 * compute the SAME k bit positions for a key. If they used different
 * algorithms, B would look at the wrong bits and could wrongly drop
 * matching rows (false negatives), and the join result would silently be
 * wrong. So whenever the bit positions or the layout change, this number
 * goes up, and a filter with a different version is rejected with a clear
 * error (see read_filter in sjbloom.c), never read with the wrong
 * positions.
 *
 *   1 = classic double hashing,  position_i = (h1 + i*h2) mod m
 *   2 = enhanced double hashing  (see compute_positions in bloom.c),
 *       with h2 forced odd (h2 | 1)
 *   3 = enhanced double hashing, h2 NOT forced odd (as in PostgreSQL)
 */
#define SJB_VERSION 3

/*
 * The filter mode (stored in the "mode" field of the header). This
 * version has only the Bloom filter. Mode numbers 2 and 3 are reserved
 * for other filter kinds; this build rejects them with an error.
 */
#define SJB_MODE_BLOOM 1

/*
 * Seed for the hash function. Site A and site B must use the same seed,
 * so it is stored in the header of every filter.
 * UINT64CONST is a PostgreSQL macro that writes a 64-bit unsigned constant
 * in a way every C compiler accepts.
 */
#define SJB_SEED UINT64CONST(0x5EED5EED)

/* Allowed range for the Bloom filter size m (in bits): 8 bits to 64 MB. */
#define SJB_MIN_BITS 8
#define SJB_MAX_BITS (8 * 64 * 1024 * 1024)

/* Allowed range for k, the number of hash positions per key. */
#define SJB_MIN_K 1
#define SJB_MAX_K 32

/*
 * The 24-byte header at the start of every filter.
 *
 *   offset  size  field
 *   0       1     version  (always SJB_VERSION)
 *   1       1     mode     (always SJB_MODE_BLOOM)
 *   2       2     k        (number of hash positions per key)
 *   4       4     nkeys    (number of keys that were added)
 *   8       8     m        (number of bits in the Bloom filter)
 *   16      8     seed     (hash seed)
 *
 * The fields are ordered so that no padding is needed. Bytes inside a
 * bytea may not be aligned for 8-byte reads, so we always copy the header
 * in and out with memcpy instead of casting a pointer to SjbHeader *.
 */
typedef struct SjbHeader
{
	uint8		version;
	uint8		mode;
	uint16		k;
	uint32		nkeys;
	uint64		m;
	uint64		seed;
} SjbHeader;

/*
 * StaticAssertDecl is a PostgreSQL macro that checks a condition while
 * compiling. If the header is ever not exactly 24 bytes, the build fails.
 */
StaticAssertDecl(sizeof(SjbHeader) == 24, "SjbHeader must be exactly 24 bytes");

/*
 * Results of sjb_check_filter: OK, or what is wrong with the filter.
 * sjbloom.c turns a problem into an SQL error; the EXPLAIN hook
 * (sjb_explain.c) just prints nothing.
 */
#define SJB_FILTER_OK 0
#define SJB_FILTER_TOO_SMALL 1		/* shorter than the 24-byte header */
#define SJB_FILTER_WRONG_VERSION 2	/* version is not SJB_VERSION */
#define SJB_FILTER_WRONG_MODE 3		/* mode is not SJB_MODE_BLOOM */
#define SJB_FILTER_BAD_PARAMETERS 4 /* m or k outside the allowed range */
#define SJB_FILTER_WRONG_SIZE 5		/* payload is not ceil(m/8) bytes */

/* Bloom filter functions (bloom.c). */
extern void sjb_bloom_add(uint8 *bits, uint64 m, int k, uint64 seed, int32 key);
extern bool sjb_bloom_test(const uint8 *bits, uint64 m, int k, uint64 seed, int32 key);

/* Reading a filter without raising errors (bloom.c). */
extern uint64 sjb_bloom_payload_bytes(uint64 m);
extern uint64 sjb_count_set_bits(const uint8 *bits, uint64 num_bytes);
extern int	sjb_check_filter(const char *filter_data, Size filter_size,
							 SjbHeader *header);

#endif							/* SJB_FILTER_H */
