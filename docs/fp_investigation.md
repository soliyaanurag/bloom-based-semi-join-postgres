<!-- Presentation material: why our Bloom filter had too many false positives for small filters, and how enhanced double hashing (and an unforced step) fixed most of it. -->

# False-positive investigation: classic vs. enhanced double hashing

## 1. The problem

In the Batch 4 benchmark, `bloom_dynamic` picks a large b (bits per key) for small n, so theory predicts almost no false positives. The measurements disagreed:

| n | b | predicted false positives | measured |
|---:|---:|---:|---:|
| 100 | 26.4 | about 3 | **215** |
| 1,000 | 21.6 | about 31 | **78** |
| 10,000 | 16.8 | about 313 | 335 (matches) |

The prediction uses the standard Bloom filter formula (Broder & Mitzenmacher 2004):

```
predicted_fp = (1 - e^(-k·n/m))^k        n = keys in the filter, m = bits, k = positions per key
```

The gap only shows up when the filter is **small** (m = 2,640 bits for n = 100). Our suspect was the way we turn one hash value into k bit positions.

## 2. How a key gets its k bit positions

We hash each key **once** with PostgreSQL's `hashint4extended` and split the 64-bit result into two 32-bit numbers: `h1` (low half) and `h2` (high half). The k positions are then computed from h1 and h2.

### Classic double hashing (our version 1)

```
position_i = (h1 + i · h2) mod m        for i = 0 .. k-1
```

The positions are **evenly spaced**: start at h1, then always take the same step h2. This causes two kinds of trouble when m is small:

- **Repeats.** If the step shares a factor with m, the walk comes back to a bit it already used.
- **Shifted copies.** Two keys with the same step and nearby starting points walk along the same evenly spaced bits, so they share most of their k bits.

Example with m = 20, k = 5, step h2 = 5:

| key | h1 | positions (classic) | set of bits |
|---|---:|---|---|
| A (in the filter) | 3 | 3, 8, 13, 18, **3** | {3, 8, 13, 18}: only 4 different bits |
| B (not in the filter) | 8 | 8, 13, 18, 3, **8** | {3, 8, 13, 18}: **the same bits as A** |

B is a false positive **every time** A is in the filter.

### Enhanced double hashing (our versions 2 and 3; version 3 is the only one in use now)

This is the method of Dillinger & Manolios (FMCAD 2004). PostgreSQL uses it too, in `src/backend/lib/bloomfilter.c` (function `k_hashes`). In simple words: **take a step, then make the next step a little bigger**.

```
x = h1 mod m             (where we are)
y = h2 mod m             (how far the next step goes)
position_0 = x
for i = 1 .. k-1:
    x = (x + y) mod m    take the step
    y = (y + i) mod m    the next step is i bigger
    position_i = x
```

The steps grow by 1, 2, 3, ..., so the positions are **no longer evenly spaced**, and a shifted key no longer follows the same path. The same example (m = 20, k = 5, h2 = 5):

| key | h1 | positions (enhanced) | bits shared with A |
|---|---:|---|---|
| A | 3 | 3, 8, 14, 2, 13 | |
| B | 8 | 8, 13, 19, 7, 18 | only 8 and 13 |

It costs nothing extra: still **one hash call per key**, and k additions instead of k multiplications. PostgreSQL computes "mod m" with a bit mask because its m is always a power of two. Our m can be any number, so we use `%`.

### Version 2 vs. version 3: is the step forced odd?

Versions 1 and 2 forced h2 to be odd (`h2 | 1`). Classic double hashing needs that: with an even step and an even m, its evenly spaced walk comes back to the same bits. Enhanced double hashing changes the step every time, so it does **not** need an odd step, and PostgreSQL's `bloomfilter.c` does not force it either. Forcing it odd has a cost. When m is even, h2 mod m can then only be odd, so only half of the possible (h1 mod m, h2 mod m) pairs are ever used (see section 4). **Version 3** is enhanced double hashing with h2 *not* forced odd, as in PostgreSQL.

| version | positions | step h2 |
|---|---|---|
| 1 | classic: (h1 + i·h2) mod m | forced odd |
| 2 | enhanced (step grows by 1, 2, 3, ...) | forced odd |
| **3** (current) | enhanced | **not** forced odd |

## 3. Results: version 1 → version 2 → version 3

