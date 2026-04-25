# API Contract: Geo v2

> Current source-of-truth:
> This document defines the active backend/frontend contract for geo-facing API
> behavior.
> Read it together with `PLACE_ID_STRATEGY.md` and `GEO_INDEX_CONTRACT.md`
> before changing geo request or response semantics.

## Scope

This document defines a single, stable contract for geo search and geo concordance behavior across backend and frontend.

Goals:
- reduce overlapping entry points
- make response shapes predictable
- separate hit/count endpoints from snippet endpoints
- keep place identity lookup separate from hit/query endpoints


## Current Behavior

Geo can currently be expressed through multiple patterns and endpoints:
- query forms: `#geo`, `#geo:<name>`, `#geo:<id>`, `#geo:geonames:<id>`,
  `#geo:internal:<id>`
- request styles: `terms` and `termGroups`
- endpoints: `/or_query`, `/near_query`, `/near_fragments`

This leads to inconsistent response expectations:
- some responses include snippet-like fields (`fragHtml`, `fragRaw`)
- some return only hit rows (`bookId`, `seqStart`, `surfaceText`, etc.)
- `renderHits` behavior varies by path

Place identity should be resolved through a separate resolver endpoint before geo-hit queries
when frontend starts from a text form rather than an explicit id.

Current runtime note:

- bare numeric `#geo:<id>` is currently interpreted in code as the deployed
  NB/internal runtime key
- this is a compatibility behavior
- the architecture target remains canonical internal `place_id`


## Target Behavior (v2)

### 1) Input model

Use `termGroups` as primary query model for all search logic.

`termGroups` semantics:
- each inner group is OR
- groups are combined with AND across the query

Example:
- `termGroups: [["#geo:internal:1032414"], ["krig", "slag"]]`
- means: geo id AND (`krig` OR `slag`)

Allowed geo identity forms:
- `#geo`
- `#geo:<id>` (current runtime compatibility form for NB/internal numeric ids)
- `#geo:geonames:<id>`
- `#geo:internal:<id>`

`#geo:<name>` must be explicitly documented as either:
- supported with deterministic resolver behavior, or
- deprecated/removed.

Recommended contract direction:

- canonical request identity should be internal `place_id`
- external ids such as GeoNames and SSR should resolve to internal identity
  before the hot path
- bare `#geo:<id>` remains supported while runtime compatibility with `nb`
  exists


### 2) Endpoint semantics

- `/api/place/resolve`
  - purpose: pure place identity resolution
  - input: exactly one of `query` or `id`
  - output: `matches[]` with canonical name, matched form, alternate forms,
    coordinates and stable internal `id`
  - not a corpus endpoint and not an annotation-hit endpoint

- `/or_query`
  - purpose: namespace lookup, hit listing, simple result rows
  - not the canonical snippet endpoint

- `/near_query`
  - purpose: counts/statistics for near/group queries
  - canonical near endpoint with shared input payload
  - `mode=count|hits|render` decides whether backend returns counts, positions, or rendered fragments
  - supports geo namespace anchors in the same payload model:
    - `#geo`
    - `#geo:<id>`
    - `#geo:geonames:<id>`
    - `#geo:internal:<id>`
    - `#geo` or `#geo:<id>` combined with plain OR groups

- `/near_fragments`
  - purpose: snippets/concordance rendering
  - compatibility endpoint for snippet UI
  - equivalent to `/near_query` with `mode=render`


### 3) Response contract by use-case

#### A. Hits-only responses

Must clearly be hits-only and not "partial snippets".

Geo row minimum:
- `bookId`
- `seqStart`
- `tokenLen`
- `placeKeyType`
- `placeKey`
- recommended: `placeId`
- optional: `place.geonamesId`, `place.externalIds`, `place.lat`, `place.lon`

#### B. Snippet responses (canonical)

When snippet rendering is requested, fields must be stable:
- `bookId`
- `seqStart`
- `tokenLen`
- `fragRaw`
- `fragHtml`
- `surfaceText`
- `place` (geo metadata object)

Recommended `place` object direction:

- `placeId` as canonical identity
- `canonicalName`
- optional `externalIds` object
- `lat`, `lon`, `country`


## Fast Path Requirements

For explicit geo id + book filter:
- use postings fast path
- avoid text fallback logic in hot path
- avoid per-hit metadata queries

Recommended lookup strategy:
1. resolve positions from `geo_postings_v2` (`token_len = 0`)
2. attach place metadata once per key
3. use snippet endpoint for full concordance rendering if needed

Identity handling for the fast path:

