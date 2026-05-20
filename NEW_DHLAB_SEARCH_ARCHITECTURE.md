# New DHLab Search Architecture

> Status: Task-specific reference
>
> This note summarizes the emerging search architecture for the newer
> postings-based DHLab runtime. It is not a line-by-line implementation spec,
> but a compact architectural model for how identity, corpora, metadata,
> locality layers, and count/query behavior fit together.

## Purpose

Describe the architectural direction for the newer DHLab search runtime so that
future implementation work follows one consistent model instead of mixing:

- external metadata identifiers
- runtime document identity
- corpus construction
- document/block/position query layers
- exact and approximate count behavior

## Scope

In scope:

- canonical runtime identity
- corpus representation
- metadata decoupling
- layered query model from document presence to exact positional search
- fast count modes for interactive exploration
- sampling for very large candidate sets

Out of scope:

- detailed geo contracts
- legacy Dash-era runtime assumptions
- exact frontend product decisions

## Architectural summary

The newer DHLab search runtime should be understood as a postings-driven query
engine operating on one canonical document identity:

- runtime identity = `dhlabid`
- runtime corpus = `bitmap(dhlabid)`

Everything else, including `URN`, `mmsid`, bibliographic metadata, category
systems, and curated corpora, is best treated as an upstream producer of
document sets over `dhlabid`.

## 1) Identity model

### External identifiers

Examples:

- `URN`
- `mmsid`
- bibliographic or collection-specific ids

These are valid inputs for corpus definition and metadata lookup, but they are
not the canonical runtime query identity.

### Canonical runtime identity

The canonical runtime identity is:

- `dhlabid`

`dhlabid` identifies a concrete text-layer document version, typically a
specific OCR or re-OCR realization.

### Mapping principle

External identities must be translated into `dhlabid` before entering the
runtime query flow.

Minimal mapping tables can look like:

- `t(dhlabid, mmsid)`
- `t(dhlabid, urn)`

This mapping layer may live outside the query engine.

## 2) Corpus model

At runtime, a corpus is represented as:

- `bitmap(dhlabid)`

This is the central abstraction.

Once a corpus has been materialized as a document bitmap, the runtime query
engine does not need to know whether it originally came from:

- a metadata join
- a bibliography workflow
- a `URN` list
- a `mmsid` list
- a user-curated document set

## 3) Metadata model

Metadata should not be treated as a required runtime query substrate.

Instead, metadata should ideally act as a producer of document sets:

- date filters produce `bitmap(dhlabid)`
- genre/category filters produce `bitmap(dhlabid)`
- bibliography-based corpora produce `bitmap(dhlabid)`
- user-defined corpora produce `bitmap(dhlabid)`

This means the metadata layer can evolve independently of the search engine, as
long as it can produce document membership over `dhlabid`.

## 3.1) Corpus bitmaps as interoperability layer

Corpus bitmaps should also be understood as a bridge to other systems.

The key idea is:

- DHLab owns corpus construction
- other systems may consume corpora as document filters

This makes it possible to keep a specialized DHLab query engine for:

- corpus building
- near / KWIC / counting
- sampling
- annotation-oriented analysis

while still letting a broader search system such as Elastic or Nettbiblioteket
consume the resulting corpus definition.

Typical flow:

1. a user builds a corpus in DHLab
2. DHLab materializes the corpus as `bitmap(dhlabid)`
3. the corpus is mapped into the identifier space needed by the downstream
   search system
4. that external system uses the mapped set as a filter over its own search
   engine

This means the runtime corpus abstraction is useful both:

- inside DHLab, as the native query filter model
- outside DHLab, as an interoperability contract for downstream search engines

This remains true even when downstream systems do not share the exact same
tokenization or internal indexing model. The systems do not need one identical
query engine as long as they can meet on corpus membership and identifier
mapping.

The architectural principle is therefore:

- DHLab should own corpus logic
- external engines do not need to reproduce DHLab corpus logic if they can
  consume corpus-defined document filters

## 4) Query layers

The search engine has, or is moving toward, a layered model:

- `docpost`
  - term occurs somewhere in a document
- `blockpost`
  - proposed intermediate layer: term occurs in one or more local blocks in a
    document
- `post`
  - exact sequence positions

This gives a resolution ladder:

1. same document
2. same local region / same block
3. exact positional near or sequence

## 5) Block-level locality

