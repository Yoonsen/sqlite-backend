# Query and Corpus Model

This note describes the intended long-term model behind the shard-based
`sqlite-backend` runtime.

It is not only a storage description. It states what should remain stable when
the system grows from a few shards to hundreds of shards.

## 1) Main idea

The system is built around three primary objects:

- a **token stream** per text layer
- a **corpus** as a bitmap/set of `dhlabid`
- a **query plan** that is bitmap-first and positions-second

This model is meant to replace FTS5 for positional search and near search while
also supporting:

- annotation layers such as geo
- user-defined corpus filtering
- efficient sampling
- shard-parallel execution

## 2) Stable document identity

The stable document identity in the runtime is `dhlabid`.

Properties:

- `dhlabid` is globally unique for a text layer
- a corpus is therefore a set or bitmap of `dhlabid`
- shard routing, filtering, and aggregation happen over `dhlabid`

`URN` is also globally meaningful, but it plays a different role:

- NB `URN` is tied to the scanned/image-linked publication identity
- OCR/ALTO text layers do not come with an independent PID from source
- the system therefore assigns each text layer its own `dhlabid`
- when a text layer is regenerated or materially replaced, the effective text
  identity should be treated as a new layer/version

Operationally:

- `dhlabid` is the runtime anchor for search, filtering, and annotations
- `URN` is descriptive metadata and provenance, not the positional primary key

## 3) Canonical text structure

The canonical structure is a positional token stream:

- `(dhlabid, seq, token)`

Where:

- `dhlabid` identifies the text layer
- `seq` identifies token position inside that text layer
- `token` is the tokenized surface unit used by search and annotations

This means:

- text is not treated as one monolithic string in the query engine
- the truth layer is the token stream
- rendering is reconstructed from the token stream

## 4) Tokenization contract

The tokenizer may split OCR/ALTO units into multiple runtime tokens.

Example:

- OCR token: `spise.`
- runtime tokens: `spise`, `.`

This is acceptable and desirable as long as it is deterministic.

Requirements:

1. tokenization must be versioned
2. `seq` must be stable for a given tokenizer version
3. all shards in the same deployed corpus family must use the same tokenization
   contract
4. punctuation splitting must be deterministic

Whitespace does not need to exist as an explicit runtime token.

For search and annotations, the important structure is token order, not the
original spacing characters.

## 5) OCR / ALTO provenance

Even when tokenization is canonicalized for search, provenance back to OCR
should remain possible.

Recommended principle:

- the runtime search model is token-based
- OCR/ALTO provenance is attached as metadata, not used as the primary query key

That means the system may later keep mappings such as:

- OCR token id
- original OCR string
- split-part index/count
- page/line/region coordinates

But the query engine should still operate on stable runtime `seq`.

## 6) Corpus model

A corpus is a collection of `dhlabid`.

In runtime terms, the natural representation is:

- `object -> roaring bitmap of dhlabid`

Examples:

- a user-selected corpus
- a metadata-defined corpus
- a saved result set
- an annotation-defined subset

This is a first-class object in the model, not a secondary filter.

Consequences:

- users can build or refine corpora locally
- corpora can be sent to the server as bitmap/filter payloads
- each shard can intersect its local document universe with the incoming corpus
- the server can skip irrelevant shards early

## 7) Query plan

The intended query plan is:

1. receive term groups and optional corpus/filter bitmap
2. intersect corpus bitmap with shard-local document universe
3. resolve terms to per-shard ids
4. build per-group document candidate sets
5. intersect candidate sets
6. decide whether to sample or run full
7. only then do positional work (`seq` / near / fragments)

Short version:

- bitmap/doc filtering first
- positional work second

This is the key difference from generic fulltext engines where corpus semantics
and positional semantics are often less explicit.

## 8) Sampling rules

Sampling is a core feature, not an afterthought.

The design goal is that sampling should be explicit and query-safe.

Preferred rule:

- sample after shard/doc narrowing
- do not sample so early that true positional hits disappear before near logic

In practice:

- single-term queries may sample candidate books after docpost intersection
- near queries should sample after the near-capable candidate set is known
- rendering should happen after the sampled positional result is selected

This keeps sampling statistically useful while preserving correctness for near
search.

## 9) Annotation compatibility

Annotations must live in the same coordinate system as text.

That means the stable anchor for annotations is:

- `(dhlabid, seq_start, token_len)`

Example:

- geo annotation: `(dhlabid, seq_start, geonames_id, token_len)`

As long as:

- `dhlabid` remains stable
- `seq` remains stable
- tokenization remains stable

then annotation layers survive shard rebuilds and index rebuilds.

This is one of the main reasons to prefer a token-based positional model over a
character-offset primary model.

## 10) Shard contract

Before scaling to hundreds of shards, each shard family should make the
following explicit in metadata:

- tokenizer version
- token id regime used in fragment rendering
- words source / words version
- postings codec
- whether sidecar token storage uses `raw_id`, `cf_id`, or another id class
- corpus universe / shard doc count

A shard should be considered invalid if its token rendering layer and words
layer do not agree on the id regime.

## 11) Global vs local vocabulary

The model should distinguish between:

- the **full local vocabulary** inside each shard
- the **global vocabulary layer** used for cross-shard alignment

These are not the same thing.

### Local shard vocabulary

Each shard may keep its full local `words` table, including:

- low-frequency terms
- OCR noise
- rare spellings
- shard-local forms that are not useful globally

This local vocabulary remains important because:

- rendering depends on local token lookup
- direct shard search should still be able to find rare forms
- not every valid search term should be forced through a global catalog

### Global vocabulary layer

The global layer is a curated cross-shard alignment layer.

Only a subset of shard-local words should be admitted into it, for example by:

- minimum `docfreq`
- minimum `total_tf`
- top-N or top-percent retention per shard

The purpose is to avoid letting OCR noise and ultra-rare local forms dominate
the global term space.

This global layer is the natural basis for:

- cross-shard DTM building
- corpus comparison
- shard federation
- global frequency statistics
- cross-shard query planning when a shared term id is useful

### Practical rule

The intended rule is:

- all words may exist locally
- only selected words are promoted globally

That means:

- a term can be absent from the global vocabulary and still be searchable in a
  shard
- global absence does not mean local non-existence
- cross-shard analytics and local deep search do not need to use the same term
  universe

### Recommended ID split

The model already points toward three levels:

- local shard ids (`cf_id`, `raw_id`)
- global casefold/global-term ids (`global_cf_id`)
- global raw-form ids (`global_raw_id`)

Recommended usage:

- local search runtime: local shard ids
- cross-shard DTM/default global term space: `global_cf_id`
- raw-form-sensitive aggregation or rendering-oriented alignment: `global_raw_id`

For backward compatibility, `global_id` may continue to alias `global_cf_id`,
but the semantic distinction between folded term identity and raw-form identity
should remain explicit.

## 12) What must stay stable

The long-term contract should treat these as stable:

- `dhlabid`
- token order
- `seq`
- tokenization version per shard family
- corpus bitmap semantics over `dhlabid`
- annotation coordinates over `(dhlabid, seq_start, token_len)`

Everything else can be rebuilt if needed:

- postings representation
- sidecar storage layout
- shard count
- local word ids
- optional global ids

## 13) Practical summary

The intended architecture is:

- **text** = token stream
- **corpus** = bitmap over `dhlabid`
- **query** = bitmap-first, positions-second
- **annotations** = extra layers over the same positional structure

This gives:

- fast near search
- explicit corpus algebra
- efficient sampling
- shard-parallel execution
- annotation compatibility

and is the right basis for scaling from a small shard set to very large shard
families.
