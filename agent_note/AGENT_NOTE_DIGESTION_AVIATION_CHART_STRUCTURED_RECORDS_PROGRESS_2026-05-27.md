# Agent Note: Digestion Aviation Chart Structured Records + Owner Progress

Date: 2026-05-27
Branch: `codex/digestion-chart-progress-0.6.252`
Version bump: `0.6.252`

## Context
Konrad asked Codex to review the GoldGang VPS aviation-channel discussion where Goose and the aviation workflow were using Digestions to support flight-planning/chart work. The important observed failure mode was not ordinary retrieval failure. The issue was that aviation plates and chart bundles are graphical/vector-heavy operational references, so generic PDF text extraction and RAG can expose titles, marginalia, and occasional snippets while still missing or misreading safety-critical fields.

A concrete example from the discussion: a prior agent response treated KSAN RWY 27 as if an ILS/FAF fact had been extracted reliably, while the human correction clarified that the relevant chart facts must distinguish LOC/RNAV procedures, FAF, altitude, and chart source. This is exactly the class of problem where a source-bound Digestion needs a structured source-of-truth layer rather than another pile of snippets.

## Implemented Patch

### 1. Structured records output kind
Added a new Digestion output kind:

- `structured_records`
- schema: `canopy_structured_records_v1`
- source revealing: yes
- source output policy: `profiled_source_facts`

The new output is intended for profile-specific, durable, source-grounded records that are not well represented by generic chunk retrieval. Initial profile support includes `aviation_chart`.

### 2. Aviation chart profile
The `aviation_chart` profile normalizes records with fields such as:

- `airport_icao`
- `procedure_name`
- `procedure_type`
- `runway`
- `chart_cycle`
- `effective_date`
- `final_approach_fix`
- `final_approach_course`
- `nav_frequency`
- `minimums`
- `missed_approach`
- `notes`
- `warnings`

Records support source metadata, field-level provenance, verification status, confidence, tags, and arbitrary profile fields. The reuse guidance explicitly tells agents that aviation chart records should carry source/page/chart identifiers and should not be treated as operationally safe unless verified.

### 3. REST + UI AJAX endpoints
Added:

- `POST /api/v1/digestions/<digestion_id>/structured-records`
- `POST /api/v1/digestions/<digestion_id>/structured-records/search`
- `POST /ajax/digestions/<digestion_id>/structured-records`
- `POST /ajax/digestions/<digestion_id>/structured-records/search`

Append requires write-files permission plus Digestion manage access plus source metadata access. Search requires read-files permission plus query access plus source metadata access, because these records are source revealing.

### 4. MCP endpoint
Added:

- `canopy_digest_structured_records`

Modes:

- `action="append"` to append/update profile records
- `action="search"` to search the structured-record output

This gives agents a direct tool for building aviation-chart source-of-truth records from chart files, visual-evidence records, human corrections, and their own verified extraction work.

### 5. Owner-visible persistent operation progress
Added persistent Digestion operation tracking in a new `digestion_operations` table. This persists the latest operation snapshot for:

- `build`
- `datapoints`
- `structured_records`

This is specifically to satisfy the owner workflow: if an agent is rebuilding a Digestion or appending structured chart records, the Digestion owner can see whether work is idle, running, completed, or failed after the immediate in-memory operation is gone. Query-only users receive a public-safe progress payload with source labels and actor IDs stripped.

### 6. Vault and Deck UI integration
Updated the Vault Digestion cards to show:

- structured record counts in stats pills
- structured-record operation progress rows
- running filter support for structured-record operations
- structured records as a third search surface alongside semantic chunks and structured datapoints

Updated the Deck Digestion workspace to show structured record counts and allow structured-record search from the larger Deck surface.

### 7. Agent instructions updated
Updated REST/MCP agent instructions so agents know:

- ordinary RAG is not enough for chart-like operational references
- aviation plates should use `profile="aviation_chart"`
- structured records require field-level provenance and verification state
- progress polling now includes `operations.structured_records`

## Security and Access Notes

- `structured_records` is source revealing, like `structured_datapoints`, `visual_evidence`, and `pdf_figures`.
- Manage access alone does not imply source metadata access. Agents need explicit `can_read_sources=true` to append or read structured records.
- Query-only agents can still use normal RAG/context if granted, but they cannot inspect profile records without source access.
- Query-only progress payloads are source-scrubbed and actor-scrubbed.

## Files Changed

- `canopy/core/digestions.py`
- `canopy/api/routes.py`
- `canopy/ui/routes.py`
- `canopy/mcp/server.py`
- `canopy/api/agent_instructions_data.py`
- `canopy/ui/static/js/canopy-main.js`
- `tests/test_digestions.py`
- `pyproject.toml`
- `canopy/__init__.py`

## Tests / Checks Run

- `python -m py_compile canopy/core/digestions.py canopy/api/routes.py canopy/ui/routes.py canopy/mcp/server.py canopy/api/agent_instructions_data.py`
- `node --check canopy/ui/static/js/canopy-main.js`
- `pytest -q tests/test_digestions.py` (48 passed)
- `pytest -q tests/test_digestions.py tests/test_frontend_regressions.py -k "structured_records_append_search_and_persisted_progress or digestion_build_progress_is_available_after_build or digestion_progress"`

Result: passing at the time this note was written.

## Remaining Follow-Up Candidates

1. Build a chart-specific extraction UI/editor so humans can review and correct aviation chart fields before they become `verified`.
2. Add optional vision-assisted extraction from visual-evidence records into draft `aviation_chart` records, gated by source access and explicit provider settings.
3. Add chart-cycle / effective-date alerting so older aviation chart records can be marked `superseded` or `needs_review` when source cycles change.
4. Add profile templates beyond aviation, such as regulatory tables, instrument telemetry spec sheets, medical/lab protocols, and materials characterization records.
