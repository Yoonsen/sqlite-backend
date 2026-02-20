## GEO Standoff Plan (Shard-local)

Goal: run a practical pilot in one week on a subcorpus (epikk, ~1900 books), with full round-trip:
- postings search -> concordance with standoff
- LLM + GeoNames disambiguation
- writeback of tags
- rebuild geo postings

This plan is shard-local by design (no global IDs required for v1).

## 1) Core Model

Use two layers:

- **Search layer (fast):**
  - postings for all places in a book
  - postings per place id in a book

- **Standoff layer (precise):**
  - mention rows with `seq_start`, `seq_end`, `surface`, `p_id`
  - optional enrichment fields (`geoname_id`, confidence, provenance)

This keeps near/filters fast while preserving exact spans for export and LLM workflows.

## 2) Suggested Tables (v1)

```sql
-- Canonical place catalog (shard-local)
CREATE TABLE IF NOT EXISTS geo_places (
  p_id            INTEGER PRIMARY KEY,
  canonical       TEXT NOT NULL,
  geoname_id      INTEGER,
  lat             REAL,
  lon             REAL
);

-- Variant spellings/surfaces mapped to canonical p_id
CREATE TABLE IF NOT EXISTS geo_place_variants (
  p_id            INTEGER NOT NULL,
  variant         TEXT NOT NULL,
  UNIQUE(p_id, variant)
);

-- Precise standoff mentions in token coordinates
CREATE TABLE IF NOT EXISTS geo_mentions (
  book_id         INTEGER NOT NULL,
  seq_start       INTEGER NOT NULL,
  seq_end         INTEGER NOT NULL,
  p_id            INTEGER,
  surface         TEXT NOT NULL,
  geoname_id      INTEGER,
  confidence      REAL,
  source          TEXT,      -- llm|geonames|manual|rules
  model_version   TEXT,
  review_state    TEXT,      -- pending|accepted|rejected
  updated_at      TEXT,
  PRIMARY KEY (book_id, seq_start, seq_end, surface)
);

CREATE INDEX IF NOT EXISTS idx_geo_mentions_book ON geo_mentions(book_id);
CREATE INDEX IF NOT EXISTS idx_geo_mentions_pid ON geo_mentions(p_id);
CREATE INDEX IF NOT EXISTS idx_geo_mentions_geoname ON geo_mentions(geoname_id);

-- Fast postings by canonical place
CREATE TABLE IF NOT EXISTS geo_postings_by_place (
  book_id         INTEGER NOT NULL,
  p_id            INTEGER NOT NULL,
  tf              INTEGER NOT NULL,
  post            BLOB NOT NULL,  -- delta varint starts (seq_start)
  PRIMARY KEY (book_id, p_id)
);

CREATE INDEX IF NOT EXISTS idx_geo_postings_pid ON geo_postings_by_place(p_id);

-- Fast postings for any place mention in a book (#place aggregate)
CREATE TABLE IF NOT EXISTS geo_postings_all (
  book_id         INTEGER PRIMARY KEY,
  tf              INTEGER NOT NULL,
  post            BLOB NOT NULL
);
```

Notes:
- `seq_start/seq_end` solves multi-token mentions (`New York` vs `New York City`).
- `p_id` groups historical/orthographic variants (`Christiania`/`Kristiania`/`Oslo`).
- `geo_postings_all` is the `#place` equivalent for fast broad near queries.

## 3) Build/Refresh Flow

For one shard:

1. Build or update `geo_mentions` from standoff source (TEI or existing annotator output).
2. Normalize mentions to `p_id` where possible (rules + lookup).
3. Build postings:
   - group by `(book_id, p_id)` -> encode `seq_start` list to `post`
   - group by `book_id` (all mentions) -> encode aggregate `post`
4. Save to `geo_postings_by_place` + `geo_postings_all`.

Incremental update option:
- Rebuild only affected `book_id`s after enrichment writeback.

## 4) Query Patterns

### A) Places near a word (any place)

- Word postings (from `unigrams`) near `geo_postings_all.post`.
- Result: candidate positions and books quickly.
- Then join on `geo_mentions` for exact span and metadata.

### B) Specific place near a word

- Resolve input to `p_id`.
- Use `geo_postings_by_place.post` for that `p_id`.
- Near with word postings.

### C) Top places near a theme word

- Query A across corpus slice.
- Aggregate by `p_id` / `geoname_id`.

## 5) Concordance with Standoff

Return rows like:

```json
{
  "bookId": 100617608,
  "pos": 63,
  "frag": "... [Hamar] ...",
  "standoff": {
    "seqStart": 63,
    "seqEnd": 63,
    "surface": "Hamar",
    "pId": 1234,
    "geonameId": 3154084,
    "confidence": 0.93,
    "source": "llm",
    "reviewState": "pending"
  }
}
```

## 6) LLM + GeoNames Round-trip

Pipeline:

1. Export candidate mentions/concordances from `geo_mentions` (or unresolved rows).
2. Send compact batch to LLM API with:
   - local context fragment
   - surface form
   - candidate GeoNames records (if available)
3. Receive predicted `geoname_id`, normalized name, confidence.
4. Write back:
   - `geoname_id`, `confidence`, `source='llm'`, `model_version`, `updated_at`
5. Optional reviewer pass:
   - set `review_state` and optionally override.
6. Rebuild affected postings rows if `p_id` changed.

Hard requirements for traceability:
- keep `source`, `model_version`, `updated_at`, `review_state`
- never overwrite manual decisions silently

## 7) TEI Standoff Interop

The `geo_mentions` table is intentionally linearizable:
- `book_id` + `seq_start/seq_end` + `surface` + IDs can map to TEI standoff spans.
- TEI import/export can be lossless if tokenization offsets are stable.

Round-trip rule:
- Token coordinates are canonical internal anchors.
- TEI export/import converts between TEI offsets and token coordinates.

## 8) Pilot Plan (Epikk, ~1900 books)

### Scope for week-1 pilot
- One shard (or one dedicated epikk shard slice)
- One annotation type: place mentions only
- Near query: places near target word(s) (example: `sygdom`)

### Day-by-day minimum
- Day 1: schema + load mentions + build postings
- Day 2: query functions (`#place` + `p_id`) + concordance with standoff payload
- Day 3: LLM batch export + writeback script (CSV/JSONL based)
- Day 4: GeoNames alignment + confidence/provenance fields
- Day 5: validation + latency check + sample manual review

### Success criteria
- Query latency acceptable on epikk subset
- At least one stable round-trip (extract -> LLM enrich -> writeback -> re-query)
- No loss of mention span fidelity (`seq_start/seq_end`)

## 9) Risks and Mitigations

- Tokenization drift between TEI and tokens:
  - lock tokenizer version and keep conversion logs.
- Ambiguous place names:
  - require confidence threshold + manual queue.
- Overwriting previous manual tags:
  - preserve provenance and review state; never blind overwrite.

## 10) Next Step After Pilot

- Add shard federation strategy later (global mapping layer) only if needed.
- Keep search and annotation data shard-local for now to reduce complexity.
