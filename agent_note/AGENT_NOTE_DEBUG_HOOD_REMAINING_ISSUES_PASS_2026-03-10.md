# Agent Note: Debug-Hood Remaining Issues Pass

Date: 2026-03-10
Repo: local Dropbox Canopy workspace
Status: Local only. Nothing from this pass was synced to the local `Canopy-Dev` working tree.

## Scope
This pass addressed the five issues still open from `#debug-hood` after the earlier UI scale and lifecycle work:

1. Remote agent accounts sometimes appeared as human on other peers.
2. The channel reply button could intermittently do nothing.
3. Avatar/profile sync on a new mobile account was unreliable.
4. The mini player could reset a YouTube video at startup / first mini-player update.
5. Local accounts could appear as `remote` in the identity/admin UI if their row carried the local peer ID in `origin_peer`.

## Root Causes
### 1. Remote agent accounts appearing as human
The profile sync payload did not include `account_type`, and the remote profile apply path did not write `account_type` even if it was present. That meant remote peers could keep stale `human` defaults indefinitely.

### 2. Channel reply button intermittently no-op
The channel message card rendered the reply button using inline `onclick="setReplyTo(...)"` with interpolated author/content text. Certain message contents could break that inline JavaScript shape, leaving the button inert.

### 3. Avatar/profile sync on new mobile account
The local profile-card emission path was too strict about which users counted as local profiles worth syncing. It effectively assumed password-backed local users, which is too narrow for some legitimate local-account states and makes profile/avatar propagation brittle.

### 4. Mini player resetting YouTube on start
The mini-player update path eagerly auto-docked YouTube embeds whenever they were offscreen. That forced DOM movement during normal update flow and could restart/reset playback state.

### 5. Local accounts rendered as remote when `origin_peer == local_peer_id`
Parts of the identity UI were treating any non-empty `origin_peer` as remote. That is wrong when the row carries the current node's own peer ID. The effect was a local admin account on Windy showing up as `remote` on the identity card.

## Implementation
### Profile/account metadata hardening
File: `canopy/core/profile.py`

Changes:
- `get_profile_card()` now includes normalized `account_type`.
- `update_from_remote()` now applies remote `account_type` when valid and changed.

Impact:
- Remote peers receive agent/human identity metadata in normal profile sync.
- Agent accounts stop drifting back to `human` on other peers when profile sync is the source of truth.

### Local profile sync eligibility hardening
File: `canopy/core/app.py`

Changes:
- `_get_local_profile_sync_user_ids()` no longer assumes local users must have a password hash.
- It now treats local auth evidence more conservatively and correctly:
  - password hash
  - public key
  - API key
- Blank-origin synthetic `peer-*` rows without local auth evidence are still excluded.

Impact:
- Legitimate local accounts with non-password auth evidence still emit profile cards.
- Avatar/profile propagation is more robust for newer/mobile/local-first account shapes.

### Reply button rendering hardening
File: `canopy/ui/templates/channels.html`

Changes:
- Replaced fragile inline `setReplyTo(...)` interpolation with dataset-backed attributes.
- Added `setReplyFromButton(button)` helper.
- Reply buttons now carry:
  - `data-reply-message-id`
  - `data-reply-author`
  - `data-reply-preview`

Impact:
- Reply activation is no longer dependent on safe inline JS interpolation of arbitrary message text.
- This should remove the intermittent "reply click does nothing" behavior tied to message content shape.

### Mini-player YouTube startup hardening
File: `canopy/ui/static/js/canopy-main.js`

Changes:
- Removed eager YouTube auto-docking from `updateMini()`.
- Broadened YouTube control handling so playback controls still work without requiring that forced docking step.

Impact:
- The mini player no longer reparents YouTube embeds during passive update flow.
- This reduces the chance of reset/restart behavior at startup or first mini-player refresh.

### Local-vs-remote identity normalization
Files:
- `canopy/ui/routes.py`
- `canopy/ui/templates/base.html`
- `canopy/ui/static/js/canopy-main.js`
- `canopy/ui/templates/admin.html`

Changes:
- Added normalization helpers so `origin_peer == local_peer_id` is treated as local, not remote.
- `ajax/get_user_display_info` now returns local semantics for those rows.
- The identity modal now normalizes a local-peer origin before deciding whether to label the user as `remote`.
- The admin workspace header now uses the same local-peer normalization.

Impact:
- A local admin account no longer appears as `remote` just because the row carries the current node's peer ID.
- Identity-card remote/local labels now align with operational reality rather than raw row shape.

## Files Changed In This Pass
Runtime:
- `canopy/core/profile.py`
- `canopy/core/app.py`
- `canopy/ui/routes.py`
- `canopy/ui/templates/base.html`
- `canopy/ui/templates/channels.html`
- `canopy/ui/static/js/canopy-main.js`
- `canopy/ui/templates/admin.html`

Targeted regression coverage used for this pass:
- `tests/test_profile_sync_metadata.py`
- `tests/test_profile_page_regressions.py`
- `tests/test_frontend_regressions.py`
- `tests/test_mention_suggestions_account_type.py`
- `tests/test_fk_race_and_avatar_recovery.py`

## Validation
Commands run locally:

```bash
python scripts/check_jinja_templates.py
node --check canopy/ui/static/js/canopy-main.js
python -m py_compile canopy/ui/routes.py canopy/core/profile.py canopy/core/app.py
pytest -q tests/test_profile_page_regressions.py tests/test_profile_sync_metadata.py tests/test_frontend_regressions.py tests/test_mention_suggestions_account_type.py tests/test_fk_race_and_avatar_recovery.py
```

Results:
- `Parsed 20 templates successfully`
- JS syntax check passed
- Python compile checks passed
- `34 passed`

## Reviewer Focus
1. Confirm remote profile sync now carries and applies `account_type` cleanly across mixed peers.
2. Confirm the broader local-profile eligibility rule does not accidentally reintroduce synthetic `peer-*` rows into profile sync.
3. Confirm channel reply activation remains stable with quotes, backticks, `${...}`, and multiline content.
4. Confirm YouTube mini-player behavior no longer resets playback on the first update cycle.
5. Confirm identity/admin UI no longer marks a user as remote when `origin_peer` equals the current node's peer ID.

## Recommended Live Checks
1. Create an `agent` account on one peer and verify it shows as `agent` on another peer after profile sync.
2. Create a fresh mobile/local account with avatar and verify the avatar appears remotely without manual repair.
3. Reply to channel messages whose content includes quotes, code fences, `${...}`, and newlines.
4. Start a YouTube embed, let the mini player initialize, and verify playback does not restart/reset.
5. On Windy, open the identity card for the local admin account and verify it shows `local`, not `remote`.

## Notes
- This pass was intentionally narrow and local.
- I did not sync these changes into `Canopy-Dev`.
- I did not touch unrelated lifecycle, release, or architecture work already present in the local worktree.
