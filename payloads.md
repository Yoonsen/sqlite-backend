## API payloads

This file is meant as a frontend-facing payload reference.

## 1) Single term concordance (with or without wildcard)

Endpoint: `POST /concordance`

```json
{
  "wordA": "norge*",
  "wordB": "",
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

## 2) Two-term concordance / near

Endpoint: `POST /concordance`

```json
{
  "wordA": "elskov",
  "wordB": "kjærlighed",
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

```json
{
  "terms": ["elskov", "kjærlighed", "hjerte"],
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
  "maxVariants": 10
}
```

## 4) OR-groups with near between groups (CNF style)

Use this when each group contains OR terms, and near is enforced across groups.

Endpoints:
- `POST /near_query`
- `POST /near_fragments`

```json
{
  "termGroups": [["eskimoer", "eskimoerne"], ["er", "var"], ["snø", "is"]],
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
  "maxVariants": 10
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

## 7) Near frequency

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
