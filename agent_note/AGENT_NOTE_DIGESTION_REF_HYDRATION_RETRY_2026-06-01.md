# Agent Note: Digestion Reference Hydration Retry and False-Unavailable Recovery

Date: 2026-06-01
Branch: `codex/digestion-ref-hydration-retry-0.6.320`
Version bump: `0.6.320`

## User-reported issue

When a post contains multiple rendered Digestion pills, some pills can briefly or permanently render as inaccessible even when the current user has access. Reloading the page can make the same pills render correctly. This is frustrating because it makes access state appear unreliable and forces the user to refresh.

## Root cause hypothesis

`canopy/ui/static/js/digestion_refs.js` hydrated each smart pill by calling `/api/v1/digestions/<id>?summary=1` and cached the first promise/result for that Digestion ID. It also set `data-canopy-digestion-hydrating="1"` on each pill and did not consistently clear that flag.

That meant a transient failure, race, auth/session timing issue, or temporary API miss could:

- be cached as an unavailable preview for the lifetime of the page;
- leave the pill marked as hydrating and therefore skipped by future scans;
- block normal click behavior because unavailable pills returned early.

This matched the observed behavior: reload clears the JS cache/DOM flags and the same Digestion can then hydrate as accessible.

## Patch implemented

Updated `digestion_refs.js` so Digestion smart-ref hydration is recoverable:

- Added `previewPending` so multiple pills for the same Digestion share one in-flight preview request.
- Successful previews are cached; failed previews are not cached as permanent truth.
- Added short retry delays (`220ms`, `700ms`) around preview checks.
- Retryable server/network failures can retry through the full retry window.
- Non-retryable access failures still get one confirmation retry to smooth over startup/session races.
- Hydrating flags are removed after hydration completes or fails.
- Failed previews set `data-canopy-digestion-hydrated="0"` rather than permanently locking the element as complete.
- Clicking an unavailable-looking pill is no longer blocked. If the UI falsely rendered unavailable, the click can still resolve through Deck/Vault rather than leaving the user stuck.

## Why this is safe

- Does not alter Digestion ACLs, backend APIs, or data model.
- Keeps successful preview caching for performance.
- Shares in-flight requests to avoid N duplicate calls when a post includes repeated references.
- Avoids hammering the server: retries are bounded and short.
- Preserves the Deck-first behavior from `0.6.319`.

## Tests run

- `node --check canopy/ui/static/js/digestion_refs.js`
- `node --check canopy/ui/static/js/canopy-main.js`
- `python -m pytest tests/test_frontend_regressions.py -k 'digestion_reference_pills or digestion_extraction_copy'`
- `python -m pytest tests/test_frontend_regressions.py -k 'digestion or deck_widget'`
- `git diff --check`

## Review focus

Please verify with a live post containing several Digestion references:

1. All accessible Digestions hydrate reliably without needing a page reload.
2. A transiently unavailable-looking pill can still be clicked and routed to Deck/Vault.
3. Repeated Digestion IDs in one post do not cause excessive API calls.
4. Truly inaccessible Digestions still render as unavailable after the bounded retry.
