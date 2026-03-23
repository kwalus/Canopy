# Repost v1 Implementation Plan

Date: 2026-03-23
Author: Codex

Note:
- `source_reference` is now also reused by `variant_v1` lineage wrappers.
- Repost semantics in this document remain unchanged; lineage-specific behavior is documented in `LINEAGE_VARIANTS_V1_PLAN.md`.

## Scope

Implement a narrow, functionally complete repost primitive for:
- feed post -> feed repost
- channel message -> same-channel repost

Included in v1:
- local UI repost action
- agent API repost action
- live read/render resolution of the original source
- bounded unavailable state when the original can no longer be rendered
- backward-compatible reading of legacy copied shares
- same-channel repost wrappers for channel messages

Explicitly excluded from v1:
- DM reposts
- cross-channel reposts
- channel -> feed reposts
- feed -> channel reposts
- repost chains
- audience widening on repost
- copied original payloads

## Security And Privacy Invariants

1. New reposts are reference wrappers, not copies.
- no copied original body text
- no copied original metadata blobs
- no copied original attachments

2. Reposts do not widen visibility.
- repost visibility is fixed to the original feed post visibility
- v1 allows repost only for original visibility in:
  - `public`
  - `network`
  - `trusted`
- v1 disallows repost of:
  - `private`
  - `custom`
- channel reposts are limited to the exact same channel in v1
- channel reposts never cross channel membership or governance boundaries

3. Owner control is preserved.
- original source remains authoritative
- render-time resolution re-checks current availability and access
- if source is deleted, expired, or policy-tightened, repost renders an unavailable state

4. Generic post creation and update must not allow forged repost wrappers.
- caller-supplied `metadata.source_reference` is stripped unless an internal repost path explicitly allows it

5. Backward compatibility is preserved.
- legacy `shared_post_id` shares remain readable
- new writes only emit `source_reference.kind = repost_v1`

## Data Model

New repost metadata block for feed posts:

```json
{
  "source_reference": {
    "kind": "repost_v1",
    "source_type": "feed_post",
    "source_id": "POST123",
    "source_visibility": "network",
    "created_by_user_id": "user_xyz"
  }
}
```

New repost metadata block for channel messages:

```json
{
  "source_reference": {
    "kind": "repost_v1",
    "source_type": "channel_message",
    "source_id": "MSG123",
    "channel_id": "CHAN123",
    "created_by_user_id": "user_xyz"
  }
}
```

Optional source policy block on original posts:

```json
{
  "repost_policy": "same_scope"
}
```

Supported `repost_policy` values:
- `same_scope`
- `deny`

Default behavior when absent:
- treat as `same_scope`

## Read/Render Contract

A repost wrapper renders:
1. reposter attribution
2. reposter commentary
3. resolved original-source card

Resolved original-source card fields (feed and channel align where practical):
- `available`
- `source_type` (`feed_post` or `channel_message`)
- `source_id`
- `channel_id` (channel reposts only; same channel as wrapper)
- `author_id`
- `author_display` (when resolvable)
- `created_at`
- `visibility` / `message_type` (feed uses `visibility` + `post_type`; channels use `message_type` on the original)
- `preview_text` (short)
- `body_text` / `body_truncated` (rich preview; live-resolved, not stored on wrapper row)
- `embed` (optional: link/media/poll/attachment thumbnails — see `FeedManager` / `ChannelManager` helpers)
- `has_source_layout`
- `deck_default_ref`
- `href` (feed: `/feed?focus_post=…`; channel: `/channels/locate?message_id=…`)
- `unavailable_reason`

Unavailable reasons in v1:
- `missing`
- `expired`
- `access_changed`
- `policy_denied`

## API / UI Surface

**Feed (shipped):**
- `POST /api/v1/feed/posts/<post_id>/repost` — API key; optional JSON `{ "comment" }`
- `POST /ajax/repost_post` and `POST /ajax/share_post` — session; same semantics (inline composer on feed uses these)

**Channels (shipped):**
- `POST /api/v1/channels/<channel_id>/messages/<message_id>/repost` — API key; optional `{ "comment" }`
- `POST /ajax/repost_channel_message` — session; JSON `{ "channel_id", "message_id", "comment?" }`

**UI:** Inline composers under the action row (no modal) — feed: `feed.html`; channels: `channels.html`.

Suggested response payload:
- created wrapper dict (post or message)
- resolved `repost_reference` / `is_repost` on list/detail where applicable

## Implementation Steps

1. Add metadata normalizer in `feed.py`
- normalize `source_layout`
- normalize `repost_policy`
- strip `source_reference` unless explicitly allowed

2. Add repost helpers in `feed.py`
- legacy repost detection
- `extract_source_reference()`
- `is_repost_post()`
- `resolve_repost_reference()`
- `create_repost()`
- keep `share_post()` as compatibility wrapper to `create_repost()`

3. Replace legacy copy-based write behavior
- no `original_content`
- no `original_metadata`
- no forced `network` visibility

4. Decorate feed posts for UI/API
- attach resolved repost reference payload
- attach repost attribution flags

5. Render repost card in feed template
- show repost wrapper
- show resolved source card
- show unavailable tombstone when needed
- rename action label from `Share` to `Repost`

6. Add API endpoint and reuse manager rules
7. Extend the same reference-wrapper model to channels *(shipped)*
- dedicated `source_reference` + `repost_policy` columns on `channel_messages` (plus migration)
- same-channel eligibility enforcement
- live repost resolution in channel APIs and AJAX thread payloads
- inline channel repost composer under each message action row (`/ajax/repost_channel_message`)
- P2P: propagate `source_reference` and `repost_policy` on create, edit, catchup, and incoming persist (`app.py`, `network/manager.py`, `network/routing.py`)

8. Add tests
- create public/network/trusted reposts
- reject private/custom reposts
- reject repost chains
- reject `repost_policy = deny`
- verify generic create/update strips forged `source_reference`
- verify legacy repost filtering still works
- verify deletion / expiry / unavailable degradation
- verify same-channel-only channel repost enforcement
- verify channel API returns resolved `repost_reference`

## Validation

Required checks after implementation:
- `python3 -m py_compile` on touched Python files
- `python3 scripts/check_jinja_templates.py`
- `node --check` on any touched JS-bearing template or JS file
- focused `pytest` on repost tests plus feed/frontend regressions
- `git diff --check`

## Non-Goals For This Pass

Do not expand into:
- DM/E2E repost semantics
- shared collections
- cross-device repost history
- ownership transfer semantics

This pass should establish the safe primitive first.
