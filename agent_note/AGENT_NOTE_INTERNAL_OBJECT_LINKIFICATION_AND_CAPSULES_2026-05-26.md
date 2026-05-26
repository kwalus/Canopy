# Agent Note: Internal Object Linkification And Capsule Action Rails

Date: 2026-05-26
Author: Codex Agent
Target release: 0.6.241
Branch: codex/internal-object-links-0.6.241

## Summary

Agents in high-volume channels such as `MCP-hardware` are posting useful Canopy object references in plain text, but the human-facing renderer only recognized a narrower subset. This patch extends the shared Canopy entity-linkification pipeline and the agent-run Capsule artifact extractor so high-confidence internal object references become immediate clickable actions for users.

## User-visible behavior

The renderer now recognizes and turns these patterns into Canopy pills:

- Filename-adjacent file IDs, e.g. `adapter_core.py (F17beca7ce3f4d144364bd805)`.
- Explicit file contexts, e.g. `PDF: F...`, `Figure: F...`, `File ID: F...`, `source_file_id: F...`, `attachment: F...`, `output: F...`.
- Existing markdown/HTML `/files/<id>` and `/file-ref/<id>` links.
- Quoted or backticked file IDs, matching the previous safe behavior.
- Digestion IDs, e.g. `Dg9481b28c166788391c063964`, and `/vault?digestion=<id>` links.

Detected file references continue to use the existing `/file-ref/<id>` resolver and async filename hydration. Detected Digestion references link to `/vault?digestion=<id>`.

## Capsule behavior

Agent-run Capsules now include direct artifacts for the same high-confidence file patterns. This means compressed agent runs can surface direct file links and Digestion links without requiring the human to expand the full trace first.

The patch remains intentionally conservative: it does **not** restore generic bare `F...` matching. That broad matching caused false file links from ordinary text such as `@Forge_McClaw`. The semantic harness explicitly verifies this regression stays closed.

## Files changed

- `canopy/ui/static/js/canopy-main.js`
  - Added Digestion object pills.
  - Added filename-parenthetical and explicit-context file reference recognition.
  - Added plain `/vault?digestion=<id>` handling to avoid partial URL linkification.

- `canopy/ui/templates/channels.html`
  - Added Capsule artifact extraction for filename-parenthetical and explicit-context file references.
  - Preserved underscores in artifact labels so filenames such as `adapter_core.py` remain human-readable.

- `tests/test_frontend_regressions.py`
  - Added regression checks for Digestion pills and expanded internal object recognition.
  - Preserved assertion that Capsule extraction does not generic-match bare `F...` IDs.

- Version/docs/changelog bumped to `0.6.241`.

## Verification

Commands run:

```bash
node --check canopy/ui/static/js/canopy-main.js
```

```bash
node <<'NODE'
# Extracted the actual linkification and Capsule extraction functions from source.
# Verified:
# - adapter_core.py (F...) renders as a file pill with filename label.
# - README.md (F...) renders as a file pill with filename label.
# - Figure: F... renders as a fallback file pill.
# - Dg... renders as a Digestion pill.
# - /vault?digestion=Dg... renders as one clean Digestion pill, not a partial URL.
# - @Forge_McClaw does not produce a false file reference.
NODE
```

```bash
PYTHONPATH=. pytest \
  tests/test_frontend_regressions.py::TestFrontendRegressions::test_channel_header_can_hide_agent_only_threads \
  tests/test_frontend_regressions.py::TestFrontendRegressions::test_api_reference_tracks_recent_dm_collab_and_privacy_surfaces \
  -q
```

Result: `2 passed`.

## Review notes

This patch is intentionally focused on high-confidence syntax. If we want even richer object detection later, the safer next step is server-side object-reference indexing with known object existence checks, not broader client-side regex matching.

Additional verification:

```bash
PYTHONPATH=. pytest tests/test_frontend_regressions.py -q
```

Result: `176 passed, 1 failed`. The lone failure is the pre-existing local dependency issue `ModuleNotFoundError: No module named 'zeroconf'` when `test_task_parser_preserves_selected_multiple_assignees` imports the full app through `canopy.network.discovery`; it is not related to this patch's renderer/Capsule changes.
