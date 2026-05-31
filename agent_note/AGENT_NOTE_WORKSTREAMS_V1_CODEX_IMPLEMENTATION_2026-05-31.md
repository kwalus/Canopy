# AGENT NOTE: Workstreams v1 implementation for review

Date: 2026-05-31
Author: Codex Agent
Branch: `codex/workstreams-v1-0.6.308`
Version bump: `0.6.307` -> `0.6.308`

## User intent

Konrad asked for the first implementation of a flexible Canopy-native **Workstream** object after review of agent/human collaboration patterns in the `training cellular automata` channel. The goal is to create a durable object that can be represented to humans, consumed by agents, linked to Digestions/files/posts/messages, and used for sustained work effort rather than one-off task cards. Konrad also explicitly requested that this not swell the already large existing source files, so this patch is intentionally additive and uses new source files with minimal hooks.

## What changed

### Backend core

New file: `canopy/core/workstreams.py`

Adds `WorkstreamManager` with its own schema bootstrap:

- `workstreams`
- `workstream_participants`
- `workstream_events`
- `workstream_artifacts`

Core concepts:

- Workstream status: `active`, `blocked`, `review_ready`, `complete`, `closed`, `archived`, `cancelled`
- Workstream priority: `low`, `normal`, `high`, `critical`
- Participant roles: `owner`, `lead`, `contributor`, `reviewer`, `watcher`, `assignee`
- Event types: `created`, `status`, `progress`, `artifact`, `blocker`, `decision`, `evidence`, `review`, `comment`, `handoff`
- Artifact types: `file`, `digestion`, `message`, `post`, `url`, `report`, `figure`, `code`, `note`

Implemented manager methods:

- `create_workstream(...)`
- `get_workstream(...)`
- `list_workstreams(...)`
- `user_can_view(...)`
- `user_can_edit(...)`
- `update_workstream(...)`
- `set_participants(...)`
- `add_event(...)`
- `add_artifact(...)`
- `to_agent_reference(...)`

Important behavior:

- Owner is always added as participant role `owner`.
- Channel members can view channel-linked Workstreams, but cannot edit until they are participant/owner/creator or claim/join.
- Events support `dedupe_key` for idempotent retry-safe agent progress updates.
- Artifacts are unique by `(workstream_id, artifact_type, ref_id)` to avoid duplicate file/Digestion references.

### Backend API

New file: `canopy/api/workstreams.py`

New isolated blueprint registered under both `/api/v1` and legacy `/api`:

- `GET /api/v1/workstreams/schema`
- `GET /api/v1/workstreams?channel_id=&status=&include_closed=&limit=`
- `POST /api/v1/workstreams`
- `GET /api/v1/workstreams/<id>`
- `PATCH /api/v1/workstreams/<id>`
- `POST /api/v1/workstreams/<id>/participants`
- `POST /api/v1/workstreams/<id>/claim`
- `POST /api/v1/workstreams/<id>/events`
- `POST /api/v1/workstreams/<id>/artifacts`
- `GET /api/v1/workstreams/<id>/agent-reference`

Auth model:

- API keys use existing `Permission.READ_FEED` / `Permission.WRITE_FEED`.
- Browser session fallback is supported with CSRF validation for unsafe methods.
- Channel-linked Workstream creation/listing checks existing `ChannelManager.get_channel_access_decision`.
- Mutating existing Workstreams requires owner/creator or active participant with an edit-capable role.

Minimal app hooks:

- `canopy/core/app.py` imports and registers `create_workstream_api_blueprint`.
- `canopy/core/app.py` initializes `WorkstreamManager` after `TaskManager`.

### Frontend reader/linking

New files:

- `canopy/ui/static/js/workstreams.js`
- `canopy/ui/static/css/workstreams.css`

Minimal template hook:

- `canopy/ui/templates/base.html` includes the new CSS and JS.

UI behavior:

- Rich rendered post/message content is scanned for `Ws...` Workstream IDs or `workstream:Ws...` references.
- References are converted into compact Workstream pills.
- Clicking a pill opens a theme-aware Workstream reader overlay showing:
  - title/status/priority/objective
  - required output
  - participants with avatars
  - linked artifacts with file/Digestion/post/message/URL routing
  - recent progress events
  - copy agent reference button

This is intentionally isolated from `canopy-main.js` to avoid adding more logic to the large central file. The first reader is overlay/modal based. It is Deck-ready in structure but does not yet use the full Canopy Deck shell. If desired, the next patch can bind Workstreams into Deck as a first-class source type.

### Agent instructions

Updated `canopy/api/agent_instructions_data.py`:

- Adds Workstreams to high-level capabilities.
- Adds a full `workstreams` section with endpoint descriptions and usage notes.
- Agents are instructed to create Workstreams for sustained work and attach every produced file/Digestion/report/figure/code object as artifacts.

## Tests

New file: `tests/test_workstreams_v1.py`

Coverage includes:

- Manager creates Workstreams with hydrated participants/artifacts.
- Event idempotency via `dedupe_key`.
- Channel members can view channel Workstreams but cannot edit until added/claimed.
- API create/get/artifact flow works across owner and agent keys.
- Non-member cannot create a channel-linked Workstream.

Command run:

```bash
python -m pytest tests/test_workstreams_v1.py -q
```

Result:

```text
5 passed in 0.52s
```

Also run:

```bash
python -m py_compile canopy/core/app.py canopy/api/agent_instructions_data.py canopy/api/workstreams.py canopy/core/workstreams.py
node --check canopy/ui/static/js/workstreams.js
```

Both passed.

## Review notes / risks

- This is v1 scaffolding and deliberately avoids heavy coupling to tasks, requests, Digestions, Capsules, or Deck internals.
- Workstream objects are local database objects. This patch does not yet implement mesh replication semantics for Workstreams. That should be reviewed before relying on Workstreams as mesh-wide durable state.
- The UI reader is an overlay, not yet the full Canopy Deck surface. It uses the same source/object shape needed for a Deck integration in a later patch.
- The API currently uses `READ_FEED`/`WRITE_FEED` permissions because Workstreams are coordination/feed-adjacent objects. If we want a narrower agent permission in the future, add explicit `READ_WORKSTREAMS`/`WRITE_WORKSTREAMS` style permissions in a later migration.
- The artifact links defer real access enforcement to existing file/Digestion/post/message routes. This is appropriate for v1 and avoids duplicating ACL logic.

## Suggested next patch

1. Add Workstream creation from the structured work builder UI as a new card type.
2. Add a Workstream Deck source type so the reader can open in the Deck shell, not only the overlay.
3. Add capsule/workproduct integration so agent-run Capsules can promote a sustained run into a Workstream and attach outputs automatically.
4. Decide whether Workstreams should be local-only, channel-scoped mesh-synced, or selectively package/exportable like Digestions.
