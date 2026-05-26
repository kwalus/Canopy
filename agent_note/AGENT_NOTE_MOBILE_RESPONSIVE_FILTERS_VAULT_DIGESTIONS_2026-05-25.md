# Agent Note: Mobile Responsive Pass for Filters, Vault, and Digestions

Date: 2026-05-25
Branch: `codex/mobile-responsive-pass-0.6.236`
Version bump: `0.6.236`

## Summary

This patch performs a focused mobile responsive pass over the newest high-use Canopy surfaces: channel header filters, agent-run Capsules, File Vault selection/Digestion creation, and the Deck Digestion workspace. The goal was to make the current feature set usable on phone-width viewports without introducing new behavior or broad layout churn.

## What changed

### Channel filters and Capsules

- Tightened mobile layout for collapsed agent-run Capsules.
- Capsule action buttons now wrap as usable full-width/half-width buttons on narrow screens.
- Capsule trace padding was reduced for small screens.
- The Capsule level panel now has mobile-safe width constraints.
- The channel overflow menu now has a bounded viewport height and width on mobile.
- Overflow menu items can wrap text instead of forcing horizontal clipping.

### File Vault and Digestion builder

- When the selected-file bar opens the Digestion builder, the selection bar becomes a normal in-flow panel rather than a giant sticky bottom drawer.
- The selected-file actions become a two-column mobile grid when the builder is open.
- The Digestion builder source count, recipient tools, user/agent card list, and footer actions stack cleanly on narrow screens.
- Builder recipient results use a bounded mobile scroll region.
- Add-to-existing Digestion menus and share/autocomplete result popovers become fixed, viewport-safe bottom panels on mobile.

### Deck Digestion workspace

- Mobile Deck Digestion hero/header now wraps title/stats instead of truncating aggressively.
- Stat chips become a compact grid and collapse to one column on very small screens.
- Search, results, chart controls, figures, and tuning panes stack with bounded scroll regions.
- Search result areas get mobile max-height so users can see that results exist without needing maximum Deck mode.
- Chart controls remain reachable and chart SVG minimum width is reduced on very small screens.

## Files changed

- `canopy/ui/templates/channels.html`
- `canopy/ui/templates/vault.html`
- `canopy/ui/templates/base.html`
- `tests/test_frontend_regressions.py`
- Version/documentation files for `0.6.236`
- `CHANGELOG.md`

## Regression coverage

Updated `tests/test_frontend_regressions.py` to assert the new mobile-safe selectors and version scope:

- Mobile Capsule button wrapping and overflow menu bounds.
- Vault builder mobile mode when `.vault-digestion-create.is-visible` is open.
- Mobile share/menu bottom-panel behavior.
- Deck Digestion stat grid and results scroll bounds.

## Risk notes

- This patch is intentionally CSS-only for runtime behavior. It does not change Digestion APIs, query/build logic, access grants, or Capsule grouping logic.
- It uses CSS `:has()` in `vault.html`; this template already used `:has()` before this patch, so this does not introduce a new browser-support class for Canopy.
- The local Canopy instance was not listening on `7770` during implementation, so browser/device QA could not be run against a live node in this pass. The patch was validated with template/static regression tests instead.

## Suggested review focus

- Open channel pages on mobile width and verify the filter row, More menu, Capsule level control, and Capsule cards remain reachable.
- Select Vault files on mobile width, open New Digestion, and confirm the builder appears in normal page flow instead of trapping the user at the sticky bottom.
- Open a Digestion in the Deck at small and medium Deck sizes and verify search results and chart controls are visible without needing full-window mode.
