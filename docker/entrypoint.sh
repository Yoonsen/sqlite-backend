#!/usr/bin/env bash
set -euo pipefail

POSTINGS_SO_PATH="${POSTINGS_SO_PATH:-/data/dhlab/larsj/postings/postings_native.so}"
POSTINGS_CONFIG="${POSTINGS_CONFIG:-/data/dhlab/larsj/postings/config.json}"
POSTINGS_API_MODE="${POSTINGS_API_MODE:-python}"
JULIA_BIN="${JULIA_BIN:-/usr/local/bin/julia}"
JULIA_SERVER_SCRIPT="${JULIA_SERVER_SCRIPT:-/app/api_julia/hybrid_server.jl}"
POSTINGS_JULIA_SIDE_PORT="${POSTINGS_JULIA_SIDE_PORT:-}"
POSTINGS_JULIA_HYBRID="${POSTINGS_JULIA_HYBRID:-0}"

if [[ ! -f "${POSTINGS_CONFIG}" ]]; then
  echo "POSTINGS_CONFIG not found: ${POSTINGS_CONFIG}" >&2
  exit 1
fi

mkdir -p "$(dirname "${POSTINGS_SO_PATH}")"

echo "Compiling postings extension..."
gcc -O3 -march=native -fPIC -shared /app/postings.c -o "${POSTINGS_SO_PATH}"

export POSTINGS_CONFIG
export POSTINGS_SO_PATH
export JULIA_BIN
export JULIA_SERVER_SCRIPT

if [[ "${POSTINGS_JULIA_HYBRID}" == "1" && -z "${POSTINGS_JULIA_SIDE_PORT}" ]]; then
  POSTINGS_JULIA_SIDE_PORT="8001"
fi

if [[ "${POSTINGS_API_MODE}" == "julia" ]]; then
  if [[ ! -f "${JULIA_SERVER_SCRIPT}" ]]; then
    echo "Julia server script not found: ${JULIA_SERVER_SCRIPT}" >&2
    exit 1
  fi
  echo "Starting Julia API on port ${PORT:-8000}..."
  exec "${JULIA_BIN}" "${JULIA_SERVER_SCRIPT}"
fi

if [[ "${POSTINGS_API_MODE}" != "python" ]]; then
  echo "Invalid POSTINGS_API_MODE=${POSTINGS_API_MODE}. Use python or julia." >&2
  exit 1
fi

if [[ -n "${POSTINGS_JULIA_SIDE_PORT}" ]]; then
  if [[ ! -f "${JULIA_SERVER_SCRIPT}" ]]; then
    echo "Julia server script not found: ${JULIA_SERVER_SCRIPT}" >&2
    exit 1
  fi
  echo "Starting side Julia API on port ${POSTINGS_JULIA_SIDE_PORT}..."
  export POSTINGS_JULIA_PROXY_URL="http://127.0.0.1:${POSTINGS_JULIA_SIDE_PORT}"
  PORT="${POSTINGS_JULIA_SIDE_PORT}" "${JULIA_BIN}" "${JULIA_SERVER_SCRIPT}" &
fi

echo "Starting Python API on port ${PORT:-8000}..."
exec uvicorn api_python.server:app --host 0.0.0.0 --port "${PORT:-8000}"
