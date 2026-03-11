# Canopy v0.4.74 GitHub Release Copy

Canopy `v0.4.74` focuses on tightening one of the most important coordination paths in the product: structured requests that carry explicit ownership.

This release hardens request member persistence so assignees and reviewers do not disappear under SQLite write contention, restores authenticated `/api/v1/info` trust reporting, and brings the current docs/release pointers back into line with the real development surface.

## Highlights

### Request member write-path hardening
Request upsert and update flows now replace request members inside the active write transaction instead of opening a second nested write path.

That removes a self-locking failure mode where request member writes could log `database is locked` and return a request whose `members` list was unexpectedly empty.

Standalone request member replacement still keeps explicit retry/backoff behavior for transient lock contention.

### `/api/v1/info` trust manager fix
Authenticated `GET /api/v1/info` once again returns trust statistics as intended.

The route now reads the trust manager from the correct application component slot instead of attempting to call trust methods on the wrong object.

### Regression coverage for both fixes
This release adds targeted regression coverage for:
- authenticated `/api/v1/info` trust-manager wiring
- request upsert with inline member persistence
- standalone request member retry behavior after transient SQLite lock contention

## Why this matters

Canopy is strongest when the coordination structures inside the workspace are as dependable as the chat surface around them.

`v0.4.74` improves that in a narrow but important way: requests with explicit membership now survive real write-path pressure more reliably, and operator-facing system info is trustworthy again for authenticated clients and tools.

## Upgrade notes

- No manual migration is required for normal upgrades.
- Existing request records remain valid.
- This release is primarily a reliability and docs-alignment pass, not a schema expansion.

## Full changelog

See [`CHANGELOG.md`](../CHANGELOG.md) for the detailed change history.
