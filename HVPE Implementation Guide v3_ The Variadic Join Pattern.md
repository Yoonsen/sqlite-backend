# HVPE Implementation Guide v3: The Variadic Join Pattern

## 1\. Strategisk Oversikt

Vi skal legge til en **Variadic Consumer** i C. Denne funksjonen tar i mot alle postings-blobbene for en bok i ett enkelt kall.

**Instruksjon:** Implementer følgende variadiske C-funksjon i `postings.c`. Denne funksjonen skal registreres med `-1` argumenter i SQLite. Den er designet for å brukes i en `HAVING`\-klausul etter en `GROUP BY bok_id`. **Sikkerhets-guider:** \> 1\. Bruk `alloca()` for bitmaps hvis `words <= 1024` (64KB dokument-grense) for å unngå heap-overhead. 2\. Bruk `sqlite3_malloc()` som fallback for større dokumenter. 3\. Implementer **Early Exit**: Hvis `any_bits` blir 0 etter en bit-shift operasjon, avbryt prosesseringen av boken umiddelbart.

### C-Kode: `post_phrase_n_sqlite`

/\*

 \* post\_phrase\_n(max\_seq, blob1, blob2, ..., blobN)

 \* En variadisk skalar-funksjon som prosesserer N blobs i én passering.

 \* Brukes typisk: post\_phrase(40000, blobA, blobB, blobC)

 \*/

static void post\_phrase\_n\_sqlite(

    sqlite3\_context \*ctx,

    int argc,

    sqlite3\_value \*\*argv

) {

    // Minimumskrav: max\_seq \+ minst 2 ord-blober

    if (argc \< 3\) {

        sqlite3\_result\_int(ctx, 0);

        return;

    }

    sqlite3\_int64 max\_seq \= sqlite3\_value\_int64(argv\[0\]);

    uint64\_t words \= (max\_seq \+ 63\) / 64;

    size\_t byte\_len \= words \* sizeof(uint64\_t);

    // Stack-allokering for lynrask aksess i L1-cache

    uint64\_t \*acc \= (words \<= 1024\) ? alloca(byte\_len) : sqlite3\_malloc(byte\_len);

    uint64\_t \*tmp \= (words \<= 1024\) ? alloca(byte\_len) : sqlite3\_malloc(byte\_len);

    if (\!acc || \!tmp) {

        sqlite3\_result\_error\_nomem(ctx);

        return;

    }

    // 1\. Fyll akkumulator med første ord (argv\[1\])

    memset(acc, 0, byte\_len);

    fill\_bitmap(sqlite3\_value\_blob(argv\[1\]), sqlite3\_value\_bytes(argv\[1\]), max\_seq, acc);

    // 2\. Iterer gjennom resten av ordene i uttrykket

    for (int i \= 2; i \< argc; i++) {

        const unsigned char \*current\_blob \= sqlite3\_value\_blob(argv\[i\]);

        int current\_len \= sqlite3\_value\_bytes(argv\[i\]);

        

        // Hvis en av blobbene i sekvensen er tom, finnes ikke uttrykket

        if (\!current\_blob || current\_len \<= 0\) {

            if (words \> 1024\) { sqlite3\_free(acc); sqlite3\_free(tmp); }

            sqlite3\_result\_int(ctx, 0);

            return;

        }

        memset(tmp, 0, byte\_len);

        fill\_bitmap(current\_blob, current\_len, max\_seq, tmp);

        uint64\_t carry \= 0;

        uint64\_t any\_bits \= 0;

        

        // Bit-shift logikk: Flytt forrige ords posisjoner \+1 og sjekk match med neste ord

        for (uint64\_t j \= 0; j \< words; j++) {

            uint64\_t current\_val \= acc\[j\];

            uint64\_t shifted \= (current\_val \<\< 1\) | carry;

            carry \= current\_val \>\> 63;

            

            acc\[j\] \= shifted & tmp\[j\];

            any\_bits |= acc\[j\];

        }

        // SHORT-CIRCUIT: Hvis ingen treff i denne boken så langt, stopp her\!

        if (\!any\_bits) {

            if (words \> 1024\) { sqlite3\_free(acc); sqlite3\_free(tmp); }

            sqlite3\_result\_int(ctx, 0);

            return;

        }

    }

    // Har vi overlevende bits, finnes sekvensen i boken

    if (words \> 1024\) { sqlite3\_free(acc); sqlite3\_free(tmp); }

    sqlite3\_result\_int(ctx, 1);

}

---

## 3\. Registrering i `sqlite3_postings_init`

For å aktivere variadisk støtte, må funksjonen registreres med `-1`:

sqlite3\_create\_function(

    db, "post\_phrase", \-1, 

    SQLITE\_UTF8 | SQLITE\_DETERMINISTIC,

    NULL, post\_phrase\_n\_sqlite, NULL, NULL

);

---

## 4\. Den Optimale SQL-spørringen (Index Join)

Dette er mønsteret som skal brukes for å skanne 600 000 bøker. SQLite bruker indeksen til å finne rader, og `HAVING` mater alle blobbene samlet til C.

\-- Finn alle bøker med sekvensen "demokrati er viktig"

SELECT bok\_id

FROM unigrams u

JOIN (

    SELECT id as term\_id, 0 as pos FROM words WHERE term \= 'demokrati'

    UNION ALL

    SELECT id, 1 FROM words WHERE term \= 'er'

    UNION ALL

    SELECT id, 2 FROM words WHERE term \= 'viktig'

) st ON u.cf\_id \= st.term\_id

GROUP BY u.bok\_id

HAVING post\_phrase(

    40000, 

    MAX(CASE WHEN st.pos \= 0 THEN u.postings END),

    MAX(CASE WHEN st.pos \= 1 THEN u.postings END),

    MAX(CASE WHEN st.pos \= 2 THEN u.postings END)

) \= 1;

## 5\. Hvorfor dette fungerer (Argumenter til Codex)

1. **Minimale funksjonskall:** Vi gjør ett kall per bok, ikke ett per ord.  
2. **B-tre effektivitet:** `JOIN` på `cf_id` er ekstremt raskt i SQLite.  
3. **Cache Locality:** Bitmapen for boken blir liggende i CPU-ens cache under hele `for`\-loopen i C.  
4. **Ingen endring av data:** Vi bruker eksisterende unigram-postings.

### **6\. Data Ingestion & Segmentation (Strategi for store bøker)**

For å garantere at søket alltid holder seg innenfor CPU-ens L1-cache, følger vi denne strategien for indeksering:

* **Chunking:** Bøker deles opp i segmenter på maksimalt **64 000 tokens**. Dette gir en bitmap-størrelse på nøyaktig **8 KB**.  
* **Overlapping:** For å unngå "edge-cases" ved nærhetssøk, legges det inn en overlapp på **200 tokens** mellom hvert segment.  
* **Database-representasjon:** Hvert segment lagres som en egen rad i `unigrams`\-tabellen. En bok som *Krig og fred* vil dermed bestå av ca. 9-10 segment-rader.  
* **Ytelsesgaranti:** Siden hver bitmap er fiksert til 8 KB, kan C-extensionen bruke `alloca(8192)` for ekstremt rask minnehåndtering uten risiko for stack overflow.

