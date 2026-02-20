# All-Roaring Migration Plan

## Goal
- Move to one postings format for document-level postings: Roaring everywhere.
- Remove hybrid runtime complexity in the core data path.
- Keep query semantics identical (`or`, `near`, fragments, filters), while making operations easier to maintain across C and Julia.

## Decision
- We accept higher build/rebuild cost and some storage overhead on very small postings.
- We prioritize long-term maintainability and one consistent architecture over per-blob micro-optimization.

## Scope
- In scope:
  - `words.docpost` (or successor column/table) migration to Roaring.
  - C extension + Julia runtime support for Roaring-based set ops.
  - API path migration to one Roaring-first execution flow.
- Out of scope (phase 1):
  - Position postings format rewrite (`post`) for sequence/near internals.
  - Cross-shard global join engine rewrite.

## Current Baseline (2026-02-19 quick metrics)
- `imag_00_words_full.db`: 13,694 books, 942,903,382 `tokens` rows, 112,881,290 `unigrams` rows, ~31.0 GB.
- `imag_01_words_full.db`: 13,024 books, 878,064,362 `tokens` rows, 101,787,218 `unigrams` rows, ~28.8 GB.
- `docpost` size profile:
  - p99 length ~284-297 bytes.
  - max length ~6591-7440 bytes.
  - >=256 bytes is ~1.1% of rows (small fraction but query-dominant high-df terms).

## Target Data Contract
- Define one explicit codec contract for doc postings:
  - `docpost_rb` BLOB contains serialized Roaring bitmap.
  - Optional `docpost_codec` TEXT fixed to `roaring_v1` during transition.
- Phase-out legacy docpost blobs after validation.
- Keep local `cf_id`; keep/add optional `global_id` in `words` model as planned for shard federation.

## Execution Plan

## Phase 0 - Spec and Guardrails (1 day)
- Freeze format spec in repo (`ROARING_CODEC.md`).
- Define canonical semantics:
  - OR = union
  - AND/filter = intersection
  - count = popcount
  - sample = deterministic/random sample over sorted doc IDs
- Add invariants:
  - sorted IDs on decode
  - no duplicates
  - exact parity with legacy path on golden payloads

## Phase 1 - Build Tooling (1-2 days)
- Implement offline rebuild script:
  - Input: shard SQLite DB.
  - Output: migrated shard DB (new file), no in-place mutation.
  - Steps:
    - read legacy `docpost`
    - decode to integer doc IDs
    - build Roaring bitmap
    - write `docpost_rb`
- Add resumable checkpoints and progress logging (per N words).
- Add validation mode:
  - row-count parity
  - random sample row parity (`legacy set == roaring set`)
  - aggregate sanity (`sum(popcount)` range checks)

## Phase 2 - Runtime Support (2-3 days)
- C layer:
  - Add/verify Roaring UDFs for union/intersect/count/sample.
  - Ensure no fallback to legacy blobs in default runtime.
- Julia layer:
  - Add Roaring decode/ops path matching C semantics.
  - Keep near-core agnostic to origin (singleton vs OR group).
- Python API:
  - Switch doc-level filtering path to Roaring-backed ops.
  - Keep existing request payloads unchanged.

## Phase 3 - Parity and Performance Gates (1-2 days)
- Parity gate:
  - `[[x],[u]]` vs `x u` same hit sets (within expected sampling variance).
  - OR groups with missing term still return hits from remaining terms.
  - `filterIds` behavior identical across engines.
- Performance gate:
  - Must not regress p50/p95 for common payload classes.
  - Track per-phase timings (`groups_ms`, `prefilter_ms`, `near_sql_ms`, `post_ms`).
- Stability gate:
  - Long-run query soak test.
  - Memory ceiling checks under concurrent requests.

## Phase 4 - Cutover (1 day)
- Build new shard artifacts.
- Deploy with feature flag defaulting to Roaring path.
- Run smoke tests and payload matrix.
- Remove legacy runtime path after acceptance window.

## Phase 5 - Cleanup (0.5-1 day)
- Remove dead code and legacy flags.
- Update `README.md`, `payloads.md`, `LOGBOOK.md`.
- Archive migration scripts and results in `report.md` appendix.

## Shard Build Policy (updated)
- For new shard generation, use dual caps:
  - books <= 10,000
  - OR `tokens` rows <= 650M
  - OR `unigrams` rows <= 80M
- Rationale:
  - books cap controls doc-posting growth.
  - row caps control B-tree depth and token/unigram scan behavior.

## Rollout Strategy
- Blue/green per shard artifact:
  - keep current shard untouched
  - generate `*_roaring.db`
  - switch config atomically
- Rollback:
  - revert config to previous shard list
  - no data mutation rollback needed

## Risks and Mitigations
- Rebuild time too long:
  - run per shard with checkpoints; parallelize across machines if available.
- Semantic drift between C and Julia:
  - shared golden payload suite and strict parity check.
- Hidden regressions in near/fragments:
  - keep benchmark matrix in CI and pre-release smoke.

## Deliverables
- `ROARING_CODEC.md` (format and invariants)
- Rebuild script + validate mode
- Runtime support in C/Julia/Python path
- Benchmark/parity report for migrated shards
- Updated operational docs

## Suggested Next Step
- Implement phase 0 and phase 1 first on one pilot shard (epikk subset), then scale to full corpus.