For many exploratory tasks, document-level co-occurrence is too loose and exact
positional `near` is more expensive than necessary.

The proposed intermediate solution is a block-level locality layer:

- partition each document into deterministic local blocks
- for each `(cf_id, doc_id)` store the local `block_id` values where the term
  occurs
- use shared block overlap as a coarse "same area" statistic or prefilter

Recommended first prototype:

- fixed token blocks rather than page-based blocks

The core idea is:

- `docpost` answers "same document"
- `blockpost` answers "same local area"
- `post` answers exact positional queries

## 6) Count semantics

`mode=count` should be understood as a semantic instruction to the backend, not
just as "the same query without rendering".

The backend may choose different internal count paths depending on what kind of
query is being counted.

### Pairwise fast path

For simple pairwise unigram near queries, the backend now supports a fast
popcount-style count path.

This count is not the same as the earlier anchor-based grouped count. It is
better understood as a partner-token count:

- count matching partner tokens within the requested window

This gives much better interactive performance for normal pairwise count
queries.

### Multi-group queries

For `N > 2`, exact grouped semantics are harder to express with the same simple
bitmap logic. Therefore:

- pairwise fast paths are valuable and safe in their narrow scope
- multi-group queries still need the more general exact grouped logic unless a
  separate, explicitly looser metric is introduced

## 7) Sampling strategy

For exploratory workloads, the practical target is:

- run exact counting when candidate document sets are small enough
- switch to document-level sampling when candidate sets become very large

Suggested policy:

- use full exact counting up to a practical threshold
- above that threshold, sample a manageable number of candidate documents
- run the same count logic on the sample
- scale up the estimate
- attach an explicit error bar / uncertainty interval

This keeps the system fast for broad exploratory tasks without pretending that
every large count must always be exact.

## 8) Why this architecture matters

This model gives several benefits at once:

- the query engine stays specialized around postings and set algebra
- metadata logic becomes decoupled from runtime query execution
- corpora can be created outside the runtime and handed in as document bitmaps
- document/block/position layers provide a clear precision ladder
- fast pairwise count paths become possible without forcing every query into the
  same semantics
- large exploratory workloads can move toward controlled estimation rather than
  brute-force exact counting in every case

## 9) Practical examples

### A) Bibliography-driven corpus

1. user defines a corpus via bibliography or `mmsid`
2. mapping layer resolves into `dhlabid`
3. system materializes `bitmap(dhlabid)`
4. runtime queries operate on that bitmap

### B) Scanned-material workflow

1. user starts from `URN`
2. mapping layer resolves `URN -> dhlabid`
3. corpus becomes `bitmap(dhlabid)`
4. runtime uses the same search path as any other corpus

### C) Time-series / day plots for newspapers

Instead of repeated metadata joins in runtime:

- precompute day/category/corpus bitmaps
- intersect those document sets with query-specific sets
- run count / KWIC / near only on the resulting corpus bitmap

This shifts the system away from repeated SQL join logic and toward reusable set
algebra.

## 10) Core principles

- `dhlabid` is the only canonical runtime corpus identity
- a runtime corpus is `bitmap(dhlabid)`
- external ids are mapping inputs, not first-class runtime query identities
- metadata should preferably produce document bitmaps instead of participating
  in runtime query joins
- `docpost`, `blockpost`, and `post` define a precision ladder from coarse to
  exact
- count paths may differ internally as long as their semantics are explicit

## 11) Implementation areas

Likely files and areas affected by this architecture:

- `api_python/server.py`
- `api_python/postings_queries.py`
- `postings.c`
- shard rebuild scripts and index materialization tools
- external metadata/corpus-building modules that produce `bitmap(dhlabid)`

## 12) Related docs

- `AGENTS.md`
- `README.md`
- `DOCUMENTATION_STRUCTURE.md`
- `CONTRACT_TO_CODE_MAP.md`
- `BLOCKPOST_INDEX_NOTE.md`
- `RUNTIME_IDENTITY_LADDER.md`

## Historical notes

This note captures a shift away from:

- embedding rich metadata joins directly in the runtime query path
- treating multiple external identifiers as if they were equal runtime document
  identities
- forcing one generic count path to serve all interactive search cases equally

The newer direction is:

- one runtime document identity
- one corpus abstraction
- layered query precision
- explicit count semantics
- metadata and corpus building outside the core query engine wherever possible
