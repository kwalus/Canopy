# Agent Note: Channel Composer Lifespan Menu

Date: 2026-05-28
Branch: codex/composer-lifespan-menu-0.6.272
Version: 0.6.272

## Summary
Refined the channel composer layout so quick mention chips have their own full-width upper row and the post lifespan control moves into a compact dropdown button in the lower tool cluster.

## User Problem
At medium widths, the quick mention rail wrapped awkwardly with the lifespan selector. The streaming quick-create button also occupied a prime composer control slot even though streaming is not ready for normal use.

## Changes
- Replaced the old mixed lifespan/mention row with `channel-quick-mention-row`, which is only visible when quick mention chips exist.
- Moved the existing `channelExpiry` TTL selector and custom duration controls into a compact `channel-lifespan-quick-toggle` dropdown button beside the other composer tools.
- Added dynamic lifespan button labels (`3mo`, `1h`, custom values, etc.) while preserving the existing `resolveChannelTtlSelection()` send behavior.
- Hid the channel composer stream quick-create button and removed stream card create entries from the composer more menu.
- Kept all stream attachment/card rendering code untouched; this only hides unstable composer entry points.
- Added frontend regression guards for the new lifespan dropdown, quick mention row visibility class, and hidden stream quick-create entry points.

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
This patch is intentionally conservative. It does not change message TTL payload semantics, stream card rendering, stream APIs, or mesh behavior. The stream quick-create affordance is only hidden from the channel composer until streaming is ready for users.
