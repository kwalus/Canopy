# Agent Note: Capsule Vertical Run Map UI

Date: 2026-05-26
Author: Codex Agent
Target version: 0.6.248
Branch: `codex/capsule-vertical-run-map-0.6.248`

## Summary

This patch refines the Agent Run Capsule right rail. The previous run map used a compact horizontal strip on the right side of the Capsule card. In real channel use that wasted vertical space and felt disconnected from the source posts, which are naturally chronological/vertical. The map is now a vertical source spine with per-stop identity and content context.

## User-facing behavior

- Capsule run maps now render vertically in the right rail.
- Each source stop includes the posting user's avatar when available, or initials as a fallback.
- Each stop shows a compact signal label, recency, author name, and short excerpt.
- Count badges are integrated into the node metadata row instead of floating over the node.
- The existing click-to-source behavior remains unchanged: each stop still jumps to its source post, and the header stop count still opens the full source trace.
- Mobile and narrow card layouts retain bounded height and scrolling rather than forcing the Capsule card to grow indefinitely.

## Implementation details

Updated `canopy/ui/templates/channels.html`:

- Added `getAgentRunMapAuthor(message, userInfo)` to resolve author display name, avatar URL, and initials from the existing `userInfo` map and message fallback fields.
- Extended `buildAgentRunCapsuleMapNodes(...)` to carry author and excerpt metadata into each map node.
- Added `renderAgentRunMapAvatar(author)` and `getAgentRunMapSignalLabel(signal)` for compact readable source stops.
- Updated `renderAgentRunCapsuleSignalMap(...)` to accept `userInfo`, render avatar-rich vertical nodes, and preserve existing source navigation.
- Reworked `.agent-run-capsule-map*` CSS from a horizontal mini timeline to a vertical rail with a subtle vertical spine, scroll bounds, avatar cells, signal labels, author names, and excerpts.
- Added light-theme adjustments for avatar and signal contrast.

Updated regression coverage in `tests/test_frontend_regressions.py` to assert the new function signatures and the avatar/excerpt classes.

Bumped version references from `0.6.247` to `0.6.248` in the usual version/doc surfaces.

## Verification

Commands run:

```bash
node <<'NODE'
const fs = require('fs');
const text = fs.readFileSync('canopy/ui/templates/channels.html', 'utf8');
const start = text.indexOf('function getAgentRunMapAuthor');
const end = text.indexOf('function agentRunHasWorkArtifacts');
if (start < 0 || end < 0 || end <= start) throw new Error('map chunk missing');
new Function(text.slice(start, end));
console.log('capsule map JS parses');
NODE

git diff --check
```

Result: passed.

```bash
PYTHONPATH=. pytest \
  tests/test_frontend_regressions.py::TestFrontendRegressions::test_channel_header_can_hide_agent_only_threads \
  tests/test_frontend_regressions.py::TestFrontendRegressions::test_mobile_resize_dedup_gates_collapse_redundant_layout_work \
  tests/test_frontend_regressions.py::TestFrontendRegressions::test_api_reference_tracks_recent_dm_collab_and_privacy_surfaces \
  -q
```

Result: `3 passed`.

## Review focus

Please review the Capsule card in a busy agent-heavy channel at multiple widths:

1. Confirm the vertical rail uses right-side space more naturally than the prior horizontal strip.
2. Confirm avatar/initial rendering does not cause image distortion or layout jitter.
3. Confirm long author names and excerpts truncate cleanly.
4. Confirm the rail remains useful in mobile/narrow cards and scrolls internally instead of expanding the post excessively.
5. Confirm click targets still jump to the intended source post.
