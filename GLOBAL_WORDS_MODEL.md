## Global words model (`global_words_ingest_v1`)

This file defines an incremental "shard pot" catalog for cross-shard vocabulary alignment.

### Goal

Maintain one global words DB while shards are added over time. For each incoming shard:
- Look up each `word` in global catalog.
- If found: reuse global IDs.
- If missing: append to global catalog.
- Write resulting IDs back to shard `words`.

Important: global mapping is based on `word` (not shard-local `cf_id`).

### Bootstrap policy

Bootstrap with shard 0 (or any first shard): this effectively copies shard vocabulary into the
global catalog. Subsequent shards do upsert/merge.

### Output schema

`global_cf_lexicon`:
- `global_cf_id` (PK)
- `cf_word` (unique, currently `lower(word)`)

`global_raw_lexicon`:
- `global_raw_id` (PK)
- `word` (unique)
- `global_cf_id` (FK -> `global_cf_lexicon`)

Shard-local `words` updates:
- `global_id` (set equal to `global_cf_id` for backward compatibility)
- `global_cf_id`
- `global_raw_id`

`shard_ingest_log`:
- `shard_id` (PK)
- `shard_path`
- `ingested_at`
- `rows_total`
- `rows_with_global`

`meta`:
- model and build parameters

### Build command

```bash
# 1) Bootstrap from shard 0
python ingest_shard_global_words.py \
  --global-db /mnt/disk4/imagination_shards_roaring_main_v1/global_words_v1.db \
  --shard-db /mnt/disk4/imagination_shards_roaring_main_v1/imag_roaring_main_0.db \
  --shard-id imag_roaring_main_0

# 2) Ingest next shards
python ingest_shard_global_words.py \
  --global-db /mnt/disk4/imagination_shards_roaring_main_v1/global_words_v1.db \
  --shard-db /mnt/disk4/imagination_shards_roaring_main_v1/imag_roaring_main_1.db \
  --shard-id imag_roaring_main_1

python ingest_shard_global_words.py \
  --global-db /mnt/disk4/imagination_shards_roaring_main_v1/global_words_v1.db \
  --shard-db /mnt/disk4/imagination_shards_roaring_main_v1/imag_roaring_main_2.db \
  --shard-id imag_roaring_main_2
```

### Note about sidecar

The global catalog points to shard `main` DBs only. Sidecar (`token_blocks`) mapping stays implicit
through shard identity and can be resolved by runtime naming convention.
