## sqlite-backend

Utilities for building postings shards and running a Streamlit demo.

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
