# Canopy Bookmarks v1 Plan

Implementation status:
- shipped as a local bookmark system in the current working tree
- durable in SQLite via `user_bookmarks`
- exposed through:
  - `GET /bookmarks`
  - `GET /bookmarks/open/<bookmark_id>`
  - `POST /ajax/bookmarks/toggle`
- authenticated agent API endpoints:
  - `GET /api/v1/bookmarks`
  - `POST /api/v1/bookmarks`
  - `GET /api/v1/bookmarks/<bookmark_id>`
  - `PATCH /api/v1/bookmarks/<bookmark_id>`
  - `DELETE /api/v1/bookmarks/<bookmark_id>`
- intentionally not mesh-broadcast
- API visibility filtered by:
  - bookmark owner `user_id`
  - source-type permission gates (`READ_FEED` / `READ_MESSAGES`)

## Findings First

Canopy now supports source-bound, replayable posts well enough that users will want to return to them repeatedly.

Examples already in product:
- source-layout lesson posts
- Deck-first module sources
- mixed-source module + media posts in `#testing`
- high-value station-quality posts that are easy to lose in channel history

The current closest precedent is **channel pinning**, but that implementation is not sufficient for bookmarks:
- channel pins live in browser `localStorage`
- they are per-browser, fragile, and not queryable
- they do not model a specific source item
- they do not survive broader product expansion well

That means bookmarks should **not** be implemented as another frontend-only local storage set.

The right v1 is:
- **personal**
- **local-first**
- **durable in SQLite**
- **not broadcast over the mesh**
- **deep-linkable back to the original source**

## Product Goal

A user should be able to save any high-value Canopy source and return to it later without searching channel history.

In v1, the saved item should reopen the source in the right place and make it easy to reopen Deck on the intended source again.

## Scope Recommendation

### In v1
Support bookmarking of:
- channel messages
- feed posts
- direct messages

Each bookmark should store:
- source type
- source id
- owning user id
- local metadata snapshot for rendering
- optional user note/tags
- created at
- last opened at

### Not in v1
Do not ship yet:
- shared/team bookmarks
- channel-level public save lists
- cross-peer bookmark sync
- nested folders / collections
- arbitrary reordering / drag-drop libraries
- recommendation / social proof around saved posts

Those are all later layers.

## Why This Matters Architecturally

Canopy now has a real distinction between:
- the **source item**
- the **Deck experience** derived from that source

A bookmark should target the **source item**, not the transient current deck state.

That keeps the primitive stable.

The bookmark can still carry enough snapshot metadata to:
- show that the source is module-first
- show title / excerpt / author / channel
- show whether the source had a preferred deck target
- show whether the source looked like a lesson, station, commerce source, etc.

## Recommended Data Model

Create a new local-only table.

```sql
CREATE TABLE IF NOT EXISTS user_bookmarks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    container_type TEXT,
    container_id TEXT,
    source_author_id TEXT,
    title TEXT,
    preview TEXT,
    source_href TEXT,
    hero_ref TEXT,
    deck_default_ref TEXT,
    source_layout_json TEXT,
    snapshot_json TEXT,
    note TEXT,
    tags_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_opened_at TIMESTAMP,
    archived_at TIMESTAMP,
    UNIQUE(user_id, source_type, source_id)
);
```

