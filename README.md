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

Optional bitmap near (2-group and 3-group fragments):

```bash
-e POSTINGS_BITMAP_NEAR=1
-e POSTINGS_BITMAP_CHUNK=4096
```

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
- `POST /collocations`

### CNF term groups (OR groups)

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

## JS UI

Open `web/index.html` and set the backend base URL.

## Utilities

- `sql/test_concordance_samples.sql`: small concordance sampling SQL.
- `sql/benchmark_bitmap_near.sql`: benchmark bitmap near UDF.

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
