# Arkitektur: DH-Postings (Main + Sidecar, Roaring runtime)

## Oversikt
Systemet er en shardet, høy-ytelses fulltekstindeks designet for Digital Humaniora (DH). I nåværende modell brukes:

- **Main shard DB**: `words`, `unigrams`, `urns`, `urns_postings`, `meta`
- **Sidecar shard DB**: `token_blocks` for tekstvindu/visning

Målet er å holde compute-løypa kompakt (postings/bitmaps) og visningsløypa separat.

## Kjernekomponenter
1. **Global lexicon (catalog):** Mapper ordformer til `global_cf_id` / `global_raw_id`.
2. **Lokal words i shard:** Mapper `word` -> lokal `cf_id` + globale ID-er.
3. **Unigrams (main shard):** Roaring-postings per `(book_id, cf_id)` for søk/near.
4. **Token blocks (sidecar):** Segmentert tekstindeks (typisk 128) for fragmentuthenting.

## Datamodell (SQLite, nåværende)

### A) Main shard DB
- `words(word, raw_id, cf_id, global_id, global_cf_id, global_raw_id, docfreq, total_tf, docpost, ...)`
- `unigrams(book_id, cf_id, tf, post)` (Roaring BLOB i nåværende build)
- `urns(book_id)`
- `urns_postings(id, post)`
- `meta(key, value)` med `postings_codec=roaring_v1`

### B) Sidecar shard DB
- `token_blocks(book_id, block_start, block_len, raw_ids)`
- `meta(key, value)`

## Runtime-flyt (Python, nåværende)

1. Term -> global/lokal lookup (`word` -> `global_cf_id` -> lokal `cf_id` per shard).
2. Kandidatbøker fra docpost/group union/intersect.
3. Near/OR på Roaring-postings i Python (`pyroaring`) for `roaring_v1`.
4. Fragmentuthenting via `token_blocks` sidecar (fallback til `tokens` hvis finnes).

Dette gir en robust løype selv når eldre C-UDF decode-path ikke matcher `roaring_v1`.

## Map-reduce over shards

- API kjører shard-resultater som map-fase.
- Aggregasjon av counts/fragments er reduce-fase i API-laget.
- `parallelShards` finnes i payload, og Python runtime er satt til parallell som default.
- For små kall kan sekvensiell være billigere, men demo/korpus-kjøring drar nytte av parallellitet.

## Designnotater (MUS / praktiske rammer)
- **Maks n-gramlengde:** 6-gram som øvre grense i postings.
- **Forventet snittlengde:** ~2.5 gitt høy andel hapax (~40%).
- **Frekvens i postings:** behold `tf` per bok i postings-tabellen; globale frekvenser for `word_id` kan ligge i symboltabellen for å støtte selektive søkestrategier.
- **Søkestrategi:** bruk `tf` til å velge korteste postings-liste som anker før nærhetskall (unngår å måle blob-lengde).
- **Mulig indeks:** `(key, tf)` eller `(word_id, tf)` for rask filtrering/aggregat (aviser/tidslinjer).

## Praktisk søkestrategi (ytelse vs orden)
- **Symmetrisk nærhet i postings** brukes som standard for raske batch-kjøringer.
- **Ordnet sekvens/retning** kan håndteres i postprosessering når det trengs.
- **Union av postings** (f.eks. for regex/lemma-utvidelser) krever merge + re-encoding.
- Dette gir ofte bedre total gjennomstrømning: sparer mye tid i batch og
  aksepterer litt ekstra postprosessering der orden er viktig.

## Julia-branch

Julia-branch er fortsatt i API-koden (engine-switch), men er midlertidig ikke aktiv i demo-flyt.