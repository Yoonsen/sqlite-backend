# Note: `geo_disambig.db` -> sidecar test run

This note summarizes the first working import path from
`/home/larsj/geotest/geo_disambig.db` into the NB geo annotation sidecar and the
derived `geo_imagination.db`.

## What was built

The following pipeline now exists in the repo:

1. `import_geo_disambig_to_annotation_nb.py`
   - imports `nb_places`
   - imports resolved rows into `geo_annotations_base`
2. `build_geo_nb_contract_v1.py`
   - materializes:
     - `geo_annotations_resolved`
     - `geo_spans`
     - `geo_mentions_v2`
     - `geo_postings_v2`
     - `geo_book_index_v2`
3. `build_geo_imagination.py`
   - builds `geo_imagination.db` from:
     - `annotation_geo_nb.db.nb_places`
     - `annotation_geo_nb.db.geo_annotations_resolved`
     - `imagination.db.corpus`

Test run output:

- `/mnt/disk4/geo_rebuild_runs/20260423T173136Z_disambig_import`

## Short result

The import path works technically, but the current mapping produces a
**thinner** sidecar than the active one.

That means:

- fewer books
- fewer positions
- fewer distinct places
- lower mention density per book

So the current test run does **not** support the hypothesis that this imported
subset gives significantly more positions per book than the active sidecar.

## Key numbers

### Active sidecar

Source:

- `/mnt/disk4/imagination_shards_roaring_main_v1/annotation_geo_nb.db`

Counts from `geo_annotations_resolved`:

- mentions: `926459`
- books: `17835`
- mentions per book: `51.95`
- distinct places: `76684`
- `(book, place)` pairs: `901876`
- `(book, place)` pairs per book: `50.57`

### Raw `geo_disambig.db`

Source:

- `/home/larsj/geotest/geo_disambig.db`

Counts from `geo_annotations`:

- total rows: `1061519`
- rows with `seq_start`: `735499`
- books with positional rows: `15183`
- positional rows per book: `48.44`

Important caveat:

- `geo_annotations.nb_place_id` is empty in the inspected source
- the first import path therefore resolves through `geonames_id -> nb_places`

### Imported sidecar test run

Source:

- `/mnt/disk4/geo_rebuild_runs/20260423T173136Z_disambig_import/annotation_geo_nb.db`

Counts from `geo_annotations_resolved`:

- mentions: `389803`
- books: `14512`
- mentions per book: `26.86`
- distinct places: `2475`
- `(book, place)` pairs: `215865`
- `(book, place)` pairs per book: `14.87`

### Imported `geo_imagination.db`

Source:

- `/mnt/disk4/geo_rebuild_runs/20260423T173136Z_disambig_import/geo_imagination.db`

Counts:

- `corpus`: `22946`
- `places`: `969711`
- `book_places`: `215865`
- books represented in `book_places`: `14512`
- mentions per represented book: `26.86`

## Same-book comparison

The cleanest comparison is on books present in both the active sidecar and the
imported test run.

Shared books:

- `14510`

Mentions on shared books:

- active sidecar: `909902`
- imported test run: `389800`

Mentions per shared book:

- active sidecar: `62.71`
- imported test run: `26.86`

Per-book comparison on the shared set:

- books where import has more mentions than active: `526`
- books where counts are equal: `635`
- books where import has fewer mentions than active: `13349`

Conclusion:

- for the current mapping, the imported result is usually **less** dense than
  the active sidecar, even on the overlapping books

## Likely reason for the loss

The current importer is deliberately conservative.

It only promotes rows that can be mapped cleanly into the NB/internal sidecar
shape used now.

Main bottlenecks:

1. `geo_annotations.nb_place_id` is not filled in the source DB.
2. The current import therefore resolves through `geonames_id -> nb_places`.
3. Only `389814` positional rows can be joined that way.
4. After deduplication by `(dhlabid, seq_start)`, this becomes `389803` rows.
5. Only `107914` imported base rows have explicit `token_len` from
   `concordances`.
6. `281889` imported rows therefore fall back to token length derived from
   `surface_text`.

This means a large part of the richer source data is not yet reaching the
current sidecar model.

## About the much higher model cost

The observation that this run cost roughly `20x` more than the earlier pass is
plausible, but the current exported sidecar does not preserve that extra cost as
extra density.

Possible explanation:

- the source place universe is much larger (`969711` `nb_places`)
- the source appears to carry richer ambiguity/disambiguation work
- SSR-heavy place linking likely increases candidate volume and model work
- but the current importer collapses only the subset that can be safely mapped
  into the present NB/internal sidecar contract

So the extra model cost may be real upstream, while the current
GeoNames-only-to-NB bridge prevents that extra work from appearing downstream in
the sidecar.

## Operational takeaway

The pipeline is now ready for iterative builds:

1. import from `geo_disambig.db`
2. build sidecar tables
3. rebuild `geo_imagination.db`

But the current import should be treated as a **baseline integration step**, not
as the final high-recall bridge.

## What probably needs to improve next

To get closer to the expected "more positions per book" result, the import path
likely needs one or more of these:

- a better internal-id bridge than `geonames_id -> nb_places` alone
- direct SSR-to-internal resolution where available
- a way to carry the richer disambiguation result into canonical `place_id`
  without dropping most rows
- better `token_len` recovery than the current concordance-or-surface fallback

## Recommendation for the build pipeline

Use this first version as a safe, deterministic import path and as a measurement
tool.

Do **not** assume that higher upstream model spend automatically means denser
sidecar output.

For the current data, the measured answer is:

- upstream source is richer and more expensive
- imported sidecar output is currently thinner than the active one
- the missing link is the mapping layer, not the ability to build the sidecar

## Addendum: current rebuild-safe basis

After the later rebuild pass against the `book_place_annotations` +
`geo_places` export shape, the practical basis for this pipeline is now:

- `book_id`
- `seq_start`
- `geonames_id`
- `token_len`

This is the current basis to preserve across sidecar and postings.

### Meaning of `seq_start` and `token_len`

- `seq_start` is the search anchor and the coordinate used for near semantics
- `token_len` is retained mainly for rendering/highlighting and span fidelity
- `token_len` should still be stored in the postings layer so the sidecar and
  postings remain derivable from the same mention basis

### Current compatibility shape

The active runtime still materializes query keys in the `nb` compatibility form.
In the current GeoNames-based rebuild path that means:

- `place_key_type = 'nb'`
- `place_key = CAST(geonames_id AS TEXT)`
- `place_id = geonames_id`

So this pass is technically compatible with the current backend, but it should
not be read as proof that we have restored a separate internal-id model.
