#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/mnt/disk1/Github/sqlite-backend"
OUT_DIR="/mnt/disk4/imagination_shards_roaring_blk128_v1"
PREFIX="imag_roaring_blk128"
BLOCK_SIZE=128
COMMIT_EVERY=5000

SOURCES=(
  "/mnt/disk4/imagination_shards_roaring_v1/imag_roaring_00.db"
  "/mnt/disk4/imagination_shards_roaring_v1/imag_roaring_01.db"
  "/mnt/disk4/imagination_shards_roaring_v1/imag_roaring_02.db"
)

mkdir -p "${OUT_DIR}"
cd "${REPO_DIR}"

MAIN_LOG="${OUT_DIR}/convert_blk128_safe.log"
{
  echo "=== $(date -Is) start safe blk128 conversion ==="
  echo "out_dir=${OUT_DIR} block_size=${BLOCK_SIZE} commit_every=${COMMIT_EVERY}"
} | tee -a "${MAIN_LOG}"

for idx in "${!SOURCES[@]}"; do
  src="${SOURCES[$idx]}"
  out="${OUT_DIR}/${PREFIX}_${idx}.db"
  shard_log="${OUT_DIR}/convert_blk128_${idx}.log"

  echo "=== $(date -Is) shard=${idx} src=${src} out=${out} ===" | tee -a "${MAIN_LOG}" "${shard_log}"
  rm -f "${out}"

  ionice -c2 -n7 nice -n 15 \
    python "${REPO_DIR}/convert_roaring_to_block_tokens.py" \
      --sources "${src}" \
      --out-dir "${OUT_DIR}" \
      --prefix "${PREFIX}" \
      --block-size "${BLOCK_SIZE}" \
      --commit-every "${COMMIT_EVERY}" \
      2>&1 | tee -a "${MAIN_LOG}" "${shard_log}"

  echo "=== $(date -Is) shard=${idx} completed ===" | tee -a "${MAIN_LOG}" "${shard_log}"
  sleep 10
done

echo "=== $(date -Is) all shards completed ===" | tee -a "${MAIN_LOG}"
