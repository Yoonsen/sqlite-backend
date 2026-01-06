# Arkitektur: DH-Postings (Token-basert Stand-off Indeks)

## Oversikt
Systemet er en shardet, høy-ytelses fulltekstindeks designet for Digital Humaniora (DH). Det erstatter tradisjonell FTS5 med en deterministisk n-gram-indeks lagret som delta-kodede BLOB-er i SQLite.

## Kjernekomponenter
1. **Tokens (Sannhetskilden):** Vertikal lagring av hver bok. Hvert ord/tegn er en rad med posisjon (`seq`).
2. **N-grams (Søkemotoren):** En invertert indeks der nøkkelen er en sekvens av ord-ID-er (opp til 10-gram), og verdien er en komprimert liste over posisjoner.
3. **Lexicon (Mapping):** Global/lokal tabell som mapper `term` <-> `cf_id` (case-foldet integer).

## Datamodell (SQLite Shard)
Tabeller i hver `.sqlite`-fil (ca. 200-500 mill tokens per fil):

### 1. `tokens` (WITHOUT ROWID)
Lagrer den lineære tekststrømmen.
- `book_id` (INT)
- `seq` (INT)
- `cf_id` (INT): Case-foldet ID for analyse.
- `raw_id` (INT): Originalform ID for visning.
- `para` (INT), `page` (INT)
- **PK:** `(book_id, seq)`

### 2. `ngrams` (WITHOUT ROWID)
Invertert indeks for lynraske oppslag.
- `key` (BLOB): Pakket sekvens av `cf_id` (4 bytes per ord).
- `book_id` (INT)
- `df` (INT): Antall forekomster i boken.
- `post` (BLOB): Delta-kodede `seq` (Varint/LEB128).
- **PK:** `(key, book_id)`
- **Index:** `(book_id, key)` (For analyse av enkeltverk).

## Søkestrategi (Julia)
1. **Eksakt n-gram:** Ett oppslag i `ngrams` tabellen via `key`.
2. **Lengre sekvenser (L > n_index):** Slå opp det mest selektive n-grammet (lavest `df`), hent posisjoner, og gjør "hale-verifisering" mot `tokens` tabellen (seq + offset).
3. **Kollokasjoner:** Slå opp anker-ord i `ngrams`, utfør vindu-søk i `tokens` tabellen.