# Repost v1 Design Review

Date: 2026-03-23
Author: Codex

## Findings First

The current codebase already has a feed-side "share" primitive, but it is not safe enough to serve as the canonical repost model.

Current problems:

1. Existing feed share copies original content and metadata into the new post.
- `canopy/core/feed.py:993`
- `canopy/core/feed.py:1000`

2. Existing feed share forces the new shared post to `network` visibility instead of preserving or constraining the original audience.
- `canopy/core/feed.py:1008`

3. The UI route simply calls that share path without adding any extra authorization or audience check.
- `canopy/ui/routes.py:10454`

4. Channel messages do not currently have a generic metadata/reference field.
- message dataclass:
  - `canopy/core/channels.py:128`
- schema:
  - `canopy/core/channels.py:720`

That means the safest v1 is not "copy the source again." It is:

- a new source-owned reference wrapper
- no content or attachment duplication
- no authorship transfer
- no audience widening
- live resolution of the original source at render time

## Product Goal

Allow a user or agent to bring a valuable source forward again without:

- copying the original payload
- claiming ownership of the original
- weakening existing privacy / visibility rules
- breaking mesh stability

The repost should feel like:

- "X reposted this source"

not:

- "X owns a duplicate of this source"

## Strong Recommendation

Use a **reference-wrapper repost model**.

That means the repost is a new source authored by the reposter, but it contains only:

- optional commentary by the reposter
- a typed reference to the original source

The original source remains authoritative for:

- authorship
- content
- attachments
- source layout
- deck experience
- later edits/deletion/access changes

## Recommended v1 Scope

### Include in v1

1. Feed post -> feed repost
- only when the original feed post is eligible

2. Channel message -> same-channel repost
- top-level repost wrapper in the same channel only

### Exclude from v1

1. DM reposts
- too easy to leak highly private content

2. Cross-channel reposts
- target audience comparison is more complex
- too easy to broaden access accidentally

3. Channel -> feed reposts
- same audience problem

4. Feed post reposts of `private` or `custom` posts
- too risky in v1

## Eligibility Rules

### Feed repost eligibility

Allow repost only if:

- viewer can currently view the original
- original is not expired
- original is not deleted
- original repost policy is not `deny`
- original visibility is one of:
  - `public`
  - `network`
  - `trusted`

Additional rule:

- repost visibility may not be broader than the original visibility

Practical v1 simplification:

- default repost visibility to exactly the original visibility
- do not let the reposter widen it

Do not allow v1 reposts of:

- `private`
- `custom`

### Channel repost eligibility

Allow repost only if:

- reposter can currently read the original message
- reposter can post top-level content in the same channel
- original is not expired
- original is not deleted
- original repost policy is not `deny`
- target channel is the same as source channel

This keeps the audience stable.

### DM repost eligibility

Do not support in v1.

## Owner Control

Repost must preserve owner control over the original source.

Recommended source policy field:

- `repost_policy`

Values:

- `same_scope`
- `deny`

Recommended defaults:

- feed posts:
  - `same_scope`
- channel messages:
  - `same_scope`
- DMs:
  - implicitly unsupported in v1

Meaning:

- `same_scope`
  - feed: repost only without widening visibility
  - channels: repost only in the same channel
- `deny`
  - no repost wrapper may be created

If the original owner later switches the source to `deny`, existing repost wrappers should no longer render the original preview. They should degrade to a "repost unavailable" state.

## Data Model

### Feed

Feed posts already have `metadata`, so add a typed block:

```json
{
  "source_reference": {
    "kind": "repost_v1",
    "source_type": "feed_post",
    "source_id": "POST123",
    "source_author_id": "user_abc",
    "source_visibility": "network",
    "repost_policy": "same_scope",
    "created_by_user_id": "user_xyz"
  }
}
```

Important:

- do not store `original_content`
- do not store `original_metadata`
- do not copy original attachments into the repost

### Channels

Channel messages need an additive field.

Strong recommendation:

- add `source_reference TEXT` to `channel_messages`
- add `source_reference: Optional[Dict[str, Any]]` to the channel `Message` dataclass

Why not reuse:

- `security`
  - wrong semantics
- `source_layout`
  - composition and rendering only, not ownership/reference policy

Suggested shape:

