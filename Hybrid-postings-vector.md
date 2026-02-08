Project Manifesto: Hybrid Vector-Postings Engine (HVPE)
1. Visjon
Å transformere statisk tekst-indeksering til en dynamisk beregningsmodell. Ved å utnytte Zipfs lov og moderne CPU-instruksjoner (SIMD), skal systemet utføre komplekse korpusanalyser, bigram-telling og nærhetssøk on-the-fly uten behov for pre-beregnede n-gram-tabeller.

2. Kjernearkitektur
Systemet opererer på atomiske enheter (bøker) organisert i shards. Hver bok har en fast sekvensiell indeks (seq) som fungerer som et felles koordinatsystem for alle lag.

A. Den Hybride Representasjonen (The Storage Logic)
Hvert unigram (term) lagres som en BLOB med en 1-byte header som dikterer prosesseringsveien:

TYPE_BITMAP (0x01): En tett 0-1 vektor (Bitset). Brukes for høyfrekvente ord (stoppord, tegnsetting). Optimalisert for AVX-512/AVX2 bitwise-operasjoner.

TYPE_SPARSE (0x02): Delta-kodede Varints (Postings). Brukes for den lange halen av sjeldne ord. Sparer >90% plass for lavfrekvente termer.

Standoff Markup: Separate tabeller/blober for Geodata, POS-tags og entiteter som benytter samme seq-mapping.

B. Beregningsmodell (The Compute Logic)
Nærhet og assosiasjon er ikke lagrede data, men matematiske operasjoner:

Bigram-telling: POPCNT( (VecA >> 1) & VecB )

Skip-gram-telling: POPCNT( (VecA >> distance) & VecB )

Filtrert søk: VecTerm & VecStandoffTag (f.eks. ordet "Aa" AND taggen "LOCATION").

3. Tekniske Spesifikasjoner for Implementering
For LLM-basert koding (Cursor/Codex), følg disse prinsippene i postings.c:

Memory Alignment: Alloker minne for bitmaps med 64-byte alignment for å støtte AVX-512 instruksjoner.

Lazy Expansion: Sparse-lister skal ikke ekspanderes til bitmaps med mindre det er nødvendig for krysning mot en tett bitmap. Foretrekk direkte sjekk mot bitmap-bits for sparse verdier.

Hardware Acceleration:

Bruk __builtin_popcountll for hardware-akselerert telling.

Bruk intrinsics (immintrin.h) for kritiske loops hvis kompilatoren ikke autovektoriserer optimalt.

SQLite UDF Design: Funksjonene må være DETERMINISTIC og håndtere BLOB-minne via sqlite3_malloc/realloc for å unngå lekkasjer i shards med 1 mrd tokens.

4. Datastruktur-tabeller (SQLite)
SQL

-- Hovedindeks (Hybrid BLOBs)
CREATE TABLE unigrams (
    cf_id INTEGER, 
    bok_id INTEGER, 
    postings BLOB, -- Header (1b) + Data
    PRIMARY KEY (cf_id, bok_id)
) WITHOUT ROWID;

-- Standoff Markup (Eks: Geodata)
CREATE TABLE standoff_geo (
    bok_id INTEGER,
    postings BLOB, -- Markerer seq-posisjoner som er stedsnavn
    PRIMARY KEY (bok_id)
);

-- Koordinatsystem (Konkordans-kilde)
CREATE TABLE tokens (
    bok_id INTEGER,
    seq INTEGER,
    cf_id INTEGER,
    PRIMARY KEY (bok_id, seq)
) WITHOUT ROWID;
5. Målsetning for Shard-ytelse (1800-talls korpus)
Antall dokumenter: 23 000 - 25 000.

Mål: Fullstendig bigram-aggregering for vilkårlige ordpar på < 500ms per shard.

Lagring: < 1.5x original tekststørrelse inkludert alle unigram-posisjoner.
