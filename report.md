## Report: Design and tradeoffs

### Summary

This backend uses SQLite with postings blobs and custom C UDFs for fast concordance and near searches. The core idea is:

- Use **doc-level postings** to reduce the candidate document set.
- Use **positional postings** per document for near and fragment extraction.
- **Sample late** (after near) when returning fragments.
- Prefer **simple, fast paths** for high-frequency function-word queries.

### Data model (high level)

- `tokens`: positional tokens per document (by `book_id`, `seq`).
- `unigrams`: postings per (`book_id`, `cf_id`) with delta/varint blobs.
- `words`: vocabulary with `cf_id`, doc frequency, and docpost blob.
- `urns`: global list of document IDs (corpus).

Two key postings types:

- **Docpost**: list of document IDs for each term, used for fast doc filtering.
- **Pospost**: list of token positions per document, used for near and fragments.

### Query flow

1) **Resolve terms** to `cf_id` groups (supports `*` and explicit OR groups).
2) **Docpost intersection** across groups to find candidate documents.
3) **Near computation** on positional postings, per document.
4) **Sampling** happens after near; fragments are generated from sampled positions.

This keeps the “needle” by not sampling away hits before near.

### Term groups (CNF)

We support explicit OR groups using `termGroups`:

```json
{ "termGroups": [["spise","spiser"], ["middag"]] }
```

Each inner list is an OR-group; all groups are AND-ed/near-ed.

### Performance notes

- **High-frequency terms**: docpost filtering + sampling helps; full scans are expensive.
- **Bitmap near** (optional): bitmap-based near for 2 groups (and 3-group fragments) avoids building union blobs and can be faster for small/medium subcorpora.
- **Union cost**: OR unions per book are expensive for large candidate sets; avoid large `termGroups` when possible, or reduce the doc set.

### Portability

The approach can be ported to other SQL engines that support:

- BLOB storage
- UDFs/extensions
- Efficient primary key lookups

### SQLite schema (current)

#### Postings shard DB (tokens + unigrams + urns)

```sql
CREATE TABLE tokens (
    book_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    cf_id INTEGER NOT NULL,
    raw_id INTEGER NOT NULL,
    para INTEGER,
    page INTEGER,
    PRIMARY KEY (book_id, seq)
) WITHOUT ROWID;

CREATE TABLE unigrams (
    cf_id INTEGER NOT NULL,
    book_id INTEGER NOT NULL,
    tf INTEGER NOT NULL,
    post BLOB NOT NULL,
    PRIMARY KEY (cf_id, book_id)
) WITHOUT ROWID;

CREATE INDEX unigrams_book_id_cf_id ON unigrams(book_id, cf_id);

CREATE TABLE urns (
    book_id INTEGER NOT NULL PRIMARY KEY
) WITHOUT ROWID;
```

#### Words index DB (words + docpost + corpus)

```sql
CREATE TABLE words (
    word TEXT NOT NULL PRIMARY KEY,
    raw_id INTEGER NOT NULL UNIQUE,
    cf_id INTEGER NOT NULL,
    docfreq INTEGER DEFAULT 0,
    total_tf INTEGER DEFAULT 0,
    docpost BLOB,
    docpost_is_complement INTEGER DEFAULT 0
) WITHOUT ROWID;

CREATE INDEX words_cf_id ON words(cf_id);

CREATE TABLE urns (
    book_id INTEGER NOT NULL PRIMARY KEY
) WITHOUT ROWID;

CREATE TABLE urns_postings (
    id INTEGER NOT NULL PRIMARY KEY,
    post BLOB NOT NULL
) WITHOUT ROWID;

CREATE TABLE meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
```
