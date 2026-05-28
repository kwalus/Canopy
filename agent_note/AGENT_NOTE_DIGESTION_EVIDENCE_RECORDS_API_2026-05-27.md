# Agent Note: Digestion Evidence Records API and Agent Tooling

Date: 2026-05-27
Branch: `codex/digestion-evidence-records-0.6.254`
Version bump: `0.6.254`

## Purpose

Implemented priorities 1 and 5 from the Digestion collaboration plan:

1. Add a generic, durable Evidence Records layer inside Digestions.
2. Expose that layer through REST, MCP, and agent instructions so agents can preserve, search, and critically review source-backed work product.

This is intentionally **not aviation-specific**. It is a generic truth-maintenance layer for research, operations, business-development, safety, legal, engineering, or other lane-specific Digestions. Domain-specific extraction can still be layered through structured records or agent conventions, but this patch gives every Digestion a reusable backbone for durable claims, findings, risks, decisions, requirements, challenges, confirmations, and supersession history.

## Backend Changes

### New schema

Added two local tables created by `DigestionManager._ensure_tables()`:

- `digestion_evidence_records`
- `digestion_evidence_reviews`

Evidence records store:

- `record_kind`
- `statement`
- `summary`
- `scope`
- `status`: `candidate`, `stable`, `contested`, `needs_source`, `stale`, `superseded`
- `priority`: `low`, `normal`, `high`, `critical`
- `confidence`
- `tags_json`
- `evidence_refs_json`
- `source_refs_json`
- `related_ids_json`
- `metadata_json`
- `superseded_by_id`

Evidence reviews store append-only review events:

- `action`: `support`, `challenge`, `refine`, `supersede`, `mark_stale`, `request_source`, `confirm`
- `note`
- `confidence`
- `evidence_refs_json`
- reviewer identity

### New manager APIs

Added to `canopy/core/digestions.py`:

- `append_evidence_records(...)`
- `list_evidence_records(...)`
- `search_evidence_records(...)`
- `review_evidence_record(...)`

Behavior:

- Append/review requires Digestion manage access.
- List/search requires Digestion query access.
- Review actions update record status conservatively:
  - `challenge` -> `contested`
  - `request_source` -> `needs_source`
  - `mark_stale` -> `stale`
  - `supersede` -> `superseded`
  - `confirm` -> `stable`
  - `support`/`refine` preserve status unless explicit status is supplied.
- Records include `review_summary` so UI/API consumers can quickly see support/challenge/confirm counts.
- Stats now include:
  - `evidence_record_count`
  - `contested_evidence_count`
  - `needs_source_evidence_count`
  - `stable_evidence_count`
  - `evidence_by_status`

## REST API

Added routes in `canopy/api/routes.py`:

- `GET /api/v1/digestions/<digestion_id>/evidence`
- `POST /api/v1/digestions/<digestion_id>/evidence`
- `POST /api/v1/digestions/<digestion_id>/evidence/search`
- `POST /api/v1/digestions/<digestion_id>/evidence/<evidence_id>/reviews`

Representative append body:

```json
{
  "records": [
    {
      "record_kind": "finding",
      "statement": "One durable source-grounded assertion or decision.",
      "summary": "Why it matters and what evidence supports it.",
      "status": "candidate",
      "priority": "normal",
      "confidence": 0.75,
      "tags": ["topic", "review-needed"],
      "evidence_refs": [
        {
          "file_id": "<vault_or_source_file_id>",
          "file_name": "<source name>",
          "page_label": "p. 3",
          "chunk_id": "<chunk_id>",
          "quote": "short supporting quote"
        }
      ]
    }
  ]
}
```

Representative review body:

```json
{
  "action": "challenge",
  "note": "This conclusion needs a better baseline source before being treated as stable.",
  "confidence": 0.7,
  "evidence_refs": [{"quote": "baseline not stated"}]
}
```

## MCP Tooling

Added `canopy_digest_evidence` to `canopy/mcp/server.py`.

Actions:

- `list`
- `search`
- `append`
- `review`

Permission behavior:

- `list` / `search`: `read_files`
- `append` / `review`: `write_files`

This is now included in the MCP instructions and the `mcp_tools` registry string.

## Agent Instructions and Docs

Updated:

- `canopy/api/agent_instructions_data.py`
- `docs/API_REFERENCE.md`
- `docs/AGENT_ONBOARDING.md`

Instructional framing added:

- Use evidence records when a conclusion should survive chat noise.
- Search evidence records before relying on prior conclusions.
- Use review actions to challenge, refine, confirm, request source, mark stale, or supersede records instead of burying contradictions in ordinary posts.

## Tests

Added direct manager tests in `tests/test_digestions.py`:

- Evidence records append/search/review/status transitions.
- Query-only readers can list/search but cannot append/review.
- Managers can review evidence.

Extended `tests/test_frontend_regressions.py::test_digestion_agent_workflow_mcp_completeness` to guard REST/MCP/instruction surface completeness.

Commands run:

```bash
python -m py_compile canopy/core/digestions.py canopy/api/routes.py canopy/mcp/server.py canopy/api/agent_instructions_data.py
pytest -q tests/test_digestions.py -k evidence
pytest -q tests/test_digestions.py::TestDigestions::test_local_hash_digestion_builds_and_queries_owned_vault_files tests/test_frontend_regressions.py -k digestion_agent_workflow_mcp_completeness
pytest -q tests/test_digestions.py
pytest -q tests/test_frontend_regressions.py -k "digestion_agent_workflow_mcp_completeness or digestion"
git diff --check
```

Results:

- `tests/test_digestions.py`: 50 passed.
- Digestion workflow regression subset: passed.
- Py compile: passed.
- Diff check: passed.

## Deliberate Scope Boundary

This patch does **not** add the full Evidence Records UI yet. It gives UI and agent work a stable substrate first.

Likely next UI work:

- Evidence tab/panel in Digestion reader/deck.
- High-priority/contested evidence badge on Digestion cards.
- Evidence record cards with support/challenge/confirm chips.
- Owner review queue filtered by `contested` and `needs_source`.
- One-click promote from query result/datapoint/figure/contribution to evidence record.

## Risk Notes

- Evidence refs are stored as bounded JSON metadata. They are not raw file bytes and do not bypass Vault permissions.
- Query-access users can read evidence records because the point is to expose durable, reviewable derived work product. Append/review remains manager-only.
- This adds tables but does not migrate or rewrite existing Digestion data.
- Existing structured records remain available for strongly profiled extraction, but evidence records should be used for generic truth-maintenance and critical review.
