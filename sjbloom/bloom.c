/*-------------------------------------------------------------------------
 *
 * bloom.c
 *
 * The pure Bloom filter logic of the sjbloom extension.
 *
 * A Bloom filter is an array of m bits, all 0 at the start. To add a key
 * we compute k bit positions from the key and set those k bits to 1. To
 * test a key we compute the same k positions: if any of those bits is 0
 * the key was certainly never added; if all are 1 the key was probably
 * added (it may be a "false positive").
 *
 * This file only works on plain arrays of bytes. It knows nothing about
 * bytea values or SQL; sjbloom.c handles that part.
 *
 * Part 1 is the hashing: adding and testing keys.
 * Part 2 reads a filter WITHOUT raising errors: its size, how many bits
 * are set, and whether its header and size are valid (sjb_check_filter).
 * sjbloom.c turns a problem into an SQL error, and the EXPLAIN hook in
 * sjb_explain.c, which must never raise an error, just prints nothing.
 *
 * Bit position p lives in byte p / 8, at bit p % 8 inside that byte.
 *
 * The k positions of a key come from ONE hash call, using "enhanced
 * double hashing" (Dillinger & Manolios, FMCAD 2004), the same method as
 * PostgreSQL's own src/backend/lib/bloomfilter.c. See compute_positions.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "fmgr.h"
#include "utils/fmgrprotos.h"

#include "sjb_filter.h"


/* ------------------------------------------------------------------------
 * Part 1: hashing, adding and testing keys
 * ------------------------------------------------------------------------
 */

/*
 * compute_two_hashes
 *
 * What it does: hashes one key with PostgreSQL's own int4 hash function
 * and splits the 64-bit result into two 32-bit numbers h1 and h2.
 *
 * Inputs:  key  - the join key to hash
 *          seed - the hash seed (taken from the filter header)
 * Output:  *h1 = low 32 bits of the hash
 *          *h2 = high 32 bits of the hash
 *
 * Why it exists: enhanced double hashing (see compute_positions below)
 * needs two hash values.
 *
 * h2 is NOT forced to be odd (filter version 3; version 2 used h2 | 1).
 * Classic double hashing needs an odd step so that its evenly spaced walk
 * does not come back to the same bits, but enhanced double hashing changes
 * the step every time, so it does not need that. PostgreSQL's
 * bloomfilter.c does not force it either. Forcing it odd wasted one bit
 * of the hash: when m is even, h2 mod m could only be odd, so only half
 * of the possible (h1 mod m, h2 mod m) pairs were used, which doubled the
 * extra false positives of small filters (see docs/fp_investigation.md).
 * h2 mod m may now be 0; then position 1 equals position 0, and the walk
 * still moves on because the step grows by 1, 2, 3, ...
 *
 * We use hashint4extended (the same function PostgreSQL uses for hash
 * indexes on int4) so that site A and site B always get the same hash for
 * the same key, on any machine.
 */
static void
compute_two_hashes(int32 key, uint64 seed, uint32 *h1, uint32 *h2)
{
	Datum		hash_datum;
	uint64		hash;

	/*
	 * DirectFunctionCall2 calls a PostgreSQL SQL-callable C function
	 * directly from C with two arguments. Arguments and the result are
	 * passed as "Datum" values: Int32GetDatum and Int64GetDatum wrap our C
	 * numbers into Datums, and DatumGetUInt64 unwraps the result.
	 */
	hash_datum = DirectFunctionCall2(hashint4extended,
									 Int32GetDatum(key),
									 Int64GetDatum((int64) seed));
	hash = DatumGetUInt64(hash_datum);

	*h1 = (uint32) (hash & 0xFFFFFFFF);
	*h2 = (uint32) (hash >> 32);
}

/*
 * step_to_next_position
 *
 * What it does: one step of enhanced double hashing. Moves the current
 * position x forward by the step y, then makes the step i bigger:
 *     x = (x + y) mod m
 *     y = (y + i) mod m
 * After the call, *x is position number i.
 *
 * Inputs:  x, y - pointers to the current position and step (both < m);
 *                 both are updated
 *          i    - the number of the position being computed (1 .. k-1)
 *          m    - number of bits in the filter
 * Output:  none (*x and *y are changed)
 *
 * Why it exists: compute_positions (used when adding a key) and
 * sjb_bloom_test (used when testing a key) must produce exactly the same
 * positions. Keeping the rule in one function guarantees that.
 */