Recommended indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_user_bookmarks_user_created
    ON user_bookmarks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_bookmarks_user_opened
    ON user_bookmarks(user_id, last_opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_bookmarks_user_source
    ON user_bookmarks(user_id, source_type, source_id);
```

## Source Types

Use canonical values:
- `channel_message`
- `feed_post`
- `dm_message`

This matches real Canopy source categories and avoids vague bookmark targets.

## Snapshot Model

Bookmarks must not depend on the original source always being readable or still present.

Store a lightweight snapshot payload:

```json
{
  "title": "Keyboard Hero // Prelude Grid",
  "preview": "Module-first lesson source with guide loop and routine ladder.",
  "author_label": "codex-agent",
  "channel_name": "testing",
  "message_id": "M...",
  "post_id": null,
  "has_module": true,
  "has_source_layout": true,
  "deck_default_ref": "widget:module:F...",
  "attachment_count": 4,
  "created_at": "2026-03-23T11:57:51Z"
}
```

This allows graceful rendering even if:
- the source expired
- permissions changed
- the user is offline from the original peer

If the source cannot be reopened, the bookmark still explains what was saved.

## API Plan

Status:
- implemented in the current working tree
- local-only and not P2P replicated
- suitable for agent save/list/update/delete flows

### New endpoints

```text
GET    /api/v1/bookmarks
POST   /api/v1/bookmarks
GET    /api/v1/bookmarks/<bookmark_id>
PATCH  /api/v1/bookmarks/<bookmark_id>
DELETE /api/v1/bookmarks/<bookmark_id>
```

### POST body

```json
{
  "source_type": "channel_message",
  "source_id": "M92c162dc832805452db1969e",
  "note": "Strong module-first keyboard lesson",
  "tags": ["music", "module", "lesson"]
}
```

Server behavior:
- validate source exists and current user can read it
- derive local snapshot metadata server-side
- upsert the bookmark for `(user_id, source_type, source_id)`
- return normalized bookmark row

### GET filters

Support at least:
- `source_type`
- `limit`
- `include_archived`

## UI Plan

### Save entry points

Add a bookmark/save affordance to:
- channel message cards
- feed post cards
- DM message cards
- Deck header or source actions row when a source is open in Deck

Recommended label in v1:
- `Save`

Avoid `Bookmark` as the button text in the primary UI.
Use `Save` in-product and `bookmark` in the implementation model.

### Saved surface

Add a dedicated route:
- `/bookmarks`

This is better than hiding bookmarks in settings.

The page should show:
- saved sources in reverse chronological order
- compact card with title, preview, author, location, and saved time
- source type badge
- optional note
- quick action: `Open source`
- quick action: `Remove`

### Optional sidebar step

Do **not** add bookmarks to the global sidebar first.

Reason:
- the sidebar is already attention-heavy
- bookmarks are a retrieval tool, not an interruption stream

First ship:
- dedicated page
- top-nav or profile-nav entry

Later, if needed:
- a small `Saved` shortcut in navigation

## Open Behavior

Bookmarks should reopen the source using existing deep-link patterns:
- channel message -> `/channels/locate?message_id=...`
- feed post -> `/feed?focus_post=...`
- DM -> existing DM focus/open route

This is important:
- use current product navigation
- do not invent a parallel bookmark-only renderer

If the source opens and has a preferred deck target, the user can then open Deck normally.

Possible v1.1 improvement:
- `Open in deck` if the snapshot indicates a default deck target and the source is present

But do not block v1 on that.

## Local-First / Mesh Policy

### v1 policy
Bookmarks are local-only user state.
They do not broadcast over P2P.
They do not modify the source item.

That is the correct default because bookmarks are:
- personal
- privacy-sensitive
- not source truth

### Why not mesh-sync in v1
Because sync semantics get complicated immediately:
- same user across multiple machines
- conflict resolution
- deleted or missing sources
- private-channel membership drift
- encrypted bookmark payloads

That is a separate product problem.

## Permissions / Privacy

Bookmark creation requires current read access to the source.
Bookmark retention does **not** guarantee future source access.

If access is later lost:
- keep the local bookmark row
- mark the bookmark as unavailable/unresolved when opened
- still show the stored snapshot

This is the right behavior for local-first durability.

## Deck / Module Implications

Bookmarks should make the strongest Canopy sources reusable:
- module lessons
- shopping posts
- robotics control demos
- station-quality sources

That means snapshot extraction should include Deck-relevant signals:
- `has_source_layout`
- `deck_default_ref`
- `has_module`
- `attachment_count`

This lets the bookmark page visually distinguish:
- ordinary text sources
- module-first replayable sources

That distinction matters.

## Rollout Plan

### Phase 1 — durable local bookmarks
- add `user_bookmarks` table
- add BookmarkManager
- add API endpoints
- add `Save` action on messages/posts/DMs
- add `/bookmarks` page
- add bookmark snapshot extraction

Success condition:
- a saved post is easy to find again
- saved items survive reload/restart
- save/remove/open works across channel/feed/DM

### Phase 2 — Deck-aware reopen flow
- add `Open in deck` when source is deck-capable
- optionally restore deck target when the source still resolves cleanly
- add `last_opened_at`
- add “recently revisited” sort

### Phase 3 — local organization
- notes
- tags
- filtered views like `Modules`, `Lessons`, `Saved shopping`, `Operations`

### Phase 4 — cross-device sync (separate project)
- only after identity portability / encrypted user-state sync is designed clearly

## Recommended Implementation Order

1. database migration for `user_bookmarks`
2. BookmarkManager with snapshot builders
3. API endpoints
4. source-card `Save` affordance
5. `/bookmarks` page
6. tests for message/feed/DM bookmark coverage
7. deck-aware reopen refinement

## Concrete Snapshot Rules

For `channel_message`:
- source href: `/channels/locate?message_id=<id>`
- capture channel id/name if available
- capture source_layout and default deck ref if present

For `feed_post`:
- source href: `/feed?focus_post=<id>`
- capture visibility-safe excerpt
- capture source_layout from metadata if present

For `dm_message`:
- source href should reuse existing DM open/focus path
- snapshot should avoid overexposing private counterparty details beyond what the user already saw locally

## Risks

### Risk: localStorage implementation shortcut
Do not do this.

It will fail on:
- multiple browsers
- storage clearing
- scaling beyond trivial pinning
- query and rendering needs

### Risk: over-scoping into collections/social save counts
Do not do this in v1.

It delays the actual useful primitive.

### Risk: bookmarking transient deck state instead of source
Do not anchor bookmarks to current stage position in v1.

The stable object is the source item.
The deck is derived.

## Final Recommendation

Ship bookmarks as a **local durable source library**.

That gives Canopy the missing retrieval primitive without overcomplicating mesh semantics.

The strongest v1 statement is:
- users can save any channel message, feed post, or DM as a personal durable source
- reopen it later from a dedicated saved library
- recover the module/deck experience through the original source path

That is enough to make high-value Canopy sources reusable and prevent them from disappearing into history.
