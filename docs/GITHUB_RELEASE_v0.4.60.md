# Canopy v0.4.60 GitHub Release Copy

Canopy `v0.4.60` adds a safer path for larger attachments without destabilizing the existing mesh.

This release introduces a managed large-attachment store with a fixed protocol threshold, metadata-first propagation, background download by default, and peer-authorized remote fetch. The goal is straightforward: keep larger files available across the mesh without turning normal sync traffic into a blob transport.

## Highlights

### Managed large-attachment store
Attachments larger than `10 MB` now switch to a metadata-first path instead of being embedded directly into mesh sync payloads. Messages and DMs still arrive immediately, but the file itself can be fetched separately from the source peer.

That means:
- less sync-payload bloat for larger files
- better behavior for attachment-heavy teams
- a safer foundation for future replication and caching work

### Automatic download by default
Authorized peers now auto-download large attachments in the background by default. That matters because many peers are only online intermittently; forcing users to click-to-fetch later would make availability worse, not better.

Operators who need tighter disk control can switch the node to:
- `Manual`
- `Paused`

without changing the protocol threshold.

### Fixed threshold for mesh stability
The `10 MB` cutoff is intentionally fixed in `v1`. Operators can control caching behavior, but not the threshold itself.

This keeps mixed-version meshes easier to reason about:
- sender and receiver do not disagree on whether a file should sync inline
- protocol behavior stays stable during rollout
- debugging stays simpler

### Peer-authorized remote fetch
Large attachment fetches are authorized against the source content:
- open/public channel content remains fetchable where appropriate
- network/public feed content remains fetchable where appropriate
- DM fetches are only allowed when the requesting peer hosts a participant

This keeps the feature aligned with Canopy's content-scoped access model instead of creating a side door around it.

## Why this matters

Canopy has always needed a better answer for larger files than either:
- bloating the sync path
- or forcing users to give up on mesh availability

`v0.4.60` is the first disciplined step:
- fixed threshold
- managed storage root
- background download
- conservative authorization
- backward-compatible fallback behavior

It is not yet a full replicated object-store system, but it is a solid and safer base to build on.

## Upgrade notes

- No manual migration is required for normal upgrades.
- Existing small attachment behavior is unchanged.
- Large attachments now propagate as metadata-first references across capable peers.
- Nodes that leave the large-attachment store root blank still work; Canopy falls back to the normal local file storage root.
- The manual remote-download trigger currently lives in the authenticated UI path, while automatic download remains the default node behavior.

## Full changelog

See [`CHANGELOG.md`](../CHANGELOG.md) for the detailed change history.
