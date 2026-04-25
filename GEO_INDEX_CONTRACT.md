# Geo Index Contract (ImagiNation + Annotation Sidecar)

> Current source-of-truth:
> This document defines the active geo v2 storage contract and the separation
> between fulltext, geo sidecar, and map aggregate layers.
> Read it together with `PLACE_ID_STRATEGY.md` and `GEO_REBUILD_RUNBOOK.md`
> before changing geo storage or rebuild behavior.

This document defines the shared geo indexing model for:

- `imagination.db` for corpus- and place-list APIs
- the geo annotation sidecar used by `#geo`

It should describe the **current v2 storage shape** and the intended
architecture direction, not only historical rebuild layouts.

## 1) Scope and Separation

Use three distinct data layers:

- `imagination.db`
  - powers `/api/metadata/all`, `/api/places`, `/api/places/details`
  - optimized for corpus/filter/list UX
- fulltext postings shards
  - power `/concordance`, `/near_*`, `/or_query`, and related search endpoints
- annotation namespace sidecar (`#geo`)
  - stores fulltext-aligned geo annotations in shared coordinates
  - uses `book_id`, `seq_start`, `token_len`

Do not couple `imagination.db` internals directly to fulltext internals. Any
cross-layer behavior should go through explicit contracts.

## 2) Identity Rules

Every place mention must resolve to a stable identity:

1. Current rebuild-safe raw identity: `geonames_id`
2. Positional anchor: `(book_id, seq_start)`
3. Span metadata: `token_len`
4. Runtime/query compatibility keys may still exist temporarily

For the current `geo_disambig.db` import path, the safest canonical mention basis is:

- `book_id`
- `seq_start`
- `geonames_id`
- `token_len`

This basis is sufficient to rebuild:

- sidecar mention rows
- postings rows
- map aggregates

without assuming a separate internal place catalog.

Recommended normalized identity columns in v2 mention/posting tables:

- `place_key_type` TEXT
- `place_key` TEXT

Use them as the **materialized query key**, but not as the architecture source
of truth.

Current interpretation:

- long-term architecture may still introduce canonical internal `place_id`
- current deployed compatibility/runtime value often seen on disk: `nb`
- current disambig rebuild path can safely collapse `place_id` onto `geonames_id`
- accepted compatibility values may also include `geonames`

Important distinction:

- `nb` describes a currently deployed runtime/query token
- in the current rebuild path, that token may simply materialize the same numeric
  value as `geonames_id`
- if a true internal catalog is reintroduced later, it must be an explicit
  mapping layer rather than an assumed invariant

## 3) Canonical Geo Posting Structure (per book, v2)

For each `book_id`:

- `all_places_roaring`: roaring bitmap of all place start positions in the book
- for each `(place_key_type, place_key)`:
  - per-length bitmap:
    - `token_len = 1` -> roaring bitmap of start positions
    - `token_len = 2` -> roaring bitmap of start positions
    - etc.
  - aggregate bitmap:
    - `token_len = 0` -> all lengths combined

Minimum key tuple:

- `(book_id, place_key_type, place_key, token_len)`

## 4) SQL Schema Contract

Older docs may mention `geo_mentions`, `geo_postings`, or `geo_book_index`
without suffixes. The active v2 names are:

- `geo_mentions_v2`
- `geo_postings_v2`
- `geo_book_index_v2`

### 4.1 `places`

- `place_id INTEGER PRIMARY KEY`
- `canonical_name TEXT`
- `geonames_id INTEGER NULL`
- optional external mapping through `place_external_ids`
- optional feature fields such as `feature_class`, `feature_code`
- `lat REAL`
- `lon REAL`
- `country TEXT`

Canonical identity lives here.

### 4.2 `place_external_ids`

- `place_id INTEGER NOT NULL`
- `source TEXT NOT NULL`
  - examples: `geonames`, `ssr`, `wikidata`
- `external_id TEXT NOT NULL`
- `is_preferred INTEGER NOT NULL DEFAULT 0`
- `confidence REAL NULL`
- `valid_from TEXT NULL`
- `valid_to TEXT NULL`
- `mapping_method TEXT NULL`

This is the preferred place to attach GeoNames and SSR links in the long-term
model.

### 4.3 `geo_spans`

`geo_spans` remains the clean positional truth layer or rebuild source:

- `book_id`
- `seq_start`
- `token_len`
- `place_id`
- `variant_id`
- `score`
- `method`
- `surface_text`
- `surface_hash`

### 4.4 `geo_mentions_v2`

- `book_id INTEGER NOT NULL`
- `seq_start INTEGER NOT NULL`
- `token_len INTEGER NOT NULL`
- `place_key_type TEXT NOT NULL`
- `place_key TEXT NOT NULL`
- `place_id INTEGER NULL`
- `geonames_id INTEGER NULL`
- `variant_id INTEGER NULL`
- `surface_text TEXT NULL`

Recommended uniqueness:

- `UNIQUE (book_id, seq_start, place_key_type, place_key, token_len)`

Semantics:

- `place_key_type/place_key` is the key materialized for the current fast path
- `seq_start` is the anchor used for query semantics and near-distance
- `token_len` is span/render metadata and should be preserved in sidecar and
  postings
