# Agent Note: Capsule Activity Run Map UI

Date: 2026-05-26
Branch: codex/capsule-activity-map-0.6.246
Version bump: 0.6.245 -> 0.6.246

## Summary

This patch turns the unused right side of channel Agent Run Capsule cards into a compact, visual navigation surface. The new `Run map` represents the compressed source posts as clickable visual checkpoints so a human can jump directly to the likely-relevant post without expanding and scanning the entire agent trace.

## User-facing behavior

- Capsule cards now allocate the right rail to a `Run map` on desktop while preserving the existing collapsed capsule content on the left.
- Each map node is clickable and calls the existing source-focus pathway, expanding the trace if needed and scrolling to the exact source post.
- Nodes are mostly visual, with only compact time labels such as `now`, `3m`, `2h`, `4d`, or `May 25` for timeliness.
- Nodes are visually encoded by source-post signal:
  - orange warning node for likely blocker / access / review / failed / owner-action content
  - cyan/teal file nodes for direct file references or attachments
  - teal/diagram nodes for Digestion references
  - blue card nodes for structured work cards
  - lime completion nodes for completed/ready/successful checkpoints
  - violet mention nodes for mention-heavy posts
  - neutral post nodes for ordinary compressed source messages
- If a compressed run contains more source posts than can fit, the map selects a high-signal subset while preserving first/last context and reports the hidden count in the map header.
- The existing `Files & outputs` rail now includes compact recency metadata when the source post timestamp is available.

## Implementation details

Updated `canopy/ui/templates/channels.html`:

- Expanded `.agent-run-capsule-head` from `left + auto` to a structured `left + run-map rail` grid.
- Added `.agent-run-capsule-side-top` so avatar and controls remain compact above the map.
- Added the `.agent-run-capsule-map*` CSS family, including light-theme overrides and mobile-safe stacking.
- Added source timestamp propagation to `collectAgentRunDirectArtifacts()`.
- Added `getAgentRunCompactTimeLabel()` for tiny recency labels.
- Added `buildAgentRunCapsuleMapNodes()` and `renderAgentRunCapsuleSignalMap()`.
- Rendered the map from `renderAgentRunCapsule()` alongside existing summary/copy/trace behavior.

Updated `tests/test_frontend_regressions.py`:

- Added regression assertions for the map builder, map renderer, max node constant, clickable node focus path, `Run map` label, and artifact recency metadata.
- Updated version assertion to `0.6.246`.

Updated release/version files:

- `pyproject.toml`
- `uv.lock`
- `canopy/__init__.py`
- `README.md`
- `docs/API_REFERENCE.md`
- `docs/AGENT_ONBOARDING.md`
- `docs/MCP_QUICKSTART.md`
- `CHANGELOG.md`

## Design rationale

The right rail should not become another text panel. The map is intended to behave more like a cockpit instrument: it compresses the run into a small set of clickable signals that show freshness, blockers, files, cards, mentions, and completion state at a glance. This is deliberately aligned with the broader Capsule goal: slow fast agent chatter down to human speed while preserving exact traceability.

## Verification

Passed:

- `PYTHONPATH=. pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_channel_header_can_hide_agent_only_threads -q`
- `PYTHONPATH=. pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_mobile_resize_dedup_gates_collapse_redundant_layout_work -q`
- `PYTHONPATH=. pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_channel_header_can_hide_agent_only_threads tests/test_frontend_regressions.py::TestFrontendRegressions::test_api_reference_tracks_recent_dm_collab_and_privacy_surfaces -q`
- `node` syntax parse of the new capsule-map JavaScript function block
- `git diff --check`

Full frontend regression note:

- `PYTHONPATH=. pytest tests/test_frontend_regressions.py -q` runs 176 tests successfully and fails one unrelated import path test because this local interpreter lacks `zeroconf` (`ModuleNotFoundError: No module named 'zeroconf'`).
- `uv run ...` could not be used because `uv` is not installed in this shell (`zsh:1: command not found: uv`).

## Review focus requested

Please specifically review:

- Whether the right rail width feels balanced in real channel traffic at desktop widths.
- Whether the signal-priority order in `getAgentRunMapSignal()` matches operator expectations.
- Whether the node selection strategy for long runs should weight agent-authored files even more heavily.
- Whether future server/LLM capsule enrichment should also return explicit map hints, rather than relying only on deterministic client-side signal extraction.
