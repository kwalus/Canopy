# Agent Note: Capsule Deck-Ready Items

Date: 2026-05-27
Branch: codex/capsule-deck-items-0.6.253
Version: 0.6.253

## Summary

This patch makes agent-run Capsules surface Deck-readable source material without forcing the human user to expand the full trace first. If a compressed run contains obvious Deck-openable items, the capsule now shows a compact `Deck-ready` rail and adds Deck-aware signals to the Run Map.

## Why

Users increasingly use Capsules to compress fast agent chatter, but the work product often includes media, modules, maps/charts, streams, and other items that are best consumed through the Canopy Deck. Previously those items were buried inside the collapsed source trace. This patch keeps the capsule readable while exposing the Deck action path directly.

## Implemented

- Added deterministic Deck item detection for agent-run Capsules.
- Detects Deck-readable attachments including:
  - Canopy module HTML bundles.
  - stream/live-source attachments.
  - source-layout/deck manifests when present on an attachment.
  - audio/video attachments by MIME type or common extension.
- Detects Deck-readable URLs in source text including:
  - YouTube, Vimeo, Loom.
  - Spotify, SoundCloud.
  - Google Maps, OpenStreetMap.
  - TradingView.
  - direct audio/video media URLs.
- Added a `Deck-ready` rail in the capsule side panel.
- Clicking a Deck-ready item expands/renders the exact source trace and then calls `window.openDeckForChannelAntecedentMessage(messageId)` so the existing Deck pipeline owns actual Deck opening.
- Added Deck item count to Capsule source-trail summaries, copy summaries, chips, and LLM capsule payload context.
- Added `deck` signal support to the Run Map so source posts containing Deck items are visible as their own map stop type.
- Added light-theme styles for the new Deck-ready rail so it remains legible and brand-consistent.
- Added frontend regression assertions for the Deck-ready capsule path.
- Bumped Canopy version to `0.6.253`.

## Safety / Compatibility

- The patch does not create a new Deck rendering path. It delegates to the existing `openDeckForChannelAntecedentMessage` function after expanding the capsule trace.
- If Deck opening is unavailable or fails, the fallback still leaves the user focused on the exact source post.
- Detection is intentionally conservative: it uses attachment metadata and known URL/provider patterns rather than trying to infer arbitrary file references as Deck items.
- Backend LLM capsule summary code receives extra `deck_items` payload metadata but should ignore it safely if not yet used server-side.

## Review Focus

- Confirm that clicking Deck-ready items opens the source trace and launches the Deck on channel posts containing YouTube, modules, videos, streams, or TradingView embeds.
- Confirm mobile capsule layout remains usable with the new rail.
- Confirm false positives are tolerable: arbitrary PDFs/files should remain in Files & workproducts, not Deck-ready, unless they are Deck-renderable by existing source message rendering.

## Tests Run

- `python -m py_compile canopy/ui/routes.py canopy/api/routes.py`
- `pytest -q tests/test_frontend_regressions.py -k "channel_header_can_hide_agent_only_threads or mobile_resize_dedup_gates_collapse_redundant_layout_work"`
- `git diff --check`
- `node --check /tmp/canopy_capsule_block.js`
