## Logbook

### 2026-02-07
- La inn støtte for `post_union` i SQLite-utvidelsen (merge + re-varint).
- Beslutning: start med unigrams som grunnmotor og legg til bigrams/trigrams senere.
- Bygget og testet cutoff-bigrams (behold høyfrekvente) for å holde shard liten.
- Testet aggregasjoner og nærhet, viste tydelig forskjell mellom lagrede bigrams og postings-recompute.

### 2026-02-06
- Bygget converter fra `ft` til `tokens` + ngrams (split unigrams/bigrams).
- Kompilert `postings.so` og røyktestet funksjoner.
- Verifisert konkordanser, nærhet, og ytelse på demo- og full-shard.
