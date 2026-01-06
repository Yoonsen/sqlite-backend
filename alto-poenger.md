# ALTO-poenger (tokenisering og indeksløp)

- **To nivåer av token:**  
  - `raw_token`: ALTO-ordet uendret, mappes til `raw_id` (for visning/diagnostikk).  
  - `norm_token`: renset/normalisert + case-fold, mappes til `cf_id` (for analyse/søk).

- **Behold all støy, men flagg den:**  
  - Ikke dropp lange/rare tokens; lag et `noisy`-flagg (f.eks. lengde > 128, høy andel ikke-alfanum).  
  - Selve strengen lagres uendret i `raw_token`.

- **Normalisering før n-gram:**  
  - Rens kontrolltegn/whitespace-varianter; håndter soft hyphen og linje-/sidebrudd før `norm_token`.  
  - Hyphenation: join på normalisert nivå; behold originalen i `raw_token`.

- **Metadata per token:**  
  - `bok_id`, `seq` (0-indeksert), `page`, `para` (ev. `line_id`).  
  - `raw_id`, `cf_id`, `noisy`.

- **N-gram-indeks:**  
  - Bygg på `norm_token`-strømmen; pakke `cf_id` som 4-byte LE i `key`, delta/LEB128 i `post`.  
  - Generer 1–3-gram alltid, 4–10-gram selektivt (frekvens/unikhet).

- **Shard-løype fra ALTO:**  
  1) Stream ALTO i leseorden → bygg `seq` og metadata.  
  2) Map `raw_token` → `raw_id`; `norm_token` → `cf_id`; sett `noisy`.  
  3) Produser n-grammer, delta-kod, sorter etter PK.  
  4) Fyll shards til ~500M tokens per `.sqlite`; bulk-insert i PK-rekkefølge.

- **Reproduserbarhet:**  
  - Logg i build-manifest: ALTO-versjon, normaliseringsversjon, tokeniseringsversjon, noisy-heuristikk, n-gram cutoffs, shard-cutoff.  
  - Samme ALTO kan leveres uendret til Elastic; SQLite-shardene bruker `norm_token` for søk, `raw_token` for visning/diagnostikk.

