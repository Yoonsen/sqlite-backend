FROM python:3.11-slim

WORKDIR /app

ARG JULIA_VERSION=1.12.0
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsqlite3-dev \
    ca-certificates \
    curl \
    xz-utils \
  && rm -rf /var/lib/apt/lists/*

# Install Julia runtime (for optional Julia-based runners/APIs).
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) JULIA_URL_ARCH="x64"; JULIA_TAR_ARCH="x86_64" ;; \
      arm64) JULIA_URL_ARCH="aarch64"; JULIA_TAR_ARCH="aarch64" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://julialang-s3.julialang.org/bin/linux/${JULIA_URL_ARCH}/1.12/julia-${JULIA_VERSION}-linux-${JULIA_TAR_ARCH}.tar.gz" \
      -o /tmp/julia.tar.gz; \
    tar -xzf /tmp/julia.tar.gz -C /opt; \
    ln -s "/opt/julia-${JULIA_VERSION}/bin/julia" /usr/local/bin/julia; \
    rm -f /tmp/julia.tar.gz

# Pre-install Julia packages used by persistent side server/probe.
RUN julia -e 'using Pkg; Pkg.add(["HTTP","JSON3","SQLite","DBInterface"])'

COPY api_python/requirements.txt /app/api_python/requirements.txt
RUN pip install --no-cache-dir -r /app/api_python/requirements.txt

COPY api_python /app/api_python
COPY api_julia /app/api_julia
COPY dispatch_by_shard.py /app/dispatch_by_shard.py
COPY postings.c /app/postings.c
COPY docker/entrypoint.sh /app/entrypoint.sh
COPY docker/julia-run.sh /app/julia-run.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/julia-run.sh && chmod +x /app/julia-run.sh

ENV POSTINGS_CONFIG=/data/dhlab/larsj/postings/config.json
ENV POSTINGS_SO_PATH=/data/dhlab/larsj/postings/postings_native.so
ENV POSTINGS_API_MODE=python
ENV JULIA_BIN=/usr/local/bin/julia
ENV JULIA_SCRIPTS_DIR=/app/api_julia
ENV JULIA_PROBE_SCRIPT=/app/api_julia/sqlite_blob_julia_probe.jl
ENV JULIA_SERVER_SCRIPT=/app/api_julia/hybrid_server.jl
ENV POSTINGS_JULIA_HYBRID=0

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
