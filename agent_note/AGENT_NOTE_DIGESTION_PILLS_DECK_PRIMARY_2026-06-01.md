# Agent Note: Digestion Pills Should Open Deck First

Date: 2026-06-01
Branch: `codex/digestion-ref-module-deck-first-0.6.319`
Version bump: `0.6.319`

## User-reported issue

On `0.6.317`, clicking rendered Digestion pills still opened the small Digestion pop-up/modal reader, even after reload. The intended behavior is that Digestion references should project into the Canopy Deck workspace when the user has access, or route back to the source Vault UI if Deck cannot open. The user explicitly does not want a separate transient pop-up reader as the normal path.

## Root cause

The previous Deck-first patch covered the rich-content anchor path in `canopy-main.js`, but the actual smart-pill linkifier/normalizer is owned by `canopy/ui/static/js/digestion_refs.js`.

That module had its own document click handler:

- catches `.canopy-digestion-ref[data-canopy-digestion-id]`
- prevents the default click
- calls `openDigestion(id)`
- `openDigestion(id)` always opened `.canopy-digestion-modal`

As a result, button-style Digestion pills produced by `digestion_refs.js` bypassed the Deck-first behavior and kept showing the modal.

## Patch implemented

Updated `canopy/ui/static/js/digestion_refs.js` so normal Digestion pill clicks are Deck-first:

- Renamed the old modal implementation path to `openDigestionModal(id)`.
- Added a new `openDigestion(id, sourceEl, options)` that:
  - calls `window.openCanopyDigestionReferenceDeck(id, sourceEl, { initialMode: 'rag' })` when available;
  - returns immediately if the Deck opens successfully;
  - routes to `/vault?digestion=<id>` if Deck is unavailable or cannot resolve the Digestion;
  - only opens the modal when explicitly called with `{ modalFallback: true }`.
- Added `event.defaultPrevented` protection so the `canopy-main.js` anchor handler and `digestion_refs.js` handler do not double-handle the same click.
- Updated the click handler to pass the clicked pill element into the Deck opener, preserving title/access context.
- Exposed `window.CanopyDigestionRefs.openModal` only as an explicit fallback/debug surface.

## Why this is safe

- The patch does not alter Digestion API endpoints, ACLs, Vault data, or Deck manifests.
- Existing modal rendering code is preserved for fallback/debug use.
- Modified-click behavior remains intact, so command/ctrl/shift/alt and middle-click still follow normal browser behavior where applicable.
- If Deck is not available on a page, the user is taken to the Vault/source UI rather than being shown the modal.

## Tests run

- `node --check canopy/ui/static/js/digestion_refs.js`
- `node --check canopy/ui/static/js/canopy-main.js`
- `python -m pytest tests/test_frontend_regressions.py -k 'digestion_reference_pills or digestion_extraction_copy'`
- `python -m pytest tests/test_frontend_regressions.py -k 'digestion or deck_widget'`
- `git diff --check`

## Review focus for Canopy Dev Bot

Please verify on a live instance:

1. Clicking a Digestion smart pill in a channel post opens the Digestion Deck workspace directly.
2. Clicking a Digestion smart pill in a DM opens the Digestion Deck workspace directly.
3. Clicking a Digestion smart pill in Feed opens the Digestion Deck workspace directly.
4. If the user lacks access or Deck cannot resolve the Digestion, the fallback routes to Vault/source UI rather than opening the old modal.
5. Modified clicks still behave normally.
