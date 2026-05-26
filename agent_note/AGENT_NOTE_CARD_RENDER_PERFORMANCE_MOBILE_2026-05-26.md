# Agent Note: Card Rendering Performance + Mobile Hardening

Date: 2026-05-26
Branch: `codex/card-render-performance-mobile-0.6.247`
Version bump: `0.6.246` -> `0.6.247`

## Summary

This patch is a focused frontend performance and mobile-layout hardening pass for high-noise channel/feed/DM surfaces, especially Agent Run Capsules and rendered structured cards.

The most important fix is that Agent Run Capsule traces no longer eagerly render every hidden source post while the capsule is collapsed. Previously, a compressed run still paid the DOM/render cost of every hidden message, attachment, reaction control, source layout, and card inside the hidden trace. That defeated the purpose of compression on busy agent channels. The trace is now stored as a lightweight payload and rendered only when the user opens the capsule trace or jumps to a source post.

## Changes Made

### Capsule trace performance

File: `canopy/ui/templates/channels.html`

- Added `channelAgentRunCapsuleTracePayloads` as a per-render payload store.
- `renderAgentRunCapsule(...)` now stores the source messages, user info, reply metadata, and root-message IDs without calling `renderMessage(...)` for the hidden trace.
- Added `renderAgentRunCapsuleTrace(capsule)` to lazily render the exact source posts only when needed.
- `toggleAgentRunCapsuleTrace(...)` now renders the trace on first open.
- `focusAgentRunCapsuleSource(...)` now ensures the trace exists before focusing the requested source message.
- `displayMessages(...)` clears both enrichment and trace payload stores on rerender to prevent stale channel render state.

### Channel/feed rendered-card hardening

Files:

- `canopy/ui/templates/channels.html`
- `canopy/ui/templates/feed.html`

Cards covered:

- Agent Run Capsules
- inline task cards
- polls
- circles
- skills
- handoffs
- community notes
- objectives
- requests
- signals
- collaboration input/telemetry cards
- contracts

Changes:

- Added bounded `max-width`, `min-width`, and `overflow-wrap` rules to prevent long text, URLs, structured data, or agent output from forcing horizontal overflow.
- Added layout/style containment to reduce expensive layout invalidation during long channel/feed scrolls.
- Added guarded `content-visibility: auto` with intrinsic sizing so offscreen cards are cheaper to paint in long streams.
- Added mobile stacking rules so card headers, metadata rows, action groups, telemetry grids, and buttons collapse into touch-friendly vertical layouts instead of squeezing or overflowing.

Important detail: I intentionally used `contain: layout style` instead of `contain: layout paint`, because paint containment can clip dropdowns/flyouts inside cards or message rows.

### DM card/attachment hardening

File: `canopy/ui/templates/messages.html`

- Added bounded layout/style containment for DM conversation cards, DM rows, attachment cards, empty cards, and search result cards.
- Added guarded `content-visibility: auto` for long DM threads and attachment-heavy conversations.
- Added mobile attachment-card rules so attachment metadata and actions stack cleanly on narrow screens.

## Tests / Verification

Passed:

```bash
node <<'NODE'
const fs = require('fs');
const text = fs.readFileSync('canopy/ui/templates/channels.html', 'utf8');
const start = text.indexOf('function renderAgentRunCapsuleTrace');
const end = text.indexOf('function isAgentRunCapsuleNearViewport');
if (start < 0 || end < 0 || end <= start) throw new Error('lazy trace helper chunk missing');
new Function(text.slice(start, end));
console.log('lazy capsule trace JS parses');
NODE
```

```bash
PYTHONPATH=. pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_channel_header_can_hide_agent_only_threads -q
PYTHONPATH=. pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_mobile_resize_dedup_gates_collapse_redundant_layout_work -q
PYTHONPATH=. pytest tests/test_frontend_regressions.py::TestFrontendRegressions::test_api_reference_tracks_recent_dm_collab_and_privacy_surfaces -q
git diff --check
```

Full frontend regression file:

```bash
PYTHONPATH=. pytest tests/test_frontend_regressions.py -q
```

Result: `176 passed`, `1 failed` due the known local dependency gap:

```text
ModuleNotFoundError: No module named 'zeroconf'
```

The failing test imports `canopy.core.tasks`, which imports the app/network stack and trips the missing local `zeroconf` package. This appears unrelated to the card rendering patch.

## Review Focus For Canopy Dev Bot

Please review these areas carefully:

1. Confirm `content-visibility: auto` does not interfere with any card-specific browser find behavior or dynamically measured card content.
2. Confirm mobile action stacking feels good for high-density structured cards, especially request, signal, telemetry, and contract cards.
3. Confirm Capsule trace opening remains correct when jumping from a run map node, artifact rail source button, brief row, or metadata chip.
4. Confirm dropdowns/flyouts are not clipped. This patch avoids paint containment specifically to protect those controls.

## Expected User Impact

- Busy agent channels should feel much lighter when Capsules are enabled because hidden source posts no longer render until opened.
- Mobile users should see fewer squeezed headers, overflowing buttons, and clipped structured-card rows.
- Long DM threads with media/attachments should scroll more predictably.
