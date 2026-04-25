# Documentation Structure

This repository uses Markdown documents as part of the system contract, not
just as commentary around the code.

The goal is that a strong document should be precise enough that an experienced
developer can rebuild the relevant subsystem without guessing about the important
invariants.

## Status Labels

Every substantial design or operational document should make its status explicit
near the top.

Recommended labels:

- `Current source-of-truth`
  - use for canonical architecture, schema, and API contracts
- `Current operational guide`
  - use for active rebuild, deploy, or validation procedures
- `Task-specific reference`
  - use for narrow workflows such as disambiguation, migrations, or one-off tools
- `Historical note`
  - use for older plans, experiments, or superseded handoff notes

## Core Principle

Docs and code should describe the same system at different resolutions:

- docs describe purpose, invariants, allowed shapes, and operating rules
- code realizes one concrete implementation of those rules

When they diverge, the repo becomes harder to maintain and harder for a new
agent or developer to reconstruct.

## Standard Sections For Contract Docs

For architecture, schema, and API contract documents, prefer this structure:

1. `Purpose`
   - what problem the document governs
2. `Scope`
   - what is in and out of scope
3. `Invariants`
   - facts that must remain true across implementations
4. `Inputs`
   - accepted identifiers, payloads, source tables, files, or commands
5. `Outputs`
   - returned payloads, materialized tables, files, or side effects
6. `Storage / API Shape`
   - concrete fields, tables, indexes, or endpoint semantics
7. `Runtime Compatibility`
   - temporary adapters, legacy terms, accepted old forms
8. `Failure Modes`
   - common breakpoints, ambiguity rules, or known gotchas
9. `Validation`
   - how to confirm the contract is still being honored
10. `Implemented by`
   - the main files, scripts, and commands that realize the contract
11. `Related docs`
   - neighboring source-of-truth or operational docs
12. `Historical notes`
   - if relevant, what older model this replaced

Not every document needs every section, but canonical contracts should cover at
least `Purpose`, `Scope`, `Invariants`, `Inputs`, `Outputs`, and `Implemented by`.

## Standard Sections For Operational Docs

For rebuild, deploy, and maintenance runbooks, prefer:

1. `Purpose`
2. `Prerequisites`
3. `Inputs`
4. `Procedure`
5. `Outputs`
6. `Validation`
7. `Go / No-Go`
8. `Rollback or recovery`
9. `Implemented by`
10. `Related docs`

## Writing Rules

Prefer these habits when editing docs:

- use one canonical term per concept
- call out compatibility terms explicitly instead of mixing them with canonical terms
- prefer field names and table names exactly as they appear in code or SQL
- write examples using realistic payloads and identifiers
- separate architecture truth from runtime quirks
- separate current contract from project history
- make irreversible or risky operational steps obvious

## Contract Strength Test

A document is strong when a new contributor can answer these questions without
reading the whole codebase first:

- what must remain true?
- what shapes are allowed?
- what files or commands implement this?
- how do I validate that I did not break it?

If those answers are missing, the document is probably still explanatory, but
not yet reconstructive.

## Repository Usage

In this repo:

- `README.md` is the entry-point document map
- `AGENTS.md` is the onboarding and guardrail file for agents
- `CONTRACT_TO_CODE_MAP.md` links the main contract docs to implementation files

Use this file as the style guide when creating or refactoring future
architecture and operational documents.
