# Geo Rebuild Runbook (ImagiNation + Geo Sidecar)

> Current operational guide:
> This document is the practical source of truth for geo rebuild execution,
> validation, and promotion flow.
> Read it together with `GEO_INDEX_CONTRACT.md` when changing rebuild scripts or
> promoting new geo DB outputs.

This runbook is the practical execution guide for rebuilding geo data before coordinate updates.

It follows `GEO_INDEX_CONTRACT.md` and keeps these layers distinct:

- `imagination.db` for PWA corpus/place APIs
- geo annotation sidecar (`annotation_geo.db`) for `#geo` namespace/fulltext alignment

Where the current build uses the NB-oriented sidecar naming
(`annotation_geo_nb.db`), treat that as the deployed file name for a sidecar
whose canonical place identity is still internal `place_id`.

For a concrete source-to-v2 conversion note, see
`GEO_DISAMBIG_TO_V2_MAPPING.md`.

## 0) Paths and naming

Recommended base on this host:

- run root: `/mnt/disk4/geo_rebuild_runs`
- shard/annotation root: `/mnt/disk4/imagination_shards_roaring_main_v1`

Expected files:

- `annotation_geo.db`
- `annotation_registry.db`
- `imagination.db` (if available on this machine)

## 1) Start in resilient session (screen)

Always run long jobs inside `screen`:

```bash
screen -ls
screen -S geo-rebuild-<stamp>
```

or detached:

```bash
screen -dmS geo-rebuild-<stamp> bash -lc '<command>'
```

Reattach:

```bash
screen -r geo-rebuild-<stamp>
```

## 2) Phase 1 (safe bootstrap)

Phase 1 does:

1. Create run directory with logs.
2. Copy `annotation_geo.db` to a working DB (no writes to production db).
3. Optionally import/update place catalog from `imagination.db` (`places` table).
   - exclude `spurious=1`
   - exclude rows with missing coordinates or `lat/lon = 0,0`
4. Rebuild `geo_postings_all` from `geo_spans`.
5. Emit summary metrics.

Use helper script:

```bash
./run_geo_rebuild_phase1.sh \
  --run-dir /mnt/disk4/geo_rebuild_runs/<stamp> \
  --annotation-db /mnt/disk4/imagination_shards_roaring_main_v1/annotation_geo.db \
  --imagination-db /mnt/disk4/imagination_shards_roaring_main_v1/imagination.db
```

If `imagination.db` is not present, script continues and logs that import step was skipped.

## 3) Phase 2 (full remap across all books)

Target state for full run:

- iterate all books/tokens in fulltext
- resolve geo mentions to canonical internal `place_id`
- keep GeoNames / SSR as external links onto that identity
- write/refresh:
  - `geo_mentions_v2`
  - `geo_postings_v2`
  - `geo_book_index_v2` (if enabled)
  - optional rebuild sources such as `geo_spans` / `geo_annotations_resolved`

This phase should run in detached `screen` with explicit checkpoints and per-book batching.

Backfill quality defaults:

- apply `geo_blocklist.txt` in `run_geo_backfill_batches.sh`
- require capitalized first token
- require capitalized `place_variants.variant_text` (lexicon-side guard)
- exclude places with missing coordinates / `lat/lon = 0,0`
- exclude `spurious=1` when `places.spurious` exists
- replace existing per-book `geo_spans` rows during backfill (clean rebuild behavior)
- when importing from a disambiguation DB, resolve external ids to canonical
  `place_id` before writing v2 mention rows

## 4) Validation checklist

After each phase:

- row counts:
  - `geo_spans`
  - `geo_mentions_v2`
  - `geo_postings_v2`
  - `geo_book_index_v2`
  - `places`
  - `place_variants`
- smoke query:
  - `SELECT COUNT(*) FROM geo_postings_v2;`
  - `SELECT COUNT(DISTINCT book_id) FROM geo_spans;`
- optional API smoke:
  - `/api/places`
  - `/api/places/details`

Use:

```bash
python validate_geo_index.py --db <db-path> --table-suffix _v2 --sample 200
python geo_rebuild_metrics.py --run-root /mnt/disk4/geo_rebuild_runs --run-dir /mnt/disk4/geo_rebuild_runs/<stamp>
```

This writes:

- `validation_v2.json` (validator report)
- `metrics.json` (short count report + diff vs previous run when available)

## 5) Go/No-Go gates

Go when:

- no script errors in log
- counts are in expected range
- API smoke returns non-empty payload for known sample

No-Go when:

- `geo_spans` unexpectedly drops
- postings cardinalities mismatch mention counts
- large unresolved place key share
- external-id mapping leaves a large unresolved GeoNames / SSR residue

## 6) Rollout notes

- Keep old db files until new run is validated.
- Register new sidecar/db path only after validation.
- Keep run artifacts and logs under `/mnt/disk4/geo_rebuild_runs/<stamp>/`.

