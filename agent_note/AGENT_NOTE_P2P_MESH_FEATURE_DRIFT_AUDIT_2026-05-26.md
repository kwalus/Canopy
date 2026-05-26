# Agent Note: P2P Mesh Feature Drift Audit and Collaboration Card Catch-up

Date: 2026-05-26
Author: Codex Agent
Branch: `codex/p2p-collab-card-catchup-0.6.245`
Version: `0.6.245`

## Why this patch exists

Konrad raised a concern that recent development and testing have been heavily VPS-instance centered, so newer features might work on a single authoritative server but fail to recover over the P2P mesh after peers reconnect. I audited the current P2P/catch-up paths against the newer collaboration surfaces.

## Audit summary

Existing mesh paths remain present for:

- Channel messages, including channel digest catch-up and bounded attachment handling.
- Public channel bootstrap and private member fallback paths.
- Feed posts.
- Direct messages.
- Tasks.
- Circle entries, votes, and circle objects.
- Profile/device identity metadata.
- Source-advance snapshots through interaction propagation.

The notable drift risk was collaboration cards: input cards and telemetry cards are stored durably and live updates are broadcast through `INTERACTION` payloads, but missed card state was not part of the reconnect catch-up envelope. That meant telemetry progress or input responses could work on a VPS/live peer but be stale for an offline P2P peer until another live update occurred.

## Implemented fix

### Durable card snapshot catch-up

- Added `CollabCardManager.get_cards_latest_timestamp()` to report the newest network-visible collaboration card or response timestamp.
- Added `CollabCardManager.get_cards_since(...)` to export network-visible input/telemetry card snapshots with responses for catch-up recovery.
- Added `updated_at` preservation to `upsert_card(...)` and `ingest_card_snapshot(...)` so received snapshots keep their source update timestamp instead of always becoming local-now.
- Excluded `local`/private card visibility from catch-up watermarks and snapshots so local-only state cannot suppress needed network-visible catch-up.

### P2P catch-up envelope

- `P2PNetworkManager` now advertises `collab_cards_latest` alongside feed/circle/task timestamps.
- Catch-up request handling now includes `collab_cards` in `extra_data` for trusted peers when changed cards are available.
- Catch-up response handling now ingests card snapshots and nested responses.
- `MessageRouter` forwards `collab_cards_latest` and `collab_cards`, with fallback compatibility for callbacks that predate these arguments.

## Safety and compatibility notes

- The change is additive at the catch-up metadata level; older peers ignore unknown metadata or use callback fallbacks.
- Only network-visible collaboration cards are exported through this path.
- Private/local collaboration cards remain local and are not included in the P2P catch-up watermark, avoiding a subtle bug where local-only updates could make a peer believe it was current for network-visible cards.
- Payload chunking already operates generically over list-valued `extra_data`, so `collab_cards` uses the existing bounded catch-up chunking path.

## Files changed

- `canopy/core/collab_cards.py`
- `canopy/core/app.py`
- `canopy/network/manager.py`
- `canopy/network/routing.py`
- `tests/test_collab_cards.py`
- `tests/test_manager_catchup_digest_response.py`
- `tests/test_routing_catchup_digest_metadata.py`
- Version/docs/changelog files for `0.6.245`

## Tests run

- `python -m py_compile canopy/core/collab_cards.py canopy/network/manager.py canopy/network/routing.py canopy/core/app.py`
- `PYTHONPATH=. pytest tests/test_collab_cards.py tests/test_routing_catchup_digest_metadata.py tests/test_manager_catchup_digest_response.py -q`

## Recommended review focus

- Confirm that network-visible card state is the correct boundary for mesh catch-up. I intentionally did not attempt to replicate local/private cards.
- Consider whether future object types with post-update state need the same explicit catch-up surface rather than relying only on live `INTERACTION` broadcasts.
- After merge, test with two clean P2P peers: create a telemetry card, take one peer offline, update telemetry/input response, reconnect, and verify card state catches up without needing a fresh live card update.
