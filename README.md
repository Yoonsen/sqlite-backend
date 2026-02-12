## sqlite-backend

Utilities for building postings shards and running a Streamlit demo.

### REST backends (Python + Julia) and JS UI

Configuration is shared via `POSTINGS_CONFIG` pointing to a JSON file. Example:

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

#### Python backend

```bash
export POSTINGS_CONFIG=/path/to/config.json
pip install -r api_python/requirements.txt
uvicorn api_python.server:app --host 0.0.0.0 --port 8000
```

#### Docker (Python backend, compile postings at startup)

Build:

```bash
docker build -t postings-api .
```

Run (mount shards + config + output `.so`):

```bash
docker run --rm -p 8000:8000 \
  -e POSTINGS_CONFIG=/data/dhlab/larsj/postings/config.json \
  -e POSTINGS_SO_PATH=/data/dhlab/larsj/postings/postings_native.so \
  -v /data/dhlab/larsj/postings:/data/dhlab/larsj/postings \
  postings-api
```

#### Julia backend

```bash
export POSTINGS_CONFIG=/path/to/config.json
julia --project=api_julia -e 'using Pkg; Pkg.instantiate()'
julia --project=api_julia api_julia/server.jl
```

#### Vanilla JS UI

Open `web/index.html` in a browser and point it to the backend base URL.

#### API endpoints

- `GET /health`
- `POST /concordance`
- `POST /near_frequency`
- `POST /near_query` (multi-term + prefix *)
- `POST /near_fragments` (multi-term + prefix *, returns fragments)
- `POST /collocations`

### Streamlit demo

Run from the repo root (update paths in the sidebar if needed):

```bash
streamlit run streamlit_app.py
```

Notes:
- Use a **postings DB** that has `tokens`, `unigrams`, `urns`.
- Use the matching **words DB** (separate file).
- Provide the correct `.so` path (often `postings_native.so`).
- `.so` binaries are architecture-specific; recompile per machine.

### Build a shard

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

### Split a shard

Split by max tokens or by max books:

```bash
python split_shard.py \
  --src /data/db/imagination_postings.db \
  --dst-prefix /data/shards/imagination_part \
  --max-tokens 500000000
```

### Parallel build (no GNU parallel)

```bash
mkdir -p /data/parts /data/shards
split -l 4000 -d --additional-suffix=.csv /home/larsj/imagination_build/imagination_urns_1814_1905.csv /data/parts/imag_

ls /data/parts/imag_*.csv | xargs -P 4 -I {} bash -c '
f="$1"
base=$(basename "$f" .csv)
python /home/larsj/imagination_build/build_imagination_shard.py \
  --csv "$f" \
  --src-root /data/db/ft \
  --dst "/data/shards/${base}_postings.db" \
  --words-db "/data/shards/${base}_words.db" \
  --batch 20000 --max-tokens 500000000
' _ {}
```