static void
step_to_next_position(uint64 *x, uint64 *y, int i, uint64 m)
{
	*x = (*x + *y) % m;
	*y = (*y + (uint64) i) % m;
}

/*
 * compute_positions
 *
 * What it does: fills positions[0 .. k-1] with the k bit positions of a
 * key, using ENHANCED double hashing:
 *     x = h1 mod m,  y = h2 mod m
 *     position_0 = x
 *     for i = 1 .. k-1:   x = (x + y) mod m      (take a step of size y)
 *                         y = (y + i) mod m      (the next step is i bigger)
 *                         position_i = x
 *
 * Inputs:  h1, h2    - the two hash values from compute_two_hashes
 *          k         - number of positions (1 .. SJB_MAX_K)
 *          m         - number of bits in the filter
 * Output:  positions - array of at least k entries, filled in
 *
 * Why it exists: classic double hashing, position_i = (h1 + i*h2) mod m,
 * puts the k positions at evenly spaced steps. Two keys with the same step
 * and nearby starting points then share most of their bits, which gave
 * up to about 70 times more false positives than theory for small filters (see
 * docs/fp_investigation.md). Growing the step by 1, 2, 3, ... breaks that
 * even spacing. This follows k_hashes() in PostgreSQL's bloomfilter.c,
 * which uses "& (m - 1)" because its m is always a power of two; our m can
 * be any number, so we use "% m". All values stay below m, so x + y and
 * y + i cannot overflow a uint64.
 */
static void
compute_positions(uint32 h1, uint32 h2, int k, uint64 m, uint64 *positions)
{
	uint64		x = (uint64) h1 % m;
	uint64		y = (uint64) h2 % m;
	int			i;

	positions[0] = x;
	for (i = 1; i < k; i++)
	{
		step_to_next_position(&x, &y, i, m);
		positions[i] = x;
	}
}

/*
 * set_bit
 *
 * What it does: sets bit number "position" to 1.
 * Inputs:  bits     - the Bloom filter bit array
 *          position - the bit to set
 * Output:  none (the bit array is changed)
 * Why it exists: keeps the "byte p/8, bit p%8" rule in one place.
 */
static void
set_bit(uint8 *bits, uint64 position)
{
	uint64		byte_index = position / 8;
	int			bit_in_byte = (int) (position % 8);

	bits[byte_index] = bits[byte_index] | (uint8) (1 << bit_in_byte);
}

/*
 * bit_is_set
 *
 * What it does: tells whether bit number "position" is 1.
 * Inputs:  bits     - the Bloom filter bit array
 *          position - the bit to look at
 * Output:  true if the bit is 1, false if it is 0
 * Why it exists: the reading counterpart of set_bit.
 */
static bool
bit_is_set(const uint8 *bits, uint64 position)
{
	uint64		byte_index = position / 8;
	int			bit_in_byte = (int) (position % 8);

	if ((bits[byte_index] & (uint8) (1 << bit_in_byte)) != 0)
		return true;
	else
		return false;
}

/*
 * sjb_bloom_add
 *
 * What it does: adds one key to a Bloom filter by setting its k bits.
 *
 * Inputs:  bits - the bit array, (m + 7) / 8 bytes long
 *          m    - the number of bits in the filter (at least 1)
 *          k    - how many bits to set per key (1 .. SJB_MAX_K)
 *          seed - the hash seed
 *          key  - the key to add
 * Output:  none (the bit array is changed)
 *
 * Why it exists: called by sjb_build for every join key at site A.
 */
void
sjb_bloom_add(uint8 *bits, uint64 m, int k, uint64 seed, int32 key)
{
	uint32		h1;
	uint32		h2;
	uint64		positions[SJB_MAX_K];
	int			i;

	compute_two_hashes(key, seed, &h1, &h2);
	compute_positions(h1, h2, k, m, positions);

	for (i = 0; i < k; i++)
		set_bit(bits, positions[i]);
}

/*
 * sjb_bloom_test
 *
 * What it does: checks whether a key may be in the Bloom filter.
 *
 * Inputs:  the same as sjb_bloom_add (the bits are only read)
 * Output:  false if the key was certainly never added;
 *          true if the key was probably added (could be a false positive)
 *
 * Why it exists: called by sjb_contains for every row of S at site B,
 * so it must be fast. It computes the positions ONE AT A TIME (the same
 * positions as compute_positions, using the same step_to_next_position)
 * and tests each bit at once. It stops at the first 0 bit, because one
 * 0 bit already proves the key is not in the filter. In a well-sized
 * filter about half the bits are 1, so most keys that are not in the
 * filter are rejected after 1 or 2 positions instead of all k.
 */