```json
{
  "kind": "repost_v1",
  "source_type": "channel_message",
  "source_id": "M123",
  "source_channel_id": "C123",
  "source_author_id": "user_abc",
  "repost_policy": "same_scope",
  "created_by_user_id": "user_xyz"
}
```

## Rendering Model

A repost wrapper should render as:

1. reposter attribution
- `Reposted by Codex Agent`

2. optional commentary by the reposter

3. original-source card
- original author name
- original source location
- original created time
- live source preview if currently accessible
- `Open original`
- `Open in Deck` if the original has source layout / deckable media

### If the original is no longer available

Render a bounded unavailable state:

- `Original source unavailable`
- optional reason:
  - expired
  - deleted
  - access changed

Do not keep rendering copied original content in that state.

That is the key owner-control property.

## Deck Behavior

The repost wrapper does not own the original deck.

Recommended behavior:

- `Open in Deck` on the repost resolves the original source
- deck opens against the original source if access is still valid
- if not valid, show unavailable state

Do not duplicate:

- original attachments
- original module bundles
- original source layout into the repost body

## Backward Compatibility

There is already a legacy feed share model using:

- `shared_post_id`
- `original_content`
- `original_metadata`

References:

- `/Users/konradwalus/Library/CloudStorage/Dropbox/Python Toolbox/Canopy/canopy/core/feed.py:994`
- `/Users/konradwalus/Library/CloudStorage/Dropbox/Python Toolbox/Canopy/canopy/core/feed.py:1000`

Recommended migration posture:

1. continue reading legacy shared posts
2. stop writing new legacy shared posts
3. new writes use `source_reference.kind = repost_v1`
4. feed algorithm `show_reposts` should treat both as reposts during transition

## Security Review

### What to avoid

Do not:

- duplicate original body text into the repost
- duplicate original attachments into the repost
- widen visibility on repost creation
- allow DM reposts in v1
- allow cross-channel reposts in v1
- allow repost if the source is no longer viewable by the reposter

### Required checks

At create time:

1. source exists
2. reposter can currently view source
3. source repost policy allows repost
4. target scope does not widen audience
5. source is not expired/deleted

At render time:

1. current viewer can currently view original
2. original still exists
3. original policy still allows rendering of the live reference

### Why render-time checks matter

If the original owner:

- edits the source
- deletes the source
- expires the source
- tightens access

the repost should reflect that.

That is how the original owner retains control.

## API / UI Surface Recommendation

### Feed

Replace the current share write path with a repost write path.

Possible endpoints:

- `POST /api/v1/feed/posts/<id>/repost`
- `POST /ajax/repost_post`

### Channels

Add a same-channel repost endpoint:

- `POST /api/v1/channels/<channel_id>/messages/<message_id>/repost`
- `POST /ajax/repost_channel_message` (session auth) with JSON body: `channel_id`, `message_id`, optional `comment`

The created channel repost should always land in the same channel as a new top-level source.

### UI labels

Prefer:

- `Repost`

over:

- `Share`

because `share` currently implies copying or external dissemination.

## Implementation Order

1. Replace legacy feed write path
- new `repost_v1` reference envelope
- no copied original content

2. Add read-side repost resolver for feed
- live original preview card
- unavailable tombstone state

3. Add channel `source_reference` field + migration

4. Implement same-channel repost write/read path

5. Add tests for access tightening / deletion / expiry

## Minimum Test Matrix

Feed:

1. can repost `network` post
2. cannot repost `private` post
3. cannot repost `custom` post
4. repost visibility does not widen source visibility
5. deleting original degrades repost card
6. editing original updates repost preview

Channels:

1. can repost same-channel message in same channel
2. cannot repost into different channel
3. private-channel repost remains visible only to members
4. membership loss causes repost card to degrade
5. original deletion/expiry degrades repost card

Security:

1. no copied original attachments on repost row
2. no copied original body content in repost metadata
3. repost policy `deny` blocks creation

## Recommendation

Build `repost_v1` as a **reference wrapper**, not a copy.

That is the right engineering line because it:

- preserves original owner control
- keeps security semantics intact
- avoids privacy leaks from duplicated payloads
- fits the Canopy Deck / source-layout architecture
- still gives users the "bring this forward again" behavior they actually want

Do not extend the existing legacy feed `share_post()` behavior. Replace it.
