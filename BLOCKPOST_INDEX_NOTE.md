# Blockpost Index Note

> Status: Task-specific reference
>
> This note describes a proposed block-level co-occurrence layer for fast
> "same area" statistics and candidate pruning between `docpost` and full
> positional postings.

## Purpose

Define a lightweight block/chunk index that can answer:

- do two or more terms occur in the same local region of a document?
- how many documents have at least one shared region?
- how many shared regions exist across a corpus or filter?

This is meant as a practical middle layer between document-level presence and
exact positional near/sequence matching.

## Scope

In scope:

- a block-level query layer for fast "same region" statistics
- support for document-local block ids inside existing shard layout
- use as a coarse metric on its own and as a prefilter for exact `near`

Out of scope:

- replacing full positional postings
- exact `near` or `sequence` semantics
- geo-specific sidecar logic

## Core idea

Each document is partitioned into fixed local blocks, for example:

- a fixed token span such as `2048` or `4096` sequence positions
- or a coarser editorial span such as roughly `10` pages when stable page
  metadata exists

For each `(cf_id, doc_id)` we materialize the set of local `block_id` values in
which the term occurs.

This gives a three-layer model:

- `docpost`: term occurs somewhere in the document
- `blockpost`: term occurs in one or more local blocks in the document
- `post`: term occurs at exact sequence positions

## Proposed identifiers

- `doc_id`
  - remains the global identity used throughout the corpus
- `shard`
  - remains determined by the existing document-to-shard mapping
- `block_id`
  - is local inside one document, typically `0..N-1`

The important constraint is that block ids are not global corpus ids. They are
interpreted only in the context of one `doc_id`.

## Storage shape

Minimal conceptual shape:

- `cf_id`
- `doc_id`
- `blockpost`

Where `blockpost` is a compact encoding of the local block ids for that term in
that document. Two practical encodings are plausible:

1. delta/varint encoded sorted block ids
2. a small bitmap over local block ids

The right encoding depends on average document length and block count. Sparse
documents may prefer sorted ids; dense long documents may prefer a bitmap.

## Query semantics

### 1) Same-document presence

Current coarse question:

- do `A` and `B` occur in the same document?

This is what `docpost` answers.

### 2) Same-block presence

New coarse-local question:

- do `A` and `B` occur in at least one shared block in the same document?

This is the main new metric. It is much more precise than document co-occurrence
while still much cheaper than exact positional `near`.

### 3) Exact near / sequence

Existing precise question:

- do `A`, `B`, ... satisfy the actual positional constraints?

This continues to use full positional postings.

## Why this layer is useful

This layer targets a real gap:

- document-level statistics are often too loose for long newspaper articles or
  books
- exact positional queries are often too expensive for broad exploratory counts

Block-level overlap provides a practical middle ground:

- stronger than "same document"
- weaker than exact `near`
- often good enough for dashboards, trend plots, and exploratory filtering

## Invariants

- block boundaries must be deterministic for a given runtime/index version
- block ids are local to one document
- block-level counts must never be presented as exact `near` counts
- shard routing must continue to use global `doc_id`
- exact positional queries remain the source of truth for precise proximity

## Candidate uses

1. fast same-region statistics for term pairs such as `demokrati` and
   `diktatur`
2. coarse trend plots over newspapers or long corpora
3. candidate pruning before expensive exact `near` verification
4. fallback metric for exploratory UI where an exact `near` count is too slow

## Query examples

### Same-block docs

"How many documents contain both `demokrati` and `diktatur` in at least one
shared block?"

### Same-block block-count

"How many shared blocks exist for `A` and `B` across the selected corpus?"

### Hybrid verification

1. use `blockpost` to find documents or blocks with local overlap
2. run exact positional `near` only on those candidates

## Design trade-offs

### Fixed token blocks

Pros:

- simple to compute from existing positional postings
- deterministic and shard-local
- no dependency on page metadata quality

Cons:

- block boundaries are not human-visible

### Page-based or editorial blocks

Pros:

- intuitive for users
- easier to explain in UI

Cons:

- depends on stable metadata
- may vary in density and semantic quality

Recommended first prototype:

- fixed token blocks

## Relationship to current count work

The recent fast path for pairwise `mode=count` uses a popcount-style partner
metric over exact postings for two-term unigram queries.

The proposed block layer is different:

- it is coarser than exact positional counting
- it can support more than two terms without claiming exact `near`
- it offers a new intermediate metric rather than a replacement for exact
  positional `near`

## Validation

Before adopting this layer, validate at least:

1. block overlap counts correlate with intuitive "same local area" judgments
2. long-document false positives are materially lower than document-level
   co-occurrence
3. candidate pruning meaningfully reduces later exact `near` work
4. storage overhead is acceptable relative to `docpost` and exact postings

## Implemented by

This is currently a design note only. Likely implementation areas:

- `api_python/postings_queries.py`
- `api_python/server.py`
- shard rebuild scripts if a new `blockpost` layer is materialized on disk
- `postings.c` if a native block-overlap or block-popcount function is added

## Related docs

- `DOCUMENTATION_STRUCTURE.md`
- `CONTRACT_TO_CODE_MAP.md`
- `README.md`
- `AGENTS.md`

## Historical notes

This note grows out of two practical observations:

1. exact positional `near` is necessary for truth-level verification
2. document-level co-occurrence is often too loose for long documents

The proposed block layer is an attempt to add a useful middle resolution rather
than forcing one query layer to solve both problems.
