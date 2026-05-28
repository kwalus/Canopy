# Agent Note: Channel Quick Mention Rail Reordering

Date: 2026-05-28  
Branch: `codex/mention-rail-reorder-0.6.266`  
Version bump: `0.6.266`

## Summary

The channel quick mention rail is useful, but users may want their own preferred ordering rather than only the smart/relevance order. This patch adds drag-to-reorder within the mention rail while preserving drag-to-composer insertion.

## Changes Implemented

- Added per-user, per-channel local quick mention ordering:
  - `CHANNEL_QUICK_MENTION_ORDER_STORAGE_KEY`
  - stored in `localStorage` under the current user namespace
  - keyed by stable user ID when available, falling back to canonical mention handle
- Applied user ordering after the existing relevance scoring:
  - the existing scoring still selects candidates from the cache-backed candidate set and loaded message snapshot
  - user-ordered chips are promoted to the chosen order
  - unordered candidates keep their prior score order after the user-ordered entries
- Added drag/drop reordering inside the rail:
  - dragging over another chip repositions the dragged chip before/after based on horizontal midpoint
  - dropping inside the rail persists the visible chip order
  - drag-to-composer still works through the existing `application/x-canopy-mention` payload
- Added a small visual reordering state:
  - `.channel-quick-mention-rail.is-reordering`
  - `.channel-quick-mention-chip.is-dragging`
- Suppressed accidental click-to-insert immediately after an internal reorder drop.
- Extended regression coverage for the local storage key, ordering helpers, internal rail drag/drop handlers, and `copyMove` drag mode.

## Files Changed

- `canopy/ui/templates/channels.html`
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

- No backend requests or database changes were added.
- Ordering is local UX state only; it does not affect other users or channel membership.
- The rail still uses the existing `channelMentionCache` and current channel message snapshot.
- Drag-to-composer behavior is preserved by keeping the original `application/x-canopy-mention` drag payload and letting the composer/file dropzone continue handling external drops.