- in the current disambig rebuild path, `place_id` and `geonames_id` may carry
  the same numeric value
- if/when a separate internal catalog returns, `geonames_id` becomes an external
  link again rather than the basis row identity

### 4.5 `geo_postings_v2`

- `book_id INTEGER NOT NULL`
- `place_key_type TEXT NOT NULL`
- `place_key TEXT NOT NULL`
- `token_len INTEGER NOT NULL` where `0` means all lengths
- `starts_roaring BLOB NOT NULL`
- `count_mentions INTEGER NOT NULL`

Primary key:

- `PRIMARY KEY (book_id, place_key_type, place_key, token_len)`

Semantics:

- postings use `seq_start` only for search and near logic
- `token_len` is preserved so rendering/highlighting can remain faithful to the
  sidecar mention layer
- `token_len = 0` is an all-length aggregate index, not a loss of span
  information in the source layer

### 4.6 `geo_book_index_v2`

- `book_id INTEGER PRIMARY KEY`
- `all_places_roaring BLOB NOT NULL`
- `unique_places INTEGER NOT NULL`
- `total_mentions INTEGER NOT NULL`

### 4.7 `nb_places` (optional compatibility/export table)

Some exports and rebuild pipelines also carry:

- `nb_place_id`
- `geonames_id`
- optional `ssr_id`
- `name`
- feature fields
- coordinates

This is useful as a compatibility registry or import source, but it should not
replace canonical `places(place_id, ...)`.

## 5) `imagination.db` API Contract Fields

The API payloads should keep these fields stable:

- `/api/metadata/all` -> `books[]`
  - `dhlabid, urn, author, year, category, title, unique_places?, total_mentions?`
- `/api/places` -> `places[]`
  - stable place id plus list metadata
- `/api/places/details` -> `books[]`
  - `dhlabid, urn, author, year, title, category, mentions`

`id` in `/api/places` should be stable and not a transient row id. The intended
direction is canonical internal identity even if some compatibility payloads or
legacy routes still surface GeoNames-oriented values.

## 6) Build Rules

For every rebuild:

1. Normalize candidate surface forms consistently.
2. Preserve the raw mention basis:
   - `book_id`
   - `seq_start`
   - `geonames_id`
   - `token_len`
3. Materialize the query key used by the current fast path as
   `(place_key_type, place_key)`.
4. Store raw mention rows in `geo_mentions_v2`.
5. Build roaring postings grouped by:
   - `(book_id, place_key_type, place_key, token_len)`
   - `(book_id, place_key_type, place_key, token_len = 0)` for all lengths
6. Build `geo_book_index_v2`.
7. Populate/refresh `imagination.db` summary tables from the same resolved
   source.

If a separate internal `place_id` exists for the input dataset, it may be added
as enrichment. It is not required for the current `geo_disambig.db`
book/position/GeoNames rebuild path.

Typical import path from a disambiguation DB:

- source annotations -> normalized mention rows
- preserve `seq_start` as the only near-search anchor
- preserve `token_len` for rendering and per-length postings
- fill `geo_spans` or `geo_annotations_resolved`
- materialize `geo_mentions_v2`
- rebuild `geo_postings_v2` and `geo_book_index_v2`

## 7) Validation Checklist

Run after every full build:

- **Coverage**
  - number of books in `geo_book_index_v2` equals target corpus books
- **Referential**
  - every `geo_mentions_v2.place_id` exists in `places`
- **Posting integrity**
  - for random sample of `(book, place, len)`:
    - decoded bitmap cardinality equals `count_mentions`
    - decoded starts equal mention-derived starts
- **Aggregate integrity**
  - `token_len = 0` bitmap equals union of all per-length bitmaps
- **Book aggregate integrity**
  - `all_places_roaring` equals union of all place bitmaps for that book
- **External-id integrity**
  - sample `geonames` and `ssr` ids resolve to the intended `place_id`
- **Registry integrity**
  - `annotation_book_map` overlaps the active geo DB on the same `book_id`s
- **API contract integrity**
  - `/api/places` and `/api/places/details` return non-empty payload for known
    books/tokens

## 8) Performance Targets

- `/api/places` on 1k-5k books: sub-second to low seconds
- `/api/places/details`: typically sub-second for common tokens
- query patterns should use `json_each(?)` for dhlabid filters instead of giant
  `IN (...)` lists

## 9) Versioning

Add explicit metadata table:

- `geo_index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`

Minimum keys:

- `schema_version`
- `build_timestamp_utc`
- `source_corpus_version`
- `place_resolver_version`
- `roaring_codec_version`

Any incompatible change increments `schema_version`.

## 10) Current Reality Check

In the current `/mnt/disk4` setup:

- the active sidecar uses `geo_mentions_v2`, `geo_postings_v2`,
  `geo_book_index_v2`, and `places`
- the active registry points `geo` to `annotation_geo_nb.db`
- deployed query rows currently use `place_key_type = 'nb'`

That does **not** change the architecture direction:

- canonical identity is internal `place_id`
- GeoNames and SSR are external links
- `nb` is a compatibility/runtime key that should be documented explicitly until
  the query surface is aligned with `internal`

