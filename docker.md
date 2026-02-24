# Docker (main + sidecar, Python runtime)

## Build image

```bash
docker build -t postings-api:main-sidecar .
```

## Run image (local test)

```bash
docker run --rm -d -p 8012:8000 \
  -e POSTINGS_CONFIG=/app/config.main_sidecar.docker.json \
  -e POSTINGS_SO_PATH=/tmp/postings_native.so \
  -e POSTINGS_QUERY_ENGINE=python \
  -e POSTINGS_PYTHON_PARALLEL_SHARDS=1 \
  -e POSTINGS_PYTHON_SHARD_WORKERS=3 \
  -v "/mnt/disk4:/data:ro" \
  -v "/mnt/disk1/Github/sqlite-backend/config.main_sidecar.docker.json:/app/config.main_sidecar.docker.json:ro" \
  --name postings-main-sidecar-test \
  postings-api:main-sidecar
```

## Run image (Harbor, config.json on host)

```bash
docker pull harbor.nb.no/sprakbanken/postings-api:main-sidecar

docker run --rm -p 8000:8000 \
  -e POSTINGS_CONFIG=/data/dhlab/larsj/postings/config.json \
  -e POSTINGS_SO_PATH=/tmp/postings_native.so \
  -e POSTINGS_QUERY_ENGINE=python \
  -e POSTINGS_PYTHON_PARALLEL_SHARDS=1 \
  -e POSTINGS_PYTHON_SHARD_WORKERS=3 \
  -v "/data/dhlab/larsj/postings:/data/dhlab/larsj/postings:ro" \
  --name postings-api \
  harbor.nb.no/sprakbanken/postings-api:main-sidecar
```

## Publish image (Harbor)

```bash
docker build -t postings-api:main-sidecar .
docker tag postings-api:main-sidecar harbor.nb.no/sprakbanken/postings-api:main-sidecar
docker push harbor.nb.no/sprakbanken/postings-api:main-sidecar
```

## Smoke test

```bash
curl -s http://127.0.0.1:8012/health
```

```bash
curl -s http://127.0.0.1:8012/near_query \
  -H 'Content-Type: application/json' \
  -d '{
    "termGroups":[["demokrati","folkestyre"],["aristokrati"]],
    "window":10,
    "schema":"unigrams",
    "symmetric":true,
    "excludeSelf":false,
    "useFilter":false,
    "filterIds":[],
    "maxVariants":20,
    "engine":"python"
  }'
```

## Notes

- This image now includes `pyroaring` for `roaring_v1` postings decode in Python runtime.
- `parallelShards` is enabled by default in Python runtime.
- Julia code path is still present in the API but currently not used in demo flow.
- `--rm` means the container is deleted automatically when it stops.
- `-d` runs in background (detached). Without `-d`, use `Ctrl-C` to stop.
