# Future Requirement: Keyword-based Corpus Building via API

To maintain a clean architectural separation between Metadata (geography) and Search (roaring bitmaps), the current `/api/corpus/build` endpoint does NOT directly access the postings databases.

## Requirement
Implement a mechanism where the Corpus Builder can dynamically filter a corpus by content keywords. 

## Proposed Solution
- **Decoupled Communication**: Instead of internal function calls, the metadata service should communicate with the fulltext search service via its public API endpoints (e.g., using a configurable `SEARCH_API_URL`).
- **Workflow**:
    1. The client (or the metadata service) performs a search request to the search API.
    2. The search API returns a list of matching Document IDs.
    3. These IDs are then passed to the `/api/corpus/build` endpoint via the `baseCorpus` field.
- **Benefits**:
    - Metadata and Search services can be hosted on different machines.
    - No shared code dependencies between the two domains.
    - Easier to scale and maintain independently.

## Status
Currently, the `contentKeywords` field in `CorpusBuildRequest` is a placeholder. Integration should be done using the `baseCorpus` intersection pattern.
