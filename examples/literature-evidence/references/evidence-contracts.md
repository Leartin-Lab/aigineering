# Literature evidence contracts

These contracts are intentionally database-neutral. JSON documents should be
serialized with sorted keys when reproducible byte identity matters.

## `literature_query`

Required fields: `question`, `search_expression`, `databases`,
`inclusion_criteria`, `exclusion_criteria`, `max_records`, and
`retrieval_date`. Optional limits must be explicit rather than inferred.

## `retrieval_manifest`

Required fields:

- `schema_version`: `literature-retrieval-v1`;
- `source`, `endpoint`, `query`, `filters`, and `retrieved_at`;
- `source_count`, `returned_count`, `truncated`, and `warnings`;
- `records`, each with `id`, `title`, `publication_year`, `type`, `doi`,
  `landing_page`, and `cited_by_count`.

Stable source IDs, not titles, identify records. An HTTP 200 response is not a
success unless the documented response shape and record fields validate.

## `screening_decisions`

Include the retrieval manifest content hash and exactly one entry per record:
`id`, `decision` (`include`, `exclude`, or `uncertain`), `reason`, and
`reviewer`. Report missing abstracts or metadata as uncertainty, not exclusion
by convenience.

## `evidence_cards`

Each card contains `source_id`, `claim`, `source_support`, `locator`,
`study_type`, `population_or_data`, `limitations`, and `worker_inference`.
Keep quoted source text short and distinguish it from interpretation.

## `literature_synthesis`

Include `question`, `answer`, `supporting_source_ids`, `conflicts`,
`coverage_limitations`, `uncertainty`, and a claim-to-source mapping. A
synthesis is a proposed result until an independent attestation binds to its
exact Asset ID.

## Acceptance checks

The verifier checks exact source IDs and locators, unsupported claims,
contradictory evidence, query coverage, and whether limitations change the
answer. It does not merely score prose quality or count citations.
