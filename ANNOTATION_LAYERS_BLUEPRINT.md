# Annotation Layers Blueprint (Geo + Struktur + Bilder)

> Current architecture reference:
> This document describes the shared model for annotation namespaces, registry
> behavior, and common table patterns across layers.
> Use it when designing or extending non-geo annotation layers, and read it
> together with `GEO_INDEX_CONTRACT.md` for geo-specific details.

Dette notatet beskriver en felles modell for kommende annotasjonslag:

- `geo` (allerede i bruk)
- `linebreak` (linjeskift)
- `paragraph` (avsnitt)
- `page` (side)
- `image` (bilder i bok)

Mål: samme driftsmønster for alle lag, men med lag-spesifikk semantikk.

## 1) Felles prinsipp

All annotasjon er et lag over fulltekst, knyttet til:

- `book_id` (global dhlab-id)
- posisjon i tokenrom (`seq_start`)
- lengde (`token_len`) når relevant
- stabil nøkkel (`ann_key_type`, `ann_key`) når relevant

Frontend går alltid via API, ikke direkte mot SQLite-filer.

## 2) Registry som kontrollplan

`annotation_registry.db` er sannhetskilden for aktive lag:

- `annotation_namespaces` peker hvert namespace til DB-fil + resolver
- `annotation_book_map` sier hvilke bøker som er dekket for namespace

Kritisk driftspunkt:
- `annotation_book_map` må rebuildes når man bytter DB for et namespace.

## 3) Felles tabellmønster per lag

Anbefalt minimumsmønster i hvert namespace-db:

1. **mentions (posisjonsnivå)**
   - én rad per annotert forekomst i bok
2. **postings (nøkkel -> posisjoner)**
   - rask søkbar struktur per nøkkel (bitmap/blob)
3. **book index (bok -> union)**
   - raskt filter over bøker som har laget

Geo følger allerede dette mønsteret (`geo_mentions_v2`, `geo_postings_v2`, `geo_book_index_v2`).

## 4) Forslag per nytt lag

### `linebreak`

- key: ikke nødvendig i første versjon
- lagre posisjoner der linjeskift starter/slutter
- query-eksempel: `#linebreak` eller strukturfiltre i render

### `paragraph`

- key: evt. `paragraph_id` per bok
- tokenlen kan brukes for hele avsnitts-spenn
- query-eksempel: `#paragraph` eller “innen samme avsnitt”

### `page`

- key: side-id (f.eks. skannet side/folio)
- behov for metadatafelt (sideetikett, sideordre)
- query-eksempel: `#page:<id>`

### `image`

- key: stabil `image_id`
- metadata: bbox/region, score, type, kilde, sidekobling
- query-eksempel: `#image`, `#image:<id>`, `#image + wordgroups`
- estimat: ca. 400k forekomster (sparse nok til sidecar/postings-strategi)

## 5) API-kontrakt (felles retning)

Behold samme query-stil som geo:

1. `#namespace` (alle forekomster)
2. `#namespace:<key>` (forekomster for én identitet)
3. `#namespace + termGroups` (forankret nærhet mot fulltekst)

Respons bør standardiseres:

- `bookId`, `seqStart`, `tokenLen`
- `keyType`, `key` (når laget har identitet)
- valgfritt `rendered[]` via `renderHits`
- valgfritt `_perf` ved profilering

## 6) Drift og deploy (for alle lag)

1. bygg namespace-db
2. valider tabeller + kardinalitet
3. oppdater `annotation_namespaces` (`db_path`, `resolver`, `active`)
4. rebuild `annotation_book_map` for namespace
5. smoke-test `#namespace`, `#namespace:<key>`, `#namespace + wordgroups`

## 7) Hvorfor dette er viktig

Med felles mønster kan man:

- legge til nye lag uten å skrive om hele API-et
- gjenbruke frontend-interaksjon på tvers av lag
- holde querylogikk stabil mens datalag byttes ut
- unngå driftfeil ved eksplisitt registry + book-map sync

## 8) Neste konkrete steg (bilder)

For `image` anbefales en første “v1 slice”:

1. definer `image_mentions_v1` + `image_postings_v1` + `image_book_index_v1`
2. registrer namespace `image` i registry
3. implementer `#image` og `#image:<id>`
4. aktiver `#image + wordgroups` når basis er stabil

