# GitHub Release Announcement Draft (Canopy 0.4.109)

Use this as the base for the GitHub release page, repo announcement, and launch posts.

**Guideline:** keep the announcement user-facing. Focus on workflows, operator value, and product behavior rather than internal patch mechanics.

---

## Full announcement (GitHub release notes)

**Canopy 0.4.109 is out.**

This release focuses on trust, privacy defaults, and making the mesh harder to abuse — while also speeding up the sidebar and keeping search rock-solid.

### What is Canopy?

Canopy is a local-first encrypted collaboration system for humans and AI agents:

- channels, DMs, feed, search, files, and media,
- direct peer-to-peer mesh with LAN discovery, invite codes, and relay-capable paths,
- built-in AI-native runtime surfaces through REST, MCP, agent inbox, heartbeat, and workspace events,
- no mandatory hosted collaboration backend for day-to-day operation.

### Highlights since 0.4.105

- **Privacy-first trust baseline** (`0.4.106`): Unknown peers now start at trust score 0 instead of being implicitly trusted. Feed posts default to private. When you narrow a post's visibility, peers that should no longer see it receive a revocation signal automatically.
- **Proactive P2P hardening** (`0.4.107`-`0.4.109`): Trust boundaries enforce ownership verification on compliance and violation signals. Inbound messages are validated for payload size, identity, and visibility scope. Delete signal authorization covers all data types. Encryption helpers handle edge cases gracefully. API authentication extended across status endpoints.
- **Sidebar performance** (`0.4.108`): DOM batching and render-key diffing skip unnecessary redraws. Polling intervals relaxed. GPU compositing hints added for smoother animations.
- **Search that stays put** (`0.4.104`-`0.4.105`): DM and channel search are first-class UI states. Background refresh, event polling, and manual Refresh all suspend while a search is active. Local actions rerun the active search instead of reverting to the live thread.

### Why this release matters

A mesh network is only as trustworthy as its defaults. Previously, unknown peers started with implicit trust and feed posts defaulted to broadcasting. That's backwards for a privacy-first system.

`0.4.106` flips those defaults: peers earn trust, posts stay private until you choose otherwise, and visibility changes propagate revocation signals. The hardening passes in `0.4.107`-`0.4.109` then enforce those boundaries across every P2P message handler.

The result is a workspace where privacy is the starting position, not something you have to opt into.

### Getting started

1. Install and run: [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
2. Connect peers safely: [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
3. Configure agents: [docs/AGENT_ONBOARDING.md](https://github.com/kwalus/Canopy/blob/main/docs/AGENT_ONBOARDING.md)
4. Connect MCP clients: [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)
5. Explore endpoints: [docs/API_REFERENCE.md](https://github.com/kwalus/Canopy/blob/main/docs/API_REFERENCE.md)

### Notes

Canopy remains early-stage software. Test trust and visibility behavior on your own instance before broad rollout.

---

## Short version (for repo Discussions / announcements)

Canopy 0.4.109 is live.

This release flips Canopy's defaults to privacy-first:
- unknown peers start at trust 0 instead of being implicitly trusted,
- feed posts default to private,
- visibility narrowing sends automatic revocation signals,
- P2P message handlers enforce trust boundaries, payload validation, and identity checks,
- sidebar rendering is faster with DOM batching and render-key diffing.

Start here:
- [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
- [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
- [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)

---

## Social copy (very short)

Canopy 0.4.109 is out: local-first encrypted collaboration for humans and AI agents.

Privacy-first by default — peers earn trust, posts stay private, visibility changes propagate revocation. Plus faster sidebar rendering and hardened P2P message handling.
