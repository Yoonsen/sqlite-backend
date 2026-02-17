## Logbook

### 2026-02-17
- Generaliserte bitmap-løypa i Python for nærhet slik at `len(groups) >= 2` går via samme bitmap-strategi (anchor mot alle grupper), både for count (`/near_query`) og fragments (`/near_fragments`).
- La inn robust funksjonsdeteksjon i API (`pragma_function_list`) slik at manglende bitmap-UDF i `.so` ikke gir 500, men faller tilbake til ikke-bitmap løype.
- Fikset `near_query`-bug der request-modellen manglet `totalLimit/perBook` men koden refererte feltene.
- Verifiserte lokalt at 3-gruppe bitmap-kall går stabilt med sub-sekund respons på representative payloads.
- Avklart neste skaleringssteg: dagens shard-loop i API er sekvensiell; må flyttes til parallell per-shard eksekvering for hundrevis av sharder.
- Skissert fremtidig "shard park"/federasjon: hver shard er self-contained, men synkroniserer ord mot global words-katalog (`global_id` + lokal `cf_id`) for tverr-shard korpusalgebra og DTM-flyt.

### 2026-02-06 (oppdatert)
- La inn `post_count` og `post_near_positions_blob` i SQLite‑utvidelsen for rask blob‑sampling uten JSON.
- Flyttet sampling til blob‑nivå (`post_count` + `post_sample`) i concordance/near/fragment‑flyten.
- Innført `docSamples` i API‑skjema og web‑UI; brukes etter docpost‑snitt (filter → snitt → sample).
- Standardisert docpost‑filtrering med `json_each` og fallback til `sample_urns` når `urns_postings` mangler.
- Strammet logikken slik at docpost‑filter alltid kjøres før downsampling.
- Utvidet samme filter‑og‑sampling‑logikk til `near_query`, `near_fragments`, `collocations` og `near_frequency`.

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
