## Logbook

### 2026-02-07
- La inn støtte for `post_union` i SQLite-utvidelsen (merge + re-varint).
- La inn `post_to_bitmap` og `post_bigram_bitmap` for å teste bitmap-basert bigram-telling.
- Beslutning: start med unigrams som grunnmotor og legg til bigrams/trigrams senere.
- Bygget og testet cutoff-bigrams (behold høyfrekvente) for å holde shard liten.
- Testet aggregasjoner og nærhet, viste tydelig forskjell mellom lagrede bigrams og postings-recompute.

### 2026-02-08
- Utforsket bitmap vs postings: on-the-fly, cached bitmap og hybrid (bitmap+postings) for nærhet/bigram.
- Erfarte at postings er fleksibelt for komplekse uttrykk, mens lagret ngram er raskest for enkel lookup.
- Første forsøk på å bygge produksjonsnær shard (imagination-korpus) med unigrams/postings.
- Merknad: .so må recommpileres per maskin/arkitektur (dhlab1 vs db2 vs Mac M4).

### 2026-02-06
- Bygget converter fra `ft` til `tokens` + ngrams (split unigrams/bigrams).
- Kompilert `postings.so` og røyktestet funksjoner.
- Verifisert konkordanser, nærhet, og ytelse på demo- og full-shard.
- La inn `post_union_agg` og brukte OR-union direkte i SQLite.
- Oppdatert API til docpost-basert URN-liste (ingen temp-table, `json_each`).
