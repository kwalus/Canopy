# Agent Note: DM X/Twitter Embed Stability

Date: 2026-05-28  
Branch: `codex/dm-x-embed-stability-0.6.264`  
Version bump: `0.6.264`

## Summary

A user observed that an X/Twitter post embedded in a DM appeared to flicker, likely during scroll or thread refresh. I audited the DM rendering path and found two safe, narrow improvements:

1. DM rich-content hydration could rewrite `.dm-message-text` and then rerun shared rich embed processing when a DM thread snapshot or older-message load reapplied hydration. For live X widgets, that can tear down and rebuild the rendered iframe, which looks like flicker.
2. The existing Canopy iframe scroll/focus guard covered generic `.iframe-embed` embeds but not Twitter/X's injected iframe inside `.x-embed`, so X widgets could still trigger unexpected focus/scroll behavior.

## Changes Implemented

- Added idempotent DM rich-content markers in `canopy/ui/templates/messages.html`:
  - `data-dm-rich-source`
  - `data-dm-rich-processed`
- Cleared those markers during inline DM edits before rehydration, so edited messages still update correctly.
- Added idempotent shared rich-content markers in `canopy/ui/static/js/canopy-main.js`:
  - `data-canopy-rich-source`
  - `data-canopy-rich-rendered`
- Added X widget in-flight protection:
  - `data-canopy-x-rendering`
  - `.is-rendering`
- Deferred `data-canopy-x-processed` until the widget load path resolves or fails, preventing duplicate in-flight widget calls while preserving previous fallback behavior on failure.
- Extended `installCanopyEmbedScrollGuards` to cover `.x-embed iframe` in addition to ordinary iframe embeds.
- Added a stable minimum loading footprint for `.x-embed.is-rendering:not(.is-rendered) .x-embed-render` to reduce visual collapse while Twitter widgets hydrate.
- Added frontend regression coverage for the DM/X idempotence markers, X rendering lock, X iframe scroll guard, and inline edit marker reset.

## Files Changed

- `canopy/ui/static/js/canopy-main.js`
- `canopy/ui/templates/messages.html`
- `canopy/ui/templates/base.html`
- `tests/test_frontend_regressions.py`
- `CHANGELOG.md`
- `README.md`
- `pyproject.toml`
- `canopy/__init__.py`

## Validation

Passed locally:

- `./venv/bin/python -m pytest tests/test_frontend_regressions.py -q`
  - `178 passed`
- `node --check canopy/ui/static/js/canopy-main.js`
- `./venv/bin/python -m py_compile canopy/__init__.py canopy/ui/routes.py canopy/api/routes.py`
- `git diff --check`

## Risk Notes

- This is intentionally narrow and does not change DM ordering, message polling, thread snapshot behavior, or X embed provider detection.
- The rich-content idempotence is DOM-local. Full thread replacements still hydrate normally because new DOM nodes do not carry the markers.
- Inline DM edits explicitly clear markers before rehydration to avoid stale rendered content.
- If Twitter widgets fail to load, the fallback card remains visible as before.
