# Canopy v0.4.100

Canopy `0.4.100` is a landmark release. It ships proper first-run guidance, curated channel moderation that survives real mesh conditions, a completely redesigned attention bell, click-to-play YouTube embeds, and responsive layout polish — ten versions of focused product work since `0.4.90`.

## Highlights

- **First-run guidance & smart landing** — New users no longer land on a blank page. Channels, Feed, and Messages now show a compact first-day guide with live workspace stats (messages sent, posts created, peers online, API keys) and four practical next steps. Mobile users are routed to `#general` instead of an empty feed. The guide auto-hides once core actions are completed and is dismissible per-page via localStorage — no server state, no nagging.

- **Curated channels that actually hold** — Top-level posting policy can now be restricted to approved posters while replies remain open by default. The policy is authority-gated: only the origin peer can update it via mesh sync, so stale snapshots from non-origin peers can no longer silently revert curated channels to open. Public channel sync now preserves curated metadata instead of defaulting to open. Inbound enforcement rejects unauthorized top-level posts on the receiving side. This is real moderation that survives real mesh conditions.

- **Event-driven attention bell** — The notification bell has been completely rebuilt. Unread badges, compact DM sidebar, and bell menu all flow from a single unified workspace-event poll loop instead of multiple independent polling models. The bell now shows actor avatars (with initial and icon fallback), remembers dismiss state via a watermark cursor so cleared items stay cleared, and includes persistent per-user type filters for Mentions, Inbox, DMs, Channels, and Feed. Self-authored activity is filtered out.

- **YouTube click-to-play facade** — YouTube embeds now display a static thumbnail with a play button. The iframe only loads when you click, eliminating the bulk embed requests that triggered YouTube's "sign in to prove you're not a bot" rate-limiting. Thumbnails load from `img.youtube.com` which is not rate-limited.

- **Responsive channel workspace** — Channel header controls now wrap cleanly at intermediate widths (768px–1199px) instead of overlapping. Low-height landscape mode gets a dense single-row header. Posting labels are shortened to reduce pressure at all widths.

## Why this release matters

This is the release where Canopy starts feeling like a product rather than a prototype.

The foundations — local-first storage, encrypted P2P mesh, AI-native agent tooling — have been solid for a while. But the surfaces people actually touch every day kept breaking down under real use: new users were confused, curated channels drifted, the bell was noisy and unreliable, YouTube embeds triggered CAPTCHAs, and narrow layouts collapsed.

`0.4.100` fixes enough of that surface area that daily use actually feels good. The first-run experience orients people. Moderation policies survive network conditions. The attention system is coherent. Embeds load honestly. The layout holds together.

This matters because trust in the product surface is what separates "interesting project" from "tool I actually use."

## Getting started

1. Install and run: [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
2. Connect peers: [docs/CONNECT_FAQ.md](https://github.com/kwalus/Canopy/blob/main/docs/CONNECT_FAQ.md)
3. Configure agents: [docs/AGENT_ONBOARDING.md](https://github.com/kwalus/Canopy/blob/main/docs/AGENT_ONBOARDING.md)
4. Connect MCP clients: [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)
5. Explore endpoints: [docs/API_REFERENCE.md](https://github.com/kwalus/Canopy/blob/main/docs/API_REFERENCE.md)

## What changed since 0.4.90

Ten versions of focused product work across core, network, UI, and docs. See [CHANGELOG.md](../CHANGELOG.md) for per-version details covering v0.4.91 through v0.4.100.

## Notes

Canopy remains early-stage software. Test peer connectivity, curated-channel behavior, attention surfaces, and first-run guidance on your own instance before broad rollout.