1. normalize input identity to canonical internal `place_id` where possible
2. map that identity to the currently materialized query key if runtime still
   uses `nb`
3. execute postings lookup


## Breaking Changes

Potentially breaking for frontend:
- do not rely on geo metadata embedded inside `fragHtml` attributes
- read geo metadata from JSON row fields (`place`, `placeKeyType`, `placeKey`, `placeId`)
- assume snippets come from `/near_fragments`, not arbitrary `/or_query` paths
- do not assume `placeKeyType="geonames"` is the canonical response form


## Frontend Assumptions (v2-safe)

- if UI starts from a place string, call `/api/place/resolve` first to obtain a stable place id
- if UI needs snippets: call snippet endpoint explicitly
- if UI needs only geo hits/map route: use hits-only endpoint and row metadata
- prefer explicit internal identity in new code
- tolerate bare numeric `#geo:<id>` only as compatibility with the currently
  deployed runtime


## Migration Plan

1. Backend:
   - enforce endpoint semantics above
   - keep canonical internal `place_id` in response fields
   - keep `nb`/bare-id compatibility only as an adapter layer while query tables
     still expose that key type
   - keep temporary compatibility behavior behind explicit flags if needed
2. Frontend:
   - migrate popup/data consumers to JSON fields, not `fragHtml` parsing
   - treat `placeId` as the stable state/cache key
   - route all snippet rendering to canonical snippet endpoint
3. Cleanup:
   - deprecate ambiguous geo input forms
   - collapse `nb` runtime compatibility into explicit internal identity when
     backend/query tables are aligned
   - remove inconsistent `renderHits` behavior across endpoints


## Example Payloads

### Place form to id

`POST /api/place/resolve`

```json
{
  "query": "rio de janeiro",
  "limit": 5
}
```

### Place id to known forms

`POST /api/place/resolve`

```json
{
  "id": "<place-id>",
  "limit": 5
}
```

### Geo id hits-only

`POST /or_query`

```json
{
  "terms": ["#geo:internal:1032414"],
  "useFilter": true,
  "filterIds": [100617608],
  "totalLimit": 200
}
```

Compatibility form still accepted in current runtime:

```json
{
  "terms": ["#geo:1032414"],
  "useFilter": true,
  "filterIds": [100617608],
  "totalLimit": 200
}
```

### Geo + word groups counts

`POST /near_query`

```json
{
  "termGroups": [["#geo:internal:1032414"], ["krig", "slag"]],
  "window": 8,
  "mode": "count",
  "useFilter": true,
  "filterIds": [100617608, 100617609]
}
```

### Geo + word groups snippets

`POST /near_query`

```json
{
  "termGroups": [["#geo:internal:1032414"], ["krig", "slag"]],
  "window": 8,
  "before": 8,
  "after": 8,
  "useFilter": true,
  "filterIds": [100617608, 100617609],
  "totalLimit": 300,
  "mode": "render"
}
```

### Geo all hits

`POST /near_query`

```json
{
  "termGroups": [["#geo"]],
  "useFilter": true,
  "filterIds": [100617608, 100617609],
  "totalLimit": 300,
  "mode": "hits"
}
```

## Corpus Build Content Filter

`POST /api/corpus/build`

Purpose:
- build a corpus from metadata filters
- optionally intersect with an external corpus (`baseCorpus`)
- optionally filter by fulltext keywords across shards (`contentKeywords`)

Keyword semantics:
- `contentOperator: "AND"` (default) means all terms must match
- `contentOperator: "OR"` means at least one term must match
- empty/omitted `contentKeywords` means no content filter
- `baseCorpus` is optional; when omitted the metadata result is used as base

Example (`AND`):

```json
{
  "filters": {
    "author": "Ibsen",
    "yearRange": [1850, 1900]
  },
  "baseCorpus": [100617608, 100617609, 100617610],
  "contentKeywords": ["krig", "slag*"],
  "contentOperator": "AND"
}
```

## Compatibility Summary

Use this document with the following rule of thumb:

- architecture source of truth: internal `place_id`
- accepted compatibility inputs: `#geo:<id>`, `#geo:geonames:<id>`,
  `#geo:internal:<id>`
- current deployed runtime may still materialize `placeKeyType="nb"` in some
  v2 tables and responses

That compatibility should be preserved only where necessary and described
explicitly wherever it leaks into the public surface.

Example (`OR`):

```json
{
  "baseCorpus": [],
  "contentKeywords": ["rio", "janeiro", "brasil"],
  "contentOperator": "OR"
}
```
