# Contract to Code Map

This document links the main Markdown contracts to the files and commands that
currently realize them.

Use it as a bridge:

- from docs to implementation when you need to change code safely
- from code to docs when you need to update the contract after a behavior change

## Entry Point

- `README.md`
  - repo entry point and documentation map
- `AGENTS.md`
  - onboarding order and guardrails for new agents
- `DOCUMENTATION_STRUCTURE.md`
  - standard structure for writing or refactoring contract docs

## `PLACE_ID_STRATEGY.md`

Purpose:
- canonical place identity rules

Implemented by:
- `api_python/server.py`
  - request handling and response fields for geo-facing endpoints
- `api_python/annotations.py`
  - namespace parsing and geo-key resolution behavior
- `build_geo_contract_v2.py`
  - materializes `place_id` / `geonames_id` carrying v2 mention and postings rows
- `build_geo_nb_contract_v1.py`
  - current NB-oriented materialization path still used in compatibility flows
- `import_geo_disambig_to_annotation_nb.py`
  - import path from `geo_disambig.db` into the NB sidecar model
- `sql/place_external_ids.sql`
  - long-term external-id mapping layer for canonical place identity

Operational touchpoints:
- `POST /api/place/resolve`
- `POST /api/places`
- `POST /api/places/details`

## `GEO_INDEX_CONTRACT.md`

Purpose:
- active geo v2 storage contract and layer separation

Implemented by:
- `build_geo_contract_v2.py`
  - creates and populates `geo_mentions_v2`, `geo_postings_v2`, `geo_book_index_v2`
- `build_geo_nb_contract_v1.py`
  - current deployed NB-oriented contract materialization
- `api_python/annotations.py`
  - reads namespace DBs and positional geo rows
- `api_python/server.py`
  - uses sidecar and postings data during API execution
- `validate_geo_index.py`
  - validates table shape, counts, and bitmap consistency against the contract
- `geo_rebuild_metrics.py`
  - summarizes rebuild outputs in contract-relevant count form

Related SQL / data shape files:
- `sql/annotation_geo.sql`
- `sql/annotation_geo_nb.sql`

## `API_CONTRACT_GEO_V2.md`

Purpose:
- current request/response contract for geo-facing API behavior

Implemented by:
- `api_python/server.py`
  - FastAPI endpoints and payload/response handling
- `api_python/annotations.py`
  - parsing and resolving `#namespace` and `#geo` query forms

Primary endpoint surface:
- `GET /health`
- `POST /api/place/resolve`
- `POST /api/places`
- `POST /api/places/details`
- `POST /api/places/stats`
- `POST /api/place/qa`
- `POST /api/places/first-year`
- `POST /or_query`
- `POST /near_query`
- `POST /near_fragments`

Frontend consumers:
- `web/app.js`
- `web/index.html`

## `GEO_ENRICHMENT_PIPELINE.md`

Purpose:
- current upstream geo candidate, KWIC, gazetteer, and LLM disambiguation flow

Implemented by:
- external enrichment processes outside this repository
  - candidate generation from fulltext on `/mnt/disk4`
  - direct KWIC extraction from corpus fulltext
  - gazetteer combination across `GeoNames` and `SSR`
  - LLM disambiguation
- `ANNOTATION_GEO_DISAMBIGUATION_SCHEMA.md`
  - expected LLM input/output row shape
- `import_geo_disambig_to_annotation_nb.py`
  - import into the sidecar pipeline
- `build_geo_nb_contract_v1.py`
  - materialization into current deployed query tables
- `build_geo_contract_v2.py`
  - materialization into current v2 tables

## `GEO_REBUILD_RUNBOOK.md`

Purpose:
- practical rebuild, validation, and promotion flow

Implemented by:
- `run_geo_rebuild_phase1.sh`
  - phase 1 wrapper
- `run_geo_rebuild_phase2.sh`
  - phase 2 wrapper
- `build_geo_contract_v2.py`
  - v2 table rebuild
- `build_geo_nb_contract_v1.py`
  - NB-compatible table rebuild
- `build_geo_imagination.py`
  - derived map DB rebuild
- `sync_geo_annotation_book_map.py`
  - registry coverage sync
- `validate_geo_index.py`
  - validation report generation
- `geo_rebuild_metrics.py`
  - metrics report generation

Related commands:
- `python validate_geo_index.py --db <db-path> --table-suffix _v2 --sample 200`
- `python geo_rebuild_metrics.py --run-root <run-root> --run-dir <run-dir>`

## `ANNOTATION_LAYERS_BLUEPRINT.md`

Purpose:
- shared namespace, registry, and per-layer table model

Implemented by:
- `api_python/annotations.py`
  - namespace parsing and registry lookup
- `sync_geo_annotation_book_map.py`
  - active namespace coverage sync
- `sql/annotation_registry.sql`
  - registry schema
- `sql/annotation_geo.sql`
  - geo namespace schema
- `sql/annotation_geo_nb.sql`
  - NB-oriented geo namespace schema

Operational concepts realized here:
- `annotation_namespaces`
- `annotation_book_map`

## Secondary References

### `GEO_DISAMBIG_TO_V2_MAPPING.md`

Implemented by:
- `import_geo_disambig_to_annotation_nb.py`
- `build_geo_nb_contract_v1.py`
- `build_geo_contract_v2.py`

### `GEO_IMAGINATION_DB.md`

Implemented by:
- `build_geo_imagination.py`
- `api_python/server.py`

### `ANNOTATION_GEO_DISAMBIGUATION_SCHEMA.md`

Implemented by:
- `api_python/server.py`
- `api_python/annotations.py`

### `DEPLOY_GEO_CHECKLIST.md`

Implemented by:
- `run_api.sh`
- `sync_geo_annotation_book_map.py`

## Maintenance Rule

Whenever one of the main contract documents changes in a way that affects
behavior, update one of these too:

- the implementing code
- this map
- the relevant operational doc

That keeps the repo readable in both directions: from contract to code and from
code back to contract.
