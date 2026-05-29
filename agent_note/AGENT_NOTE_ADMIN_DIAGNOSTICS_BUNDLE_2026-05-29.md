# Agent Note: Admin Diagnostics Bundle (0.6.276)

## Summary
Implemented a safe first-pass admin-only diagnostics export so operators can download one paste-friendly text file containing bounded Canopy server diagnostics for support/performance review.

## User Need
Konrad asked for an Admin page panel that can produce a single file containing server/Canopy errors and performance issue context, so he can paste it into Codex and get help optimizing backend load time and behavior without hunting through separate logs.

## Files Changed
- `canopy/ui/templates/admin.html`
  - Added a **Support Diagnostics Bundle** panel under Admin -> Data Operations & Backups.
  - Panel provides a direct `Download Diagnostics` button.
  - Panel copy explicitly notes read-only behavior, secret redaction, log tails only, and DB counts rather than message content.
  - Added theme-token overrides for the new panel.
- `canopy/ui/routes.py`
  - Added admin-only `GET /ajax/admin/diagnostics/download`.
  - Generates a `.txt` attachment with runtime state, database stats, mesh summary, workspace-event counts, and bounded log tails.
  - Reads only active logging handlers and a small explicit set of known Canopy log paths; it does not scan broadly.
  - Tails at most 192 KiB per log, caps logs to 8, and caps bundle size to 1.5M characters.
  - Redacts common credential shapes including authorization headers, cookies, API keys, tokens, passwords, secrets, private keys, CSRF/session values, invite codes, GitHub tokens, and bearer tokens.
  - Database output is schema/table counts and file/WAL/SHM size metadata, not row content.
  - Workspace event output is counts/types/metadata only, not message previews.
- `tests/test_admin_user_workspace.py`
  - Added admin page rendering assertions for the diagnostics panel and route.
  - Added a download test proving log-tail redaction removes password and bearer-token content.
- `pyproject.toml`, `canopy/__init__.py`, `CHANGELOG.md`
  - Bumped to `0.6.276` and documented the feature.

## Endpoint Behavior
- Route: `GET /ajax/admin/diagnostics/download`
- Auth: instance owner/admin only via existing `require_admin`.
- Output: `canopy-diagnostics-YYYYMMDDTHHMMSSZ.txt`.
- Intended workflow: Admin clicks the button, reviews the text locally, then pastes it into a trusted support/Codex context.

## Safety Notes
- The export is read-only. It does not run checkpoints, backups, cleanup, or broad filesystem scans.
- It deliberately avoids message bodies, vault file contents, private channel content, and workspace event payload previews.
- Redaction is best-effort and intentionally broad, but the UI still tells admins to review before sharing outside trusted support.

## Validation
- `python -m compileall -q canopy/ui/routes.py` passed.
- `PYTHONPATH=. pytest tests/test_admin_user_workspace.py -q` passed: 24 tests.
- `PYTHONPATH=. pytest tests/test_frontend_regressions.py -q -k 'not task_parser_preserves_selected_multiple_assignees'` passed: 177 tests, 1 deselected.
- Full `tests/test_frontend_regressions.py` currently hits an environment-level optional dependency failure on `zeroconf` when one unrelated task-parser test imports `canopy`; this appears unrelated to the diagnostics patch.

## Follow-Up Ideas
- Add a small “copy summary” endpoint later if admins want a smaller support snippet without downloading the full file.
- If production deployments add non-standard log locations, consider storing the active log directory in app config during `setup_logging` so diagnostics can include it without relying on handler inspection.
