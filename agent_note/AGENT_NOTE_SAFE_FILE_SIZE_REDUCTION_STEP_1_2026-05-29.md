# Agent Note: Safe File Size Reduction Step 1

Date: 2026-05-29
Branch: `codex/safe-file-size-step-0.6.274`
Version: `0.6.274`

## Context
Konrad requested a first, maximally safe step toward reducing the size of the largest Canopy source files. The explicit constraint was no functionality loss and no logic rewrite. Prior attempts by agents had apparently rewritten behavior while splitting files, so this patch deliberately avoids touching logic.

## What Changed

### Extracted Capsule CSS into a template partial
- Moved the contiguous agent-run Capsule CSS block out of `canopy/ui/templates/channels.html`.
- New file: `canopy/ui/templates/_channels_agent_run_capsule_styles.html`.
- `channels.html` now includes that partial at the same exact location where the CSS previously lived:
  - `{% include "_channels_agent_run_capsule_styles.html" %}`

## Why This Is Safe
- No JavaScript was moved.
- No Python was moved.
- No template markup/DOM structure was changed.
- No CSS declarations were rewritten during the extraction.
- The include is placed at the same cascade position, preserving style ordering for the extracted block.
- This reduces the largest template by about 1,350 lines without affecting runtime behavior.

## Size Impact
- `canopy/ui/templates/channels.html`: about 30,708 lines before the recent split sequence, now 29,361 lines.
- New extracted partial: 1,348 lines.

## Validation
- `venv/bin/python -m pytest tests/test_frontend_regressions.py -q`
  - Result: `178 passed in 3.02s`
- `venv/bin/python -m pytest tests/test_ui_polish_regressions.py -q`
  - Result: `33 passed in 0.11s`
- Jinja parse check:
  - `channels.html` and `_channels_agent_run_capsule_styles.html` both parsed through a `jinja2.Environment` with the templates directory loader.

## Review Guidance
This should be reviewed as a move-only refactor. The most important checks are:
- Confirm `channels.html` includes the new partial in the style block.
- Confirm the extracted CSS content is present exactly once in the rendered page.
- Confirm no Capsule behavior, grouping, trace expansion, map clicks, output links, or enrichment logic changed.

## Suggested Next Safe Step
If this pattern proves safe in testing, the next similarly low-risk move would be another isolated CSS/template partial extraction from `channels.html` or `base.html`, not a JavaScript split yet. JavaScript extraction should wait until we can define strict module boundaries and browser-level smoke tests.
