# Deploy Checklist: `#geo` Generalprove v2

> Operational checklist:
> This is a deploy-oriented checklist tied to a specific geo rollout phase.
> Use it when doing server promotion or production smoke-testing, but do not
> treat it as the architecture source of truth.
> Read `GEO_REBUILD_RUNBOOK.md`, `GEO_INDEX_CONTRACT.md`, and `README.md` first
> for the current rebuild/runtime workflow.

Kort, operativ sjekkliste for neste deploy av geo-annotasjon og API.

## 0) Mål

Få disse to kallene til å fungere i produksjon:

1. `#geo + wordgroups`
2. `#geo:<geoid> + wordgroups`

## 1) Kode og image

1. `git pull origin main`
2. `docker build -t harbor.nb.no/sprakbanken/postings-api:latest .`
3. `docker push harbor.nb.no/sprakbanken/postings-api:latest`
4. Noter pushed digest (`sha256:...`).

## 2) Riktig annotation-db på server

Bruk samme DB som ble validert lokalt (ikke en tilfeldig eldre/kompakt variant).

Eksempel:

```bash
scp "/mnt/disk4/geo_rebuild_runs/<stamp>/annotation_geo.annotation_only.db" \
    larsj@sprakbankdb1.lx.nb.no:/data/dhlab/larsj/postings/
```

## 3) Registry-peking (`annotation_namespaces`)

Sørg for at `geo` peker til riktig fil:

```bash
sqlite3 /data/dhlab/larsj/postings/annotation_registry.db "
UPDATE annotation_namespaces
SET db_path='/data/dhlab/larsj/postings/annotation_geo.annotation_only.db'
WHERE namespace='geo';

SELECT namespace, db_path, resolver, active
FROM annotation_namespaces
WHERE namespace='geo';
"
```

Forventet:
- `resolver = geo_resolver`
- `active = 1`

## 4) Sync `annotation_book_map` mot aktiv geo-db (kritisk)

Dette var hovedfeilen i generalprøven: `annotation_book_map` var ute av sync.

```bash
sqlite3 /data/dhlab/larsj/postings/annotation_registry.db "
ATTACH DATABASE '/data/dhlab/larsj/postings/annotation_geo.annotation_only.db' AS geo;
BEGIN;
DELETE FROM annotation_book_map WHERE namespace='geo';
INSERT INTO annotation_book_map(namespace, book_id, coverage_status)
SELECT 'geo', book_id, 'full'
FROM (SELECT DISTINCT book_id FROM geo.geo_postings_all);
COMMIT;
DETACH DATABASE geo;
"
```

## 5) Sanity SQL før restart

Bekreft at geoid faktisk finnes:

```bash
sqlite3 /data/dhlab/larsj/postings/annotation_geo.annotation_only.db "
SELECT COUNT(*)
FROM geo_postings_v2
WHERE place_key_type='geonames' AND place_key='2680854';
"
```

Bekreft at registry og db overlapper:

```bash
sqlite3 /data/dhlab/larsj/postings/annotation_geo.annotation_only.db "
ATTACH DATABASE '/data/dhlab/larsj/postings/annotation_registry.db' AS reg;
WITH b AS (
  SELECT book_id
  FROM reg.annotation_book_map
  WHERE namespace='geo' AND coverage_status IN ('full','partial')
)
SELECT COUNT(*)
FROM geo_postings_v2 p
JOIN b ON b.book_id = p.book_id
WHERE p.place_key_type='geonames' AND p.place_key='2680854';
"
```

Forventet: `> 0`.

## 6) Pull + restart API-container

```bash
docker rm -f postings-api 2>/dev/null || true
docker pull harbor.nb.no/sprakbanken/postings-api:latest
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

Valider image digest:

```bash
docker image inspect harbor.nb.no/sprakbanken/postings-api:latest --format '{{index .RepoDigests 0}}'
```

## 7) API smoke test (må være grønn)

```bash
curl -sS "https://api.nb.no/dhlab/imag/or_query" \
  -H "Content-Type: application/json" \
  -d '{"termGroups":[["#geo"],["og"],["i"]],"before":8,"after":8,"perBook":2,"totalLimit":10,"renderHits":true}'
```

```bash
curl -sS "https://api.nb.no/dhlab/imag/or_query" \
  -H "Content-Type: application/json" \
  -d '{"termGroups":[["#geo:2680854"],["og"],["i"]],"before":8,"after":8,"perBook":2,"totalLimit":10,"renderHits":true}'
```

## 8) Feilsøking (rask)

- `500`:
  - `docker logs postings-api --tail 200`
- `No geo anchor positions...` for `#geo:<id>`:
  - sjekk `geo_postings_v2` count for id
  - sjekk overlap mot `annotation_book_map` (query i steg 5)
- gammel oppførsel:
  - sjekk at runtime digest matcher pushet digest

## 9) Go / No-Go

Go:
- begge smoke-kall returnerer `200` + `rows` + (valgfritt) `rendered`
- ingen nye exceptions i `docker logs`

No-Go:
- geoid-kall returnerer `No geo anchor positions` når SQL-sjekk viser at id finnes
- mismatch mellom `annotation_book_map` og aktiv annotation-db

