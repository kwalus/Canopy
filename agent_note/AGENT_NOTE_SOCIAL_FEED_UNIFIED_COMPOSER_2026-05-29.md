# AGENT NOTE - Social Feed unified composer

Date: 2026-05-29
Branch: `codex/feed-composer-unification-0.6.287`
Target version: `0.6.287`

## User request

Konrad noted that Social Feed posting had drifted away from the channel composer experience. The old Feed flow forced users to choose a post type such as text, image, video, audio, or link before posting, while channel posting lets users simply write, attach, and send. The desired direction was a uniform Feed input that is quick for normal users, still supports mentions, but does not need the channel-style mention chips because Feed posting is more about a broad audience or a selected subgroup.

## Changes implemented

### 1. Unified Feed composer

File:
- `canopy/ui/templates/feed.html`

Changes:
- Removed the visible Feed post-type selector.
- Removed the separate media-upload section that was tied to selected image/video/audio type.
- Removed the separate link URL field.
- Kept the existing unified textarea, drag/drop attachment area, attachment button, Vault attach button, emoji picker, work-card builder, tags, lifespan, and structured-block validation.
- Added a compact “Audience / Lifespan / Tags” metadata grid so the user can choose where the post goes without being forced to classify the content first.
- `Local Network` is now the visible Feed composer default because the Feed is intended for broad social/workspace broadcast; the backend/API default remains private for safety when clients omit visibility.

Behavior:
- Users can write text, paste URLs, attach images/videos/audio/docs/code files, or combine file types in one flow.
- The browser infers a compatible `post_type` for existing Feed rendering:
  - all image attachments -> `image`
  - all video attachments -> `video`
  - all audio attachments -> `audio`
  - URL-only/content URL -> `link`
  - mixed files/docs/general posts -> `text`

### 2. Searchable custom audience picker

File:
- `canopy/ui/templates/feed.html`

Changes:
- Replaced the raw “Enter user ID” custom-permission input with a searchable, avatar-aware people/agent picker.
- The custom audience picker reuses the Feed user/agent directory already used by mention filtering/building.
- Users can search by display name, handle, username, role, or paste a user ID.
- Selected users render as removable chips, while `permissions` still submits the existing list of user IDs to avoid backend permission-model churn.

Rationale:
- This is the selected-subgroup counterpart to the broad Feed default.
- It avoids making users memorize internal IDs while preserving the existing `visibility: custom` storage and routing semantics.

### 3. Server-side Feed post-type inference

File:
- `canopy/ui/routes.py`

Changes:
- `/ajax/create_post` now treats omitted/blank `post_type` as `auto`.
- Server inference mirrors the browser fallback for attachments and URLs.
- Poll parsing still works for `auto`, `text`, and explicit `poll`.
- If a link is inferred server-side, URL/title metadata is filled from the first URL in the content.

Rationale:
- Browser UI now infers post type, but agents/API clients should not have to provide one for ordinary Feed posts.
- This keeps Feed posting robust if clients lag behind the UI.

## Regression coverage added/updated

Files:
- `tests/test_ui_polish_regressions.py`
- `tests/test_ui_structured_tool_feedback.py`
- `tests/test_privacy_first_trust_and_feed.py`

Coverage:
- Guards that the Feed composer uses a hidden post type field rather than visible type selection.
- Guards that old `mediaSection` / `postLink` / raw user-ID custom permission UI does not return.
- Guards the custom audience picker functions and UI hooks exist.
- Guards omitted `post_type` infers a `link` post and populates URL metadata.
- Updates the privacy-first template test to reflect the new visible Feed audience default while retaining backend/API private default checks.

## Verification

Passed:

```bash
python -m py_compile canopy/ui/routes.py canopy/__init__.py
PYTHONPATH=. pytest tests/test_ui_polish_regressions.py tests/test_ui_structured_tool_feedback.py tests/test_privacy_first_trust_and_feed.py tests/test_frontend_regressions.py -q
git diff --check
```

Results:
- `235 passed`
- `git diff --check` clean

## Review focus

Please verify in-browser:

1. Feed posting no longer asks the user to choose text/image/video/audio/link type.
2. Text-only, URL-only, image-only, video-only, audio-only, document-only, and mixed attachment Feed posts render correctly after creation.
3. Custom audience selection can find and add humans/agents by name or handle.
4. Mentions typed directly into Feed content still resolve and notify normally.
5. Backend/API clients that omit `post_type` can still create useful Feed posts.

## Known non-goals

- This patch does not remove the optional Feed Mention Team builder; it remains behind a tool button for users who want explicit mentions.
- This patch does not redesign Feed cards or Feed algorithms.
- This patch does not change FeedManager/API default visibility, which remains private when a client does not submit visibility.
