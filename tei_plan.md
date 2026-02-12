## TEI standoff plan

### 1) Linearisering som koordinatsystem
- Bruk `tokens.seq` som felles posisjonssystem per `book_id`.
- Gir stabile offsets og gjør annotasjoner presise og join‑bare.

### 2) Annotasjoner som stand‑off
- Lagre annotasjoner separat: `book_id, start_seq, end_seq, label, meta`.
- Kan eksporteres som CSV/JSON, eller TEI `<spanGrp>/<span>` med `from/to`.

### 3) Sampling → annotering
- Postings/near‑spørringer gir posisjoner.
- Konverter `pos` eller `pos‑range` til standoff‑records direkte.

### 4) Korpus‑filter via temp‑tabell
- Bruk `urn_filter` for å lage delkorpus på tvers av metadata.
- Samme SQL kan produsere annotasjoner per subkorpus.

### 5) Historien vi kan fortelle
- Med postings som vanlige tabeller får vi full SQL‑flyt.
- Vi får nøyaktige posisjoner, raske joins og enkel standoff‑eksport.
- Dette gir både skala og forsknings‑presisjon for DH.
