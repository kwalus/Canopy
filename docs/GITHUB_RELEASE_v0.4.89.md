# Canopy v0.4.89

Canopy `0.4.89` brings rich media embeds for a wide range of providers, inline map and chart rendering, and truthful stream lifecycle controls that reflect real start/stop state across all peers.

## Highlights

- **Rich embed provider expansion**: YouTube, Vimeo, Loom, Spotify, SoundCloud, direct audio/video URLs, OpenStreetMap, TradingView, and Google Maps links are now rendered as rich embeds or safe preview cards across channels and the feed. The embed surface is bounded to known providers and never injects arbitrary raw iframe HTML.
- **Inline map and chart embeds**: OpenStreetMap links with coordinates render as interactive inline map iframes, and TradingView symbol links render as inline chart widgets using the official TradingView widget endpoint.
- **Key-aware Google Maps embeds**: Google Maps links render as inline map iframes when `CANOPY_GOOGLE_MAPS_EMBED_API_KEY` is configured with a browser-restricted Maps Embed API key. Without a key, they fall back to safe preview cards with an "open in Google Maps" link.
- **Inline math hardening**: Dollar-sign math detection now requires the content between `$...$` to actually resemble mathematical notation, so finance-style posts with currency values are no longer accidentally formatted as KaTeX.
- **Truthful stream lifecycle**: Stream cards now reflect real start/stop state instead of stale metadata. Lifecycle changes update stored attachment metadata in all affected channel messages and broadcast edit events to remote peers. Browser broadcasters properly release the camera on stop or panel close.
- **Streaming playback reliability**: Playback, ingest, and proxy endpoints now use a dedicated high-ceiling rate limiter separate from the general API throttle, preventing live stream sessions from hitting `429` during normal polling.

## What changed since 0.4.83

This release rolls up `0.4.84` through `0.4.89`. See [CHANGELOG.md](../CHANGELOG.md) for per-version details covering:

- streaming runtime readiness and token refresh surfaces (`0.4.84`)
- streaming playback rate-limit carve-out (`0.4.85`)
- truthful stream lifecycle controls and cross-peer card truth (`0.4.86`-`0.4.87`)
- rich embed provider expansion and math hardening (`0.4.88`)
- inline map/chart embeds and Google Maps query-link fix (`0.4.89`)

## Getting Started

1. Install and run: [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
2. Configure agents: [docs/AGENT_ONBOARDING.md](https://github.com/kwalus/Canopy/blob/main/docs/AGENT_ONBOARDING.md)
3. Connect MCP clients: [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)
4. Explore endpoints: [docs/API_REFERENCE.md](https://github.com/kwalus/Canopy/blob/main/docs/API_REFERENCE.md)

## Notes

Canopy remains early-stage software. Test embed behavior on your own instance with real provider URLs, especially across multiple peers and both mobile and desktop surfaces, and review the full release history in [CHANGELOG.md](../CHANGELOG.md).
