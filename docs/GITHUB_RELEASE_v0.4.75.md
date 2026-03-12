# Canopy v0.4.75 GitHub Release Copy

Canopy `v0.4.75` focuses on making the Channels experience more responsive without widening the product into a second rendering model.

This release extends the unified workspace event journal so the Channels UI can apply common channel state changes incrementally, keeps the active channel-thread event cursor isolated so message edit/delete events are not skipped, and refreshes the current release/onboarding docs so the repo stays ready for the next public push.

## Highlights

### Incremental channel state updates
The Channels UI now applies common `channel.state.updated` changes in place for:
- lifecycle updates
- privacy changes
- notification toggles
- member-count changes
- channel deletion handling

That means the sidebar no longer needs a full snapshot refresh for every routine state change.

### Channel thread cursor isolation hardening
This release also fixes a subtle event-consumer race in the active channel thread.

The thread consumer now keeps its own workspace-event cursor instead of borrowing the sidebar cursor, so unseen `channel.message.edited` and `channel.message.deleted` events are not skipped when unrelated sidebar state events advance first.

### Channel message snapshot cursor hardening
`/ajax/channel_messages/<channel_id>` now captures its workspace-event cursor before building the message snapshot response.

That preserves the same cursor-order safety pattern already used in the DM and sidebar consumers, so the thread view does not advance past unseen changes during concurrent activity.

### Onboarding and release-doc refresh
The current docs now better separate:
- packaged Windows users
- technical repo users
- agent operators

This keeps the working repo and release copy easier to publish cleanly.

## Why this matters

Canopy’s event journal is most valuable when it improves live responsiveness without becoming a second rendering truth or introducing missed-update races.

`v0.4.75` pushes that forward carefully: more channel UI state now updates in place, while the existing snapshot routes still define what the interface should render when richer state recovery is needed.

## Upgrade notes

- No manual migration is required for normal upgrades.
- Existing snapshot/render routes remain in place.
- This is a UI responsiveness and correctness pass, not a protocol reset.

## Full changelog

See [`CHANGELOG.md`](../CHANGELOG.md) for the detailed change history.
