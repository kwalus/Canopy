# Canopy v0.4.59 GitHub Release Copy

Canopy `v0.4.59` makes the direct-message experience more trustworthy under real peer churn.

This release brings together four practical improvements that matter in day-to-day use: more honest DM security classification, better DM search against older encrypted-at-rest history, cleaner messaging-layout behavior, and less noisy failure handling when a peer connection dies mid-send.

## Highlights

### More trustworthy DM delivery state
Canopy is now more conservative about when it calls a DM recipient `local_only`. If a remote human row has blank or stale `origin_peer` metadata, Canopy no longer quietly assumes the message stayed on the instance. Instead, it keeps the remote delivery path alive unless there is positive evidence that the user is truly local.

That means:
- fewer false `Local only` security states
- safer fallback behavior for legacy or partially synced remote user rows
- less risk of suppressing mesh delivery for a real remote recipient

### DM search now reaches older encrypted history
The DM search path no longer depends on SQL-side plaintext matching. Search now pages through relevant DM history and matches after decrypting stored content, which means older encrypted-at-rest messages are less likely to disappear behind a wall of newer non-matching rows.

Attachment metadata is also included in the searchable surface, so attachment-heavy conversations are easier to recover.

### Better DM workspace scrolling
The messages workspace continues the conversation-first DM redesign with a more stable scroll model. The sidebar, thread, and composer now behave more like coordinated regions of a messaging client instead of one drifting page surface.

This makes it easier to:
- browse the conversation list while reading a long thread
- keep the active thread independently scrollable
- keep the composer visually anchored at the bottom of the thread pane

### Less terminal noise when a peer dies mid-send
When a send times out or the websocket is already closing, Canopy now retires that connection immediately instead of letting queued sends keep hammering the same dead socket. In practice, that reduces the flood of repeated `no close frame received` errors that could follow one broken peer connection.

## Why this matters

Canopy is strongest when operators can trust what the product is telling them. `v0.4.59` improves that trust in two ways:

1. It makes DM transport state more truthful instead of optimistically guessing that a recipient is local.
2. It makes peer-failure behavior less noisy and easier to reason about when the mesh is under churn.

The result is a DM workflow that feels more dependable without changing the overall product model.

## Upgrade notes

- No special migration steps are required for normal upgrades.
- DM security summaries may now show a non-local transport mode for ambiguous recipient rows that were previously misclassified as `local_only`.
- Mixed-version meshes remain supported; the new behavior prefers safer remote delivery instead of suppressing it when recipient provenance is incomplete.

## Full changelog

See [`CHANGELOG.md`](../CHANGELOG.md) for the detailed change history.
