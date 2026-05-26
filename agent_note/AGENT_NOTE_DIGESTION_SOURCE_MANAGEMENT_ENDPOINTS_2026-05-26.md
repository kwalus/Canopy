# Agent Note - Digestion Source Management Endpoints

Date: 2026-05-26
Branch: `codex/digestion-agent-management-0.6.239`
Version bump: `0.6.238` -> `0.6.239`

## Context reviewed

I reviewed recent activity on the Agonic VPS in `#MCP-hardware` using the Codex Agent API key. The relevant pattern is now clear: Digestions are being used as living collaborative corpora, not one-shot search indexes. In particular, agents created and iterated a TDS 2012 Automation Reference Library Digestion (`Dg9481b28c166788391c063964`), appended missing manuals, rebuilt the corpus, and then used the result to support MCP hardware / SCPI adapter work.

That usage exposes a real endpoint gap. Agents can create, add to, build, query, append contributions, and transfer Digestions, but until this patch they did not have a clean, documented source lifecycle API for routine corpus maintenance: remove stale/wrong sources, replace material, and update source-facing labels/URIs/metadata.

## What changed

### Core Digestion manager

Updated `canopy/core/digestions.py` with three manager operations:

- `remove_sources(digestion_id, actor_user_id, source_file_ids)`
- `replace_sources(digestion_id, actor_user_id, remove_file_ids, add_file_ids, materials, build_after)`
- `update_source_metadata(digestion_id, actor_user_id, file_id, source_label, source_uri, source_metadata, merge_metadata)`

Removal is intentionally non-destructive:

- Detaches the source from the Digestion.
- Preserves the underlying Vault file.
- Preserves contribution ledger/audit history.
- Invalidates derived chunks, extracted figures, visual-evidence rows, and generated Digestion outputs that may contain stale snippets or datapoints.
- Marks the Digestion back to `draft` until rebuilt.

This keeps owner data safe while preventing removed source content from continuing to influence future query/output surfaces.

### REST API

Updated `canopy/api/routes.py` with source lifecycle endpoints:

- `POST /api/v1/digestions/<digestion_id>/sources/remove`
- `POST /api/v1/digestions/<digestion_id>/sources/replace`
- `PATCH /api/v1/digestions/<digestion_id>/sources/<source_file_id>`
- `DELETE /api/v1/digestions/<digestion_id>/sources/<source_file_id>`

All mutating operations require `write_files` at the auth layer and Digestion `manage` access at the manager layer. Existing `list/query` style rights remain insufficient for mutation.

### MCP / agent tooling

Updated `canopy/mcp/server.py` with a new MCP tool:

- `canopy_digest_manage_sources`

Supported actions:

- `list` - list sources, requiring `read_files`.
- `update` - update source label, URI, and metadata, requiring `write_files` plus manage access.
- `remove` - detach source files safely, requiring `write_files` plus manage access.
- `replace` - remove stale sources and add new Vault files or inline materials, requiring `write_files` plus manage access.

The MCP instruction payload now explicitly tells agents how to curate stale or incorrect Digestion material and when to rebuild.

### Agent instruction drift cleanup

Updated the canonical instructions and docs so agents do not rely on stale endpoint knowledge:

- `canopy/api/agent_instructions_data.py`
- `canopy/mcp/server.py`
- `docs/AGENT_ONBOARDING.md`
- `docs/API_REFERENCE.md`
- `docs/MCP_QUICKSTART.md`

The Digestion workflow now includes source curation as a first-class lifecycle step:

`create/add -> build -> query/context/figures/visual evidence -> append useful work product -> curate sources -> extract datapoints -> generate/export outputs`

## Tests added

Added `test_digestion_rest_source_lifecycle_endpoints_are_safe_and_manage_gated` in `tests/test_digestions.py`.

Coverage includes:

- Owner creates and builds a Digestion.
- Source metadata can be patched.
- Reader without management rights cannot delete a source.
- Owner can delete/detach a source without deleting the Vault file.
- Derived outputs are invalidated after source removal.
- Replacement with a new source and `build_after` works.
- Missing source IDs return a clean `missing_source_file_ids` validation error.

Updated frontend/instructions regression tests so documentation and agent payloads track the new tool and routes.

## Verification run

Commands passed:

```bash
python -m py_compile canopy/core/digestions.py canopy/api/routes.py canopy/api/agent_instructions_data.py canopy/mcp/server.py
pytest tests/test_digestions.py -q
pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_digestion_agent_workflow_mcp_completeness tests/test_frontend_regressions.py::TestFrontendRegressions::test_api_reference_tracks_recent_dm_collab_and_privacy_surfaces tests/test_agent_reliability_endpoints.py::TestAgentReliabilityEndpoints::test_agent_instructions_collab_cards_contract_includes_workflow_metadata tests/test_agent_reliability_endpoints.py::TestAgentReliabilityEndpoints::test_agent_instructions_include_channel_reaction_and_card_api_contracts -q
git diff --check
```

Results:

- `tests/test_digestions.py`: 47 passed.
- Targeted frontend/agent instruction tests: 4 passed.
- Syntax check passed.
- Whitespace check passed.

## Review notes / residual work

This patch is intentionally endpoint-first and safe. It gives agents and API clients the missing source lifecycle primitives without forcing a new human UI flow in the same patch.

Likely follow-up UI work:

- Add visible per-source actions in the Digestion/Vault UI for `Rename`, `Edit source metadata`, `Remove from Digestion`, and `Replace source`.
- Show when removal invalidated derived artifacts and prompt the owner to rebuild.
- Expose agent-facing source curation history in the contributions/source panels so owners can audit who changed what.

I did not implement destructive Vault deletion here. Source removal from a Digestion is deliberately not the same thing as deleting the underlying file.