False positives among **1,000,000** keys that are not in the filter. m = ceil(n·b), k = round(0.693·b), so everything except the position rule is identical. Filter keys are 1..n, and the tested keys are n+1 .. n+1,000,000.

| n | b | m | k | predicted (formula) | v1 (classic) | v2 (enhanced, odd step) | v3 (enhanced) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 26.4 | 2,640 | 18 | 3.1 | 170 | 29 | **14** |
| 1,000 | 21.6 | 21,600 | 15 | 31.1 | 51 | 29 | **35** |
| 10,000 | 16.8 | 168,000 | 12 | 313.0 | 342 | 355 | **300** |
| 10,000 | 10.0 | 100,000 | 7 | 8,193.7 | 7,906 | 8,140 | **8,363** |

The same check with the **real** benchmark keys (the distinct customer_ids of the first n orders, tested against all other customer ids). v1 reproduces the Batch 4 numbers exactly (215 and 78):

| n | b | distinct keys | m | k | predicted | v1 | v2 | v3 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 26.4 | 100 | 2,640 | 18 | 3.1 | 215 | 37 | **23** |
| 1,000 | 21.6 | 999 | 21,579 | 15 | 31.1 | 78 | 27 | **36** |

And the standard false-positive experiment (`queries/fp_rate.sql`, 10,000 keys, large m) with version 3 still matches the formula:

| b | m | k | measured (v3) | predicted |
|---:|---:|---:|---:|---:|
| 4 | 40,000 | 3 | 0.145933 | 0.146892 |
| 8 | 80,000 | 6 | 0.021308 | 0.021577 |
| 12 | 120,000 | 8 | 0.003179 | 0.003142 |
| 16 | 160,000 | 11 | 0.000445 | 0.000459 |
| 22 | 220,000 | 15 | 0.000020 | 0.000026 |

How to read the counts: false positives are random, so a count c varies by about ±2·√c between experiments (for example 14 ± 7, 31 ± 11, or 8,194 ± 181). Within that noise, v2 and v3 match the formula at n = 1,000 and n = 10,000. The differences between versions at large m (for example 7,906 / 8,140 / 8,363) are noise. Only the n = 100 row is clearly above the formula.

**Summary:** at n = 100, the excess over the prediction goes from about 55× (v1) to 9× (v2) to **4.5×** (v3). With the real keys it goes from 69× to 12× to 7×. At n = 1,000 and above, v2 and v3 match the formula.

## 4. Remaining gap at small m: a known limitation

At n = 100 (m = 2,640) we still measure 14 false positives where the formula says 3. The cause is that every version computes all k positions from just two numbers, h1 mod m and h2 mod m:

- There are only m² such pairs: 2,640² ≈ 7.0 million.
- A non-member whose pair equals a member's pair gets **exactly the same k positions**, so it is **always** a false positive. Making k bigger does not help.
- Expected number of such keys: 1,000,000 × n / m² = 1,000,000 × 100 / 6,969,600 ≈ **14**.
- In version 2, h2 was forced odd. With an even m, h2 mod m could then only be odd, so only m²/2 pairs were possible and this number doubled to ≈ 29. That is exactly why version 3 stopped forcing the step odd.

We counted these keys directly from `hashint4extended` (Step 3 of `queries/fp_cases.sql`):

| n | m | non-members with exactly a member's positions, v2 (odd step) | expected v2 (m²/2 pairs) | v3 | expected v3 (m² pairs) |
|---:|---:|---:|---:|---:|---:|
| 100 | 2,640 | 25 | 28.7 | **13** | 14.3 |
| 1,000 | 21,600 | 3 | 4.3 | **2** | 2.1 |

So at n = 100, 13 of the 14 false positives in version 3 come from this effect, and the formula's ~3 is within the noise of the rest. This floor shrinks like n/m², which is why it disappears for larger filters.

This is a known property of double hashing. Kirsch & Mitzenmacher (2006) prove that a Bloom filter that derives its k positions from two hash values reaches the standard false-positive rate only **asymptotically**, as the filter grows. For small m some extra false positives remain.

**Decision.** Our false-positive model stays ONLY the standard formula. We document the small-m gap as a limitation and do not model it, because:

- it only matters when the formula predicts very few false positives (tiny filters with a large b);
- even then the cost is small: at n = 100, about 11 to 20 extra rows ≈ 190 to 340 bytes of narrow-row text. A wrong b would cost far more;
- the final join result is always exactly correct, whatever the false-positive rate.

