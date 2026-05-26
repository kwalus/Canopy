# Agent Note: Maximum Capsule Compression For One-Off Agent Posts

Date: 2026-05-26
Author: Codex Agent
Target release: 0.6.242
Branch: codex/max-capsule-single-agent-0.6.242

## Summary

In channels with heavy agent activity, especially `MCP-hardware`, users can still see many explicit one-off agent posts even when the Capsule filter is set to Maximum. The previous Max configuration only compressed agent reply runs with at least two messages and top-level agent-only runs with at least two messages, so single agent replies/posts leaked through as normal message cards.

This patch changes only the Maximum Capsule level so it behaves like a true “agent chatter digest” mode.

## Behavior change

- `Maximum` now compresses single agent reply runs (`replyMin: 1`).
- `Maximum` now compresses single top-level agent-only threads (`rootMin: 1`).
- Light/Balanced/Strong remain unchanged, so users can still choose a level where one-off agent replies remain visible.
- The Max level description now explicitly tells users to use Strong if they want one-off replies visible.

## Files changed

- `canopy/ui/templates/channels.html`
  - Updated Max Capsule level thresholds and description.

- `tests/test_frontend_regressions.py`
  - Added a regression guard verifying that Max has `replyMin: 1`, `rootMin: 1`, and the user-facing description preserves the level distinction.

- Version/docs/changelog bumped to `0.6.242`.

## Verification

```bash
PYTHONPATH=. pytest \
  tests/test_frontend_regressions.py::TestFrontendRegressions::test_channel_header_can_hide_agent_only_threads \
  tests/test_frontend_regressions.py::TestFrontendRegressions::test_api_reference_tracks_recent_dm_collab_and_privacy_surfaces \
  -q
```

Result: `2 passed`.

## Review note

This is intentionally a configuration-level change, not a renderer rewrite. It should be low-risk because the existing Capsule grouping/rendering machinery already handles one-message runs; the earlier thresholds simply prevented those runs from being encapsulated at Max.
