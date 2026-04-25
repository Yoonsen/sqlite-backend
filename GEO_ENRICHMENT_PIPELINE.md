# Geo Enrichment Pipeline

> Current operational guide:
> This document describes the active pre-sidecar enrichment flow for geo
> annotations: candidate generation, KWIC context extraction, gazetteer
> enrichment, LLM disambiguation, and promotion into backend-facing geo tables.
> Read it together with `PLACE_ID_STRATEGY.md`, `GEO_INDEX_CONTRACT.md`, and
> `GEO_REBUILD_RUNBOOK.md`.

## Purpose

Describe the current upstream pipeline that produces enriched geo annotations
before they are imported into the backend sidecar and map aggregate databases.

This document exists so a new contributor or agent can understand:

- where geo candidates come from
- how disambiguating context is produced
- how GeoNames and SSR are used
- where the LLM fits into the flow
- what the output of the enrichment stage must look like before rebuild/import

## Scope

This document covers the pipeline up to the point where enriched results are
ready to be imported into backend geo storage.

It includes:

- candidate generation from fulltext
- KWIC extraction for disambiguation
- lookup/enrichment against external gazetteers
- LLM-assisted disambiguation
- output expectations for downstream import/materialization

It does not define:

- the final backend API contract
- the final geo sidecar schema in detail
- frontend behavior

Those are defined by:

- `API_CONTRACT_GEO_V2.md`
- `GEO_INDEX_CONTRACT.md`
- `GEO_IMAGINATION_DB.md`

## Invariants

The following must remain true across implementations:

1. Geo enrichment begins from the corpus fulltext, not from pre-rendered snippets.
2. Disambiguation context must be anchored in the same token/text stream as the
   later annotation output.
3. Candidate generation and KWIC extraction currently read directly from the
   fulltext files under `/mnt/disk4`.
4. External gazetteers enrich the candidates, but do not by themselves define
   the final backend contract.
5. LLM output is a resolver step, not the long-term storage contract.
6. The stable mention identity through enrichment is the textual occurrence:
   - `book_id` / `dhlabid`
   - `seq_start`
   - `token_len`
7. A resolved place decision must remain traceable to the underlying occurrence
   and its disambiguating context.

## Inputs

### 1) Corpus fulltext

Primary source:

- fulltext files on `/mnt/disk4`

These files are used to produce:

- place candidates
- KWIC/disambiguating context

### 2) External gazetteers

Current sources:

- `GeoNames`
- `SSR` (Sentralt stedsregister) for Norwegian place names

Practical role:

- `GeoNames` provides the current global place identity used by the enrichment
  pipeline
- `SSR` is attached where a plausible mapping to `GeoNames` can be established
- both sources may contribute names, coordinates, feature information, and
  disambiguating metadata

### 3) LLM disambiguation input

The LLM receives structured KWIC-style context derived from the candidate
occurrence plus gazetteer candidates/metadata.

Current model family in use:

- `gpt-5.4.nano`

Treat the chosen model as an implementation detail of the resolver step, not as
the long-term contract.

## Procedure

### 1) Candidate generation

Potential place mentions are detected directly from corpus fulltext.

Outputs of this phase typically include:

- `book_id` / `dhlabid`
- candidate surface form
- occurrence position
- candidate span length

### 2) KWIC extraction

For each candidate occurrence, the pipeline extracts disambiguating context from
the same fulltext source.

Typical output shape:

- left context
- matched surface
- right context
- stable occurrence anchor

The point of KWIC here is not display, but resolver-quality evidence.

### 3) Gazetteer enrichment

Candidate occurrences are enriched with data from `GeoNames` and `SSR`.

Typical enrichment fields:

- gazetteer ids
- canonical names
- alternate names
- feature class / feature code
- country / admin metadata
- coordinates

Important rule:

- enrichment should preserve ambiguity when the source evidence is still
  ambiguous
- do not collapse multiple plausible candidates too early

### 4) LLM disambiguation

The enriched candidate plus KWIC context is sent to the LLM resolver.

The LLM is expected to:

- rank or choose the most plausible place candidate
- preserve uncertainty where disambiguation is weak
- return a result that is still anchored to the original occurrence

### 5) Output normalization

The resolved output is normalized into a form that can be imported into the geo
annotation pipeline.

This normalized output should be sufficient to later materialize:

- positional truth rows
- query-layer mentions
- postings
- map aggregates

## Outputs

The enrichment stage should emit rows that preserve both occurrence identity and
resolved place identity.

Minimum practical occurrence basis:

- `book_id` / `dhlabid`
- `seq_start`
- `token_len`
- `surface_text`

