# Manifest: Implementasjon og Pipeline

## 1. Byggefasen (Python)
- **Verktøy:** Python 3.x, Polars/Pandas, `sqlite3`.
- **Prosess:**
    1. Tokeniser tekst per bok.
    2. Map ord til globale `cf_id`.
    3. Generer n-grammer (1-gram, 2-gram, 3-gram alltid; 4-10 gram hvis frekvente/unike).
    4. Aggreger posisjoner per n-gram per bok.
    5. **Encoding:** Delta-kode posisjoner og pakk til LEB128 Varint BLOBs.
    6. **Bulk Insert:** Sorter data etter PK før innsetting i SQLite for optimal B-Tree pakking.
- **Output:** Shardede SQLite-filer (001.db, 002.db, ...).

## 2. Søkefasen (Julia)
- **Verktøy:** Julia, `SQLite.jl`, `Threads`.
- **Egenskaper:**
    - **Parallellisering:** Bruk `Threads.@threads` for å søke i alle shards samtidig.
    - **Bit-manipulering:** Dekoding av Varint-blobs skjer i native Julia-ytelse.
    - **Minnehåndtering:** Shards åpnes som read-only, utnytter OS-nivå page cache.
- **API Funksjoner:**
    - `get_counts(ngram)`: Returnerer totalfrekvens over alle shards.
    - `get_concordance(ngram, window)`: Returnerer fragmenter ved å slå opp i `ngrams` så `tokens`.
    - `get_collocations(anchor, distance)`: Aggregerer nabo-IDer fra `tokens` tabellen.

## 3. Kodestandarder
- **Posisjonskoding:** Alle `seq` er 0-indekserte per bok.
- **Nøkkelpakking:** `cf_id` lagres som 4-byte Little Endian i BLOB-nøkler.
- **Varint:** Standard LEB128 (7-bit per byte, MSB som continuation flagg).