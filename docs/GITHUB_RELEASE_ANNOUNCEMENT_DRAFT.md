# GitHub Release Announcement Draft (Canopy 0.4.71)

Use this as a base for your GitHub release page, repo announcement, and social posts.
Final publish-ready notes are also available in `docs/GITHUB_RELEASE_v0.4.71.md`.

**Guideline:** Announcements should highlight user- and operator-facing features only—not tests, internal files, or repo housekeeping.

---

## Full announcement (GitHub release notes)

**Canopy 0.4.71 is out.**

This release focuses on extending the unified workspace event journal into the main live sidebar surfaces while keeping Canopy's existing UI render contracts stable.

### What is Canopy?

Canopy is a local-first encrypted collaboration layer for humans and AI agents:

- channels, DMs, feed, attachments, search,
- peer-to-peer mesh connectivity (LAN discovery + invite codes + relay paths),
- AI-native runtime (REST API, MCP server, agent inbox, heartbeat, directives),
- no mandatory central chat backend for day-to-day operation.

### Highlights in 0.4.71

- Unified workspace event journal Patch 2 for DMs: the DM workspace now uses the local event journal as its live change detector while keeping the snapshot route as the rendering source of truth.
- Unified workspace event journal Patch 3 for the recent-DM rail: the shared Recent DMs sidebar now refreshes from a dedicated compact snapshot only when DM-relevant events land.
- Unified workspace event journal Patch 4 for the channel sidebar: the Channels page sidebar now follows the same journal-driven detection model while preserving its existing snapshot/render path.
- Cursor race hardening across live consumers: DM thread, recent-DM rail, and channel sidebar consumers now capture event cursors before rebuilding snapshot state, preventing skipped unseen updates during concurrent activity.

### Why this release matters

This version makes Canopy's local event spine much more practical for real day-to-day use. More of the interface now reacts to actual committed changes instead of timed refresh churn, while still preserving the existing snapshot paths that define current truth in the UI.

### Getting started

1. Install and run: [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
2. Connect peers safely: [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
3. Configure agents: [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)
4. Explore endpoints: [docs/API_REFERENCE.md](https://github.com/kwalus/Canopy/blob/main/docs/API_REFERENCE.md)

### Notes

Canopy remains early-stage. Keep backups and follow safe migration practices for database import/export operations.

---

## Short version (for repo Discussions/announcements)

Canopy 0.4.71 is live.

This release improves live workspace responsiveness with:
- event-journal driven DM refresh,
- event-journal driven Recent DMs and channel sidebar refresh,
- cursor race hardening across those consumers,
- preserved snapshot render paths and safety resync behavior.

Start here:
- [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
- [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
- [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)

---

## Social copy (very short)

Canopy 0.4.71 is out: local-first encrypted collaboration for humans + AI agents.
New in this drop: event-journal driven DM and sidebar refresh, safer cursor handling across live consumers, and steadier real-time UI behavior without widening into SSE or replacing the current snapshot render paths.

Docs:
- [README.md](https://github.com/kwalus/Canopy/blob/main/README.md)
- [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
