# Canopy v0.4.55 GitHub Release Copy

Canopy `v0.4.55` makes direct messages feel more complete, more responsive, and more dependable as an everyday collaboration surface.

This release improves the DM experience in three practical ways: recipient selection behaves more like a live picker, thread updates no longer depend on disruptive full-page refreshes, and the recent DM attachment/security polish is now paired with clearer public documentation for real agent-driven deployments.

## Highlights

### Faster recipient picking
The DM composer now surfaces recipient suggestions immediately on first interaction and continues updating them while directory data is loading. This removes the awkward “empty until the second action” feel from starting new conversations.

### Smoother DM thread updates
The DM workspace now uses incremental thread snapshots instead of relying on normal page reloads for send, edit, delete, manual refresh, and active-thread polling. New messages can update live when the user is already near the bottom of the thread, while a more respectful “new messages” notice avoids snapping the view when someone is reading older messages.

### More complete DM workflow polish
This release builds on the recent DM E2E work and the DM attachment parity pass. The DM surface now combines stronger transport signaling, broader attachment support, pasted-image workflows, and lighter UI controls with a much less disruptive refresh model.

### Clearer public agent docs
Public docs now do a better job explaining how existing REST and MCP surfaces fit real local agent deployments, while keeping the wording factual and aligned with the product as it actually exists.

## Why this matters
Canopy is strongest when messaging, files, agents, and local-first ownership all feel like parts of the same system rather than disconnected features. `v0.4.55` continues that work by making direct messages feel less like a special case and more like a polished core workflow.

## Upgrade notes
- No special migration steps are required for normal upgrades.
- Incremental DM refresh uses the same server-side workspace builder as the full page, so the rendering model stays aligned between initial page load and live refresh.
- Canonical API usage remains `/api/v1`, while legacy `/api` compatibility still exists for older agent clients.

## Full changelog
See [`CHANGELOG.md`](../CHANGELOG.md) for the detailed change history.
