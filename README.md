## sqlite-backend

Postings-based SQLite backend for concordance and near search, with a FastAPI service and a simple JS UI.
See `DATABASE_MODEL.md` for the model and `report.md` for design notes.
For the current local-to-server workflow, see `ARBEIDSFLYT_LOCAL_TO_SERVER.md`.
For internal AI usage and deploy guardrails, see `AI_FIREWALL_WORKFLOW_POLICY.md`.
For the long-term token/corpus/shard contract behind query planning and
annotation compatibility, see `QUERY_CORPUS_MODEL.md`.
For the operational shard admission and consistency checklist, see
`SHARD_VALIDATION_CHECKLIST.md`.
For current geo identity, v2 sidecar tables, and migration notes, see:

- `PLACE_ID_STRATEGY.md`
- `GEO_INDEX_CONTRACT.md`
- `API_CONTRACT_GEO_V2.md`
- `GEO_DISAMBIG_TO_V2_MAPPING.md`
- `GEO_IMAGINATION_DB.md`

## Quick start (Docker)

Build and push:

```bash
./docker_publish.sh
```

Run (mount shards + config + output `.so`):

```bash
docker run --rm -p 8000:8000 \
  -e POSTINGS_CONFIG=/data/dhlab/larsj/postings/config.json \
  -e POSTINGS_SO_PATH=/data/dhlab/larsj/postings/postings_native.so \
  -v /data/dhlab/larsj/postings:/data/dhlab/larsj/postings \
  harbor.nb.no/sprakbanken/postings-api:latest
```

Switch API runtime inside the same image:

```bash
# Default
-e POSTINGS_API_MODE=python

# Optional Julia HTTP API (api_julia/server.jl)
-e POSTINGS_API_MODE=julia
```

Hybrid mode (one Python API, per-request engine switch):

```bash
# Keep Python API as main server
-e POSTINGS_API_MODE=python

# Enable Julia engine from Python endpoints (engine="julia")
-e POSTINGS_JULIA_HYBRID=1

# Optional override (default side Julia port is auto-set to 8001 when hybrid=1)
-e POSTINGS_JULIA_SIDE_PORT=8001
```

In hybrid mode, Python keeps serving on `:8000`, and forwards `engine="julia"` near requests
to a persistent Julia side service (`http://127.0.0.1:8001`) to avoid per-request Julia startup.

Julia runtime paths bundled in the image:

```bash
/usr/local/bin/julia
/app/api_julia/server.jl
/app/api_julia/sqlite_blob_julia_probe.jl
/app/julia-run.sh
```

Run the Julia probe manually in the container:

```bash
docker exec -it <container> /app/julia-run.sh /app/api_julia/payload_probe.json
```

Optional bitmap near (generalized near path, `len(groups) >= 2`):

```bash
-e POSTINGS_BITMAP_NEAR=1
-e POSTINGS_BITMAP_CHUNK=4096
```

With bitmap enabled and the matching extension loaded, both `/near_query` and `/near_fragments`
use bitmap-based near-position blobs for all multi-group queries (anchor group against all other groups),
then intersect the resulting blobs to enforce full-group near constraints.

## Configuration

`POSTINGS_CONFIG` points to a JSON file:

```json
{
  "postings_dbs": [
    "/mnt/disk4/imagination_shards/imag_00_postings.db",
    "/mnt/disk4/imagination_shards/imag_01_postings.db"
  ],
  "words_db": "",
  "imagination_db": "",
  "annotation_registry_db": "/mnt/disk4/annotations/annotation_registry.db",
  "annotation_base_dir": "/mnt/disk4/annotations",
  "ext_path": "/path/to/postings_native.so",
  "default_schema": "unigrams"
}
```

Set `words_db` to an empty string to use per-shard `words` embedded in each postings DB.
Set `imagination_db` to enable ImagiNation corpus/place endpoints (`/api/*`).
Set `annotation_registry_db` to enable `#namespace` resolution (for now: `#geo`).
Set `annotation_base_dir` to resolve relative namespace `db_path` values.

