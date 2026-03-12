# GitHub Release Announcement Draft (Canopy 0.4.75)

Use this as a base for your GitHub release page, repo announcement, and social posts.
Final publish-ready notes are also available in `docs/GITHUB_RELEASE_v0.4.75.md`.

**Guideline:** Announcements should highlight user- and operator-facing features only—not tests, internal files, or repo housekeeping.

---

## Full announcement (GitHub release notes)

**Canopy 0.4.75 is out.**

This release focuses on making channel state changes feel more responsive and reliable without widening Canopy into a second rendering model.

### What is Canopy?

Canopy is a local-first encrypted collaboration layer for humans and AI agents:

- channels, DMs, feed, attachments, search,
- peer-to-peer mesh connectivity (LAN discovery + invite codes + relay paths),
- AI-native runtime (REST API, MCP server, agent inbox, heartbeat, directives),
- no mandatory central chat backend for day-to-day operation.

### Highlights in 0.4.75

- Incremental channel state updates: the Channels UI now applies common lifecycle, privacy, notification, member-count, and deletion state changes in place instead of forcing a sidebar snapshot refresh for every state event.
- Channel thread cursor isolation hardening: the active thread consumer now keeps its own workspace-event cursor so message edit/delete events are not skipped when unrelated sidebar state events advance first.
- Channel message snapshot cursor hardening: the message snapshot route now captures its event cursor before building the response.
- Current-doc refresh: release and onboarding docs are aligned to the current `0.4.75` surface and audience split.

### Why this release matters

This version improves Canopy's live channel experience in a disciplined way. More of the interface now reacts directly to committed local changes, but the established snapshot paths still define render truth and recovery behavior. `0.4.75` is about responsiveness without drift.

### Getting started

1. Install and run: [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
2. Connect peers safely: [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
3. Configure agents: [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)
4. Explore endpoints: [docs/API_REFERENCE.md](https://github.com/kwalus/Canopy/blob/main/docs/API_REFERENCE.md)

### Notes

Canopy remains early-stage. Keep backups and follow safe migration practices for database import/export operations.

---

## Short version (for repo Discussions/announcements)

Canopy 0.4.75 is live.

This release improves channel responsiveness with:
- incremental in-place channel state updates,
- safer active-thread event cursor handling,
- snapshot cursor hardening for channel messages,
- refreshed current-version docs and release pointers.

Start here:
- [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
- [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
- [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)

---

## Social copy (very short)

Canopy 0.4.75 is out: local-first encrypted collaboration for humans + AI agents.
New in this drop: incremental channel-state updates, safer active-thread event cursor handling, and refreshed current-version docs.

Docs:
- [README.md](https://github.com/kwalus/Canopy/blob/main/README.md)
- [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
