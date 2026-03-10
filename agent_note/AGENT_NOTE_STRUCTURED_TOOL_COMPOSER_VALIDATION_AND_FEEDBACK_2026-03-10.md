# Agent Note: Structured Tool Composer Validation and Feedback (2026-03-10)

## Scope
Local-only implementation in the Dropbox repo:
- local Dropbox Canopy workspace

Nothing from this pass was synced to:
- local `Canopy-Dev` working tree

## Problem
Agents in `#canopy-architecture` are attempting to use Canopy's inline coordination blocks, but the current UX makes it too easy to post malformed or near-miss syntax that humans can read but Canopy cannot reliably materialize.

Observed failure modes:
- non-canonical block names such as `[artifact]`, `[status]`, `[request-accepted]`
- valid blocks wrapped in markdown decoration like `**[task] ...**`
- no send-time validation
- no in-composer templates for canonical block creation
- no confirmation after send showing what actually materialized into first-class Canopy objects

## Implemented
### 1. Shared structured-composer utility
Updated:
- `canopy/ui/static/js/canopy-main.js`

Added to `window.canopyStructuredComposer`:
- canonical supported tag set
- canonical template labels
- `buildToolBlock(toolType, sourceText)`
- `applyTemplateToDraft(toolType, currentText)`
- `hasStructuredToolBlock(text)`
- `validate(text)`
- alias replacement helper
- syntax normalization helper

Supported canonical types in the shared helper now include:
- `task`
- `request`
- `objective`
- `handoff`
- `signal`
- `circle`
- `contract`
- `skill`

Important behavior:
- plain text draft -> clicking a tool now wraps that draft into a canonical block
- existing structured draft -> clicking a tool appends a fresh canonical block instead of duplicating the current draft body into a second block

### 2. Channel composer UX
Updated:
- `canopy/ui/templates/channels.html`

Added:
- `Structured tools` dropdown with canonical block templates:
  - task
  - request
  - objective
  - handoff
  - signal
- send-time validation panel for malformed/unknown blocks
- inline fix actions for known aliases
- normalization action for decorated block syntax
- post-send result panel showing which structured objects actually materialized

Behavior:
- message send is blocked if structured validation finds blocking issues
- successful send now displays authoritative `structured_objects` returned by the server

### 3. Feed composer UX
Updated:
- `canopy/ui/templates/feed.html`

Added the same UX model as channels:
- structured tool insertion dropdown
- validation panel
- result/materialization panel
- send-time blocking on malformed blocks
- post-send materialization feedback from the route response

Additional details:
- composer reset now clears validation and prior materialization state
- result rendering happens after reset so the user can briefly see what materialized before feed refresh

### 4. Route-layer materialization feedback
Previously added and retained in this pass:
- `canopy/ui/routes.py`

Confirmed behavior:
- `/ajax/create_post` returns `structured_objects`
- `/ajax/send_channel_message` returns `structured_objects`
- inline handoffs are now synchronized in the local UI write path for both feed posts and channel messages
- edit paths also synchronize inline handoffs so canonical tool behavior is consistent over time
- post-send structured summaries now also cover inline `contract` and `circle` materialization when those managers resolve durable objects

## Why this matters
This reduces the gap between “agents tried to coordinate” and “Canopy actually captured durable structured work.”

The intended operator outcome is:
- faster, more canonical composer usage
- fewer malformed pseudo-tool posts
- clearer feedback on whether a post created a real task/request/objective/handoff/signal
- better agent coordination quality without relying purely on training or discipline

## Files changed
### Runtime / UI
- `canopy/ui/static/js/canopy-main.js`
- `canopy/ui/routes.py`
- `canopy/ui/templates/channels.html`
- `canopy/ui/templates/feed.html`

### Tests
- `tests/test_frontend_regressions.py`
- `tests/test_ui_structured_tool_feedback.py`

## Validation
### Template and syntax checks
- `python scripts/check_jinja_templates.py`
  - result: passed
- `node --check canopy/ui/static/js/canopy-main.js`
  - result: not run in this review environment (`node` not installed on this machine)
- `python -m py_compile canopy/ui/routes.py tests/test_ui_structured_tool_feedback.py tests/test_frontend_regressions.py`
  - result: passed

### Focused regression suite
- `pytest -q tests/test_frontend_regressions.py tests/test_ui_structured_tool_feedback.py`
  - result: `7 passed`
- `pytest -q tests/test_frontend_regressions.py tests/test_ui_structured_tool_feedback.py tests/test_messages_ui_workspace.py tests/test_channel_sidebar_state_delta.py`
  - result: `16 passed`

## Reviewer checklist
1. In the channel composer, type plain text and insert a `Task block`.
   - Expected: the plain text is wrapped into a canonical `[task]...[/task]` block.
2. In the channel composer, intentionally type `[artifact]`.
   - Expected: validation panel appears and blocks send.
3. In the channel composer, type `**[task]`.
   - Expected: normalization guidance appears.
4. Send a valid `[handoff]` block in a channel.
   - Expected: result panel shows the structured object that materialized.
5. Repeat the same checks in the feed composer.
6. Confirm malformed posts are stopped before send rather than silently producing no durable object.

## Residual limitations
- This pass does not yet add pre-send validation to every composer surface in the product, only the main feed and channel composers.
- Skills are still not included in the authoritative post-send `structured_objects` summary because the current route helper is intentionally conservative and uses manager-backed lookups only.
- The current composer dropdowns still surface only the five primary coordination blocks (`task`, `request`, `objective`, `handoff`, `signal`) even though the shared helper now recognizes `circle`, `contract`, and `skill`.

## Recommendation
This is appropriate for review and then selective sync into `Canopy-Dev`.

If approved, the next product-grade step would be:
- composer-side block templates/validation for additional authoring surfaces
- explicit dropdown insertion affordances for `circle`, `contract`, and `skill`
- optional post-send confirmation chips that deep-link directly to the created structured object
