## Startup Commands

Use this checklist when bringing the API back online.

## 1) Reachability check

```bash
ping -c 2 sprakbankdb1.lx.nb.no || true
curl -m 8 -sS http://sprakbankdb1.lx.nb.no:8000/health || true
```

## 2) Pull latest image

```bash
docker pull harbor.nb.no/sprakbanken/postings-api:latest
```

## 3) Safe start (Python-only, recommended first)

```bash
docker rm -f postings-api 2>/dev/null || true
docker run --name postings-api --rm -p 8000:8000 \
  -e POSTINGS_CONFIG=/data/dhlab/larsj/postings/config.json \
  -e POSTINGS_SO_PATH=/data/dhlab/larsj/postings/postings_native.so \
  -e POSTINGS_BITMAP_NEAR=1 \
  -e POSTINGS_BITMAP_CHUNK=4096 \
  -e POSTINGS_JULIA_HYBRID=0 \
  -e POSTINGS_QUERY_ENGINE=python \
  -v /data/dhlab/larsj/postings:/data/dhlab/larsj/postings \
  harbor.nb.no/sprakbanken/postings-api:latest
```

## 4) Sanity checks

```bash
curl -sS http://127.0.0.1:8000/health
curl -sS -X POST http://127.0.0.1:8000/near_query \
  -H "content-type: application/json" \
  -d '{"terms":["spiser","middag"],"window":15,"engine":"python"}'
```

## 5) Optional: enable Julia hybrid later (after stable Python)

```bash
docker rm -f postings-api 2>/dev/null || true
docker run --name postings-api --rm -p 8000:8000 \
  -e POSTINGS_CONFIG=/data/dhlab/larsj/postings/config.json \
  -e POSTINGS_SO_PATH=/data/dhlab/larsj/postings/postings_native.so \
  -e POSTINGS_BITMAP_NEAR=1 \
  -e POSTINGS_BITMAP_CHUNK=4096 \
  -e POSTINGS_JULIA_HYBRID=1 \
  -e POSTINGS_QUERY_ENGINE=python \
  -e POSTINGS_JULIA_THREADS=4 \
  -e POSTINGS_JULIA_PARALLEL_SHARDS=1 \
  -v /data/dhlab/larsj/postings:/data/dhlab/larsj/postings \
  harbor.nb.no/sprakbanken/postings-api:latest
```

## 6) If API does not respond

```bash
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"
docker logs --tail 200 postings-api
```
