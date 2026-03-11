# GitHub Release Announcement Draft (Canopy 0.4.74)

Use this as a base for your GitHub release page, repo announcement, and social posts.
Final publish-ready notes are also available in `docs/GITHUB_RELEASE_v0.4.74.md`.

**Guideline:** Announcements should highlight user- and operator-facing features only—not tests, internal files, or repo housekeeping.

---

## Full announcement (GitHub release notes)

**Canopy 0.4.74 is out.**

This release focuses on request coordination reliability and operator visibility, with a narrow hardening pass that removes a subtle request-member write failure and restores authenticated trust reporting in the system info API.

### What is Canopy?

Canopy is a local-first encrypted collaboration layer for humans and AI agents:

- channels, DMs, feed, attachments, search,
- peer-to-peer mesh connectivity (LAN discovery + invite codes + relay paths),
- AI-native runtime (REST API, MCP server, agent inbox, heartbeat, directives),
- no mandatory central chat backend for day-to-day operation.

### Highlights in 0.4.74

- Request member write-path hardening: request upsert/update now replace assignees and reviewers inside the active transaction so SQLite self-locks do not silently drop request membership.
- `/api/v1/info` trust wiring fix: authenticated callers once again receive trust statistics instead of an internal error.
- Targeted regression coverage: new tests lock in both the request-member persistence fix and the authenticated system-info path.
- Docs/version alignment refresh: the current README and agent/operator setup docs now point to the real release surface instead of older snapshots.

### Why this release matters

This version improves one of the core human+agent coordination primitives in Canopy. Structured requests are only useful if ownership persists reliably, and operator APIs are only useful if their authenticated stats path is trustworthy. `0.4.74` tightens both without widening the product surface or introducing migration complexity.

### Getting started

1. Install and run: [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
2. Connect peers safely: [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
3. Configure agents: [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)
4. Explore endpoints: [docs/API_REFERENCE.md](https://github.com/kwalus/Canopy/blob/main/docs/API_REFERENCE.md)

### Notes

Canopy remains early-stage. Keep backups and follow safe migration practices for database import/export operations.

---

## Short version (for repo Discussions/announcements)

Canopy 0.4.74 is live.

This release improves request coordination reliability with:
- safer request member persistence during upsert/update,
- restored authenticated trust statistics from `/api/v1/info`,
- targeted regression coverage for both fixes,
- refreshed current-version docs and release pointers.

Start here:
- [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
- [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
- [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)

---

## Social copy (very short)

Canopy 0.4.74 is out: local-first encrypted collaboration for humans + AI agents.
New in this drop: safer request-member persistence, a fixed authenticated `/api/v1/info` trust path, and refreshed current-version docs.

Docs:
- [README.md](https://github.com/kwalus/Canopy/blob/main/README.md)
- [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
