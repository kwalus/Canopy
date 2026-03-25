# Lineage Variants v1

Date: 2026-03-23

## Scope

Implement a narrow lineage primitive for Canopy sources using explicit reference wrappers.

Included in v1:
- feed post -> feed variant
- channel message -> same-channel variant
- dedicated agent API endpoints
- dedicated local UI inline composers
- live render-time antecedent resolution
- explicit relationship metadata
- additive use of existing `source_reference`

Explicitly excluded from v1:
- DM variants
- cross-channel variants
- feed -> channel or channel -> feed variants
- copied original payloads
- variant graph traversal or reverse lookup UI
- mesh-visible shared memory semantics

## Security And Privacy Invariants

1. Variants are new sources, not copied originals.
- no copied original body
- no copied original attachments
- no copied full source-layout payload

2. Variants preserve owner control over the antecedent.
- the antecedent remains authoritative
- render-time resolution re-checks access and availability
- if the antecedent is deleted, expired, or no longer visible, the variant degrades to an unavailable antecedent card

3. Variants do not widen visibility or scope.
- feed variants inherit the original feed visibility exactly
- only `public`, `network`, and `trusted` feed posts are eligible in v1
- channel variants are same-channel only in v1
- channel variants do not cross membership or governance boundaries

4. Generic write paths must not allow forged lineage.
- generic feed/channel create/update strips caller-supplied `source_reference`
- only dedicated variant creation paths may write `source_reference.kind = variant_v1`

5. Repost wrappers are not valid antecedents in v1.
- reposts can be resurfaced
- variants can be derived from authoritative sources
- repost wrappers cannot be used as variant sources

## Data Model

Feed and channel variants both use a typed `source_reference` block:

```json
{
  "source_reference": {
    "kind": "variant_v1",
    "source_type": "feed_post",
    "source_id": "POST123",
    "created_by_user_id": "user_xyz",
    "relationship_kind": "module_variant",
    "module_param_delta": "tempo=138; loop=bars 5-8"
  }
}
```

Channel variants add the source channel id:

```json
{
  "source_reference": {
    "kind": "variant_v1",
    "source_type": "channel_message",
    "source_id": "MSG123",
    "channel_id": "CHAN123",
    "created_by_user_id": "user_xyz",
    "relationship_kind": "curated_recomposition",
    "module_param_delta": "compact deck layout"
  }
}
```

Supported `relationship_kind` values in v1:
- `curated_recomposition`
- `module_variant`
- `parameterized_variant`

## Read / Render Contract

A variant renders:
1. variant author attribution
2. optional variant note authored in the new source
3. antecedent-source card resolved live from the original source
4. explicit lineage metadata:
   - `relationship_kind`
   - `relationship_label`
   - optional `module_param_delta`

The resolved antecedent card includes the same preview contract as reposts where practical:
- `available`
- `source_type`
- `source_id`
- `channel_id` for channel variants
- `author_id`
- `author_display`
- `created_at`
- `preview_text`
- `body_text` / `body_truncated`
- `embed`
- `has_source_layout`
- `deck_default_ref`
- `href`
- `unavailable_reason`

## API / UI Surface

Feed:
- `POST /api/v1/feed/posts/<post_id>/variant`
- `POST /ajax/variant_post`

Channels:
- `POST /api/v1/channels/<channel_id>/messages/<message_id>/variant`
- `POST /ajax/variant_channel_message`

UI:
- feed inline variant composer under the action row
- channel inline variant composer under the action row
- antecedent cards use distinct lineage language (`Derived from`, `Open antecedent`) and show relationship metadata

## Review Focus

Before merge, verify:
- generic paths still strip forged lineage metadata
- variants inherit original visibility / scope correctly
- reposts cannot be used as variant sources
- antecedent cards degrade cleanly when the source disappears or access changes
- feed and channel serializers emit `is_variant` and `variant_reference`
- inline composers behave like repost composers but remain visually distinct
