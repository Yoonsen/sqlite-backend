# Mapping `geo_disambig.db` to Geo v2 Sidecar

This note describes how `/home/larsj/geotest/geo_disambig.db` can be converted
into the current v2 geo sidecar shape used by the backend.

Target sidecar shape:

- `places`
- `geo_spans`
- `geo_mentions_v2`
- `geo_postings_v2`
- `geo_book_index_v2`
- registry rows in `annotation_registry.db` / `annotation_book_map`

This is a **transform-and-rebuild** path, not a direct file swap.

## Short Assessment

`geo_disambig.db` contains enough information to feed the current v2 sidecar,
but it is not already stored in v2 form.

What maps well:

- per-book ids via `dhlabid`
- mention positions via `seq_start`
- resolved GeoNames ids on annotation rows
- explicit span length via `len` in `book_place_annotations`
- enough metadata to fill sidecar mention rows and map aggregates

What does not map directly:

- roaring postings (`geo_postings_v2`, `geo_book_index_v2`) must be rebuilt
- multiple candidates can still exist for the same `(book_id, seq_start)`
- current runtime still materializes compatibility keys as `nb`
- a separate internal `place_id` is not available as a trustworthy invariant in
  this import path

## Source Tables of Interest

From `geo_disambig.db`:

- `book_place_annotations`
  - carries `dhlabid`, `seq_start`, `geonames_id`, `surface`, `len`
- `geo_places`
  - carries `geonames_id`, coordinates, feature fields, canonical-ish name

Older source tables such as `nb_places`, `geo_annotations`, `concordances`, and
`predictions` may still exist in some exports, but the current rebuild path
should prefer `book_place_annotations` + `geo_places` when available.

## Canonical Mapping Rules

Use these rules during import:

1. Canonical raw mention identity is `(book_id, seq_start, geonames_id)`.
2. `token_len` belongs to that same raw mention basis and must be preserved.
3. Current runtime may still materialize the query key as:
   - `place_key_type = 'nb'`
   - `place_key = CAST(geonames_id AS TEXT)`

That runtime shape is acceptable in the current v2 sidecar as a compatibility
layer even when no separate internal `place_id` is available.

## Source -> Target Mapping

### 1) Canonical places

Primary source:

- `geo_disambig.geo_places`

Target:

- `places`

Mapping:

- `geo_places.geonames_id` -> `places.place_id`
- `geo_places.name` -> `places.canonical_name`
- `geo_places.geonames_id` -> `places.geonames_id`
- `NULL` -> `places.feature_class` unless a source column exists
- `geo_places.feature_code` -> `places.feature_code`
- `geo_places.lat` -> `places.lat`
- `geo_places.lon` -> `places.lon`
- `geo_places.country_code` -> `places.country`

In this path, `place_id` and `geonames_id` intentionally collapse to the same
value.

### 2) Span / truth rows

Primary source:

- `geo_disambig.book_place_annotations`
- joined with `geo_places`

Target:

- `geo_spans`

Mapping:

- `book_place_annotations.dhlabid` -> `geo_spans.book_id`
- `book_place_annotations.seq_start` -> `geo_spans.seq_start`
- `book_place_annotations.len` -> `geo_spans.token_len`
- `book_place_annotations.geonames_id` -> `geo_spans.place_id`
- `NULL` -> `geo_spans.variant_id`
- `NULL` -> `geo_spans.score`
- `'geo_disambig_bpa'` -> `geo_spans.method`
- `book_place_annotations.surface` -> `geo_spans.surface_text`
- computed hash if desired -> `geo_spans.surface_hash`

`seq_start` is the near-search anchor. `token_len` is retained for span fidelity
and rendering, not as the core near-distance coordinate.

### 3) Query-layer mentions

Primary source:

- resolved span rows or `geo_annotations_resolved`-style staging output

Target:

- `geo_mentions_v2`

Mapping:

- `book_id` -> `geo_mentions_v2.book_id`
- `seq_start` -> `geo_mentions_v2.seq_start`
- `token_len` -> `geo_mentions_v2.token_len`
- current runtime compatibility value `'nb'` -> `geo_mentions_v2.place_key_type`
- `CAST(geonames_id AS TEXT)` -> `geo_mentions_v2.place_key`
- `geonames_id` -> `geo_mentions_v2.place_id`
- `geonames_id` -> `geo_mentions_v2.geonames_id`
- `NULL` -> `geo_mentions_v2.variant_id`
- `surface_text` -> `geo_mentions_v2.surface_text`

This keeps sidecar and postings derivable from the same mention basis even while
runtime still uses the `nb` label.

### 4) Postings and book index

Primary source:

- final deduplicated mention rows

Targets:

- `geo_postings_v2`
- `geo_book_index_v2`

These are rebuild products:

- group by `(book_id, place_key_type, place_key, token_len)`
- emit per-length roaring bitmaps
- emit aggregate `token_len = 0` bitmaps
- emit per-book `all_places_roaring`, `unique_places`, `total_mentions`

Do not try to copy these tables directly from `geo_disambig.db`.

## Required Transformations

### Resolve `nb_place_id`

In the current `book_place_annotations` import path, there is no trustworthy
separate internal id to recover. The practical rule is:

1. read `book_place_annotations.geonames_id`
2. join to `geo_places.geonames_id`
3. carry `geonames_id` forward as both `place_id` and `geonames_id`

### Preserve `token_len`

`book_place_annotations` already carries `len`, so no concordance recovery step
is required in this path.

Policy:

- copy `len` into every sidecar mention/span row
- preserve `token_len` in `geo_postings_v2`
- use `seq_start` for near semantics and `token_len` primarily for rendering

### Drop non-positional rows

Rows with `seq_start IS NULL` are book-level or unresolved-to-position outputs.
They do not belong in:

- `geo_spans`
- `geo_mentions_v2`
- `geo_postings_v2`
- `geo_book_index_v2`

If they remain valuable, store them in a separate diagnostic or provenance table.

### Deduplicate before materialization

Before building v2 tables, deduplicate by a stable key such as:

- `(book_id, seq_start, geonames_id, token_len)`

If multiple candidates exist for the same position, choose one by explicit
policy, for example:

1. exact/clean surface-name match if available
2. longest span
3. deterministic tie-break on `geonames_id`

## Practical Import Pipeline

1. Load or refresh `places` from `geo_places`.
2. Build a resolved staging table with:
   - `book_id`
   - `seq_start`
   - `token_len`
   - `geonames_id`
   - `surface_text`
   - `provenance`
3. Populate `geo_spans`.
4. Populate `geo_mentions_v2`.
5. Rebuild `geo_postings_v2`.
6. Rebuild `geo_book_index_v2`.
7. Refresh `annotation_book_map` for the active geo namespace.
8. Run `validate_geo_index.py`.

## Main Blockers to Watch

- multiple GeoNames candidates can still collide on the same `(book_id,
  seq_start)` and force deterministic collapse
- current runtime still expects `nb` in some query paths even though this build
  path is GeoNames-based underneath

## Recommendation

Use `geo_disambig.db` as a **source dataset**, not as the deployable sidecar.

For the current export shape, the safest path is:

1. treat `(book_id, seq_start, geonames_id, token_len)` as the canonical mention
   basis
2. materialize the current v2 query layer from that basis
3. keep `nb` only as a runtime compatibility label until the backend query
   surface is fully aligned with a more explicit GeoNames or internal key story
