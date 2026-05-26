# Agent Note: Low-latency @Canopy LLM enrichment for Agent Run Capsules

Date: 2026-05-26
Author: Codex Agent
Branch: `codex/llm-capsule-enrichment-0.6.243`
Target version: `0.6.243`

## Summary

Implemented a bounded first embodiment of LLM-assisted Agent Run Capsule summaries. Deterministic Capsules still render immediately and remain the canonical fallback. When Capsules are enabled and visible, the browser may request a low-token `@Canopy` refinement from a new authenticated AJAX endpoint. The endpoint uses the existing personal/admin fallback Canopy AI credential chain, disables web search, enforces short output limits and a short capsule-specific provider timeout, caches by viewer/capsule/source hash, and returns quiet fallback responses when no model/key is available or provider output is weak.

The intent is to make high-noise agent channels easier for humans to enter without making channel rendering depend on an LLM.

## Files changed

- `canopy/core/canopy_ai.py`
  - Added `CanopyLLMManager.summarize_capsule(...)`.
  - Added strict capsule summarizer system prompt and bounded capsule packet normalization.
  - Added `canopy_capsule_summaries` local cache table keyed by `(viewer_user_id, capsule_id, source_hash)`.
  - Added `CANOPY_CAPSULE_LLM_MAX_OUTPUT_TOKENS`, `CANOPY_CAPSULE_LLM_TIMEOUT_SECONDS`, and `CANOPY_CAPSULE_LLM_CACHE_SECONDS` support via env with safe bounds.
  - Capsule summaries use no web search and use compose memory/team context only as tone/routing context, not hidden source facts.

- `canopy/ui/routes.py`
  - Added `POST /ajax/canopy_llm/capsule_summary`.
  - Requires login, CSRF, and channel membership/read access.
  - Does not require posting permission.
  - Quietly returns `{success:false, fallback:true}` for LLM unavailable/provider failures so the deterministic Capsule remains visible.

- `canopy/ui/templates/channels.html`
  - Captures a compact Capsule packet while rendering each Capsule.
  - Sends only visible Capsules, max four per batch, one request active at a time.
  - Browser abort timeout is 8 seconds; backend provider timeout defaults to 7.5 seconds.
  - Updates title, overview, brief rows, and copy-summary text if a valid LLM JSON response arrives.
  - Keeps deterministic summary when disabled, timed out, stale, or invalid.
  - Keeps a hidden `Needs attention` row available so the LLM can surface a blocker even if deterministic heuristics missed it.

- `docs/API_REFERENCE.md`, `README.md`, `CHANGELOG.md`, version docs
  - Documented the new endpoint and release behavior.

- `tests/test_canopy_llm_compose.py`
  - Added manager tests for bounded no-web-search capsule prompt, short timeout, and server cache reuse.
  - Added route test for channel access and summary response.

- `tests/test_frontend_regressions.py`
  - Added static guards for Capsule enrichment endpoint, timeout constant, source hash attributes, and brief field mapping.

## Safety and privacy notes for review

- This feature may send visible channel Capsule content to the user/admin configured AI provider when Capsule mode is active. That is consistent with the existing `@Canopy` credential chain but is more automatic than typing `@Canopy` in a composer. If we want stricter privacy posture, add a user-facing setting such as `AI-refine Capsules` default off or default only when the user has a personal key.
- Web search is always disabled for Capsules.
- The prompt is capped to 9,000 characters and uses at most 12 source messages and 10 artifacts.
- The frontend limits visible enrichment to 4 Capsules per render pass and one active request to avoid stampedes.
- The server cache is node-local and keyed per viewer, so summaries can adapt to user memory but do not mesh-sync.

## Tests run

- `PYTHONPATH=. pytest tests/test_canopy_llm_compose.py -q`
- `PYTHONPATH=. pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_channel_header_can_hide_agent_only_threads tests/test_frontend_regressions.py::TestFrontendRegressions::test_api_reference_tracks_recent_dm_collab_and_privacy_surfaces -q`
- `python -m py_compile canopy/core/canopy_ai.py canopy/ui/routes.py`
- `git diff --check`

## Suggested reviewer focus

1. Confirm the automatic enrichment privacy posture is acceptable for private channels when an admin fallback key exists.
2. Confirm the 7.5 second backend timeout and 8 second frontend timeout are appropriate for the desired UX.
3. Confirm the JSON schema fields are sufficient for Capsule UI; I kept this intentionally narrow to stay cheap and stable.
