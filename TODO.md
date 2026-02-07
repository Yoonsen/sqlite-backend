## Status
- Bygget postingsbasert pipeline fra `ft` til `tokens` + ngrammer.
- Kompilert og testet SQLite‑utvidelse (`postings.so`) for nærhet/posisjoner.
- Kjørt demo‑ og full‑shard‑tester, samt split‑modell (unigrams/bigrams).
- Verifisert konkordanser og nærhetssøk; målt ytelse bigram vs postings‑sekvens.

## Hva vi testet
- Sample av setningsfragmenter basert på punktum.
- Nærhetssøk (og ~ i) og konkordanser (og).
- Sammenligning: bigram‑oppslag vs postings‑sekvens for "demokrati og".
- Trigram‑oppslag via postings (tre unigram) vs bigram+unigram.

## Veien videre
- Bygge realistisk 1800‑talls shard (ca. 23k bøker).
- Pipeline for standoff‑markup:
  - Ngrammer per bok, færre ngrammer enn fulltekst.
  - Ingen `tokens`‑tabell, kun ngram‑tabell(er) + ev. ekstra metadata.
  - Standoff‑data i egne filer/sharder.
- Avklare schema for standoff‑tabeller (felter/metadata).
- Eventuelt legge inn cutoffs (f.eks. dropp trigram‑hapax) ved behov.
