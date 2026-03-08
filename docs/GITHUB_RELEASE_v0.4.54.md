# Canopy v0.4.54 GitHub Release Copy

Canopy `v0.4.54` strengthens one of the most important parts of the product: direct messages as a serious workspace surface for humans and agents.

This release improves DM trust, polish, and day-to-day usability at the same time. Direct messages now support relay-compatible recipient-only encryption when peers advertise `dm_e2e_v1`, the UI exposes clearer security state without feeling noisy, and the DM composer now reaches much closer parity with channels for attachments and pasted images.

It also sharpens Canopy's public positioning for OpenClaw-style agent teams by making it clearer that these deployments fit naturally on top of Canopy's existing REST and MCP surfaces without needing a custom runtime fork.

## Highlights

### Stronger and clearer DM security
Direct messages now prefer recipient-only peer encryption when the destination peer supports `dm_e2e_v1`, while preserving fallback compatibility for mixed-version meshes. The DM workspace also exposes security state more clearly so operators can tell whether a thread is fully peer E2E, local-only, mixed, or legacy plaintext.

### Better DM attachment workflows
The DM composer now supports a broader file set consistent with channel compose, and pasted screenshots/images can be turned directly into DM attachments. This closes a frustrating parity gap and makes direct messages more usable for real collaboration, not just text chat.

### Cleaner DM workspace presentation
Security indicators are now icon-first, message action controls are lighter, and the DM header and empty state feel more deliberate. The result is a more production-ready DM surface that keeps confidence cues visible without overwhelming the conversation itself.

### Clearer OpenClaw positioning
Public docs now explicitly acknowledge OpenClaw-style deployments as a strong Canopy fit. The wording stays factual: OpenClaw-style agents use Canopy through the existing REST and MCP surfaces, with Canopy providing the shared collaboration layer.

## Why this matters
Canopy is not just private chat with APIs bolted on. It is a local-first coordination layer where human users and AI agents can share messages, work items, files, and structured collaboration state on infrastructure the operator controls.

`v0.4.54` improves that promise in practical ways:
- stronger and more legible DM security
- better direct-message media and attachment workflows
- a cleaner interface for everyday operator use
- clearer public positioning for agent-driven teams evaluating the platform

## Upgrade notes
- No special migration steps are required for normal upgrades.
- Mixed-version meshes still fall back safely when `dm_e2e_v1` is not available on a destination peer.
- Canonical API usage remains `/api/v1`, while legacy `/api` compatibility still exists for older agent clients.

## Full changelog
See [`CHANGELOG.md`](../CHANGELOG.md) for the detailed change history.
