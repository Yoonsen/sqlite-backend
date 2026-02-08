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


eksempel på bigram sammenligning
#include <stdint.h>
#include <stddef.h>

/**
 * Teller antall treff hvor ord A etterfølges av ord B innenfor 'dist' ord.
 * @param vec_a: Bitvektor for ord A (ferdig utpakket bitmap)
 * @param vec_b: Bitvektor for ord B
 * @param num_words: Antall uint64_t elementer i vektoren (f.eks. 625 for 40k bits)
 * @param dist: Maksimal avstand (1 = rett etter hverandre, dvs. bigram)
 * @return Totalt antall treff (POPCNT)
 */
uint64_t count_proximity_matches(const uint64_t* vec_a, const uint64_t* vec_b, 
                                 size_t num_words, int dist) {
    uint64_t total_matches = 0;
    
    // Vi lager en "skyggevektor" av A som er forskjøvet og OR-et sammen
    // For dist=1 (bigram), trenger vi bare ett skift.
    // For dist=3, sjekker vi (A<<1 | A<<2 | A<<3) & B.
    
    for (int d = 1; d <= dist; d++) {
        uint64_t carry = 0;
        
        for (size_t i = 0; i < num_words; i++) {
            uint64_t a_val = vec_a[i];
            
            // Lag det skiftede ordet med biten som "falt ut" av forrige 64-bit blokk
            uint64_t a_shifted = (a_val << d) | carry;
            
            // Forbered carry til neste 64-bit blokk i loopen
            carry = (d < 64) ? (a_val >> (64 - d)) : 0; 
            
            // Finn treff mot ord B i denne blokken
            uint64_t matches = a_shifted & vec_b[i];
            
            // Bruk CPU-ens innebygde popcount (Hevnen!)
            total_matches += __builtin_popcountll(matches);
        }
    }
    
    return total_matches;
}
