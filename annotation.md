# Sparse Annotation Model

This document defines the annotation architecture used for demo and scaling.

## Core idea

The annotation layer is corpus-dependent, but not shard-dependent.

- Global coordinate system is always `(book_id, seq)`.
- `book_id` is global dhlabid and is never recoded.
- Text shards remain local/self-contained.
- Annotation is stored in a separate DB (for example `annotation_geo.db`).

## Seq-first principle

Everything is anchored to `seq`.

- The primary truth is position in text: `(book_id, seq)`.
- A token is observed at a `seq`.
- A span is a collection of `seq` values (`seq_start` + `token_len`).
- Point annotations (for example images) are also anchored to `seq`.
- Classifications such as POS, geo, names, idioms, or fixed expressions are
  annotations over the same `seq` axis.

This gives one shared coordinate model across all layers and shards.

## Why "sparse"

We only store annotation spans that exist, not a full parallel token stream.
This keeps storage small and lookup fast.

## Data model

## 1) Layer metadata

`layers`

- `layer_id` (PK)
- `name` (for demo: `geo`)
- `version`
- `created_at`
- `notes`

## 2) Gazetteer entities

`places`

- `place_id` (PK, stable internal id)
- `canonical_name`
- `disambig_no`
- `geonames_id` (optional but recommended)
- `lat`, `lon`
- `country`

`place_variants`

- `variant_id` (PK)
- `place_id` (FK -> places.place_id)
- `variant_text` (surface form, e.g. `New York City`)
- `norm_text` (normalized)
- `token_len` (optional dictionary hint)

## 3) Span truth table (coordinate truth)

`geo_spans`

- `book_id`
- `seq_start`
- `token_len`
- `place_id`
- `variant_id`
- `score` (optional)
- `method` (optional)
- `surface_text` (optional but useful for UI/debug)
- `surface_hash` (optional integrity check)

Primary key recommendation:

- `PRIMARY KEY (book_id, seq_start, place_id, variant_id)`

Indexes:

- `(place_id, book_id)`
- `(book_id, seq_start)`

## 4) Optional postings materialization (fast inverted lookup)

`geo_postings`

- `place_id`
- `book_id`
- `post_blob` (encoded `seq_start` list)

This is optional in v1. It can be generated from `geo_spans`.

## Mapping to text model

This is intentionally parallel to text shards:

- Text: `tokens` + `unigrams/postings`
- Annotation: `geo_spans` + `geo_postings`

## Retrieval guarantee

For each annotation in `geo_spans`, span is:

- start: `seq_start`
- end: `seq_start + token_len - 1`

To reconstruct exact text:

1. Resolve `book_id -> shard_id` via dispatcher index.
2. Query text shard tokens for `[seq_start, seq_start + token_len - 1]`.
3. Join tokens in `seq` order.

If `surface_hash` is stored, verify reconstructed text hash equals stored hash.

## Demo scope

- Base corpus: 27k books across 3 shards.
- Demo corpus: 1900 selected `book_id`s.
- If stable: extend to all fiction.

## Notes

- Keep annotation ids independent from text `cf_id`s.
- Keep `book_id` and `seq` as the shared global bridge.
- Prefer simple `geo_spans` first; add `geo_postings` when needed for speed.
