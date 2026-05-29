# Agent Note: Capsule Artifact Readability Polish

Date: 2026-05-29
Branch: `codex/capsule-artifact-polish-0.6.275`
Version: `0.6.275`

## Context
Konrad reviewed Capsule artifact behavior and raised three UX issues:

- Long filenames in Capsule output rails would marquee left and feel like they never returned to the readable start.
- The output/workproduct icon could be slightly larger for scannability.
- Business documents should stand out more because they are high-value user-actionable outputs.

## Implemented Changes

### 1. Filename marquee now cycles back predictably
- Updated the long-label hover/focus animation from an `alternate` animation to a deterministic cycle:
  - readable start hold,
  - pan to the tail,
  - short tail hold,
  - return to the start.
- This prevents the perceived failure mode where the filename scrolls away and seems gone.
- Reduced-motion behavior remains unchanged: no marquee animation when `prefers-reduced-motion` is active.

### 2. Capsule artifact icons are more readable
- Increased direct artifact icon size from `2rem` to `2.22rem`.
- Mobile/narrow icon size increased from `1.8rem` to `1.95rem`.
- This preserves the compact rail layout while making the workproduct/file type easier to scan.

### 3. Business-document outputs get stronger visual treatment
- Added presentational classification for likely business-document artifacts:
  - PDF,
  - Word / OpenDocument / RTF / Pages,
  - PowerPoint / Keynote-style formats,
  - Excel / Numbers / OpenDocument spreadsheets,
  - CSV / TSV.
- These artifacts receive a warmer document treatment in both dark and light themes.
- File references that initially render as fallback IDs can also receive the business-document class after their real filename and content type hydrate through the existing file-reference metadata path.

## Safety Notes
- No file-open, save-to-vault, trace, Capsule grouping, or backend logic was changed.
- Business-document classification is visual only; it does not grant access, change links, or alter file handling.
- Hydrated file-reference styling only adds a CSS class to the existing Capsule artifact card when the resolved filename/content type clearly looks like a business document.

## Files Changed
- `canopy/ui/templates/_channels_agent_run_capsule_styles.html`
- `canopy/ui/templates/channels.html`
- `canopy/ui/static/js/canopy-main.js`
- `tests/test_frontend_regressions.py`
- `CHANGELOG.md`
- `pyproject.toml`
- `canopy/__init__.py`
- `README.md`

## Validation
- `venv/bin/python -m pytest tests/test_frontend_regressions.py tests/test_ui_polish_regressions.py -q`
  - Result: `211 passed in 12.24s`
- Jinja parse check for:
  - `channels.html`
  - `_channels_agent_run_capsule_styles.html`
- `git diff --check`