Minimum practical resolved place basis today:

- `geonames_id`
- optional `ssr_id`
- canonical/display name fields
- coordinates when available
- confidence / resolver metadata

Recommended normalized output fields:

- occurrence identity:
  - `book_id`
  - `seq_start`
  - `token_len`
  - `surface_text`
- resolution:
  - `geonames_id`
  - `ssr_id` (nullable)
  - `canonical_name`
  - `feature_class`
  - `feature_code`
  - `country_code`
  - `lat`
  - `lon`
- provenance:
  - `resolver`
  - `model_version`
  - `confidence`
  - optional source evidence / candidate trace

## Current Identity Rules

### Current practical rule

As of now, `geonames_id` functions as the effective global place identity in the
enrichment pipeline.

That means:

- resolved places are currently keyed primarily by `geonames_id`
- `SSR` is attached where it can be matched onto that identity
- the current import/rebuild path may collapse backend `place_id` onto
  `geonames_id` in practice

This is the current operational reality, not the long-term architecture goal.

### Future direction

A new internal place id system is planned later, intended to sit above:

- `GeoNames`
- `SSR`
- other future gazetteers

When that arrives:

- `geonames_id` should become an external id link rather than the effective
  global identity
- `SSR` should also map through the same external-id layer
- new gazetteers should be attachable without changing query/storage semantics

Until that migration happens:

- treat `geonames_id` as the current global enrichment id
- treat `SSR` as a linked external source where mapping is available
- keep docs explicit about the difference between current operational identity
  and future canonical internal identity

## Runtime Compatibility

The enrichment pipeline and the backend sidecar are not identical layers.

Current practical compatibility rules:

- enrichment output may still flow into NB-oriented sidecar names such as
  `annotation_geo_nb.db`
- runtime query layers may still expose or accept `nb`-style compatibility keys
- this compatibility should not be mistaken for the long-term place identity
  model

See also:

- `PLACE_ID_STRATEGY.md`
- `GEO_DISAMBIG_TO_V2_MAPPING.md`

## Failure Modes

Common risks in this pipeline:

- candidate extraction misses true place mentions in fulltext
- KWIC is too weak or too short to disambiguate reliably
- multiple gazetteer candidates remain plausible
- `SSR` names cannot be confidently linked to `GeoNames`
- the LLM over-commits where the evidence is ambiguous
- output rows lose the original occurrence anchor
- enrichment rows contain ids but not enough provenance to audit later

## Validation

Before importing a new enrichment output, check:

1. occurrence anchors are present:
   - `book_id` / `dhlabid`
   - `seq_start`
   - `token_len`
2. resolved rows preserve current place identity:
   - `geonames_id` present when a place is considered resolved
3. optional `SSR` links do not overwrite or fragment the effective place id
4. coordinates and feature metadata are consistent with the chosen place
5. a sample of ambiguous names has traceable KWIC/context evidence
6. downstream import can rebuild sidecar/query tables without dropping the
   mention basis

After import/materialization, validate via:

- `GEO_REBUILD_RUNBOOK.md`
- `validate_geo_index.py`
- smoke checks on known places such as `Norge` and `Bergen`

## Implemented by

The enrichment pipeline currently spans both this repository and upstream
processes outside it.

Repo touchpoints:

- `ANNOTATION_GEO_DISAMBIGUATION_SCHEMA.md`
  - schema guidance for LLM input/output rows
- `import_geo_disambig_to_annotation_nb.py`
  - import path from enriched disambiguation output into the sidecar
- `build_geo_nb_contract_v1.py`
  - materializes current deployed geo query tables
- `build_geo_contract_v2.py`
  - materializes v2 geo mention/posting/book-index tables
- `build_geo_imagination.py`
  - derives the map aggregate database
- `sync_geo_annotation_book_map.py`
  - syncs registry coverage after rebuild

External implementation note:

- candidate generation, direct fulltext reading, KWIC extraction, and at least
  part of the disambiguation loop may run outside this repository

## Related docs

- `PLACE_ID_STRATEGY.md`
- `GEO_INDEX_CONTRACT.md`
- `GEO_REBUILD_RUNBOOK.md`
- `GEO_DISAMBIG_TO_V2_MAPPING.md`
- `GEO_IMAGINATION_DB.md`
- `ANNOTATION_GEO_DISAMBIGUATION_SCHEMA.md`

## Historical notes

This document describes the current enrichment reality more explicitly than the
older rebuild/mapping notes.

Older related context:

- `GEO_DISAMBIG_BUILD_NOTE.md`
- `GEO_STANDOFF_PLAN.md`
