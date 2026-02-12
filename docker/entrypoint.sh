#!/usr/bin/env bash
set -euo pipefail

POSTINGS_SO_PATH="${POSTINGS_SO_PATH:-/data/dhlab/larsj/postings/postings_native.so}"
POSTINGS_CONFIG="${POSTINGS_CONFIG:-/data/dhlab/larsj/postings/config.json}"

if [[ ! -f "${POSTINGS_CONFIG}" ]]; then
  echo "POSTINGS_CONFIG not found: ${POSTINGS_CONFIG}" >&2
  exit 1
fi

mkdir -p "$(dirname "${POSTINGS_SO_PATH}")"

echo "Compiling postings extension..."
gcc -O3 -march=native -fPIC -shared /app/postings.c -o "${POSTINGS_SO_PATH}"

export POSTINGS_CONFIG

echo "Starting API on port ${PORT:-8000}..."
exec uvicorn api_python.server:app --host 0.0.0.0 --port "${PORT:-8000}"
