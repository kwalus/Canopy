# Canopy v0.4.71 GitHub Release Copy

Canopy `v0.4.71` focuses on extending the unified workspace event journal into the remaining high-traffic sidebar surfaces while keeping the existing render contracts intact.

This release moves the DM workspace, shared recent-DM rail, and channel sidebar onto journal-driven change detection, hardens cursor handling so live consumers do not skip unseen updates during concurrent activity, and preserves the snapshot-based rendering paths that already define current truth in the UI.

## Highlights

### Unified workspace event journal Patch 2 for DMs
The DM workspace now uses the local workspace event journal as its live change detector instead of refreshing the thread snapshot on a fixed timer regardless of change.

The existing DM snapshot route remains the rendering source of truth, and a safety resync still exists for recovery if an event poll is missed or the client drifts.

### Unified workspace event journal Patch 3 for the recent-DM rail
The shared recent-DM sidebar now follows the same pattern:
- journal-driven change detection
- dedicated compact snapshot refresh only when DM-relevant events arrive
- preserved queueing behavior
- preserved long-interval safety resync

This removes repeated recent-DM rail rebuilds from the generic peer-activity poll path.

### Unified workspace event journal Patch 4 for the channel sidebar
The Channels page sidebar now also uses the workspace event journal as its change detector while keeping the existing sidebar snapshot/render path intact.

Relevant channel changes now trigger a sidebar refresh only when channel-relevant events arrive, rather than forcing the sidebar to depend on timed polling alone.

### Cursor race hardening across live event consumers
This release also fixes an important reliability issue in the event-driven UI consumers.

The DM thread snapshot, shared recent-DM sidebar snapshot, and initial channel sidebar render now capture their workspace-event cursors before rebuilding snapshot state. That prevents the UI from advancing its local cursor past changes that are not yet represented in the rendered payload during concurrent activity.

## Why this matters

Canopy’s event journal is most useful when it improves responsiveness without becoming a second rendering truth or introducing subtle missed-update races.

`v0.4.71` pushes that approach forward carefully: more UI surfaces now react to real changes, but the established snapshot routes still define what the interface should render.

## Upgrade notes

- No manual migration is required for normal upgrades.
- The workspace event journal remains additive and local-only.
- Existing snapshot/render routes remain in place for DM and channel surfaces.
- `attachment.available` remains DM-scoped in the current event-journal rollout.

## Full changelog

See [`CHANGELOG.md`](../CHANGELOG.md) for the detailed change history.
