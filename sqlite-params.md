SQLite Parametere for DH-Postings
Disse innstillingene er optimalisert for en WORM (Write Once, Read Many) arkitektur der basen bygges sekvensielt og brukes til tunge søk og analyser.

1. Under Bygging (Python/Pipeline)
Bruk disse for å maksimere skrivehastighet og minimere overhead.

• `PRAGMA journal_mode = OFF;` - Deaktiverer rollback-logg (raskere skriving, ingen transaksjonssikkerhet ved krasj).

• `PRAGMA synchronous = OFF;` - SQLite venter ikke på at OS-et bekrefter skriving til disk.

• `PRAGMA cache_size = -2000000;` - Bruker ca. 2GB RAM som cache under bygging.

• `PRAGMA temp_store = MEMORY;` - Lagrer midlertidige tabeller og sortering i RAM.

2. Under Søk og Analyse (Julia/API)
Bruk disse for å maksimere lese-ytelse og utnytte systemressurser.

• `PRAGMA query_only = ON;` - Sikrer at ingen utilsiktede skriveoperasjoner skjer.

• `PRAGMA mmap_size = 30000000000;` - (30GB) Mapper databasen direkte i minnet for lynrask I/O.

• `PRAGMA cache_size = -1000000;` - 1GB cache per shard-tilkobling.

• `PRAGMA threads = 4;` - Lar SQLite bruke flere tråder på interne operasjoner hvis nødvendig.

3. Generelle Filinnstillinger
Settes én gang ved opprettelse av databasen.

• `PRAGMA page_size = 16384;` - Større sider er ofte mer effektivt for store BLOB-lagre.

• `PRAGMA auto_vacuum = NONE;` - Unngår fragmentering og overhead i en statisk database.

• `PRAGMA encoding = 'UTF-8';` - Standard for DH-tekst.