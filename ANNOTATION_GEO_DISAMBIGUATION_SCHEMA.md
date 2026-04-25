# Geo Disambiguation Annotation Schema

> Task-specific reference:
> This document is relevant when working on LLM-assisted geo disambiguation or
> annotation writeback flows.
> It does not replace the main geo storage/API contracts.
> Read `PLACE_ID_STRATEGY.md`, `GEO_INDEX_CONTRACT.md`, and
> `API_CONTRACT_GEO_V2.md` first for the current canonical model.

## Scope

This document defines:

1. the structured occurrence payload sent to an LLM for geo disambiguation
2. the annotation row written back after a place has been resolved

The occurrence key is always:

- `bookId`
- `seqStart`
- `len`

These three fields identify one span in one book and are the stable writeback key.

General coordinate semantics:

- `seqStart` is always the anchor position in token coordinates
- `len > 0` means a span annotation covering one or more tokens
- `len = 0` means a point annotation anchored at `seqStart`

Point annotations are layer-specific. For example:

- in `layout` or `book-codex`, `len = 0` can represent `linebreak`, `paragraphbreak`, or `pagebreak`
- in `geo`, `len = 0` will usually be invalid or unused

Context is intentionally capped at:

- `before <= 25`
- `after <= 25`

This gives up to 50 context tokens around the matched span.

## 1) LLM Input Contract

Use the structured concordance rendering from:

- `POST /concordance`
- `POST /or_query`
- `POST /near_fragments`
- `POST /near_query` with `mode = "render"`

with:

- `renderMode = "structured"`
- typically `before = 25`
- typically `after = 25`

### Input row shape

```json
{
  "bookId": 100617263,
  "seqStart": 35261,
  "len": 1,
  "before": "Schwejts eller oppe i",
  "hit": "Norge",
  "after": ". For De rejser",
  "surface": "Norge"
}
```

### Suggested batch payload to LLM

```json
{
  "task": "geo_disambiguation",
  "source": {
    "endpoint": "/or_query",
    "renderMode": "structured",
    "before": 25,
    "after": 25
  },
  "rows": [
    {
      "bookId": 100617263,
      "seqStart": 35261,
      "len": 1,
      "before": "Schwejts eller oppe i",
      "hit": "Norge",
      "after": ". For De rejser",
      "surface": "Norge"
    }
  ]
}
```

## 2) Annotation Output Contract

The writeback layer should store both:

- the occurrence identity in the text
- the resolved GeoNames identity and selected display names

### Output row shape

```json
{
  "layer": "geo",
  "source": "geonames",
  "bookId": 100617263,
  "seqStart": 35261,
  "len": 1,
  "matchedText": "Norge",
  "surface": "Norge",
  "geonamesId": 3144096,
  "canonicalName": "Kingdom of Norway",
  "canonicalNameNo": "Norge",
  "canonicalNameEn": "Norway",
  "featureClass": "A",
  "featureCode": "PCLI",
  "countryCode": "NO",
  "admin1Code": null,
  "lat": 60.472,
  "lon": 8.4689,
  "population": 5511370,
  "elevation": null,
  "timezone": "Europe/Oslo",
  "confidence": 0.94,
  "resolver": "llm-geonames",
  "modelVersion": "v1"
}
```

## 3) Cross-Layer Coordinate Model

The same coordinate model can be reused across annotation layers:

- `geo`
- `layout`
- `book-codex`
- literary interpretation layers such as tropes, motifs, rhetoric, or narrative structure

### Example: point annotation in layout

```json
{
  "layer": "layout",
  "kind": "pagebreak",
  "bookId": 100617263,
  "seqStart": 35261,
  "len": 0,
  "page": 128
}
```

### Example: span annotation in an interpretive layer

```json
{
  "layer": "literary-tropes",
  "kind": "biblical_allusion",
  "bookId": 100617263,
  "seqStart": 35261,
  "len": 6,
  "label": "biblical allusion",
  "confidence": 0.81
}
```

## 4) Overlap and Layering

Overlap should be allowed by default across different layers.

Examples:

- a geo span can overlap a literary trope span
- a person-name span can overlap a rhetorical figure span
- a layout point annotation with `len = 0` can sit at the start or end of any span annotation

Recommended rule:

- uniqueness should usually be enforced on `(layer, bookId, seqStart, len, kind, primary-id)`
- do not enforce global non-overlap across layers

For interpretive layers such as literary tropes, overlap is often necessary rather than exceptional.
One phrase may simultaneously be:

- a metaphor
- a biblical allusion
- part of a larger narrative motif

That means the system should support:

- multiple annotations with identical `(bookId, seqStart, len)` in different layers
- multiple annotations with partially overlapping spans in the same layer when the semantics differ

If a layer later needs stricter rules, those constraints should be layer-specific rather than global.

## 5) Required Fields

Required occurrence fields:

- `bookId`
- `seqStart`
- `len`

Required resolved place fields:

- `geonamesId`
- `canonicalName`
- `canonicalNameNo`
- `canonicalNameEn`
- `featureClass`
- `featureCode`
- `countryCode`
- `lat`
- `lon`

If a language-specific canonical name is unavailable, use fallback values:

- `canonicalNameNo`: preferred Norwegian canonical name, else `canonicalNameEn`, else `canonicalName`
- `canonicalNameEn`: preferred English canonical name, else `canonicalName`

## 6) Recommended Optional Fields

- `matchedText`
- `surface`
- `admin1Code`
- `population`
- `elevation`
- `timezone`
- `confidence`
- `resolver`
- `modelVersion`

## 7) Notes

- `matchedText` is the text span as it appeared in the source context.
- `surface` can be used for normalized or pipeline-stable text if needed.
- `canonicalName` is the primary canonical name from GeoNames or the selected canonical record.
- `canonicalNameNo` and `canonicalNameEn` are preferred presentation names stored directly in the annotation layer to avoid later name lookup.
- `featureClass` and `featureCode` should be preferred over custom booleans such as "river" or "populated place".
- The annotation layer should be append-safe and reproducible from `(bookId, seqStart, len) + geonamesId`.
- `len = 0` should be interpreted together with `layer` and `kind`, not as a universal meaning by itself.
- overlap should be treated as normal for many scholarly and interpretive layers, not as a data error.
