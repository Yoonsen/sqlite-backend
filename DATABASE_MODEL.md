## Database model

This project uses a postings-centric model optimized for fast concordance, near, and sampling queries over large corpora.
The core idea is to keep **compact postings blobs** (delta/varint) in SQLite tables, and do the heavy list/near math in
SQLite C-extensions for cache-friendly processing. This is a **hybrid** approach: SQLite B-tree indexes handle term/doc
lookups, while CPU-efficient blob operations handle union/intersect/near in tight C loops (good L1/L2 locality).

### Conceptual model

Entities:

- **Corpus (urns)**: the global list of document IDs (`book_id`).
- **Tokens**: per-document positional tokens, keyed by `(book_id, seq)`.
- **Unigrams**: per-document postings blobs for a term (`cf_id`) inside each doc.
- **Words**: vocabulary table, mapping surface forms to `cf_id`, plus doc-level postings (`docpost`).
- **Docpost**: delta/varint blob of all `book_id` where a term appears (or its complement if `docpost_is_complement=1`).

### Query flow (current model)

1) **From term → doc list**  
   Resolve term(s) into `cf_id` list(s). For each term (or OR-group), build a **docpost** blob and intersect across groups.  
   Intersect with the **corpus list** (`urns`) if needed.

2) **Single term**  
   If the term is high-frequency, we **sample doc IDs** after the docpost intersection.  
   Then look up `unigrams` per `book_id` and return either:
   - **counts** (number of positions), or
   - **(book_id, seq)** list, then render fragments.

3) **Multiple terms / near**  
   Build docpost intersection across all term groups, then run **near** on positional postings in each document.  
   **Sampling happens after near**, on the resulting near-position blob.  
   Finally produce fragments or counts.

This ensures that sampling never “cuts away the needle” before we compute near hits.

### CNF term groups (OR groups)

We support explicit OR groups as a **CNF-like** structure:

```json
{
  "termGroups": [["spise","spiser","spiste"], ["middag"]]
}
```

Each inner list is an **OR-group**. All groups are AND-ed/near-ed across documents.

### Hybrid postings approach

- **Compact blobs**: postings stored as delta/varint in BLOBs for minimal I/O.
- **C UDFs**: `post_union_agg`, `post_intersect_blob`, `post_near_positions_blob`, `post_count`, `post_sample`.
- **CPU cache friendly**: sequential decoding in C loops favors L1/L2 cache locality.
- **Optional bitmap path**: can be used for very high-frequency counting, but positional retrieval still requires postings.

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

Note: In some builds (words-index DB) `unigrams` uses `PRIMARY KEY (book_id, cf_id)`. Both are supported, but
queries typically join by `book_id`, so the `(book_id, cf_id)` index is important either way.

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

### Portability to other SQL engines

The model is portable as long as the engine supports:

- BLOB storage
- UDFs (or equivalent extension mechanism)
- Efficient ordered key/value access

The same approach can be implemented in other SQL engines with a similar extension mechanism for postings operations.