## Python backend (local)

```bash
# Use one fixed Python runtime for all local starts.
# Example runtime on this host:
REPO_PYTHON=/home/larsj/miniconda3/bin/python \
POSTINGS_CONFIG=/path/to/config.json \
API_HOST=0.0.0.0 \
API_PORT=8000 \
./run_api.sh
```

`run_api.sh` enforces one runtime and checks `uvicorn` + `pyroaring` in that runtime before startup.

## API endpoints

- `GET /health`
- `GET /api/metadata/all`
- `POST /api/place/resolve`
- `POST /api/places`
- `POST /api/places/details`
- `POST /concordance`
- `POST /near_frequency`
- `POST /near_query`
- `POST /near_fragments`
- `POST /or_query`
- `POST /collocations`

### Place resolver

Use this for place identity lookup before geo search. It is a pure resolver against the
place catalog in `imagination.db`, not a corpus-count endpoint and not an annotation lookup.

Query by form:

```json
{
  "query": "rio de janeiro",
  "limit": 5
}
```

Query by id:

```json
{
  "id": "<place-id>",
  "limit": 5
}
```

Response shape:

```json
{
  "matches": [
    {
      "id": "4473178",
      "canonicalName": "Rio de Janeiro",
      "matchedForm": "Rio de Janeiro",
      "alternateForms": ["Rio", "Janeiro"],
      "lat": -22.9,
      "lon": -43.2,
      "country": "Brazil",
      "matchType": "exact"
    }
  ]
}
```

Use the returned `id` as the stable internal place id in frontend code.

Current compatibility note:

- new code should treat this as canonical internal identity
- current runtime may still accept bare `#geo:<id>` and materialize that through
  the deployed `nb` query key

### CNF term groups (OR groups, recommended)

For multi-term near, you can send `termGroups` (CNF-style):

```json
{
  "termGroups": [["spise","spiser"], ["middag"]],
  "window": 5,
  "before": 5,
  "after": 5,
  "perBook": 2,
  "totalLimit": 100
}
```

If `termGroups` is omitted, the API uses `terms` as single-item groups.

For new clients, prefer group-based payloads (`termGroups` or `terms`) with:
- `POST /or_query` for union/single-group concordance-style search
- `POST /near_query`, `POST /near_fragments`, `POST /near_hits` for near semantics

Optional per-request fields for `/near_query`, `/near_fragments`, `/near_hits`, `/or_query`:

```json
{
  "matchMode": "near",
  "engine": "python",
  "parallelShards": false
}
```

- `engine`: `python` (default) or `julia` (requires `POSTINGS_JULIA_HYBRID=1`)
- `parallelShards`: enables shard-parallel execution in Python near/or paths and Julia probe path
- `useFilter` + `filterIds`: supported in both engines for near endpoints
- `matchMode`: `near` (default) or `sequence` (strict phrase-like sequence). `sequence` is currently supported for `engine=python` with `schema=unigrams`.
- `_perf`: set `POSTINGS_PROFILE_NEAR=1` to include runtime diagnostics (workers, shard task info) in responses.

### OR query

`/or_query` supports generic union search over terms or OR groups:

```json
{
  "termGroups": [["a","b","c"]],
  "before": 5,
  "after": 5,
  "perBook": 3,
  "docSamples": 10,
  "totalLimit": 200,
  "parallelShards": true
}
```

Notes:
- Exact term lookup is case-robust (for example, searching `øysterdalen` matches indexed `Øysterdalen`).
- With `POSTINGS_PROFILE_NEAR=1`, `/or_query` also returns `_perf` metadata.

Annotation namespace mode for `/or_query` (v1):

```json
{
  "terms": ["#geo"],
  "docSamples": 200,
  "totalLimit": 500,
  "useFilter": true,
  "filterIds": [100617608, 100617609]
}
```

