## Status
- Bygget postingsbasert pipeline fra `ft` til `tokens` + ngrammer.
- Kompilert og testet SQLite‑utvidelse (`postings.so`) for nærhet/posisjoner.
- Innført `post_count` og `post_near_positions_blob` for blob‑sampling uten JSON.
- Lagt inn docpost‑filtrering med `json_each` + fallback til `sample_urns` når `urns_postings` mangler.
- `docSamples` i API‑skjema + web‑UI; brukes kun når ingen docpost‑filter finnes.
- Kjørt demo‑ og full‑shard‑tester, samt split‑modell (unigrams/bigrams).
- Verifisert konkordanser og nærhetssøk; målt ytelse bigram vs postings‑sekvens.

## Hva vi testet
- Sample av setningsfragmenter basert på punktum.
- Nærhetssøk (og ~ i) og konkordanser (og).
- Sammenligning: bigram‑oppslag vs postings‑sekvens for "demokrati og".
- Trigram‑oppslag via postings (tre unigram) vs bigram+unigram.
- SQL‑maler for doc_sample=10 (count + list) med `post_near_positions_blob`.

## Veien videre
- Bygge realistisk 1800‑talls shard (ca. 23k bøker).
- Pipeline for standoff‑markup:
  - Ngrammer per bok, færre ngrammer enn fulltekst.
  - Ingen `tokens`‑tabell, kun ngram‑tabell(er) + ev. ekstra metadata.
  - Standoff‑data i egne filer/sharder.
- Avklare schema for standoff‑tabeller (felter/metadata).
- Eventuelt legge inn cutoffs (f.eks. dropp trigram‑hapax) ved behov.
- Optimalisere URN-listing fra docpost (cache OR-blobs, mindre JSON).
- Vurdere pre‑filtering via docpost‑union/intersect før per‑bok OR‑union + near.
- Parallellisere shard-kjøring i API (nå sekvensiell loop over shards).
- Definere "shard park" (shard federation) for stor skala:
  - Shards er self-contained for drift/innlasting.
  - Et globalt `words`-register vedlikeholdes på tvers av shards.
  - Lokal `words` får `global_id` i tillegg til `cf_id`.
  - Synk-jobb ved innlegging av ny shard (map lokal ordliste mot global katalog).
  - Bruke `global_id` for tverr-shard kollokasjon, concordance og senere DTM-bygging.
- Implementere sekvensmodus med bit-shift-kjede (phrase-like), f.eks. `x y z`:
  - Krav: `x` én posisjon fra `y` og `y` én posisjon fra `z`.
  - Prototype: `H = Gx & shift_right(Gy,1) & shift_right(Gz,2)` (evt. motsatt retning).
  - Avklare optimal implementasjon ved word-boundary carry (kan kreve to delrunder per shift i praksis).
  - Sammenligne sekvens-kjerne mot eksisterende near-kjerne for 2/3+ ord.
- All-Roaring migrering (uten hybridformat) for docpost:
  - Følg `ROARING_MIGRATION_PLAN.md` (faser 0-5).
  - Leveranse 1: `ROARING_CODEC.md` + rebuild-script med validate mode.
  - Leveranse 2: C/Julia/Python runtime med én Roaring-basert docpost-løype.
  - Leveranse 3: parity/perf-gater og cutover per shard (blue/green).