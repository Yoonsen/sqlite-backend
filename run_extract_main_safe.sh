#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/mnt/disk1/Github/sqlite-backend"
OUT_DIR="/mnt/disk4/imagination_shards_roaring_main_v1"
PREFIX="imag_roaring_main"

SOURCES=(
  "/mnt/disk4/imagination_shards_roaring_v1/imag_roaring_00.db"
  "/mnt/disk4/imagination_shards_roaring_v1/imag_roaring_01.db"
  "/mnt/disk4/imagination_shards_roaring_v1/imag_roaring_02.db"
)

mkdir -p "${OUT_DIR}"
cd "${REPO_DIR}"

MAIN_LOG="${OUT_DIR}/extract_main.log"
{
  echo "=== $(date -Is) start main-only extraction ==="
  echo "out_dir=${OUT_DIR}"
} | tee -a "${MAIN_LOG}"

for idx in "${!SOURCES[@]}"; do
  src="${SOURCES[$idx]}"
  out="${OUT_DIR}/${PREFIX}_${idx}.db"
  shard_log="${OUT_DIR}/extract_main_${idx}.log"

  echo "=== $(date -Is) shard=${idx} src=${src} out=${out} ===" | tee -a "${MAIN_LOG}" "${shard_log}"
  rm -f "${out}" "${out}-wal" "${out}-shm"

  ionice -c2 -n7 nice -n 15 \
    python -u "${REPO_DIR}/extract_main_from_roaring.py" \
      --sources "${src}" \
      --out-dir "${OUT_DIR}" \
      --out-file "${out}" \
      --prefix "${PREFIX}" \
      2>&1 | tee -a "${MAIN_LOG}" "${shard_log}"

  echo "=== $(date -Is) shard=${idx} completed ===" | tee -a "${MAIN_LOG}" "${shard_log}"
  sleep 10
done

echo "=== $(date -Is) all main-only shards completed ===" | tee -a "${MAIN_LOG}"
