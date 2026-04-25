# Place ID Strategy

> Current source-of-truth:
> This document defines the canonical place identity model for the project.
> Use it together with `GEO_INDEX_CONTRACT.md` and `API_CONTRACT_GEO_V2.md`
> when making storage, API, or frontend identity decisions.

## Decision

Use **internal `place_id`** as the only canonical identity in backend storage,
API responses, and frontend state.

External identifiers are links attached to that canonical place:

- `geonames`
- `ssr`
- `wikidata`
- other future gazetteers

The backend may still expose or accept compatibility key types during the
transition, but those must resolve to the canonical internal id as early as
possible.

## Why

- one stable contract across rebuilds and source systems
- room to attach both GeoNames and SSR to the same place
- frontend cache/state keys remain stable when external mappings change
- query semantics stay fixed even when new gazetteers are added

## Canonical Model

- canonical id:
  - `place_id` (internal)
  - target API form: `placeKeyType="internal"`, `placeKey="<place_id>"`

- external mapping:
  - one-to-many links from `place_id` to external systems
  - one place can carry both `geonames` and `ssr`
  - additional sources can be added without changing query/storage shape

## Runtime Compatibility

Current live geo sidecars still use NB-internal ids in the v2 query tables and
parse bare numeric geo tokens as `nb` in runtime code.

Treat that as a **compatibility/runtime detail**, not as the long-term
architecture term:

- architecture term: `internal`
- current runtime/query token commonly seen on disk: `nb`
- practical meaning today: `nb` resolves to the same internal place identity

As long as this compatibility remains, docs and code should be explicit about
the distinction instead of mixing `internal` and `nb` as if they were separate
place models.

## Schema Direction

### 1) Canonical places table

`places`

- `place_id INTEGER PRIMARY KEY`
- `canonical_name TEXT`
- `lat REAL`
- `lon REAL`
- `country TEXT`
- optional feature / provenance fields

### 2) External id mapping layer

`place_external_ids`

- `place_id INTEGER NOT NULL`
- `source TEXT NOT NULL`
  - examples: `geonames`, `ssr`, `wikidata`
- `external_id TEXT NOT NULL`
- `is_preferred INTEGER NOT NULL DEFAULT 0`
- `confidence REAL NULL`
- `valid_from TEXT NULL`
- `valid_to TEXT NULL`
- `mapping_method TEXT NULL`
- `updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP`

Constraints and indexes:

- `PRIMARY KEY (source, external_id, place_id)`
- `INDEX idx_place_external_place (place_id, source, is_preferred)`
- `INDEX idx_place_external_source_id (source, external_id)`

The current SQL helper in `sql/place_external_ids.sql` already covers the table
shape and a GeoNames backfill. SSR links should be added through the same table,
not by introducing a second canonical id system.

## Query Semantics

- fast path and postings should conceptually operate on internal identity
- external ids should be resolved once (`geonames`/`ssr` -> `place_id`)
- response payloads should always expose internal identity fields
- runtime compatibility tokens such as `nb` may be accepted on input while the
  migration is in progress

## API Contract Direction

### Request identity

Prefer internal identity in all new and updated contracts:

- `placeKeyType: "internal"`
- `placeKey: "<place_id>"`

### Response identity

Always return:

- `placeId` (internal canonical id)
- `placeKeyType="internal"`
- `placeKey="<place_id>"`

Optional enrichment:

- `externalIds`, for example
  - `{ "geonames": "317552", "ssr": "3387" }`

## Migration Plan

1. Keep `place_id` as the canonical key in storage and responses.
2. Backfill current `geonames_id` links into `place_external_ids`.
3. Add SSR links into the same mapping table.
4. Keep API/query compatibility for existing input forms:
   - `#geo:<id>` where runtime still interprets bare digits as `nb`
   - `#geo:geonames:<id>`
   - `#geo:internal:<id>`
5. Normalize all accepted input forms to canonical internal identity early in
   request handling.
6. Move frontend state/cache keys fully to `place_id`.
7. Deprecate direct external-id query paths when compatibility is no longer
   needed.

## Compatibility Rules (Interim)

- still accept:
  - `#geo:<id>` where bare digits map to the current NB/internal runtime key
  - `#geo:geonames:<id>`
  - `#geo:internal:<id>`
- resolve `geonames` and `ssr` through external-id mapping to `place_id`
- keep `nb` documented as runtime compatibility only

## Open Decisions

- ambiguity policy:
  - can one external id map to multiple internal places?
- confidence policy:
  - hard threshold vs exposing all candidates
- temporal policy:
  - when to use `valid_from`/`valid_to`
- naming policy:
  - when to remove `nb` from externally visible query docs once runtime catches up

## Recommendation

Use this document as the architecture source of truth.

In short:

- internal `place_id` is canonical
- GeoNames and SSR are external links
- `nb` is a compatibility/runtime form that should eventually collapse into the
  internal model
