# Shard Validation Checklist

This checklist defines what must be true before a shard, or a shard family, is
considered safe to admit into the shard park.

The goal is not only to detect obvious corruption. It is to prevent subtle
runtime mismatches before the system scales to hundreds of shards.

Use this together with:

- `QUERY_CORPUS_MODEL.md`
- `validate_shards.py`

## 1) Identity and corpus contract

Every shard must satisfy the basic document identity contract:

- `dhlabid` is the stable document id used at runtime
- `urns.book_id` is the shard-local corpus universe
- the shard doc universe is compatible with the deployed corpus/filter model

Checks:

- `urns` exists
- `urns.book_id` is unique
- shard metadata records expected document count
- all annotation/query layers use the same `dhlabid` space

Fail the shard if:

- `book_id`/`dhlabid` identity is ambiguous
- shard document universe is inconsistent with shard metadata

## 2) Required tables

For a main shard DB, the following must exist:

- `words`
- `unigrams`
- `urns`
- `meta`

For a sidecar shard DB used for rendering, the following must exist:

- `token_blocks`
- `meta`

Fail the shard if any required table is missing.

## 3) Token and postings consistency

The positional model depends on a stable token stream.

Checks:

- `unigrams.cf_id` must resolve to `words.cf_id`
- if `tokens` exists, `tokens.raw_id` must resolve to `words.raw_id`
- if `token_blocks` is used, the token id regime stored in `token_blocks.raw_ids`
  must match the runtime lookup path

Minimum SQL-style checks:

- every distinct `unigrams.cf_id` exists in `words.cf_id`
- every distinct `tokens.raw_id` exists in `words.raw_id`

Fail the shard if:

- postings point to missing `cf_id`
- token stream points to ids that cannot be rendered through the shard `words`
  table

## 4) Sidecar rendering consistency

This check is critical because runtime fragment rendering depends on it.

Each sidecar shard must declare:

- source shard path
- token storage format
- token block size
- token id regime

Checks:

- `sidecar.meta.source_db` matches the intended main shard family
- `token_blocks` can be decoded with the expected codec
- sampled windows from `token_blocks` resolve to real tokens in `words`
- runtime fragment extraction produces plausible text, not large runs of `?`

Recommended validation:

1. pick a sample of books from each shard
2. sample random windows or known hit positions
3. decode `token_blocks`
4. resolve ids through shard `words`
5. measure missing-token ratio

Fail the shard if:

- sidecar was built from an incompatible shard family
- decoded token ids systematically fail lookup in `words`
- fragment windows contain widespread unresolved tokens

## 5) Tokenization contract

All shards in one deployed shard family must share the same tokenization
contract.

Checks:

- tokenizer version is recorded in shard metadata
- punctuation splitting rules are the same across shards
- `seq` is stable within the declared tokenizer version

Fail the shard family if:

- some shards use a different tokenizer version without explicit migration
- `seq` semantics differ between shards

## 6) Words table sanity

The `words` table is not only a lexicon; it is the bridge between search and
rendering.

Checks:

- `word` is present
- `cf_id` is present
- `raw_id` is present where rendering depends on raw token recovery
- one `cf_id` should not map to multiple unrelated casefold families

Useful checks already reflected in `validate_shards.py`:

- `cf_id` must not span multiple `lower(word)` families
- `global_id` presence can be checked when global vocabulary is enabled

Fail the shard if:

- the same `cf_id` points to multiple incompatible folded forms
- required id columns are missing for the runtime mode in use

## 7) Global vocabulary sync

If the shard participates in the shard park/global vocabulary:

- shard `words` must carry the expected global id columns
- global ids must resolve in the configured master/global DB
- folded/global identity must agree with the shard word form contract

Checks:

- `global_id` exists when global sync is expected
- `global_id` is non-null for admitted rows
- global ids exist in the master/global words DB
- shard words and master words match at the intended normalization level

Fail the shard if:

- required global ids are missing
- shard/global vocabulary mapping is inconsistent

Important note:

- not every local shard word needs to be global
- but every word that is promoted into the global layer must validate cleanly

## 8) Metadata contract

Each shard family should expose enough metadata to diagnose mismatches quickly.

Recommended metadata fields:

- tokenizer version
- postings codec
- shard family/version
- source build path
- token id regime
- token storage mode
- document count
- token count
- words source/version

Fail the shard family if metadata is too incomplete to determine compatibility.

## 9) Query-path smoke tests

A shard should pass lightweight runtime smoke tests before production admission.

Recommended tests:

- term lookup on common words
- OR query over a filtered corpus
- near query over a filtered corpus
- fragment rendering on sampled hit positions
- annotation-backed query if annotation layers are enabled

Fail the shard if:

- postings work but fragment rendering is broken
- filtered corpus routing returns structurally inconsistent results
- annotation coordinates work but text windows do not

## 10) Annotation compatibility

Annotation layers must remain valid across shard rebuilds.

Checks:

- annotation coordinates use `(dhlabid, seq_start, token_len)`
- shard tokenization matches the annotation family
- sampled annotation positions render the expected token window

Fail the shard family if:

- annotation coordinates no longer align with the token stream
- tokenizer changes invalidate `seq`

## 11) Minimum gate for shard admission

A shard should not be admitted into the shard park unless all of these are true:

1. required tables exist
2. `unigrams.cf_id -> words.cf_id` is clean
3. token rendering ids resolve correctly through `words`
4. sidecar source and id regime match the main shard
5. tokenizer/version metadata is present
6. if global vocabulary is enabled, global id sync validates
7. sample query and fragment smoke tests pass

## 12) Suggested workflow

For each new shard or shard rebuild:

1. run structural validation
2. run id consistency checks
3. run sidecar rendering checks
4. run optional global vocabulary checks
5. run query-path smoke tests
6. only then admit the shard into production/shard park

## 13) Existing tool support

Current repo support already covers part of this:

- `validate_shards.py`
  - required table checks
  - `unigrams.cf_id -> words.cf_id`
  - optional `tokens.raw_id -> words.raw_id`
  - optional global-id validation
- `ingest_shard_global_words.py`
  - shard promotion into the global vocabulary layer

What should be expanded over time:

- explicit sidecar/token-block validation
- sampled fragment rendering validation
- tokenizer/version validation across shard families
- shard-family admission reports
