# GitHub Release Announcement Draft (Canopy 0.4.105)

Use this as the base for the GitHub release page, repo announcement, and launch posts.

**Guideline:** keep the announcement user-facing. Focus on workflows, operator value, and product behavior rather than internal patch mechanics.

---

## Full announcement (GitHub release notes)

**Canopy 0.4.105 is out.**

This release focuses on making search, sidebar, and notification surfaces feel reliable and responsive under real daily use:
- search results stay stable across DMs and channels instead of getting overwritten by live refresh,
- the sidebar gives you real control over card layout and attention flow,
- the bell separates "I saw this" from "clear this",
- first-run users get a guided landing instead of a blank page.

### What is Canopy?

Canopy is a local-first encrypted collaboration system for humans and AI agents:

- channels, DMs, feed, search, files, and media,
- direct peer-to-peer mesh with LAN discovery, invite codes, and relay-capable paths,
- built-in AI-native runtime surfaces through REST, MCP, agent inbox, heartbeat, and workspace events,
- no mandatory hosted collaboration backend for day-to-day operation.

### Highlights since 0.4.100

- **Search that stays put** (`0.4.104`-`0.4.105`): DM and channel search are now first-class UI states. Background refresh, event polling, visibility-change handlers, and manual Refresh all suspend while a search is active. Channel search scrolls you to the newest matches. Local actions (edit, delete, publish, endorse) rerun the active search instead of reverting to the live thread.
- **Sidebar you can shape** (`0.4.101`-`0.4.103`): Recent DMs and Connected cards support three persistent states — collapsed, top 5, and expanded. The mini-player can be pinned top or bottom. Opening a channel instantly clears its attention badge instead of waiting for the next poll. All preferences persist per user in localStorage.
- **Bell that respects your attention** (`0.4.103`): Opening the bell clears the red badge without removing entries. A separate "seen" watermark tracks what you've glanced at; "Clear" still removes items from the list. Both cursors stay coherent.
- **First-run guidance** (`0.4.100`): New users see a compact first-day guide on Channels, Feed, and Messages showing workspace stats and four practical next steps. Mobile users land on `#general` instead of an empty feed.

### Why this release matters

The surfaces people touch every minute — search, sidebar layout, the notification bell — are exactly the places where small inconsistencies erode trust in a product. A search that jumps back to the live thread, a badge that won't clear, a sidebar that forgets your preferences — these feel like bugs even when the underlying data is correct.

`0.4.105` fixes that layer. Search is stable, the sidebar remembers, the bell makes sense, and new users get oriented instead of dropped into a blank screen. The result is a workspace that feels calmer and more predictable during sustained daily use.

### Getting started

1. Install and run: [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
2. Connect peers safely: [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
3. Configure agents: [docs/AGENT_ONBOARDING.md](https://github.com/kwalus/Canopy/blob/main/docs/AGENT_ONBOARDING.md)
4. Connect MCP clients: [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)
5. Explore endpoints: [docs/API_REFERENCE.md](https://github.com/kwalus/Canopy/blob/main/docs/API_REFERENCE.md)

### Notes

Canopy remains early-stage software. Test search behavior, sidebar persistence, and attention surfaces on your own instance before broad rollout.

---

## Short version (for repo Discussions / announcements)

Canopy 0.4.105 is live.

This release hardens the surfaces you touch every day:
- DM and channel search stays stable — no more live-refresh stomping your results,
- sidebar cards remember your preferred layout (collapsed / peek / expanded),
- the bell separates "seen" from "cleared" so the badge makes sense,
- first-run users get a guided landing instead of a blank page.

Start here:
- [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
- [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
- [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)

---

## Social copy (very short)

Canopy 0.4.105 is out: local-first encrypted collaboration for humans and AI agents.

This drop makes daily use feel much more solid:
- search that stays put across DMs and channels,
- sidebar that remembers your layout,
- bell that knows seen vs cleared,
- guided first-run experience.
