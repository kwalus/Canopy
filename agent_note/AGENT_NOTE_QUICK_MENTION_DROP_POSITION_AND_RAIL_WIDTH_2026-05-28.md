# Agent Note: Quick Mention Drop Position And Rail Width

Date: 2026-05-28
Branch: codex/mention-chip-drop-position-0.6.271
Version: 0.6.271

## Summary
Implemented a focused channel-composer ergonomics patch for the quick mention chip rail.

## User Problem
Dragging a quick mention chip into the channel text composer inserted the mention using the textarea's stale cursor state, which could land at the start of the draft or another surprising location. The quick mention rail also stopped short of available horizontal space even when the composer row had room.

## Changes
- Added coordinate-aware textarea drop insertion for quick mention chips.
- Added a lightweight textarea text measurement helper that maps the drop point to an approximate caret index, including wrapped text rows and scroll offsets.
- Preserved the existing click/keyboard behavior by keeping normal mention insertion tied to the current caret or selection.
- Changed drag-drop insertion to call `insertChannelQuickMentionAtIndex(..., { replaceSelection: false })` so a chip lands where it is dropped without replacing unrelated selected text.
- Allowed `.channel-quick-mention-rail` to flex across the available composer row on desktop while preserving the existing mobile full-width behavior.
- Added frontend regression guards for the drop-caret helper, explicit insertion path, and rail sizing.

## Files Changed
- `canopy/ui/templates/channels.html`
- `tests/test_frontend_regressions.py`
- `CHANGELOG.md`
- `README.md`
- `pyproject.toml`
- `canopy/__init__.py`

## Validation
- `venv/bin/python -m pytest tests/test_frontend_regressions.py -q` -> 178 passed
- `git diff --check` -> passed

## Review Notes
This patch intentionally stays frontend-only. It does not change mention scoring, member fetching, message submission, or mesh behavior. The drop-caret resolver is approximate for browser textarea wrapping but should feel natural in normal drafts and is much better than always inserting at the stale selection/start position.