Possible future improvement (**not done**; it would change the fixed design decision of one hash call per key): take more hash bits per key, for example a second `hashint4extended` call with another seed, so the positions depend on more than m² pairs.

## 5. Effect on the benchmark

We reran the full Batch 4 benchmark (`bench/run_all.sh`) after each change. All 36 rows are exactly correct every time. Results for `bloom_dynamic`, narrow rows (false positives = rows from B − distinct keys):

| n | b | distinct keys | false positives v1 → v2 → v3 | total bytes v1 → v2 → v3 | time (ms) v1 → v2 → v3 |
|---:|---:|---:|---:|---:|---:|
| 100 | 26.40 | 100 | 215 → 37 → **23** | 5,995 → 3,009 → **2,774** | 157 → 240 → **164** |
| 1,000 | 21.60 | 999 | 78 → 27 → **36** | 23,516 → 22,660 → **22,811** | 155 → 229 → **163** |
| 10,000 | 16.80 | 9,955 | 335 → 310 → **315** | 214,505 → 214,086 → **214,170** | 231 → 282 → **242** |
| 50,000 | 13.41 | 48,738 | 1,531 → 1,502 → **1,520** | 1,006,849 → 1,006,362 → **1,006,664** | 362 → 379 → **373** |

- **Bytes.** At n = 100 the Bloom query now moves less than half the bytes of v1. At n = 1,000 `bloom_dynamic` (22,811 bytes) beats the exact key list (23,648 bytes). For large n nothing changes, as expected.
- **Time.** v2 was slower, because the first version of the new code computed all k positions before testing any bit. v3 includes an *early exit*: positions are computed one at a time and the test stops at the first 0 bit (the positions are unchanged). On its own, a full scan of the 1,000,000 customers at site B with the n = 100 filter dropped from 102.7 ms to 69.2 ms. Times are single-machine medians of 3 runs, so differences of about ±10% are noise.
- **b sweep** (n = 10,000): the minimum is still next to the predicted b*: narrow b = 18 (b* = 16.8; b = 16 to 18 are within 0.3%), wide b = 20 (b* = 20.45).

The original Batch 4 charts (v1) are kept in `docs/midsem/v1/`; `docs/midsem/` has the current (v3) ones.

## 6. Compatibility: the filter version

Site A builds the filter and site B tests keys against it, so both **must** compute the same positions. A filter built by older code but read with the new positions could silently drop matching rows (false negatives), and the join result would be wrong. So the filter header's version went up with every change of the position rule: 1 → 2 → 3 (`SJB_VERSION` in `sjbloom/sjb_filter.h`). A filter with any other version is refused:

```
ERROR:  sjbloom: filter has version 2, but this sjbloom understands only version 3
HINT:  Site A and site B must run the same sjbloom build. Install it on both sites and rebuild the filter.
```

## 7. How to reproduce

```bash
source setup/env.example.sh
make -C sjbloom install
psql -d site_a -c "DROP EXTENSION sjbloom CASCADE; CREATE EXTENSION sjbloom;"
psql -d site_b -c "DROP EXTENSION sjbloom CASCADE; CREATE EXTENSION sjbloom;"
psql -X -d site_a -f queries/fp_rate.sql     # standard experiment, large m
psql -X -d site_a -f queries/fp_cases.sql    # the small-m cases, real keys, and the pair count
```

The version 1 numbers come from the branch `fp-investigation` (commit `cc29986`, file `queries/fp_v1_v2.sql`), which had versions 1 and 2 side by side. The version 2 numbers can be reproduced on master at commit `8fe846c` (the last version-2 commit). Master now has only version 3.

## References

- P. C. Dillinger and P. Manolios. *Bloom Filters in Probabilistic Verification.* Formal Methods in Computer-Aided Design (FMCAD 2004), LNCS 3312, pp. 367–381. Introduces enhanced double hashing.
- A. Broder and M. Mitzenmacher. *Network Applications of Bloom Filters: A Survey.* Internet Mathematics 1(4), 2004, pp. 485–509. The standard false-positive formula.
- A. Kirsch and M. Mitzenmacher. *Less Hashing, Same Performance: Building a Better Bloom Filter.* European Symposium on Algorithms (ESA 2006), LNCS 4168, pp. 456–467. Double hashing reaches the standard rate asymptotically.
- PostgreSQL 18 source, `src/backend/lib/bloomfilter.c`, function `k_hashes`. Enhanced double hashing in PostgreSQL itself.
