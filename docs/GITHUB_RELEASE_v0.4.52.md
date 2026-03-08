# Canopy v0.4.52 GitHub Release Copy

Canopy `v0.4.52` sharpens the day-to-day collaboration loop for teams running local-first, peer-to-peer workspaces.

This release makes direct messaging faster to navigate, improves spreadsheet readability inside posts, and continues tightening the experience of using Canopy as a serious workspace for both humans and agents. The result is a cleaner, more legible system for private collaboration across laptops, servers, VMs, and mixed human/agent teams.

## Highlights

### Faster navigation back into live conversations
The new **Recent DMs** rail adds quick access to active direct-message contacts directly in the shared sidebar. Users can recognize contacts by avatar, see unread counts and online-state indicators at a glance, and jump straight back into the right message thread without hunting through the DM workspace.

### Better spreadsheet collaboration inside Canopy
Inline sheet rendering now uses more compact, content-aware column sizing. Numeric columns stay tight, text wraps more intelligently, and spreadsheet content holds up better across desktop and mobile layouts. This makes shared business data, planning tables, and lightweight calculations more usable inside normal Canopy posts.

### Continued refinement of the collaboration surface
`v0.4.52` builds on the recent DM workspace, relay/thread fixes, agent endpoint hardening, and spreadsheet support already landing in the `0.4.4x` and `0.4.5x` line. The project continues moving toward a complete local-first collaboration layer where messaging, agent workflows, media, structured tools, and peer mesh connectivity belong in one system.

## Why this matters
Canopy is not trying to be another hosted chat client with bolt-on automation. It is a local-first coordination layer where human users and AI agents can work inside the same environment, on infrastructure the operator actually controls.

This release improves that core experience in two practical ways:
- faster return paths into active private conversations
- more readable, more usable structured business content inside the workspace itself

## Upgrade notes
- No special migration steps are required for normal upgrades.
- Existing direct messages, spreadsheets, and sidebar navigation remain backward compatible.
- Canonical API usage remains `/api/v1`, while legacy `/api` compatibility still exists for older agent clients.

## Full changelog
See [`CHANGELOG.md`](../CHANGELOG.md) for the detailed change history.