In decoupled geo mode, namespace queries are geo-only and do not call fulltext internals.
That means `#geo` and `#geo:<value>` are supported, while mixed namespace+plain-term
queries (for example `#geo krig`) should be run as two separate API calls
(`POST /or_query` for geo + `POST /near_query` or `POST /near_fragments` for fulltext near).

Identity note for `#geo:<value>`:

- target architecture term: internal `place_id`
- current runtime compatibility may still interpret bare numeric ids as `nb`
- explicit `#geo:internal:<id>` should be preferred in new integrations

When query contains only `#geo`, backend resolves namespace via `annotation_registry_db`
and returns rows anchored in shared coordinates: `bookId`, `seqStart`, `tokenLen`.
If available in `places`, each row also includes geolocation under `place`
(`canonicalName`, `geonamesId`, `lat`, `lon`, `country`, `variantText`).

Bootstrap helper SQL lives in:
- `sql/annotation_registry.sql`
- `sql/annotation_geo.sql`
- `sql/annotation_geo_nb.sql`

Example namespace registration:

```sql
INSERT INTO annotation_namespaces(namespace, db_path, version, resolver, active)
VALUES ('geo', '/mnt/disk4/annotations/annotation_geo.db', 'v1', 'geo_resolver', 1);
```

If you want one registry shared across environments, store relative `db_path`:

```sql
INSERT INTO annotation_namespaces(namespace, db_path, version, resolver, active)
VALUES ('geo', 'annotation_geo.db', 'v1', 'geo_resolver', 1);
```

Then only change `annotation_base_dir` per environment.

Import geolocation into `annotation_geo.db` from existing token/source DB:

```bash
python import_geo_places_from_tokens_db.py \
  --annotation-db /mnt/disk4/annotations/annotation_geo.db \
  --source-db /path/to/current_geo_source.db \
  --source-table geo_tokens \
  --token-col token \
  --canonical-col canonical_name \
  --geonames-col geonames_id \
  --lat-col lat \
  --lon-col lon \
  --country-col country \
  --link-spans
```

### Legacy note

`POST /concordance` with `wordA`/`wordB` is retained for compatibility, but new clients should use the group-based endpoints above.

## JS UI

Open `web/index.html` and set the backend base URL.

## Utilities

- `sql/test_concordance_samples.sql`: small concordance sampling SQL.
- `sql/benchmark_bitmap_near.sql`: benchmark bitmap near UDF.
- `benchmark_api.py`: fixed API benchmark matrix runner.
- `benchmark_payloads.json`: standard benchmark payload set.
- `benchmark_payloads_light.json`: fast daily sanity profile.
- `benchmark_payloads_heavy.json`: pre-release stress profile.

Run fixed API benchmarks:

```bash
python benchmark_api.py --base-url http://127.0.0.1:8000 --repeats 3
```

Run light profile:

```bash
python benchmark_api.py --base-url http://127.0.0.1:8000 --payloads benchmark_payloads_light.json --repeats 2
```

Run heavy profile:

```bash
python benchmark_api.py --base-url http://127.0.0.1:8000 --payloads benchmark_payloads_heavy.json --repeats 3
```

## Build a shard

```bash
python build_imagination_shard.py \
  --csv /path/imagination_urns_1814_1905.csv \
  --src-root /data/db/ft \
  --dst /data/db/imagination_postings.db \
  --words-db /data/db/imagination_words.db \
  --batch 20000
```

Optional cap on total tokens:

```bash
python build_imagination_shard.py \
  --csv /path/imagination_urns_1814_1905.csv \
  --src-root /data/db/ft \
  --dst /data/db/imagination_postings_part1.db \
  --words-db /data/db/imagination_words.db \
  --batch 20000 --max-tokens 500000000
```

## Split a shard

```bash
python split_shard.py \
  --src /data/db/imagination_postings.db \
  --dst-prefix /data/shards/imagination_part \
  --max-tokens 500000000
```
