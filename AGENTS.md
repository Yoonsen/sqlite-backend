# AGENTS.md

This repository is a postings-based SQLite backend with a FastAPI service. The
main active workstream is geo annotation: importing updated place annotations,
materializing them into a fulltext-aligned sidecar, and deriving map-friendly
aggregates for frontend use.

## Read These First

If you are working on geo, read these documents before changing code:

1. `README.md`
   - entry point for runtime/config/build workflow
2. `DOCUMENTATION_STRUCTURE.md`
   - standard structure for contract and runbook docs in this repo
3. `CONTRACT_TO_CODE_MAP.md`
   - bridge from the docs to the main implementation files and commands
4. `PLACE_ID_STRATEGY.md`
   - architecture source of truth for place identity
5. `GEO_INDEX_CONTRACT.md`
   - current v2 storage shape and separation of concerns
6. `API_CONTRACT_GEO_V2.md`
   - stable request/response contract for geo-facing API behavior
7. `GEO_ENRICHMENT_PIPELINE.md`
   - current candidate/KWIC/gazetteer/LLM enrichment flow before sidecar import
8. `GEO_REBUILD_RUNBOOK.md`
   - operational rebuild flow and validation gates
9. `ANNOTATION_LAYERS_BLUEPRINT.md`
   - shared model for annotation layers and registry behavior

## Secondary Geo References

Read these when the task touches a specific rebuild path or map aggregate behavior:

- `GEO_DISAMBIG_TO_V2_MAPPING.md`
  - concrete source-to-v2 mapping for `geo_disambig.db`
- `GEO_IMAGINATION_DB.md`
  - role and rebuild contract for `geo_imagination.db`

## Task-Specific References

Read these only when the task clearly matches the topic:

- `DEPLOY_GEO_CHECKLIST.md`
  - server-side deploy/smoke-test checklist for geo changes
- `ANNOTATION_GEO_DISAMBIGUATION_SCHEMA.md`
  - LLM/disambiguation payload and writeback schema
- `FRONTEND_GEO_HANDOFF.md`
  - frontend-facing handoff notes from an earlier geo contract phase; use with care

## Historical / Evolution Notes

Older notes can still be valuable because they document how the current model was
reached. Use them as background or migration context, but do not let them
override the current source-of-truth documents above.

In particular, treat these as historical design notes unless the current task is
explicitly about archaeology or migration:

- `GEO_STANDOFF_PLAN.md`
  - early shard-local design sketch
- `GEO_DISAMBIG_BUILD_NOTE.md`
  - first working import/test-run notes and observed counts

## Mental Model

There are three distinct data layers:

- fulltext postings shards:
  - power `/concordance`, `/near_*`, `/or_query`
- geo annotation sidecar:
  - currently `annotation_geo_nb.db`
  - powers `#geo`, concordance-aligned geo, and positional inspection
- map aggregate DB:
  - `geo_imagination.db`
  - powers place lists, plotting, per-book place stats, and timeline views

Do not confuse `geo_imagination.db` with the positional annotation source of
truth. It is derived from `annotation_geo_nb.db` + `imagination.db`.

## Current Geo Reality

- Current enrichment flow reads candidates and KWIC directly from fulltext on
  `/mnt/disk4`
- Candidate enrichment currently combines `GeoNames` and `SSR`
- KWIC/context is processed by an LLM resolver in the enrichment pipeline
- Current rebuild source is usually `/home/larsj/geotest/geo_disambig.db`
- Current safe raw mention basis is:
  - `book_id` / `dhlabid`
  - `seq_start`
  - `geonames_id`
  - `token_len`
- Current practical global place identity in enrichment is `geonames_id`
- `SSR` currently links onto `geonames_id` where mapping is possible
- Runtime compatibility still uses `nb` in several places
- Long-term architecture term is still canonical internal `place_id`

In practice today:

- `annotation_geo_nb.db` is the deployed geo sidecar name
- `geo_mentions_v2`, `geo_postings_v2`, `geo_book_index_v2` are the active
  query tables
- `geo_imagination.db` should only contain places that actually occur in at
  least one book

## Important Files

- `api_python/server.py`
  - main API implementation
- `build_geo_imagination.py`
  - rebuilds `geo_imagination.db`
- `import_geo_disambig_to_annotation_nb.py`
  - imports source annotation data into `nb_places` + `geo_annotations_base`
- `build_geo_nb_contract_v1.py`
  - materializes resolved/query tables into the sidecar
- `sync_geo_annotation_book_map.py`
  - syncs `annotation_book_map` coverage

## Rebuild Workflow

Typical safe local rebuild flow:

1. copy current local DBs into a new run dir under `/mnt/disk4/geo_rebuild_runs`
2. import from updated `geo_disambig.db`
3. rebuild `annotation_geo_nb.db` materialized tables
4. rebuild `geo_imagination.db`
5. verify counts and a few known places
6. promote run output into `/mnt/disk4/imagination_shards_roaring_main_v1`
7. only then copy files to server and restart container

## Frontend-Facing Endpoints Added Recently

- `POST /api/places`
  - now returns `featureCode` and `kind`
- `POST /api/places/stats`
  - returns totals, `kinds`, `featureClasses`, `featureCodes`
- `POST /api/place/qa`
  - returns corpus coverage plus tagged vs non-tagged ratio for a query/id
- `POST /api/places/first-year`
  - returns first observed year per place for a chosen corpus

## Known Gotchas

- Geo render/namespace paths effectively cap `before` and `after` at `25`
  because `NearQueryRequest` is adapted into `OrQueryRequest`
- `annotation_book_map` must stay in sync with the active geo DB
- A larger `annotation_geo_nb.db` after rebuild is expected; `annotation_registry.db`
  is usually comparatively stable
- For ambiguous names, prefer resolving to `id` before treating QA numbers as
  place-specific truth

## Do / Don't

Do:

- keep `annotation_geo_nb.db` and `geo_imagination.db` conceptually separate
- treat rebuilds as transform-and-materialize, not file swaps
- use run directories for safety before promoting DBs
- verify known place examples like `Norge`, `Bergen`, etc. after rebuilds

Don't:

- treat `geo_imagination.db` as the positional truth layer
- assume `nb` is the long-term architecture model
- change registry/book-map casually without checking active DB alignment
