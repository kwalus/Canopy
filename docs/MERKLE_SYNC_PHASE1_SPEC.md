# Merkle Sync Phase 1 Spec (Backward-Compatible)

Status: Draft for implementation  
Target train: 0.4.x additive rollout  
Owner: Canopy core

## 1) Decision Summary

This phase implements a **Merkle-assisted sync optimization** without changing the existing sync authority model.

- Keep current timestamp catch-up as source of truth.
- Add optional channel digest exchange to skip unnecessary per-channel scans/transfers.
- Use **feature negotiation** and **graceful degradation** for mixed-version meshes.
- Do **not** introduce CRDT operation logs in this phase.
- Do **not** introduce irreversible data format migrations in this phase.

## 2) Current-State Constraints (Must Preserve)

- Catch-up is timestamp-based (`channel_timestamps` + per-channel `get_messages_since`).
- Catch-up is bounded and iterative (limit-based), not guaranteed full-history in one pass.
- Expired/deleted content is hard-removed locally and propagated by delete signals.
- Private/confidential channels can carry encrypted payload fields.
- Mesh runs mixed-version peers today.

## 3) Phase 1 Goals

1. Reduce unnecessary catch-up work when peers are already in sync.
2. Preserve exact behavior for peers that do not support digest mode.
3. Avoid mesh fragmentation during rollout and rollback.
4. Avoid privacy regressions in restricted channels.

## 4) Non-Goals (Phase 1)

- No CRDT merge engine.
- No `PURGE`/new op-type protocol.
- No protocol-version bump requirement.
- No new mandatory P2P message types.
- No API breaking changes.

## 5) Compatibility Strategy

### 5.1 Feature negotiation

Use existing handshake capabilities to advertise support:

- `sync_digest_v1` (new)

Behavior:

- Both peers support `sync_digest_v1` -> digest optimization enabled.
- Any peer missing capability -> fallback to legacy catch-up only.

### 5.2 Graceful degradation rules

- New peer must always accept legacy behavior.
- Legacy peer ignores unknown metadata fields.
- No channel state is marked "failed" solely due to missing digest support.

### 5.3 No hard version gates

- Do not block mixed 0.4.x peers.
- Log compatibility state for diagnostics only.

## 6) Wire Contract (Additive)

No new P2P message type is required in Phase 1.

### 6.1 `CHANNEL_CATCHUP_REQUEST` metadata additions

Existing fields remain unchanged (`channel_timestamps`, `feed_latest`, etc.).  
Add optional field:

```json
{
  "digest": {
    "version": 1,
    "channels": {
      "Cabc123": {
        "root": "hex_sha256",
        "live_count": 1452,
        "max_created_at": "2026-03-04 10:30:00"
      }
    }
  }
}
```

### 6.2 `CHANNEL_CATCHUP_RESPONSE` metadata additions

Add optional field:

```json
{
  "digest": {
    "version": 1,
    "channels": {
      "Cabc123": {
        "remote_root": "hex_sha256",
        "remote_live_count": 1452,
        "status": "match|mismatch|missing"
      }
    }
  }
}
```

Rules:

- `status=match` means sender skipped message lookup/transfer for that channel.
- `status=mismatch|missing` means sender used current timestamp catch-up path.

## 7) Canonical Digest Definition (v1)

Digest scope: **live channel message set only**  
(`expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP`)

### 7.1 Canonical message fingerprint

For each message row, compute a stable fingerprint over:

- `id`
- `created_at` (normalized DB format)
- `edited_at` (or empty)
- `message_type`
- `parent_message_id` (or empty)
- `expires_at` (or empty)
- `crypto_state` (or empty)
- `key_id` (or empty)
- payload hash:
  - if `encrypted_content` present -> hash encrypted payload fields (`encrypted_content`, `nonce`, `key_id`)
  - else -> hash plaintext `content`
- attachment metadata hash (canonical JSON, no embedded `data`)