bool
sjb_bloom_test(const uint8 *bits, uint64 m, int k, uint64 seed, int32 key)
{
	uint32		h1;
	uint32		h2;
	uint64		x;
	uint64		y;
	int			i;

	compute_two_hashes(key, seed, &h1, &h2);

	/* Position 0. */
	x = (uint64) h1 % m;
	y = (uint64) h2 % m;
	if (!bit_is_set(bits, x))
		return false;

	/* Positions 1 .. k-1, each tested as soon as it is computed. */
	for (i = 1; i < k; i++)
	{
		step_to_next_position(&x, &y, i, m);
		if (!bit_is_set(bits, x))
			return false;
	}

	return true;
}


/* ------------------------------------------------------------------------
 * Part 2: reading a filter without raising errors
 * ------------------------------------------------------------------------
 */

/*
 * sjb_bloom_payload_bytes
 *
 * What it does: returns how many bytes are needed to hold m bits,
 * i.e. ceil(m / 8).
 * Input:  m - the number of bits
 * Output: the number of bytes
 * Why it exists: the same rule is needed when building and when checking
 * a filter, so it lives in one place.
 */
uint64
sjb_bloom_payload_bytes(uint64 m)
{
	return (m + 7) / 8;
}

/*
 * sjb_count_set_bits
 *
 * What it does: counts how many bits are 1 in a byte array.
 * Inputs:  bits      - the byte array
 *          num_bytes - its length in bytes
 * Output:  the number of 1 bits
 * Why it exists: sjb_info and the EXPLAIN hook report how full the Bloom
 * filter is. A filter with about 50% of its bits set is well sized; much
 * more means too many false positives.
 */
uint64
sjb_count_set_bits(const uint8 *bits, uint64 num_bytes)
{
	uint64		count = 0;
	uint64		byte_index;
	int			bit_in_byte;

	for (byte_index = 0; byte_index < num_bytes; byte_index++)
	{
		for (bit_in_byte = 0; bit_in_byte < 8; bit_in_byte++)
		{
			if ((bits[byte_index] & (uint8) (1 << bit_in_byte)) != 0)
				count = count + 1;
		}
	}
	return count;
}

/*
 * sjb_check_filter
 *
 * What it does: copies the 24-byte header out of a filter and checks, in
 * this order, that
 *   1. the filter is at least 24 bytes long,
 *   2. its version is SJB_VERSION,
 *   3. its mode is SJB_MODE_BLOOM,
 *   4. m and k are inside the allowed ranges,
 *   5. the payload after the header is exactly ceil(m/8) bytes.
 *
 * Inputs:  filter_data - pointer to the first byte of the filter (the
 *                        start of SjbHeader, after the bytea's length)
 *          filter_size - number of bytes at filter_data
 * Output:  SJB_FILTER_OK, or the SJB_FILTER_... code of the first check
 *          that failed. *header is filled in whenever the filter is at
 *          least 24 bytes long.
 *
 * Why it exists: every reader of a filter must make sure the bytes really
 * are a Bloom filter of our version before it looks at any bit; a filter
 * of another version or mode would be read with the wrong layout, and a
 * damaged one could make us read outside the payload. It raises NO error
 * itself, so the EXPLAIN hook can use it too. memcpy is used because the
 * bytes inside a bytea may not be aligned for 8-byte reads.
 */
int
sjb_check_filter(const char *filter_data, Size filter_size, SjbHeader *header)
{
	Size		payload_size;

	if (filter_size < sizeof(SjbHeader))
		return SJB_FILTER_TOO_SMALL;

	memcpy(header, filter_data, sizeof(SjbHeader));

	if (header->version != SJB_VERSION)
		return SJB_FILTER_WRONG_VERSION;

	if (header->mode != SJB_MODE_BLOOM)
		return SJB_FILTER_WRONG_MODE;

	if (header->m < SJB_MIN_BITS || header->m > SJB_MAX_BITS)
		return SJB_FILTER_BAD_PARAMETERS;
	if (header->k < SJB_MIN_K || header->k > SJB_MAX_K)
		return SJB_FILTER_BAD_PARAMETERS;

	payload_size = filter_size - sizeof(SjbHeader);
	if (payload_size != sjb_bloom_payload_bytes(header->m))
		return SJB_FILTER_WRONG_SIZE;

	return SJB_FILTER_OK;
}
