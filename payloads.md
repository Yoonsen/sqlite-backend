## API payloads

This file is meant as a frontend-facing payload reference.

## 1) Single-group concordance-style search (recommended)

Endpoint: `POST /or_query`

```json
{
  "termGroups": [["norge*"]],
  "before": 5,
  "after": 5,
  "perBook": 3,
  "docSamples": 10,
  "totalLimit": 200,
  "schema": "unigrams",
  "useFilter": false,
  "filterIds": [],
  "maxVariants": 10
}
```

## 2) Two-term near fragments (recommended)

Endpoint: `POST /near_fragments`

```json
{
  "termGroups": [["elskov"], ["kjærlighed"]],
  "window": 5,
  "before": 5,
  "after": 5,
  "perBook": 3,
  "docSamples": 10,
  "totalLimit": 200,
  "schema": "unigrams",
  "useFilter": false,
  "filterIds": [],
  "symmetric": true,
  "excludeSelf": false
}
```

## 3) Multi-term near (simple list)

Use this when each term is its own group.

Endpoints:
- `POST /near_query` for counts (`total`, `docs`)
- `POST /near_fragments` for sampled fragments
- `POST /near_hits` for sampled hits only (`bookId`, `pos`)

```json
{
  "terms": ["elskov", "kjærlighed", "hjerte"],
  "matchMode": "near",
  "window": 5,
  "before": 5,
  "after": 5,
  "perBook": 3,
  "docSamples": 10,
  "totalLimit": 200,
  "schema": "unigrams",
  "symmetric": true,
  "excludeSelf": false,
  "useFilter": false,
  "filterIds": [],
  "maxVariants": 10,
  "engine": "python",
  "parallelShards": false
}
```

## 4) OR-groups with near between groups (CNF style)

Use this when each group contains OR terms, and near is enforced across groups.

Endpoints:
- `POST /near_query`
- `POST /near_fragments`
- `POST /near_hits`

```json
{
  "termGroups": [["eskimoer", "eskimoerne"], ["er", "var"], ["snø", "is"]],
  "matchMode": "near",
  "window": 5,
  "before": 5,
  "after": 5,
  "perBook": 3,
  "docSamples": 10,
  "totalLimit": 200,
  "schema": "unigrams",
  "symmetric": true,
  "excludeSelf": false,
  "useFilter": false,
  "filterIds": [],
  "maxVariants": 10,
  "engine": "python",
  "parallelShards": false
}
```

## 5) Pure OR query (no near)

Endpoint: `POST /or_query`

```json
{
  "termGroups": [["elskov", "kjærlighed", "forelskelse"]],
  "before": 5,
  "after": 5,
  "perBook": 3,
  "docSamples": 10,
  "totalLimit": 200,
  "schema": "unigrams",
  "useFilter": false,
  "filterIds": [],
  "maxVariants": 10
}
```

## 6) Collocations

Endpoint: `POST /collocations`

```json
{
  "word": "kjærlighed",
  "before": 5,
  "after": 5,
  "perBook": 3,
  "docSamples": 10,
  "schema": "unigrams",
  "useFilter": false,
  "filterIds": []
}
```

## 7) Near frequency (legacy shape)

Endpoint: `POST /near_frequency`

```json
{
  "wordA": "elskov",
  "wordB": "kjærlighed",
  "window": 5,
  "schema": "unigrams",
  "symmetric": true,
  "excludeSelf": false,
  "useFilter": false,
  "filterIds": [],
  "docSamples": 10
}
```

## Notes

- `docSamples` controls document-level sampling where applicable.
- `maxVariants` controls wildcard expansion for terms ending with `*`.
- `filterIds` is optional corpus filtering (book IDs).
- `symmetric=false` means forward-only matching.
- For near endpoints, `termGroups` is now sufficient by itself; `terms` is optional.
- For near endpoints, `matchMode` can be:
  - `near` (default): window-based proximity
  - `sequence`: strict phrase-style sequence (`group1@p`, `group2@p+1`, ...)
- For near endpoints, `engine` can be `python` (default) or `julia` (requires server env `POSTINGS_JULIA_HYBRID=1`).
- For Julia engine, `parallelShards` controls shard tasking (`true`/`false`).
- `wordA`/`wordB` + `/concordance` are legacy compatibility fields/endpoints. New clients should use `termGroups`/`terms` with `/or_query` or `/near_*`.
