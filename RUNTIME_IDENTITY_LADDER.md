# Runtime Identity Ladder

> Status: Task-specific reference
>
> This note defines the identity layers used when moving from external
> bibliographic identifiers into the runtime query system.

## Purpose

Make the identity model explicit so that corpus building, metadata integration,
and runtime querying do not get mixed together.

## Scope

In scope:

- external identifiers such as `URN` and `mmsid`
- mapping into the canonical runtime identity
- corpus materialization for query use

Out of scope:

- detailed metadata schemas
- frontend-specific corpus UX
- geo-specific identity contracts

## Identity ladder

### 1) External identifiers

Examples:

- `URN`
- `mmsid`
- other bibliographic or collection-specific ids

These are valid input identities for corpus definition and metadata lookup, but
they are not the canonical runtime query identity.

### 2) Mapping layer

The mapping layer translates external identifiers into the runtime document
identity.

Minimal examples:

- `t(dhlabid, mmsid)`
- `t(dhlabid, urn)`

This layer may live outside the query engine. Its main job is to produce a
stable set of runtime document ids.

### 3) Canonical runtime identity

The canonical runtime document identity is:

- `dhlabid`

`dhlabid` identifies a concrete text-layer document version, typically a
specific OCR or re-OCR realization. It is the query engine's first-class
document identity.

### 4) Corpus representation

At runtime, a corpus is represented as:

- `bitmap(dhlabid)`

Once a corpus has been materialized as a bitmap over `dhlabid`, the query
engine does not need to know whether it originally came from `URN`, `mmsid`, a
metadata join, or a user-defined list.

### 5) Query layers

Queries then operate on `dhlabid`-based corpus filters through the existing or
proposed posting layers:

- `docpost`
- `blockpost` (proposed intermediate locality layer)
- `post`

## Core principle

External identifiers define or select corpora.

The query engine runs on `dhlabid`.

Therefore:

- external ids are mapping inputs
- `dhlabid` is the canonical runtime identity
- a corpus in the runtime system is a bitmap over `dhlabid`

## Why this matters

This separation has several benefits:

- metadata systems can evolve without changing the query core
- multiple external identity systems can coexist
- corpus building can happen outside the query engine
- runtime queries stay focused on postings and document sets rather than joins

## Practical consequences

1. National bibliography integration does not need to be a runtime query join.
   It can be an external producer of `bitmap(dhlabid)`.
2. The same applies to scanned-material workflows based on `URN`.
3. User-defined corpora can be created outside the query engine and handed in
   as `dhlabid` sets or bitmaps.
4. The query engine can remain optimized around set algebra and postings rather
   than metadata table logic.

## Invariants

- `dhlabid` is the only canonical runtime corpus identity
- external ids must map into `dhlabid` before entering runtime query flow
- corpus filters should be representable as `bitmap(dhlabid)`
- metadata tables are not required to be part of the runtime query plan

## Validation

This model is being followed when:

1. external ids such as `URN` and `mmsid` are translated before query
2. corpora can be represented as document sets over `dhlabid`
3. runtime count / KWIC / near logic consumes document sets rather than metadata
   joins

## Related docs

- `AGENTS.md`
- `README.md`
- `DOCUMENTATION_STRUCTURE.md`
- `BLOCKPOST_INDEX_NOTE.md`

## Historical notes

This note captures a shift away from embedding rich metadata tables directly in
the runtime query path. The new model keeps metadata useful, but treats it as a
producer of runtime document sets rather than as a first-class query substrate.
