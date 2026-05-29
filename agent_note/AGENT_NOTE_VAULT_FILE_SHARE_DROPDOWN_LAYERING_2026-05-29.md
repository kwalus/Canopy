# Agent Note: Vault File Share Picker Layering Fix

Date: 2026-05-29
Branch: codex/vault-file-share-dropdown-0.6.288
Version: 0.6.288

## Problem
In the File Vault, the per-file Share / access UI opens a user search picker inside each file card. In details/list view the file card is rendered as a compact row with constrained layout, and the picker behaved like a floating dropdown. That made the user list appear clipped or visually trapped by the row allocation, especially when the user was granting access from details view.

## Patch
- Changed the Vault file-share user results from an absolutely positioned overlay into an in-flow results panel inside the share/access form.
- Added active-card state styling so the file card opens cleanly when sharing is active and elevates above neighboring rows while the picker is open.
- Added details/list-specific layout so an open share panel occupies a full second row instead of being squeezed into the compact row chrome.
- Preserved usability for icon, preview, and mobile modes; mobile now keeps the file-share results in the access panel instead of using the generic fixed-bottom dropdown behavior intended for other menus.
- Added JS state classes for `is-file-share-open`, `is-file-share-search-open`, and `is-search-open` so the CSS state is explicit and not dependent only on `:has()` support.
- Added frontend regression coverage to guard the unclipped details/list view behavior.

## Files Changed
- `canopy/ui/templates/vault.html`
- `canopy/ui/static/js/canopy-main.js`
- `tests/test_frontend_regressions.py`
- `canopy/__init__.py`
- `CHANGELOG.md`

## Verification
Run:

```bash
PYTHONPATH=. pytest tests/test_frontend_regressions.py -q
PYTHONPATH=. pytest tests/test_vault_api.py -q
python -m compileall canopy -q
git diff --check
```

## Review Notes
This is intentionally a UI-layer fix only. It does not change Vault ACL endpoints, permissions semantics, file ownership, or sharing behavior. The main behavior change is that the user list now expands within the visible share panel instead of attempting to escape the compact file row as an overlay.
