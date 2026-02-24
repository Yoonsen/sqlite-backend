## sqlite-backend

Postings-based SQLite backend for concordance and near search, with a FastAPI service and a simple JS UI.
See `DATABASE_MODEL.md` for the model and `report.md` for design notes.

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
  "ext_path": "/path/to/postings_native.so",
  "default_schema": "unigrams"
}
```

Set `words_db` to an empty string to use per-shard `words` embedded in each postings DB.

## Python backend (local)

```bash
export POSTINGS_CONFIG=/path/to/config.json
pip install -r api_python/requirements.txt
uvicorn api_python.server:app --host 0.0.0.0 --port 8000
```

## API endpoints

- `GET /health`
- `POST /concordance`
- `POST /near_frequency`
- `POST /near_query`
- `POST /near_fragments`
- `POST /or_query`
- `POST /collocations`

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

Optional per-request engine fields for `/near_query`, `/near_fragments`, `/near_hits`:

```json
{
  "matchMode": "near",
  "engine": "python",
  "parallelShards": false
}
```

- `engine`: `python` (default) or `julia` (requires `POSTINGS_JULIA_HYBRID=1`)
- `parallelShards`: used by Julia probe path (`true` enables shard tasks)
- `useFilter` + `filterIds`: supported in both engines for near endpoints
- `matchMode`: `near` (default) or `sequence` (strict phrase-like sequence). `sequence` is currently supported for `engine=python` with `schema=unigrams`.

### OR query

`/or_query` supports generic union search over terms or OR groups:

```json
{
  "termGroups": [["a","b","c"]],
  "before": 5,
  "after": 5,
  "perBook": 3,
  "docSamples": 10,
  "totalLimit": 200
}
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
