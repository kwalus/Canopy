# Agent Note - Capsule Actionable Source Rail Cleanup

Date: 2026-05-26
Branch: `codex/capsule-actionable-ux-0.6.240`
Version bump: `0.6.239` -> `0.6.240`

## User report

Konrad reviewed a Capsule in `0.6.238` where the collapsed card showed `1 file source` and a `Files & outputs` rail even though the underlying post did not contain a real file reference. The visible text included agent/team names such as `@Forge_McClaw`, which exposed a false-positive file detection problem. He also noted that the card should use its available space better, make more card elements directly actionable, and avoid stale helper copy such as `Jump back in` once users learn the `Show trace` interaction.

## Root cause

`canopy/ui/templates/channels.html` was counting and collecting file references with a permissive bare-token regex:

- any `F...` word-like token could be treated as a Canopy file ID;
- agent names such as `@Forge_McClaw` can satisfy that shape;
- the Capsule then inferred a file rail and file-count chip from ordinary mention text.

## Changes made

### Source-accurate file detection

Updated Capsule artifact collection so direct file rails now require explicit file-reference syntax:

- Markdown links to `/files/<id>` or `/file-ref/<id>`;
- rendered anchor links to `/files/<id>` or `/file-ref/<id>`;
- explicit `file:<id>` references;
- quoted/backticked file IDs such as `'F...'` or `` `F...` ``;
- explicit metadata labels such as `file_id: F...`, `source_file_id: F...`, `vault_file_id: F...`, `attachment_id: F...`, `origin_file_id: F...`, or `image_file_id: F...`.

Removed the bare `\b(F...)\b` Capsule extraction path so `@Forge_McClaw` and similar agent names no longer create fake file artifacts.

### More actionable Capsule cards

Changed the compressed brief rows from passive panels into buttons:

- `What changed` opens the source trace at the message that produced the detected outcome/fallback.
- `Needs attention` opens the source trace at the message that appears blocked or permission-gated.
- `Source trail` opens the trace for the run.
- `Next action` replaces the older `Jump back in` copy and opens the most relevant source message when available.

Changed Capsule metadata chips into clickable controls that open the trace, so the counts are not dead decoration.

### Layout and affordance polish

- Brief cards now have hover/focus styling and a small `Open source` affordance.
- Chip hover/focus styling clarifies that chips are interactive.
- Copy summary now says `Next action` instead of `Jump back in`.

## Files changed

- `canopy/ui/templates/channels.html`
- `tests/test_frontend_regressions.py`
- `CHANGELOG.md`
- `README.md`
- `canopy/__init__.py`
- `pyproject.toml`
- `uv.lock`
- `docs/API_REFERENCE.md`
- `docs/AGENT_ONBOARDING.md`
- `docs/MCP_QUICKSTART.md`

## Verification

Passed:

```bash
pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_channel_header_can_hide_agent_only_threads tests/test_frontend_regressions.py::TestFrontendRegressions::test_mobile_resize_dedup_gates_collapse_redundant_layout_work tests/test_frontend_regressions.py::TestFrontendRegressions::test_api_reference_tracks_recent_dm_collab_and_privacy_surfaces -q
python -m py_compile canopy/__init__.py
git diff --check
```

Additional manual JS extraction check passed with Node:

- `@Forge_McClaw owns the implementation...` produced `0` Capsule file refs.
- explicit refs like `file:'F8a74478bacf41437caae9bd5'` and `'F4532d53bca2f02f358b400e6'` produced `2` direct artifacts.

Full `pytest tests/test_frontend_regressions.py -q` was also attempted. It ran the static tests but failed on an existing local dependency/import path issue unrelated to this patch: `ModuleNotFoundError: zeroconf` through `canopy/network/discovery.py`. `uv` is not available in this shell (`zsh:1: command not found: uv`), so I used the targeted test set plus the direct JS semantic check above.

## Review notes

This is intentionally a narrow, safe Capsule patch. It does not change the compression grouping algorithm or server data model. It only makes the collapsed card more accurate and more directly navigable.

Likely future improvements:

- Hydrate Capsule file rail labels from `/ajax/files/<id>/reference` once rendered, the same way inline file pills do.
- Add a one-click `Reply from this source` control on the brief rows if users want to interject from the Capsule without manually opening the source trace first.