### 7.2 Channel root

- Order fingerprints by `id` ascending (deterministic across peers).
- Build binary Merkle root from ordered leaf hashes.
- Empty-channel root constant:
  - `SHA256("canopy:sync_digest:v1:empty")`

### 7.3 Privacy requirement

For restricted channels, digest material must not require plaintext access if encrypted payload exists.

## 8) Local Storage (Additive)

Add a cache table:

```sql
CREATE TABLE IF NOT EXISTS channel_sync_digests (
  channel_id TEXT PRIMARY KEY,
  digest_version INTEGER NOT NULL,
  root_hash TEXT NOT NULL,
  live_count INTEGER NOT NULL,
  max_created_at TIMESTAMP,
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Notes:

- Cache is an optimization only; can be recomputed from channel messages.
- No migration of existing message rows required.

## 9) Runtime Algorithm

1. On catch-up request build, if peer supports `sync_digest_v1`, include local digest map.
2. On catch-up handling:
   - if remote digest present and local digest equals remote digest -> mark `match`, skip `get_messages_since` query.
   - else -> run existing timestamp catch-up logic unchanged.
3. Include per-channel digest decision in response metadata.
4. Receiver behavior remains unchanged for message application/dedup.

## 10) Failure/Recovery Behavior

- Any digest error (parse, compute, mismatch ambiguity) -> fallback to legacy timestamp mode for that channel.
- Never abort entire catch-up due to digest failure.
- Rollback safety: disabling feature flag returns system to current behavior with no data migration.

## 11) Feature Flags

Config defaults:

- `sync_digest_enabled = false`
- `sync_digest_require_capability = true`
- `sync_digest_max_channels_per_request = 200`

Rollout:

1. Off by default in first shipping build.
2. Enable on canary peers only.
3. Expand gradually after stability gates pass.

## 12) Observability

Add counters/timers:

- `catchup.digest.channels_checked`
- `catchup.digest.channels_matched`
- `catchup.digest.channels_mismatched`
- `catchup.digest.fallbacks`
- `catchup.messages_sent`
- `catchup.bytes_sent`
- `catchup.duration_ms`

Admin diagnostics should expose digest usage and match rate.

## 13) Test Matrix (Required Before Default-On)

### 13.1 Compatibility

- New<->new (digest enabled)
- New<->old (new must fallback)
- Old<->new (no parse failures)
- Rollback new->old with existing DB

### 13.2 Functional

- Match path skips channel transfer
- Mismatch path transfers via existing logic
- Missing channel digest path behaves like legacy
- Attachments and encrypted messages unaffected

### 13.3 Privacy/Security

- No plaintext dependency for encrypted restricted channels
- No sensitive field leakage in diagnostics
- Signature verification unchanged for all P2P messages

### 13.4 Reliability

- Relay topologies (direct, relayed, mixed)
- Peer churn/reconnect storms
- Offline peer rejoin after long gap
- Concurrent edits + deletes while catch-up active

### 13.5 Data lifecycle

- Expired message purge updates digest
- Delete-signal application updates digest
- No resurrection after purge/delete events

### 13.6 Performance

- CPU overhead within budget on 10k+ message channels
- No lock amplification under SQLite WAL contention
- Catch-up bytes/queries reduced in steady-state

## 14) Exit Criteria for Phase 1

Phase 1 is complete when:

1. Mixed-version mesh runs without fragmentation.
2. Digest mode never blocks legacy catch-up.
3. Steady-state catch-up load is measurably reduced.
4. No regressions in privacy, delete behavior, or private-channel E2E handling.

## 15) Post-Phase-1 Decision Gate

After Phase 1 metrics, decide whether to:

- proceed to richer Merkle delta sync (Phase 2), or
- evaluate scoped CRDT only where conflicts are empirically significant.

Default position until evidence: **do not introduce full CRDT for message bodies**.
