# PERF notes (near vs concordance)

This note is a handoff for performance review of the current near/group pipeline.

## Problem statement

There is a large latency gap between:

- two-term search via `POST /concordance` (very fast)
- equivalent two-group search via `POST /near_fragments` (much slower)

Example:

- `spise middag` via `/concordance`: around ~120 ms (observed)
- `[[spise],[middag]]` via `/near_fragments`: around ~3.6 s (observed)

The expected direction is that these should be much closer when semantics are equivalent.

## Core design goal

Use one CNF-style bitmap pipeline:

1. postings -> bitmap (decode once)
2. OR inside each group
3. near window mask/shift
4. AND across groups
5. output count/sample positions

Avoid roundtrips like `bitmap -> postings blob -> bitmap` for intermediate steps.

## Repro payloads

## A) Fast baseline (concordance)

Endpoint: `POST /concordance`

```json
{
  "wordA": "spise",
  "wordB": "middag",
  "window": 15,
  "before": 15,
  "after": 15,
  "perBook": 2,
  "docSamples": 0,
  "totalLimit": 100,
  "schema": "unigrams",
  "useFilter": false,
  "filterIds": [],
  "symmetric": true,
  "excludeSelf": false
}
```

## B) Slow equivalent path (near fragments)

Endpoint: `POST /near_fragments`

```json
{
  "termGroups": [["spise"], ["middag"]],
  "window": 15,
  "before": 15,
  "after": 15,
  "perBook": 2,
  "docSamples": 0,
  "totalLimit": 100,
  "schema": "unigrams",
  "symmetric": true,
  "excludeSelf": false,
  "useFilter": false,
  "filterIds": [],
  "maxVariants": 6
}
```

## C) Typical group case

Endpoint: `POST /near_fragments`

```json
{
  "termGroups": [["spiser", "spise"], ["middag"]],
  "window": 15,
  "before": 15,
  "after": 15,
  "perBook": 2,
  "docSamples": 0,
  "totalLimit": 100,
  "schema": "unigrams",
  "symmetric": true,
  "excludeSelf": false,
  "useFilter": false,
  "filterIds": [],
  "maxVariants": 6
}
```

## What has already been tested

- OR-only (`/or_query`) is fast and appears healthy.
- Group resolution bug fixed: missing term inside OR group no longer invalidates full group.
- Bitmap mask internals in `postings.c` improved (bit-shift based window mask instead of per-bit/per-offset loops).
- Tried SQL-side grouped union CTE before near; no major win in end-to-end latency.
- Restored fused aggregate input path (`t.grp, u.post`) for near bitmap multi-group.
- Moved `docSamples` handling in `near_fragments` to after near candidate creation (to avoid pre-downsampling semantics).

## Important observation about sampling

With current control flow:

- `docSamples=10` can return fewer rows (expected),
- but can still be slower than `docSamples=0` if totalLimit-based early stop is not hit quickly.

So lower returned row count does not automatically imply lower total latency.

## Known semantic mismatch to inspect

`/concordance` and `/near_fragments` can center snippets on different token positions for equivalent two-term input:

- concordance often centers on `wordA`
- near_fragments path may center on the matched anchor from near blob logic

This can produce different `(bookId, pos)` even when underlying match pairs are equivalent.

## Review checklist for external contributors

- Confirm exact hotspot split:
  - SQL scan/group work
  - aggregate/UDF CPU
  - post-processing (`post_count`/`post_sample`/`fetch_window`)
- Verify no hidden encode/decode roundtrip remains in group-near path.
- Evaluate a fused aggregate that returns sampled positions directly (or compact bitmap sample output) to cut per-row SQL calls.
- Ensure singleton-groups path and two-term path share the same core execution strategy where semantics overlap.

## Suggested next step

Implement a dedicated fused near-fragments aggregate UDF that:

- accepts `(grp, post, off_min, off_max, chunk_size, per_book)` in aggregate mode,
- performs OR+near in bitmap space,
- samples positions in-C with early-stop,
- returns compact sampled positions per book directly.

That should remove a large amount of SQL/Python orchestration overhead after near match creation.
