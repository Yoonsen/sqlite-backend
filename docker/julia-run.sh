#!/usr/bin/env bash
set -euo pipefail

JULIA_BIN="${JULIA_BIN:-/usr/local/bin/julia}"
JULIA_SCRIPTS_DIR="${JULIA_SCRIPTS_DIR:-/app/api_julia}"
JULIA_SCRIPT="${JULIA_SCRIPT:-${JULIA_SCRIPTS_DIR}/sqlite_blob_julia_probe.jl}"

if [[ ! -x "${JULIA_BIN}" ]]; then
  echo "Julia binary not executable: ${JULIA_BIN}" >&2
  exit 1
fi

if [[ ! -f "${JULIA_SCRIPT}" ]]; then
  echo "Julia script not found: ${JULIA_SCRIPT}" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: /app/julia-run.sh /path/to/payload.json" >&2
  exit 1
fi

exec "${JULIA_BIN}" "${JULIA_SCRIPT}" "$1"
