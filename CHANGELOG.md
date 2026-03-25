# Changelog

All notable changes to Canopy will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.5.0] - 2026-03-24

### Added
- **Canopy Module public-release baseline** — `Canopy Module` is now documented and versioned as a first-class layer in the product surface, making `0.5.0` the right milestone for the first curated public release candidate.

### Changed
- **Public release scrub** — Removed personal attribution and AI-process metadata from committed code/doc headers that are intended to ship in the public-facing tree.
- **Release candidate alignment** — Package metadata, README versioning, and operator-facing docs now point to the `0.5.0` surface so the first public candidate is coherent across install, API, MCP, and tray documentation.

## [0.4.159] - 2026-03-24

### Fixed
- **Deck queue YouTube selection** — Queue clicks now stay bound to the clicked item instead of drifting across hidden facade/iframe transitions, and YouTube facade activation reuses the shared media registration hook so first-click playback in the deck no longer dies with a `registerMediaNode` reference error.

### Changed
- **Quieter deck launchers** — The `Deck` / `Mini` launcher controls at the bottom of posts and messages now use the same height, calmer chrome, and lower-contrast emphasis as the other action buttons so they no longer stretch or visually dominate the action row.
- **Theme debug cleanup** — Removed the temporary theme/light-element debug helpers and automatic console spam that had accumulated during UI debugging.

## [0.4.158] - 2026-03-24

### Changed
- **Mobile deck prioritizes playback** — On small screens the expanded deck now defaults to a playback-first layout by collapsing secondary queue/detail panels, shrinking the deck header chrome, and giving the stage more vertical room.
- **Deck item changes play in place** — Selecting an item from the mobile deck queue or using `Prev` / `Next` now starts playback directly in the deck and recenters the stage, making it clearer that media is playing there instead of pushing attention back to the source.

## [0.4.157] - 2026-03-24

### Changed
- **Mobile channel header cleanup** — On narrow screens the channel header now keeps search one tap away behind a dedicated icon instead of permanently consuming a full header row, while lower-frequency policy controls stay under `More`.
- **Mobile composer prioritization** — The channel composer now keeps `attach`, `emoji`, `more`, and `send` on the main row, moving mention-builder, structured blocks, and stream tools behind a compact compose overflow menu.

## [0.4.156] - 2026-03-24

### Changed
- **More consistent post actions** — Feed posts and channel messages now keep `Like` visible with the primary actions instead of burying it in overflow, while lower-frequency controls such as thread inbox stay behind `More`.
- **Theme-matched action styling** — Primary post/message controls now use a shared Canopy-themed treatment and active states, replacing the noisier mix of Bootstrap accent colors.
- **Tighter channel header controls** — The channels header now shifts to icon-first controls, moves secondary utilities into a single `More` menu, and wraps more cleanly on smaller widths to avoid cramped overlaps.

## [0.4.155] - 2026-03-24

### Changed
- **Simpler post action toolbars** — Channel message and feed post action rows now keep the most-used controls front and center: `Reply`, `Bookmark`, and `Repost`, with `Deck` still shown when relevant. Lower-frequency actions move behind a single `More` menu to reduce visual clutter.

### Added
- **Toolbar regression coverage** — Expanded `tests/test_ui_polish_regressions.py` so the primary-vs-overflow action layout is guarded against accidental drift.

## [0.4.154] - 2026-03-24

### Added
- **Public release audit note** — Added `docs/PUBLIC_RELEASE_AUDIT.md` to summarize the latest low-risk repo exposure review and record the path-scrub hardening that came out of it.
- **UI polish regression coverage** — Added `tests/test_ui_polish_regressions.py` and included it in the public CI list to guard the latest accessibility and empty-state tweaks.

### Changed
- **README highlights** — Rewrote the `Recent Highlights` section to focus on user-facing capabilities such as bookmarks, reposts, variants, richer deck flows, and Windows tray usability instead of debug-oriented release notes.
- **Doc version alignment** — Updated the remaining version-scope notes in `API_REFERENCE`, `QUICKSTART`, `MENTIONS`, and `WINDOWS_TRAY`, and removed stale links to a non-existent identity-portability doc.

### Fixed
- **Small UI/UX rough edges** — Feed post sharing now shows a loading state, feed empty-search results offer a clear reset path, message/channel empty states are friendlier, the channel reply cancel button is labeled for assistive tech, and profile avatar upload is keyboard accessible.
- **Path disclosure cleanup** — Replaced absolute local-machine file citations in `docs/REPOST_V1_DESIGN_REVIEW.md` with project-relative paths.

## [0.4.153] - 2026-03-23

### Fixed
- **Channel antecedent deck retry ladders** — The `open_deck=1` handoff on the channels page now delegates to the shared antecedent-deck helper with a short `requestAnimationFrame` retry path instead of a long sequence of timeout retries. The shared channel helper also drops its extra `120ms` / `450ms` retries, keeping the deck-open flow aligned with the intended “focus, apply layouts, retry briefly” behavior.

## [0.4.151] - 2026-03-23

### Fixed
- **Antecedent deck open** — Removed **forced `/feed?focus_post` / locate redirect** after 420ms when the original post was **already** in the DOM (it could reload or race the deck). **Layout compositor** now runs **after** the first open attempt, not before every attempt (avoids redundant shell churn). One **rAF** retry only.

## [0.4.150] - 2026-03-23

### Fixed
- **Deck from repost/variant “Open deck”** — Opening the antecedent’s deck now **runs `canopyApplySourceLayoutsInScope` on that post/message first**, retries on animation frames, tries **module/widget manifest** hosts if the generic scan is empty, then falls back to **`/feed?focus_post=…&open_deck=1`** or **`/channels/locate?…&open_deck=1`**. Fixes silent no-op when layout compositor had not yet run on the original card.
- **Feed/channel repost chrome** — **Deck** control is in the **Original source** header next to **Deck-ready** (with `white-space: nowrap` on the badge) so it is not buried below the quote; duplicate bottom deck button removed.

### Added
- **Deck helpers without sidebar mini host** — If `#sidebar-media-mini` is missing, **`openDeckForFeedAntecedentPost` / `openDeckForChannelAntecedentMessage`** still exist and **navigate** via the deep-link URLs.

## [0.4.149] - 2026-03-23

### Changed
- **Feed & channel variant UI** — Lineage variants are no longer shown as a large nested “antecedent card” (duplicate body, embeds, and framing). The **author’s variant text** reads like a normal post; **parameter delta** (if any) stays as a light note; provenance is a **compact bar** (relationship badge, link to antecedent, optional deck). **Reposts** are unchanged.

## [0.4.148] - 2026-03-23

### Fixed
- **Deck on feed/channel repost & variant cards** — Wrappers never had **`data-canopy-source-layout`** (only the antecedent does), so the deck launcher did not appear. Added **Open deck** on repost/variant cards (when **`has_source_layout`**) via **`openDeckForFeedAntecedentPost`** / **`openDeckForChannelAntecedentMessage`**: opens the deck from the antecedent row if it is already in the DOM, otherwise navigates with **`focus_post`/`focus_message` + `open_deck=1`**. **`/channels/locate`** forwards **`open_deck`** to the channel view.

## [0.4.147] - 2026-03-23

### Fixed
- **Channels page `ReferenceError: currentChannelName is not defined`** — Repost/variant UI in **`renderMessage`** used **`currentChannelName`** in template strings without a global. Added **`currentChannelName`** (initial value from first channel) and **`selectChannel`** keeps it in sync with **`channelName`**.

## [0.4.146] - 2026-03-23

### Fixed
- **Channels “Error loading messages” after lineage work** — `displayMessages` failures were chained to the same `.catch` as `fetch`, so a **render** exception looked like a **load** failure. Render errors now use a **dedicated inner `.catch`** plus a **try/catch** around the render block (with console logging). API errors still show “Error loading messages”; render errors show a distinct message.

## [0.4.145] - 2026-03-23

### Fixed
- **Channel lineage (repost/variant) breaking message load** — Building the reference preview could raise on odd `message_type` / `created_at` values on the resolved original, which caused **`ajax_get_channel_messages`** to **skip the entire wrapper message**. Previews now use safe serializers and catch preview build errors (mark reference unavailable). UI decoration failures clear **`is_repost` / `is_variant`** instead of aborting the message row.
- **Channels spinner** — **`loadChannelMessages`** now **chains on `displayMessages`’s promise** so failures in the render path hit **`catch`** and **`finally`** (sidebar attention refresh still runs only when **`marked_read`**).

## [0.4.144] - 2026-03-23

### Fixed
- **Channels not loading / empty threads** — Channel message AJAX decorated repost/variant links with `url_for('ui.locate_channel_message')`, which **does not exist**, causing **`BuildError`** and **skipped messages** in the response (data remained in DB). Now uses **`ui.channels_locate`** with **`/channels/locate?...`** fallback if `url_for` fails.
- **Channel sidebar snapshot** — **`archived_at`** serialized via **`_sidebar_archived_at_iso`** so non-datetime values cannot break **`/ajax/channel_sidebar_state`**.

### Notes
- Restart web + hard-refresh for **`?v=0.4.144`**. After restart, ensure **one** process owns **7770** and **7771** (avoid duplicate instances).

## [0.4.143] - 2026-03-23

### Security
- **Channel repost & lineage variant API** — `POST .../channels/<id>/messages/<msg_id>/repost` and `.../variant` now require **`WRITE_MESSAGES`** (decorator) **and** **`READ_MESSAGES`** (explicit handler check). Keys with **only** `READ_FEED` + `WRITE_FEED` no longer pass the auth gate; `WRITE_MESSAGES` without `READ_MESSAGES` returns **`READ_MESSAGES permission required`**. Aligns channel lineage mutations with the message surface and least-privilege agent keys.

### Tests / docs
- **`tests/test_channel_repost_v1.py`**, **`tests/test_channel_variant_v1.py`** — permission regression cases; **`docs/API_REFERENCE.md`**, **`docs/AGENT_ONBOARDING.md`**.

### Notes
- Restart web + hard-refresh for **`?v=0.4.143`**.

## [0.4.142] - 2026-03-23

### Added
- **Lineage variants v1** — Narrow provenance wrappers using **`source_reference.kind = variant_v1`** alongside **`repost_v1`**. Feed **post → feed variant** (visibility same as antecedent; `public` / `network` / `trusted` only); channel **message → same-channel variant**. Optional **`relationship_kind`** (`curated_recomposition`, `module_variant`, `parameterized_variant`) and **`module_param_delta`**. No copied antecedent body/attachments/layout; **repost wrappers cannot be antecedents**; generic create/update strips forged `source_reference`; feed **PATCH** preserves legitimate existing provenance. **API:** `POST /api/v1/feed/posts/<id>/variant`, `POST /api/v1/channels/<id>/messages/<msg_id>/variant`. **UI:** inline amber-styled variant composer; **`variant_reference`** / **`is_variant`** on responses. **P2P:** variant metadata propagates with channel messages where applicable.

### Tests / docs
- **`tests/test_variant_v1.py`**, **`tests/test_channel_variant_v1.py`**, **`docs/LINEAGE_VARIANTS_V1_PLAN.md`**, updates to **`docs/API_REFERENCE.md`**, **`docs/AGENT_ONBOARDING.md`**, **`docs/REPOST_V1_IMPLEMENTATION_PLAN.md`**, **`README.md`**.

### Notes
- Restart web + hard-refresh feed and channels for **`?v=0.4.142`**.

## [0.4.141] - 2026-03-23

### Docs
- **README** — Version badge `0.4.141`, nav link to **Repost v1** plan, **Documentation Map** entries for `REPOST_V1_DESIGN_REVIEW.md`, `REPOST_V1_IMPLEMENTATION_PLAN.md`, `BOOKMARKS_V1_PLAN.md`.
- **`docs/API_REFERENCE.md`** — Web UI AJAX notes for feed (`/ajax/repost_post`, `/ajax/share_post`) and channels (`/ajax/repost_channel_message`).
- **`docs/AGENT_ONBOARDING.md`** — Channel repost: UI uses session AJAX path.
- **`docs/MCP_QUICKSTART.md`** — Version alignment + repost v1 endpoint reminder for MCP agents.
- **`docs/REPOST_V1_DESIGN_REVIEW.md`** — Correct channel UI path to `POST /ajax/repost_channel_message` + JSON body shape.
- **`docs/REPOST_V1_IMPLEMENTATION_PLAN.md`** — Read/render contract (`body_text`, `embed`, `channel_id`); API/UI/P2P shipped summary.

### Notes
- Prepares repo docs for upcoming **public** release curation so the public-facing documentation stays aligned with the primary development tree.

## [0.4.140] - 2026-03-23

### Added
- **Channel repost v1 (same-channel reference wrappers)** — `channel_messages.source_reference` + `repost_policy` columns (migrated on startup). **`ChannelManager`**: `get_repost_eligibility`, `resolve_repost_reference`, `create_repost`; `send_message` / `update_message` only accept `source_reference` when **`allow_source_reference=True`** (forged wrappers stripped on generic paths). **API:** `POST /api/v1/channels/<channel_id>/messages/<message_id>/repost`, channel payloads include **`is_repost`** / **`repost_reference`** (aligned with feed: `body_text`, `embed`, `href` → `/channels/locate?message_id=…`). **UI:** inline repost composer in **`channels.html`**, **`POST /ajax/repost_channel_message`**. **P2P:** create/edit/catchup carry `source_reference` and `repost_policy`.

### Tests / docs
- **`tests/test_channel_repost_v1.py`**, **`tests/test_frontend_regressions.py`** markers; **`docs/API_REFERENCE.md`**, **`docs/AGENT_ONBOARDING.md`**, **`docs/REPOST_V1_IMPLEMENTATION_PLAN.md`**, **`README.md`**.

### Notes
- Restart web + hard-refresh channels view for **`?v=0.4.140`**.

## [0.4.139] - 2026-03-23

### Changed
- **Feed repost UI** — Repost no longer uses a Bootstrap modal (avoids overlay/focus issues). **Repost** toggles an **inline composer** directly under that post’s action row (optional note, Cancel / Repost, character count). **Esc** closes when focus is inside the composer; **Ctrl/Cmd+Enter** submits. Active **Repost** button is highlighted while open.

### Notes
- Restart the web process and **hard-refresh** the feed.

## [0.4.138] - 2026-03-23

### Changed
- **Feed repost UX** — Embedded original shows **full card-style preview**: long `body_text` (with truncation hint), link/image/video/audio embeds, poll question + option previews, and image **attachment thumbnails** (live-resolved; not copied into the repost row). Repost action uses a **Bootstrap modal** with optional commentary, character counter, and **Ctrl/Cmd+Enter** to publish (replaces `window.prompt`).

### Docs
- **`docs/API_REFERENCE.md`** — `repost_reference` rich fields documented.

### Notes
- Restart the web process and **hard-refresh** for **`?v=0.4.138`** and template/JS updates.

## [0.4.137] - 2026-03-23

### Added
- **Feed repost v1 (secure reference wrappers)** — Reposts are **reference wrappers**, not copied posts: no copy of original body, attachments, or full metadata; visibility matches the source (**public** / **network** / **trusted** only; **private** / **custom** rejected). **Repost chains** and **`repost_policy: deny`** blocked. Generic **`POST/PATCH` feed** strips caller-forged **`source_reference`** / legacy share keys. **`FeedManager.create_repost`**, **`resolve_repost_reference`**, **`get_repost_eligibility`**; **`share_post`** aliases **`create_repost`**. Legacy **`shared_post_id`** rows still render/filter as reposts.

### API / UI
- **`POST /api/v1/feed/posts/<id>/repost`** (optional JSON **`comment`**). Feed payloads include **`repost_reference`** / **`is_repost`** when applicable.
- **UI:** Feed template repost shell + **`/ajax/repost_post`** and **`/ajax/share_post`** (same handler, secure semantics); action label **Repost**.

### Tests / docs
- **`tests/test_repost_v1.py`**, **`docs/REPOST_V1_DESIGN_REVIEW.md`**, **`docs/REPOST_V1_IMPLEMENTATION_PLAN.md`**, **`docs/API_REFERENCE.md`**, **`docs/AGENT_ONBOARDING.md`**, **`README.md`**.

### Notes
- Restart the web process and **hard-refresh** so static assets use **`?v=0.4.137`**.

## [0.4.136] - 2026-03-23

### Added
- **Bookmarks v1** — Local private saves for **`feed_post`**, **`channel_message`**, and **`dm_message`**: SQLite **`user_bookmarks`**, **`/bookmarks`** page, **`POST /ajax/bookmarks/toggle`** (CSRF), **`GET /bookmarks/open/<id>`**, REST **`/api/v1/bookmarks`** with permission-filtered list/detail ( **`READ_FEED`** for feed + channel sources, **`READ_MESSAGES`** for DMs). Server-derived snapshots; no P2P replication of bookmarks.

### Tests
- **`tests/test_bookmarks.py`** and related frontend/workspace/API session tests.

### Docs
- **`docs/BOOKMARKS_V1_PLAN.md`**, **`README.md`**, **`docs/API_REFERENCE.md`**, **`docs/AGENT_ONBOARDING.md`**.

### Notes
- Restart the web process and **hard-refresh** so static assets use **`?v=0.4.136`**.

## [0.4.135] - 2026-03-22

### Added
- **Source layout v1 (channels)** — Compositor upgrades: promote single-cell **media grids** for hero sizing; **split image grids** when the layout references multiple `attachment:` ids; **widget `data-canopy-source-ref`** on module cards for `supporting` / deck; **Open deck** in the action toolbar; shell / hero / lede / side / strip CSS.
- **`scripts/post_source_layout_multimedia_demo.py`** — Optional API demo post (images + module + full `source_layout`).
- **`docs/CANOPY_SOURCE_LAYOUT_V1.md`** — Source layout documentation.
- **Bundled `.canopy-module.html` surfaces** under `canopy/ui/static/modules/` (with SVG assets).

### Changed
- **AJAX channel messages** — Batch-merge `source_layout` from `channel_messages` so payloads match DB.
- **Channels template** — `scheduleChannelSourceLayouts` (rAF) after message inject; `displayAttachments(..., source_layout)`.

### Tests
- **`tests/test_source_layout.py`**, **`tests/test_frontend_regressions.py`** — Layout / UI regressions.

### Notes
- Restart the Canopy web process and **hard-refresh** so static `?v=0.4.135` and templates load.

## [0.4.134] - 2026-02-27

### Security
- **Source layout action URLs** — Reject **protocol-relative** URLs (`//host/...`) in `normalize_source_layout` (they were incorrectly allowed as “paths” because they start with `/`). Client compositor mirrors the same rule before emitting `<a href>`.

### Fixed
- **Source layout compositor** — If layout JSON changes while a shell already exists, **unwrap** the old shell and **rebuild** instead of leaving a stale layout.

### Tests
- **`tests/test_source_layout.py`** — Assert `//…` action URLs are stripped alongside `javascript:`.

### Notes
- **Local:** Restart the Canopy web process and **hard-refresh** so **`canopy-main.js`** and any template changes load at **v0.4.134**.

## [0.4.133] - 2026-03-21

### Changed
- **Media deck: scroll channel/feed behind dimmer (desktop)** — On **`(hover: hover)`** and **`pointer: fine`**, the deck backdrop uses **`pointer-events: none`** so **wheel scroll** reaches the channel/feed while the deck stays open. Touch/coarse pointers unchanged (tap-outside on dimmer still closes).

### Tests
- **`tests/test_frontend_regressions.py`** — backdrop pass-through comment substring.

### Notes
- **Local testing:** Restart the Canopy web process and **hard-refresh** the browser (or open a fresh tab) so inlined **`base.html`** styles and bumped **`?v={{ canopy_version }}`** script URLs pick up **v0.4.133**.

## [0.4.132] - 2026-03-21

### Fixed
- **Module deck: module visible again (regression from absolute iframe)** — **`position: absolute`** on the module iframe removed it from flow, so **`.sidebar-media-deck-widget-stage`** could collapse to **0 height** and the module disappeared. Restored **in-flow** **`flex`** sizing with **`min-height` clamps** on stage, widget-stage, and module iframe; kept **`object-fit: fill`** and **`injectDeckModuleRuntime`** **`html, body`** height shell.

### Tests
- **`tests/test_frontend_regressions.py`** — module iframe **`min-height` clamp** substring (replaces absolute/inset asserts).

## [0.4.131] - 2026-03-21

### Fixed
- **Module deck: iframe fills stage on Windows (Chromium)** — **`height:100%` / flex on replaced iframes** often left a large empty band (stage background visible). **`is-module-active`** module iframe now uses **`position: absolute; inset: 0`** inside **`position: relative`** **`.sidebar-media-deck-widget-stage`** (desktop, mobile, short landscape). **`injectDeckModuleRuntime`** adds base **`html, body { height: 100%; … }`** so module documents can fill the frame.

### Tests
- **`tests/test_frontend_regressions.py`** — absolute/inset module frame + **`data-canopy-module-shell`**.

## [0.4.130] - 2026-03-21

### Fixed
- **Module deck: stage fills available pane (Windows)** — Replaces the fixed **`vh` height clamp** on **`is-module-active`** (which capped the stage and made the module look smaller). **`sidebar-media-deck-scroll`** and **`stage-shell`** use a column **flex** chain so the stage **grows with the deck**; **`is-module-active`** shell **`max-height`** aligned with the taller deck cap; **`object-fit: fill !important`** on the module **iframe** so it overrides the global stage **`object-fit: cover`**.

### Tests
- **`tests/test_frontend_regressions.py`** — **`is-module-active`** **`stage-shell`** / **`scroll`** layout rules.

## [0.4.129] - 2026-02-27

### Fixed
- **Module deck: module iframe not filling the stage (Windows / Chromium)** — When **`is-module-active`**, the deck stage now uses a **definite `height`** (same **`clamp`** as min/max) so **`height: 100%`** on the module iframe resolves; widget stage remains a column flex child with **`min-height: 0`**; module frame uses **`flex: 1`**, **`object-fit: fill`**, and matching rules for mobile and short-landscape breakpoints.

## [0.4.128] - 2026-03-21

### Fixed
- **Module deck: queue missing for mixed sources** — `syncDeckLayoutMode` ran before `renderDeckQueue`, so the queue item count could be stale and the list stayed collapsed with no way to pick YouTube. Layout now keys off `state.deckItems.length` (`deckLayoutLastQueueCount`), keeps the **FROM THIS SOURCE** strip **expanded** when there is more than one deck item, and re-runs `syncDeckLayoutMode` after `renderDeckQueue`.

### Changed
- **Module deck scroll** — Sticky **FROM THIS SOURCE** queue header while **`is-module-active`** so **Show list** / the strip stays reachable above a tall module stage.

### Tests
- **`tests/test_frontend_regressions.py`** — `deckLayoutLastQueueCount` substring.

## [0.4.127] - 2026-03-21

### Changed
- **Module-focused deck layout** — When the selected deck item is **`module_runtime`**, the deck uses **`is-module-active`**: larger stage budget, **FROM THIS SOURCE** queue and detail/context default collapsed (**Show list** / **Show details** restore them). Non-module selection returns to the usual expanded layout. CSS in **`base.html`**; **`syncDeckLayoutMode`** / toggles in **`canopy-main.js`**.

### Fixed
- **Deck fails to open / disappears after queue rebuild** — **`reconcileDeckQueueItemsBuilt`** no longer uses **`!item.key`** (drops valid keys only when **`undefined` / `null` / ''**). If a rebuild would yield an empty list while the prior queue had items, the previous list is reused. **`buildSourceWidgetList`** / union paths are wrapped in **`try/catch`** so malformed DOM cannot abort deck open.
- **Deck queue loses module after switching to YouTube (mixed source)** — Docked media breaks **`sourceContainer`**, so **`renderDeckQueue`** could rebuild with only the active clip. Mitigations: **`deckOriginSourceEl`** + **`deckOriginMessageId` / `deckOriginPostId`**; **`deckItemSourceEl`** skips sidebar hosts; **`widgetManifestFromDeckNode`** + module-card discovery in **`buildSourceWidgetList`**; **`reconcileDeckQueueItemsBuilt`** keeps prior widget rows when DOM nodes still belong to the pinned message/post (**`widgetDeckOriginContainsEl`**) and backfills from **`buildSourceWidgetList(origin)`**; **`mergeDeckWidgetUnionIntoDeckItems`** on **`openMediaDeckForSource`** and full-item **`openMiniPlayerForSource`** so Deck / mini / Open module share the same widget union; explicit widget merge in **`renderDeckQueue`** uses **`widgetDeckOriginContainsEl`** instead of **`deckItemSourceEl === sourceEl`**.

### Tests
- **`tests/test_frontend_regressions.py`** — **`is-module-active`** layout CSS and **`syncDeckLayoutMode`** toggle string; deck queue / **`deckItemKeyUsable`** / **`reconcileDeckQueueItemsBuilt`** substrings as applicable.

## [0.4.126] - 2026-02-27

### Fixed
- **Module card “Open module” silent failure / wrong deck item** — Module attachment roots now carry **`data-canopy-module-bundle-id`** (and name) whenever a file id is known. **`openMediaDeckForManifestNode(this)`** resolves that host and, if inline JSON fails to parse/sanitize, rebuilds the manifest from the bundle id so the deck opens on the **module** (not only when a YouTube embed is also present). User-visible **`showAlert`** when metadata is still unusable.
- **“Metadata incomplete or invalid” follow-up** — **`sanitizeDeckModuleBundleUrl`** accepts percent-encoded same-origin **`/files/<id>`** paths (no traversal). **`normalizeDeckModuleRuntime`** uses trim-only **`bundle_file_id`** and **`encodeURIComponent`** on the fallback **`/files/…`** path. Channel/feed/DM **`file_id`** resolution includes **`origin_file_id`**. **`extractDeckModuleBundleFileIdFromManifestAttr`** scrapes **`bundle_file_id` / `bundle_url`** from a broken manifest attribute when needed.
- **Open module matched wrong DOM node** — Module cards use **`data-canopy-module-card="1"`**. **`resolveCanopyModuleDeckManifestHost`** prefers that root so **`closest('[data-canopy-widget-manifest]')` no longer stops on an unrelated ancestor** (e.g. empty/broken manifest). **`extractCanopyModuleBundleFileIdFromHost`** reads the file id from same-origin **`/files/…`** links on the card (e.g. **Download**) when data attributes fail. Clear alert if the source message/post container cannot be resolved.

### Documentation
- **`docs/CANOPY_MODULE_RUNTIME_V1.md`** — Web UI deck-open path: data attributes and JS helpers used by channels, feed, and DMs.

### Tests
- **`tests/test_frontend_regressions.py`** — `resolveCanopyModuleDeckManifestHost`, `extractCanopyModuleBundleFileIdFromHost`, `data-canopy-module-card`, bundle attrs, `openMediaDeckForManifestNode(this)` wiring.

## [0.4.125] - 2026-02-27

### Fixed
- **Canopy Module deck queue on Open module** — When opening the deck from a module card, the clicked module is now passed as an **`explicitItem`** (`buildDeckWidgetItem` + **`mergeExplicitDeckItem`**) into **`openMediaDeckForSource`**, so the module row cannot disappear if DOM recollection omits it. **`renderDeckQueue`** re-merges the **selected non-media widget** the same way on the first queue rebuild so the queue does not collapse to media-only items.

### Tests
- **`tests/test_frontend_regressions.py`** — Substrings for `buildDeckWidgetItem`, `mergeExplicitDeckItem`, `explicitItem` / `explicitSelectedWidget`, and `state.deckItems = mergeExplicitDeckItem(`.

## [0.4.124] - 2026-02-27

### Fixed
- **Canopy Module cross-peer deck queue** — Module manifests now set **`bundle_url`** to the local **`/files/<id>`** path (channels JS, feed + DM Jinja) instead of reusing **`attachment.url`**, which could be an absolute remote URL and fail **`sanitizeDeckModuleBundleUrl`**. **`normalizeDeckModuleRuntime`** falls back to **`/files/<bundle_file_id>`** when the primary URL sanitizes to empty so older manifests can recover.

### Tests
- **`tests/test_frontend_regressions.py`** — Strings for `primaryBundleUrl` / fallback `bundleUrl`, channels `encodeURIComponent` bundle path, Jinja `bundle_url` bindings.

## [0.4.123] - 2026-02-27

### Added
- **`openMediaDeckForManifestNode(node)`** — Resolves a widget manifest node, finds its source, opens the media deck with **`preferredKey`** set to that manifest’s **`key`** so the correct module item is selected (not a generic source default).

### Changed
- **Module attachment cards** (channels, feed, DMs) — Primary **Open module** button on the card; optional **Download** when a file URL exists. Removes passive “use the deck launcher” style copy so single-module posts have a direct affordance. Source-level **Deck | Mini** launcher unchanged for mixed-media sources.

### Tests
- **`tests/test_frontend_regressions.py`** — Assertions for `openMediaDeckForManifestNode`, `window` export, and template `onclick` wiring across channels, feed, and DM macros.

## [0.4.122] - 2026-02-27

### Added
- **Canopy Module bundle validation** — Filenames ending in `.canopy-module.html` / `.canopy-module.htm` use `_validate_canopy_module_bundle()`: UTF-8 HTML document, **300 KiB** max, inline script allowed; blocks external scripts, inline event handlers, CSP override meta, embedded browsing tags, and non–self-contained resource URLs (`data:` / `blob:` / `#` only).
- **Module-aware MIME inference** — Generic uploads (`application/octet-stream`, etc.) still normalize to `text/html` when the filename extension implies HTML, so agents can upload modules without spoofing types.
- **Module preview semantics** — `build_file_preview()` returns `previewable: false`, `kind: "module"` for module bundles; `is_text_previewable()` excludes them.
- **Module bundle coverage** — Generic `Canopy Module` fixtures are covered in validation and manual deck-check workflows without shipping a local showcase bundle in the public repo.
- **Documentation** — `docs/CANOPY_MODULE_RUNTIME_V1.md` and cross-links in README / API / agent docs.

### Changed
- **Channel / feed / DM templates** — Module attachments emit `module_surface` / `module_runtime` deck manifests instead of behaving like plain HTML files.
- **Frontend** — `canopyIsModuleBundle()` excludes module bundles from generic text preview eligibility (`canopy-main.js`).

### Tests
- **`tests/test_spreadsheet_preview_support.py`** — Module accept/reject and preview behavior.
- **`tests/test_frontend_regressions.py`** — Module preview helpers and runtime/template assertions.

## [0.4.121] - 2026-02-27

### Changed
- **Version bump** — release-candidate alignment for current local testing; no functional runtime change in this commit.

### Documentation
- **Agent note (local only)** — `agent_note/AGENT_NOTE_CANOPY_MODULE_RUNTIME_V1_SCAFFOLD_2026-03-20.md` revised: repo-relative paths, explicit capability allowlist and gating (`context.get` vs broker methods), `CanopyModule` API, CSP/sandbox notes, link to `docs/CANOPY_MODULE_RUNTIME_V1.md`.

## [0.4.120] - 2026-03-20

### Improved
- **Deck transport controls always visible** — Seek bar and primary deck actions (**Prev / Play / Next**, collapse, mini bar, **Return**) live in a pinned **footer** below the scroll region. Only the stage, horizontal queue, and detail blocks (title, station summary, widget actions) scroll, so you no longer scroll past content to reach controls.

## [0.4.119] - 2026-03-20

### Fixed
- **Deck scroll reachability** — On desktop/tablet, the deck shell grid uses `minmax(0, 1fr)` for the main body row and `.sidebar-media-deck-body` scrolls (`min-height: 0`, `overflow-y: auto`, stable scrollbar gutter) so controls below a tall stage/queue remain reachable.

### Improved
- **Station surface dedup** — `renderDeckStationSummary` skips the station summary for **simple reference** widgets (maps/charts: `reference_surface`, source-scoped, view-only, no human gate) while preserving the summary for streams, telemetry, station scope, low-risk actions, and gated flows.

## [0.4.118] - 2026-03-20

### Changed
- Version bump (local testing / mesh verification).

## [0.4.117] - 2026-03-20

### Added
- **Widget manifest v1 contract** — Sanitized deck manifests now always include **`station_surface`** (kind, domain, label, summary, recurring, scope), **`action_policy`** (`max_risk`, `human_gate`, `audit_label`, bounded flag), and **`source_binding`** (`return_label` drives deck **Return** copy). Defaults apply when older embed producers omit these fields.
- **Bounded action model** — Per-action **`risk`** (`view` \| `low`) and **`scope`** (`source` \| `station`); `canRunDeckWidgetAction` enforces policy at runtime; optional **`requires_confirmation`** before run.
- **Station surface UI** — Deck shows a **Station Surface** summary (policy pill + badges for domain, recurring/source-bound, scope, risk tier, human gate) when a widget with `station_surface` is selected.
- **Stream cards** — Channel stream/telemetry cards emit the full manifest (explicit `station_surface`, `action_policy`, `source_binding`, and per-action risk/scope).

### Documentation
- **[docs/CANOPY_DECK_WIDGET_MANIFEST_V1.md](docs/CANOPY_DECK_WIDGET_MANIFEST_V1.md)** — Integrator reference for manifest v1, enums, and non-goals.

## [0.4.116] - 2026-03-20

### Improved
- **Deck / mini launcher on posts** — Single fused **Deck | Mini** segmented control (one pill, subtle divider, count badges per side) instead of two separate buttons; deck-only sources hide the Mini segment.
- **Deck panel copy** — Item total appears once in the header chip (`Canopy Deck · N items`); queue header no longer repeats the count; meta line shows **Item k of N** when there are multiple entries instead of duplicating totals.

## [0.4.115] - 2026-03-20

### Fixed
- **Deck widget iframe flash** — Map and other deck widgets no longer rebuild their iframe on every mini-player tick (~700ms); the stage is reused when the embed signature is unchanged.
- **Spotify deck embed** — Optional `intl-*/` path segment in Spotify URLs is recognized; Spotify (and SoundCloud) deck iframes omit the sandbox attribute so behavior matches in-feed embed previews.

## [0.4.114] - 2026-03-20

### Added
- **Canopy Deck widget foundation (phase 1 + bounded phase 2)** — Typed, sanitized widget manifests (`map`, `chart`, `media_embed`, `story`, `media_stream`, `telemetry_panel`) with allowlisted iframe hosts, external links, and callback actions (`open_stream_workspace`, clipboard copy, etc.). Rich embeds (Vimeo, Loom, Spotify, SoundCloud, Google Maps, OSM, TradingView, etc.) publish deck-safe manifests; channel stream cards expose `stream_summary` with workspace / copy-id actions. Deck UI supports widget summary, badges, details, and safe iframe or summary staging; mini player stays media-only; **Deck** launcher counts all deck items while **Mini player** counts playable media only.

### Fixed
- **Media deck on iOS** — Deck portal outside `.app-container`; touch/narrow `pointerdown` on deck launcher avoids first-tap-only YouTube materialize.

### Improved
- **Media deck vs mini player** — Deferred YouTube materialize for deck open; dual launchers; `forceDockMini` on minimize; facade-friendly mini chrome; iframe-resolved controls.
- **YouTube handoff** — Snapshot time/play across hosts; skip URL rewrite for mini↔deck reparents; restore path for post placeholder.
- **Deck resilience** — `repairMediaCurrentReference` uses full deck items (media + widgets) and leaves `state.current` null for widget-only repair; stage reconciliation; short-screen footer **Collapse** / **Mini bar**.

## [0.4.113] - 2026-03-20

### Improved
- **Media deck mobile** — Fullscreen-style surface on narrow portrait and short landscape; modal body scroll lock (`canopy-media-deck-modal`); sticky header and bottom controls with safe-area; visible Minimize/Close labels on touch; mini-player hidden while the deck is open; landscape compaction query scoped so short phones in landscape keep fullscreen instead of the floating tablet layout.
- **Source launcher and return** — Stale `returnUrl` / `dockedSubtitle` cleared when opening from a post or message; Return closes the deck with force-close so media restores to the source without handing off to the mini-player; mini-player **Show source** vs deck **Return to source** copy for clearer semantics.
- **Playback hardening** — YouTube auto-dock suppressed while the deck is open; global YouTube facade handler skips `defaultPrevented` clicks; deck selection key resyncs after facade→iframe materialization; redundant `keepDeckVisible` paths removed from `updateMini` (open deck already handled earlier).

### Tests
- Frontend regression coverage for mobile deck layout, source/return labels, first-click hardening, and related guards.

## [0.4.112] - 2026-03-19

### Improved
- **Media deck second pass** — Playback UI refreshes now coalesce through a shared scheduler, deck queue rerenders are skipped when membership and active item are unchanged, and media deactivation/cleanup paths are more centralized. Source-level deck launchers remain available on playable posts and messages.

## [0.4.111] - 2026-03-19

### Added
- **Expanded media deck and related queue** — The sidebar mini-player can now open into a larger floating media deck with a stage area, richer controls, seek support, PiP for supported video, and a related-media queue scoped to the same post or message.

### Improved
- **Mini-player continuity** — Off-screen audio, direct video, and YouTube playback now share one media state across the compact dock and expanded deck, including source return, minimize/close handling, and placeholder-preserving docking for larger playback.
- **Frontend coverage for media deck** — Regression assertions now cover the expanded deck markup, expand control, queue wiring, docking helpers, and seek behavior.

## [0.4.110] - 2026-03-19

### Hardened
- **Message replay prevention** — Inbound P2P messages older than 2 hours or timestamped more than 30 seconds in the future are rejected, preventing replay attacks after the seen-message cache evicts old IDs. Locally-created messages are exempt for store-and-forward compatibility.
- **Routing table size cap** — The routing table is capped at 500 entries. When full, the oldest entry is evicted to bound memory and limit the impact of stale or poisoned routes.
- **Relay offer validation** — Relay offers are only accepted from directly connected peers, preventing a relayed offer from creating a routing entry through an unreachable intermediary.
- **Generic error responses** — API, UI, and MCP error responses no longer include raw exception strings. Internal details (paths, SQL, stack frames) are replaced with safe generic messages while full context is preserved in server logs.

## [0.4.109] - 2026-03-19

### Hardened
- **Encryption helper robustness** — `DataEncryptor.encrypt()` and `decrypt()` handle `None` inputs gracefully. Large-payload warnings alert operators before performance-sensitive paths. Debug logging no longer includes raw metadata.

## [0.4.108] - 2026-03-19

### Hardened
- **Delete signal authorization** — Inbound P2P delete signals for channel messages now verify requester ownership (message author or channel admin). Revocation signals are prioritised in the store-and-forward queue to survive offline-peer overflow.

### Performance
- **Sidebar rendering efficiency** — DM contacts and peer list use DocumentFragment batching and render-key diffing to skip unnecessary DOM writes. Attention poll interval relaxed from 2.5s to 5s. GPU compositing hints added to animated sidebar elements.

## [0.4.107] - 2026-03-19

### Hardened
- **Trust boundary enforcement** — Delete-signal compliance and violation handlers verify signal ownership before adjusting trust scores. Manually penalised peers are locked from automated trust recovery. Trust score operations validate against non-existent records.
- **P2P input validation** — Inbound messages enforce payload size limits (512 KB total, 256 KB content, 512-byte IDs). Feed posts with private or custom visibility are rejected at the P2P layer. Author identity is verified against origin peer on inbound feed posts. Delete signal handlers verify requester ownership across all data types.
- **API authentication tightening** — All P2P status endpoints require authentication. Session-based API key generation validates CSRF tokens.
- **Feed visibility defaults** — `can_view()` defaults to untrusted, requiring callers to pass explicit trust context. `get_user_posts()` applies standard visibility filters. Feed statistics include custom-visibility posts the viewer has permission to see.

### Performance
- **Channel rendering** — O(n) orphan-reply check via Set lookup (previously O(n²)). `displayMessages` returns its Promise for proper search-banner chaining.

## [0.4.106] - 2026-03-18

### Changed
- **Privacy-first trust baseline** — Unknown peers now start at trust score 0 (pending review) instead of 100 (implicitly trusted). `is_peer_trusted()` requires an explicit trust row before a peer qualifies. The Trust UI now separates connected-but-unreviewed peers into a "Potential peers" queue rather than placing them into trust tiers by default.
- **Feed defaults to private** — Feed post creation defaults to `private` ("Only Me") across UI, API, and MCP. Agents and users that omit visibility no longer broadcast unintentionally. Helper text in the feed composer clarifies the default and explains trusted sharing.
- **Trusted feed visibility consistency** — All feed query paths (`get_user_feed`, `search_posts`, `count_unread_posts`, `get_feed_statistics`, `_get_smart_feed`, `get_posts_since`) now include `trusted` visibility so trusted posts are no longer inconsistently omitted.
- **Targeted feed propagation** — `broadcast_feed_post()` now computes target peers by visibility scope: public/network → all connected, trusted → only peers meeting the trust threshold, private/custom → no P2P broadcast. Catch-up sync includes trusted posts only for explicitly trusted peers.
- **Feed visibility narrowing revocation** — When a post is edited from a broader to a narrower visibility, peers that are no longer in scope receive a delete signal. Update call sites in UI, API, and MCP now pass `previous_visibility` so revocation logic can run.
- **Operator copy clarity** — Settings advise using a separate node for public relay. Channel privacy descriptions clarify that Guarded is moderated/mesh-visible (not private) and Private is for sensitive work.

## [0.4.105] - 2026-03-18

### Fixed
- **DM search stability** — The DM workspace now uses an explicit `isDmSearchActive()` helper that consistently suspends all live-refresh paths (event polling, snapshot resync, visibility-change refresh, manual Refresh button) while a search query is active. The Refresh button reloads the current search page instead of silently reverting to the live thread.
- **Channel search stability** — Background thread refresh no longer overwrites channel search results. A new `rerunActiveChannelSearch()` path keeps search results coherent after local actions (delete, edit, stream create, note publish/rate, skill endorse) without reverting to the live thread. Search results scroll to the newest matches on initial search; reruns after local actions preserve scroll position.

### Improved
- **Left-rail card labels** — Card mode labels are hidden when collapsed (the chevron already indicates the state) and tightened to prevent overlap with count badges on narrow sidebars.

## [0.4.103] - 2026-03-18

### Improved
- **Bell seen vs clear separation** — Opening the bell now clears the red badge without removing entries from the dropdown. A new `seenThrough` localStorage watermark tracks which items the user has already glanced at, while the existing `dismissedThrough` cursor still controls list removal via the Clear button. Badge count reflects only items newer than the seen cursor. Both cursors stay coherent (Clear advances both).

## [0.4.102] - 2026-03-18

### Improved
- **Left-rail card states** — Recent DMs and Connected cards now support three persistent viewing states: collapsed, top 5 (peek), and expanded (bounded scroll). State persists per user in localStorage. Header toggle collapses/expands; footer toggle switches between peek and full list. DM unread total now reflects all contacts, not just the visible slice.
- **Mini-player placement** — The sidebar mini-player can now be moved between a top and bottom slot. Placement persists per user in localStorage. Defaults to the top utility slot.

## [0.4.101] - 2026-03-18

### Fixed
- **Channel read clears attention immediately** — Opening a channel now triggers an immediate sidebar and bell attention refresh when unread state is cleared, instead of waiting for the next poll cycle. `mark_channel_read()` returns whether it actually cleared unread state, the AJAX response exposes `marked_read`, and the channel view calls `requestCanopySidebarAttentionRefresh({ force: true })` on a positive transition.

## [0.4.100] - 2026-03-17

### Added
- **First-run guidance** - New users now see a compact "First-day guide" card on Channels, Feed, and Messages pages showing current workspace stats (messages sent, feed posts, peers online, API keys) and four practical first-day steps. The guide is dismissible per-page via localStorage and automatically hides once core actions are completed.
- **Smarter first-run landing** - The `/` route now detects first-run users and redirects them to `#general` instead of dropping mobile users into an empty feed. Once the user has sent messages, posted, and seen a peer, the normal mobile→Feed / desktop→Channels default resumes.

## [0.4.99] - 2026-03-17

### Improved
- **Bell dismiss stability** - Clear now records a per-user dismissal watermark (workspace-event cursor) in `localStorage`. Dismissed items stay hidden across snapshot refreshes; only newer attention events reappear. Unread badges remain independent and unaffected.
- **Bell type filters** - The bell dropdown now includes persistent per-user filter chips (Mentions, Inbox, DMs, Channels, Feed) stored in `localStorage`. Filters apply only to the bell surface and do not alter event generation, sidebar unread counts, or presence.

## [0.4.98] - 2026-03-16

### Fixed
- **Bell avatar restoration** - The attention bell now renders user avatar images when available, falling back to an initial letter, then to the semantic icon. The v0.4.97 bell redesign had regressed to always showing generic icons. Avatar metadata is resolved from the profile manager and DB `avatar_file_id`.

## [0.4.97] - 2026-03-16

### Added
- **Unified attention event model** - Feed activity (`feed.post.created`, `feed.post.updated`, `feed.post.deleted`) now emits workspace events, joining channels, DMs, mentions, and inbox in a single event journal. Feed comments emit `feed.post.updated` with `update_reason=comment`.
- **Sidebar attention snapshot endpoint** - New `GET /ajax/sidebar_attention_snapshot` returns unread summary (messages, channels, feed, total), recent bell items from workspace events, stable revisions for delta polling, and a workspace event cursor.
- **Bell redesign** - The notification bell is now a workspace attention menu showing recent mentions, DMs, channel messages, feed activity, and channel state changes. Self-authored activity is filtered out. Peer presence remains on its own separate surface.

### Improved
- **Single browser event loop** - Left-sidebar unread badges, compact DM sidebar, and bell menu all refresh from one unified workspace-event poll loop instead of multiple independent polling models.

## [0.4.96] - 2026-03-16

### Fixed
- **YouTube click-to-play facade** - YouTube embeds now show a thumbnail with a play button overlay instead of loading the iframe immediately. The iframe is only injected when the user clicks, eliminating bulk embed requests that trigger YouTube's "sign in to prove you're not a bot" rate-limiting. Thumbnails load from `img.youtube.com` which is not rate-limited. The mini player integration is preserved via the MutationObserver that detects the iframe insertion on click.

## [0.4.95] - 2026-03-16

### Improved
- **Channel header responsive compaction** - Header controls now wrap cleanly at intermediate widths (768px–1199px) instead of overlapping. Low-height landscape mode (<=520px) gets a dense single-row header with hidden low-value labels and compact composer controls.
- **Shortened policy/privacy labels** - Posting labels shortened to `Curated`/`Open` (from `Posting: Curated`/`Posting: Open`); privacy labels shortened similarly. Reduces header pressure at all widths.
- **Open posting badge** - Open channels now show an explicit subdued badge alongside the curated accent badge, reducing layout ambiguity.

## [0.4.94] - 2026-03-16

### Fixed
- **Public channel sync preserves curated metadata** - `get_all_public_channels()` now includes `post_policy`, `allow_member_replies`, and `allowed_poster_user_ids` in the sync payload. Previously, public channel sync serialized curated channels with default `open` policy, causing the next sync pass to clobber curated state back to open on all peers. This was the root cause of the live `#curation-lab` revert observed on v0.4.93.

## [0.4.93] - 2026-03-16

### Fixed
- **Curated channel policy authority enforcement** - Remote posting-policy snapshots (from channel announces and piggybacked message metadata) are now authority-gated: only the origin peer can update a channel's `post_policy`, `allow_member_replies`, and allowlist. Stale snapshots from non-origin peers are rejected with audit logging. This fixes a split-brain where a non-origin peer rebroadcasting `open + empty allowlist` could clobber curated state on all receiving peers, including the origin.
- **Centralized low-level sync** - Posting-policy sync logic extracted into `_sync_channel_post_permissions_conn()` and `_normalize_allowed_poster_ids()`, keeping authority checks in `apply_remote_channel_posting_snapshot()` while trusted local paths continue using the direct `sync_channel_post_permissions()`.

## [0.4.92] - 2026-03-16

### Added
- **Inbound P2P curated enforcement** - Receiving peers now check curated posting policy before inserting synced channel messages. Unauthorized top-level posts in curated channels are rejected on receive; replies remain open when configured. Rejected message IDs are marked as processed to prevent re-loop via catch-up/replay.
- **Opportunistic curated metadata convergence** - Normal channel message broadcasts now piggyback `post_policy`, `allow_member_replies`, and `allowed_poster_user_ids` in their metadata, so receiving peers converge on curated state even when they miss a dedicated channel announce.
- **Duplicate message policy healing** - Even already-processed duplicate messages now apply their curated metadata snapshot, so replayed traffic can heal stale posting policy on receiving peers.
- **Relaxed allowlist sync** - `sync_channel_post_permissions` no longer requires pre-existing channel membership to persist allowlist entries; it only requires the referenced user to exist in `users`, matching the actual FK schema.

### Fixed
- **Channel adoption INSERT column count** - The `merge_or_adopt_channel` adoption path now includes `post_policy` and `allow_member_replies` in its INSERT, fixing a column/value count mismatch that caused adoption failures.

## [0.4.91] - 2026-03-16

### Added
- **Curated channel posting policy** - Channels now support a server-enforced `post_policy` field (`open` or `curated`). In curated channels, only admins and explicitly approved posters can start new top-level posts, while replies remain open to all members by default. Policy is enforced in the channel manager send path, session UI routes, and REST API routes.
- **Curated poster allowlist management** - New API endpoints (`/api/v1/channels/<id>/post-policy`, `/api/v1/channels/<id>/posters`) and session AJAX routes (`/ajax/update_channel_post_policy`, `/ajax/channel_posters/<id>`) allow admins to toggle posting policy, grant, and revoke top-level posting permission for individual members.
- **Curated channel creation** - The create-channel form and API now accept `post_policy` and `allow_member_replies` at creation time, so channels can start curated without a two-step reconfigure.
- **Curated metadata P2P sync** - Channel announce payloads now include `post_policy`, `allow_member_replies`, and `allowed_poster_user_ids`, ensuring curated channels stay consistent across peers during sync-create, merge/adopt, and member-add broadcasts.
- **Curated channel UI controls** - Channel header shows a posting-policy dropdown (open/curated) with summary, a curated badge, and a composer gate for non-approved members. The members modal displays per-member badges (admin, approved poster, replies only) and grant/revoke buttons.

## [0.4.90] - 2026-03-16

### Added
- **Sidebar unread badges** - Left-rail navigation items for Messages, Channels, and Social Feed now show aggregate unread counts as compact pill badges that update via periodic polling and on window focus. Zero-state badges are hidden; counts cap visually at `99+`.
- **Durable feed-view acknowledgement** - Opening the Social Feed records a per-user acknowledgement timestamp so the feed unread badge reflects genuinely new activity since the last visit. Own-authored posts are excluded from the unread count.
- **Notification deep-link to exact messages** - Bell notification clicks for channel messages now navigate to the exact target message via a server-side focused context window, even when the message is older than the recent page. DM bell clicks include a `#message-<id>` anchor for exact-message scrolling.
- **Container-aware focus scrolling** - Channel message focus now uses measured offsets within `#messages-container` instead of `scrollIntoView()`, and retries shortly after render to absorb layout shifts from async hydration.

### Fixed
- **Bell duplicate counting for mention-bearing messages** - The notification bell now deduplicates by semantic activity key so a `channel_message` event and a `mention` event for the same source message increment the unread badge only once, with the higher-priority event winning the display slot.
- **Normal WebSocket close logged as error** - Send failures caused by a normal `1000 (OK)` close are now logged at debug level instead of error, reducing misleading noise in the terminal after mesh reconnect cycles.

## [0.4.89] - 2026-03-15

### Added
- **Inline map and chart embeds** - OpenStreetMap links that include coordinates now render as true inline map iframes, and TradingView symbol links now render as inline chart widgets instead of remaining provider cards.

### Fixed
- **Google Maps query-link detection** - Google Maps provider matching now catches query-form URLs such as `https://www.google.com/maps?q=Toronto`, so shared map links no longer silently miss the embed pipeline.
- **Google Maps restricted-key embeds** - Inline Google Maps embeds now send the referrer policy expected by browser-restricted Maps Embed API keys, so configured `CANOPY_GOOGLE_MAPS_EMBED_API_KEY` deployments can actually render the official iframe instead of failing authorization at load time.

## [0.4.88] - 2026-03-15

### Added
- **Shared rich embed provider expansion** - Expanded the common rich embed renderer across post and message surfaces to support Vimeo, Loom, Spotify, SoundCloud, direct audio/video URLs, and safe provider cards for map and TradingView links while keeping the embed surface bounded away from arbitrary raw iframe HTML.

### Fixed
- **Inline math dollar-sign hardening** - KaTeX inline dollar parsing now requires the content between `$...$` delimiters to actually look mathematical, reducing accidental formatting damage in finance-style posts that contain multiple currency values.
- **Embed detection inside generated markup** - Provider URL expansion now skips matches that are already inside generated HTML tags or attributes, preventing supported-provider URLs inside ordinary markdown links from being rewritten into broken embed markup.

## [0.4.87] - 2026-03-15

### Fixed
- **Cross-peer stream card truth and camera teardown** - Channel message snapshots now reconcile stream-card statuses against current local or remote stream state so remote viewers stop seeing stale `Preparing` badges after a stream is live, stream lifecycle changes sync the stored stream attachment metadata for local invalidation, and the browser broadcaster now tears down temporary device-enumeration and preview capture streams so stopping or closing the panel actually releases the camera.

## [0.4.86] - 2026-03-15

### Fixed
- **Truthful stream lifecycle controls** - `start_now` stream cards are now posted after the stream actually transitions live, browser broadcaster start/stop actions update the real stream lifecycle endpoints, and owner-facing stream cards/workspaces expose a proper stop action with status chips that reconcile against the current stream row instead of stale attachment metadata.

## [0.4.85] - 2026-03-15

### Fixed
- **Streaming playback rate-limit carve-out** - HLS manifests, stream segments, telemetry event playback, and local stream-proxy reads now use a dedicated high-ceiling playback limiter instead of the generic `/api/` throttle, preventing valid live stream sessions from immediately failing with `429` responses under normal player polling.

## [0.4.84] - 2026-03-15

### Added
- **Streaming runtime readiness and token refresh surfaces** - Added real `STREAM_MANAGER` bootstrap wiring, stream runtime health endpoints for API/UI callers, and a first-class stream token refresh path so longer live sessions can renew access without ad-hoc reissue flows.

### Changed
- **Streaming operator UX and metadata structure** - Stream creation now uses a structured modal/profile flow, stream cards open into a reusable workspace shell, and stream attachments carry richer UI metadata such as `stream_domain`, `operator_profile`, and `viewer_layout`.
- **Truthful media stream defaults** - Newly created media streams now default `metadata.latency_mode` to `hls`, matching the currently implemented transport instead of overstating LL-HLS support.

### Fixed
- **Actionable ingest diagnostics** - Empty manifest or segment uploads now return `empty_ingest_payload` with a proxy/upload hint instead of a generic size error.
- **Remote stream proxy hot-path churn** - Remote stream proxy origin resolution now uses a short-lived cache with shorter probe/fetch timeouts to avoid repeated synchronous rescans on hot requests.

## [0.4.83] - 2026-03-14

### Fixed
- **Active channel thread refresh parity** - The Channels UI now refreshes the currently open thread when the sidebar receives a new-message event for that same channel, preventing cases where the unread bell increments but the visible thread stays stale until a manual reload.
- **Plain-text structured composer tolerance** - Structured block validation now ignores unknown bracketed section headers when they are not recognized Canopy tool aliases, so pasted `.ini` and similar config text can still be posted as plain text.
- **Inbound inline attachment ID remapping** - Incoming peer-synced channel messages now rewrite both `/files/FILE_ID` and `file:FILE_ID` references to locally materialized attachment IDs so inline uploaded images keep rendering after cross-peer attachment normalization.

## [0.4.82] - 2026-03-13

### Fixed
- **Active channel live-update recovery** - Channel thread polling now falls back to direct snapshot refresh more aggressively when workspace-event polling misses or fails, reducing cases where an already-open channel stops showing newly arrived messages on a peer.
- **Channel-scoped workspace event visibility** - Workspace event visibility checks now explicitly allow channel-scoped events for actual channel members with message-read permission instead of relying on only per-user fanout semantics.

## [0.4.81] - 2026-03-13

### Added
- **Inline uploaded-image anchors** - Rich content now supports `![caption](file:FILE_ID)` so uploaded Canopy images can appear directly inside post and message body copy while still using the local file service and lightbox viewer.

### Changed
- **Responsive attachment gallery hints** - Channel, feed, and DM image attachments now honor validated `layout_hint` values (`grid`, `hero`, `strip`, `stack`) with a shared mobile-first gallery renderer across surfaces.

## [0.4.80] - 2026-03-13

### Changed
- **Actionable inbox queue semantics** - Agent inbox list/count paths, system-health queues, and discovery/runtime summaries now continue treating `seen` items as actionable work until they are actually completed, skipped, or expired.
- **Docs and release alignment refresh** - README pointers, operator quick starts, and the current release notes now reflect the combined `0.4.80` development surface instead of a split `0.4.78`/`0.4.79` state.

### Fixed
- **Inbox reopen audit preservation** - Reopened inbox items now clear live completion fields without discarding the last terminal resolution status, timestamp, or evidence payload, so operators can reopen work without losing audit context.
- **Quiet-feed and message-authorization hardening** - Explicitly empty workspace-event subscriptions now stay empty, and message-bearing channel event families remain hidden from keys that do not have `READ_MESSAGES`.

## [0.4.79] - 2026-03-12

### Added
- **Durable agent event subscriptions** - Added stored per-agent event family preferences plus `GET/POST /api/v1/agents/me/event-subscriptions`, so long-running agents can keep a low-noise wake feed without resending `types=` on every poll.

### Changed
- **Agent heartbeat and admin runtime subscription diagnostics** - Heartbeat and admin workspace runtime now expose the active or stored event-subscription view so operators can see whether an agent is running the default feed, a custom feed, or an intentionally quiet one.

### Fixed
- **Agent event authorization and subscription drift hardening** - Message-bearing channel event families remain permission-filtered, explicit empty subscriptions now stay empty, and heartbeat now preserves non-message custom event families instead of silently dropping them from the reported active feed.

## [0.4.78] - 2026-03-12

### Changed
- **Concurrent group-DM broadcast fan-out** - Broadcast mesh sends now start peer deliveries concurrently so one slow or dead peer no longer stalls later peers during group DM attachment propagation.

### Fixed
- **Non-blocking DM attachment fan-out scheduling** - Direct-message broadcast scheduling no longer blocks the request thread waiting on slow mesh delivery completion, while completion and failure outcomes remain logged asynchronously.

## [0.4.77] - 2026-03-11

### Added
- **Agent-focused workspace event feed** - Added `GET /api/v1/agents/me/events`, a low-noise actionable workspace event route for agent runtimes that defaults to DM, mention, inbox, and DM-scoped attachment events while preserving explicit type overrides.

### Fixed
- **Agent presence scoping for agent events** - Agent-facing event polling now records presence and runtime telemetry only for real agent accounts, preventing human API keys from showing up as agent activity through the new endpoint.

## [0.4.76] - 2026-03-11

### Changed
- **Unified workspace event journal Patch 8 for channel metadata refresh** - Channel thread refreshes now queue cleanly behind in-flight loads/snapshots, local message-adjacent actions use the journal-first thread refresh path, and channel-message community-note updates emit journal-visible metadata refresh events.

### Fixed
- **Channel search refresh fallback preservation** - Search-mode channel actions now preserve the established full thread reload fallback, preventing stale search results after local edit, delete, or community-note updates.

## [0.4.75] - 2026-03-11

### Changed
- **Unified workspace event journal Patch 7 for incremental channel state updates** - The Channels UI now applies common `channel.state.updated` changes in place for lifecycle, privacy, notifications, member-count, and deletion paths instead of forcing a sidebar snapshot refresh for every state change.

### Fixed
- **Channel thread event cursor isolation** - The active channel-thread consumer now keeps its own workspace-event cursor instead of borrowing the sidebar cursor, preventing unseen message edit/delete events from being skipped when unrelated sidebar state events advance first.
- **Channel message snapshot cursor hardening** - `/ajax/channel_messages/<channel_id>` now captures its workspace-event cursor before building the message snapshot response so the thread consumer does not advance past unseen changes during concurrent activity.

## [0.4.74] - 2026-03-11

### Changed
- **Docs and version alignment refresh** - README release pointers, operator guides, and agent-facing setup docs now reflect the current `0.4.74` development surface instead of older release snapshots.

### Fixed
- **Request member write-path hardening** - Request upsert/update paths now replace members inside the active write transaction, preventing SQLite self-locks that could silently drop request assignees or reviewers while standalone member replacement keeps its retry/backoff behavior.
- **Authenticated system info trust wiring** - `/api/v1/info` now reads the trust manager from the correct app-component slot so authenticated callers receive trust statistics instead of an internal error.

## [0.4.73] - 2026-03-11

### Changed
- **Inbox completion linkage review pass** - Agent inbox completion semantics now better match the Admin discrepancy view and the updated agent contract.

### Fixed
- **Skipped inbox evidence persistence** - Inbox items marked `skipped` can now retain `completion_ref` evidence through the REST update path, so Admin discrepancy reporting no longer flags every newly skipped item as unverifiable by construction.

## [0.4.72] - 2026-03-11

### Changed
- **Unified workspace event journal Patch 5 for channel badges and agent runtime telemetry** - Channel sidebar unread badges now apply direct journal-driven deltas for common unread transitions, and the Admin Agent Workspace panel now surfaces durable runtime telemetry such as last event fetch, last cursor seen, last inbox fetch, and oldest pending inbox/mention ages.

### Fixed
- **Agent runtime telemetry scoping** - Agent runtime state is now recorded only for real agent accounts, preventing human API-key `/api/v1/events` usage from polluting the Admin runtime telemetry view.

## [0.4.71] - 2026-03-11

### Changed
- **Unified workspace event journal Patch 4 for the channel sidebar** - The Channels page sidebar now uses the local workspace event journal as its change detector and only refreshes from the existing sidebar snapshot route when channel-relevant events arrive, while preserving the established snapshot render path and safety refresh.

### Fixed
- **Initial channel sidebar cursor race hardening** - The `/channels` page now captures its workspace-event cursor before building the initial sidebar snapshot, preventing the channel sidebar consumer from advancing past unseen channel changes on first render during concurrent activity.

## [0.4.70] - 2026-03-11

### Changed
- **Unified workspace event journal Patch 3 for the recent-DM sidebar** - The shared recent-DM sidebar now follows the local workspace event journal through a dedicated compact snapshot path instead of piggybacking on the generic peer-activity poll, while still preserving queueing and a safety resync.

### Fixed
- **Sidebar snapshot/event cursor race hardening** - The recent-DM sidebar snapshot now captures its workspace-event cursor before rebuilding contact state, preventing the shared sidebar from advancing past journal changes that are not yet represented in the returned snapshot during concurrent DM activity.

## [0.4.69] - 2026-03-11

### Changed
- **Unified workspace event journal Patch 2 for DMs** - The DM workspace now uses the local workspace event journal as its live change detector, reducing idle full-snapshot churn while preserving the existing snapshot render path and safety resync behavior.

### Fixed
- **DM snapshot/event cursor race hardening** - DM snapshot responses now capture their workspace-event cursor before rebuilding sidebar and thread state, preventing the client from advancing past journal changes that are not yet included in the rendered snapshot during concurrent updates.

## [0.4.68] - 2026-03-10

### Added
- **Structured composer review pass and expanded structured feedback** - The shared structured-composer helper now recognizes additional canonical block families (`circle`, `contract`, and `skill`) while feed and channel post-send feedback can now report durable `contract` and `circle` materialization alongside the original coordination objects.

## [0.4.67] - 2026-03-10

### Added
- **Structured composer validation and materialization feedback** - Feed and channel composers now provide canonical structured block templates, pre-send validation for malformed or aliased blocks, inline normalization/fix actions, and post-send feedback showing which structured objects actually materialized.

## [0.4.66] - 2026-03-10

### Fixed
- **UI and identity follow-up hardening** — Remote profile sync now carries and applies `account_type`, local profile-card sync no longer depends on password-only account shapes, channel reply buttons no longer rely on fragile inline JavaScript interpolation, YouTube mini-player updates avoid eager startup reparenting, and identity/admin UI now treats `origin_peer == local_peer_id` as local instead of remote.

## [0.4.65] - 2026-03-10

### Added
- **Channel lifecycle controls (soft archive only)** — Channels now carry additive lifecycle metadata (`last_activity_at`, inactivity TTL, preserve flag, archive timestamp/reason) plus a new lifecycle update surface in the UI and REST API. Inactivity currently results in soft archive state only; this release does not introduce automatic hard deletion.

### Changed
- **Lifecycle-aware channel sync and sidebar state** — Channel announce/sync metadata now includes lifecycle policy and archive state, channel activity automatically revives archived channels, and the Channels UI now surfaces preserved/cooling/archived state in both the sidebar rows and header controls.

## [0.4.64] - 2026-03-09

### Fixed
- **Agent inbox follow-up delivery** — Agent recipients no longer drop legitimate rapid DM or reply follow-ups because of inbox cooldown checks. Agent inboxes now rely on their existing higher rate-limit ceilings instead of cooldown suppression, preventing missed work during active conversations.

## [0.4.63] - 2026-03-09

### Fixed
- **DM inbox reply routing for agents** — Agent-facing inbox items now surface stable DM reply metadata (`sender_user_id`, `dm_thread_id`, and `message_id`), and the API now provides `POST /api/v1/messages/reply` so DM-triggered agents can answer the originating DM by message ID instead of falling back to a channel target.

## [0.4.62] - 2026-03-09

### Changed
- **Second-pass UI polish across shared, DM, and channel surfaces** — Refined keyboard focus visibility, reduced-motion behavior, safe-area composer spacing, and scroll-region stability in `base.html`, `messages.html`, and `channels.html` without changing route contracts or page structure.

## [0.4.61] - 2026-03-09

### Added
- **Unified workspace event journal Patch 1** — Canopy now persists a local, additive `workspace_events` journal for DM create/edit/delete, mention create/acknowledge, inbox item create/update, and DM-scoped attachment availability. The journal is cursorable via `GET /api/v1/events` and exposed as an additive `workspace_event_seq` heartbeat field without changing the legacy `last_event_seq` contract.

### Changed
- **Workspace event diagnostics and admin visibility** — The workspace journal now includes richer diagnostics summaries and a dedicated admin diagnostics surface so operators can inspect recent event rows, event-type distribution, and the journal cursor state during local mesh testing without relying on raw API inspection.

## [0.4.60] - 2026-03-09

### Added
- **Managed large-attachment store v1** — Attachments larger than the fixed `10 MB` sync threshold now propagate across the mesh as metadata-first references instead of inline payload blobs. Nodes can store them under an admin-configured managed external root, track remote transfer state locally, and fetch them from the source peer in bounded chunks with checksum validation.

### Changed
- **Large-attachment download policy controls** — Settings now expose a node-wide `Automatic`, `Manual`, or `Paused` download mode for large remote attachments. `Automatic` is the default to preserve availability when peers are only online briefly, while `Manual` and `Paused` let low-disk operators keep metadata without background fetch.

### Fixed
- **Peer-side large-attachment authorization** — Source-peer visibility checks now correctly recognize metadata-first attachment identifiers (`origin_file_id`, `remote_file_id`) and open/public channel or feed references, so authorized peers can fetch large attachments without false denials while still remaining deny-by-default.

## [0.4.59] - 2026-03-09

### Fixed
- **DM remote-vs-local classification** — Ambiguous recipient rows with blank or stale `origin_peer` are no longer assumed to be local unless there is positive evidence that they belong to this instance. This prevents remote 1:1 and group DMs from being misclassified as `local_only`, preserves the correct DM security summary, and keeps mesh broadcast enabled for remote humans whose provenance metadata is incomplete.

---

## [0.4.58] - 2026-03-09

### Fixed
- **DM search and scroll layout fixes** — DM search now pages through relevant message history before filtering decrypted content and attachment metadata, so older encrypted-at-rest matches are no longer missed behind newer non-matching rows. The DM workspace layout also keeps the sidebar, thread, and composer in better-separated scrolling regions so the composer stays anchored more like a modern messaging client.

---

## [0.4.57] - 2026-03-09

### Fixed
- **Dead connection send-failure churn** — A send timeout or closed-socket error now retires the affected peer connection immediately before the asynchronous close finishes, so queued senders stop treating the socket as live and the terminal no longer floods with repeated `no close frame received` errors after the first dead-connection failure.

---

## [0.4.56] - 2026-03-09

### Changed
- **Mesh connectivity reliability hardening** — Reconnect now prefers current discovery-backed peer endpoints over stale persisted ones, while endpoint-level diagnostics retain source, attempt, and failure history so operators can see why a peer is not connectable instead of only seeing generic reconnect churn.

### Fixed
- **Discovery endpoint ownership and diagnostics accuracy** — Discovered endpoints are no longer claimed before a successful connection proves ownership, connected peers are classified correctly even when stale relay metadata lingers, reconnect scheduling only reports active tasks, and the diagnostics failure list now surfaces the newest failures first.

---

## [0.4.55] - 2026-03-08

### Changed
- **DM recipient search and incremental refresh** — The DM composer recipient picker now shows live suggestions on first interaction and reuses in-flight directory loading instead of presenting an empty state. The DM workspace also gained incremental thread snapshots for refresh, send, edit, delete, and active-thread polling so conversation updates no longer depend on disruptive full-page reloads.

---

## [0.4.54] - 2026-03-08

### Changed
- **DM attachment parity and image paste** — The DM composer now accepts the same broad Canopy-supported file set as channel compose, including additional document, archive, spreadsheet, and media formats, and pasted screenshots/images are converted into normal DM attachment entries so attachment-only and image-first workflows behave consistently.
- **DM UI polish and security indicator refinement** — DM security markers are now icon-first with tooltip and accessibility labels, the per-message action toggle is more compact and no longer shows the default caret, the header action cluster is tighter on narrow widths, and the empty-state card is centered for a more finished workspace presentation.
- **OpenClaw documentation pass** — Public docs now call out OpenClaw-style agent teams as a supported deployment pattern through Canopy's standard REST and MCP surfaces, without implying a Canopy-specific runtime fork or custom protocol.

---

## [0.4.53] - 2026-03-08

### Changed
- **DM E2E hardening and relay-safe transport** — Direct messages now use recipient-only peer encryption when the destination peer advertises `dm_e2e_v1`, while preserving plaintext fallback for older peers so mixed-version meshes stay stable during rollout. Introduced-peer capability data is propagated through peer announcements so relay paths can still decide whether the destination supports DM E2E.
- **DM security visibility** — The DM workspace now surfaces explicit shield states at both thread and message level so humans can tell whether a conversation is `peer_e2e_v1`, `local_only`, `mixed`, `legacy_plaintext`, or `decrypt_failed`.

### Fixed
- **DM inbox coverage for relayed and same-peer group recipients** — Incoming encrypted or relayed DMs now refresh local inbox triggers correctly, and group DMs reaching a peer with multiple local accounts create inbox rows for all relevant local recipients instead of only the primary target ID.

---

## [0.4.52] - 2026-03-08

### Changed
- **Sidebar recent DM contacts rail** — Added a shared left-sidebar `Recent DMs` card above the mini player with avatar recognition, unread counts, status dots, preview text, latest timestamps, and click-through links back into the relevant direct-message thread and target message anchor. The rail is hydrated on initial render and refreshed through the existing sidebar activity poller, while excluding group DMs to keep the compact contact list easy to scan.
- **Public CI and release polish** — Reworked the GitHub Actions workflow so the public repo validates the installable surface honestly via Python compile checks, Jinja template parsing, shipped JavaScript syntax/runtime checks, and a curated public-safe regression suite. Public docs and package metadata were also refreshed to align current version framing and release presentation with `0.4.52`.

---

## [0.4.51] - 2026-03-08

### Changed
- **Inline sheet responsive formatting** — Inline spreadsheet tables now use content-aware column layout heuristics (`buildColumnLayout`) that size numeric columns compactly and let text columns wrap intelligently, replacing the hard minimum-width approach. The same layout model applies to view, preview, and editor, with `colgroup`-based `ch`-unit widths and `table-layout: fixed` CSS for real responsiveness across desktop and mobile.

---

## [0.4.50] - 2026-03-08

### Fixed
- **DM dropdown stacking** — Per-message action menus (`Reply`, `Edit`, `Delete`) now elevate above neighboring message rows via explicit z-index promotion, preventing the dropdown from rendering behind adjacent cards.
- **Direction-aware DM dropdown** — The actions menu now flips to `dropup` when there is insufficient space below the message, avoiding collisions with the composer area on shorter threads and smaller viewports.
- **Inline edit layering** — Starting inline edit on a DM message promotes the row above its neighbours and removes the promotion on cancel, so the editor surface feels stable while editing.

---

## [0.4.49] - 2026-03-08

### Changed
- **Mobile DM layout pass** — Tightened tablet and phone breakpoints for the DM workspace, reduced sidebar/header/composer padding, hid low-value subtitle text on narrow screens, and added an extra narrow-phone breakpoint for compact avatar behaviour.

### Fixed
- **Relayed group DM thread identity** — Group DM threads that arrived through a relay under a different raw alias now resolve into the same logical conversation. The conversation rail, active thread selection, and message fetch all use canonical member-set identity (`compute_group_id`) instead of raw `group_id` equality, so the preview card and the thread pane always agree.
- **Backend group conversation lookup** — `get_group_conversation()` now reconciles alias group IDs by inspecting `group_members` metadata, so messages delivered under different raw aliases but with the same membership set are merged into a single thread response.

---

## [0.4.48] - 2026-03-07

### Changed
- **DM workspace redesign** — Rebuilt the Messages page into a conversation-first workspace with separate direct/group rails, grouped chat bubbles, day dividers, reply previews, integrated search results, and a single bottom composer that targets the active thread instead of a flat all-messages dump.

### Fixed
- **UI DM reply flow metadata** — The web composer now sends `reply_to` through the normal `/ajax/send_message` path, so inline DM replies keep their thread metadata whether they are 1:1 or group messages.
- **DM workspace unread-state refresh** — Opening the active DM thread now clears its unread badge in the same render pass after messages are marked read, avoiding stale unread counts in the new conversation rail.

---

## [0.4.47] - 2026-03-07

### Changed
- **Release visibility bump for current collaboration work** — Advanced the visible app/docs version after the spreadsheet collaboration, edited mention refresh, and DM agent-contract hardening work so the updated build is clearly distinguishable from the previous release.
- **DM workspace redesign** — Rebuilt the Messages page into a conversation-first workspace with separate direct/group rails, grouped chat bubbles, day dividers, reply previews, integrated search results, and a single bottom composer that targets the active thread instead of a flat all-messages dump.

### Fixed
- **UI DM reply flow metadata** — The web composer now sends `reply_to` through the normal `/ajax/send_message` path, so inline DM replies keep their thread metadata whether they are 1:1 or group messages.

---

## [0.4.46] - 2026-03-07

### Changed
- **Spreadsheet collaboration support** — Canopy now accepts modern spreadsheet attachments (`.csv`, `.tsv`, `.xlsx`, `.xlsm`), exposes bounded read-only preview JSON via `/files/<file_id>/preview`, renders spreadsheet previews inline across channels/feed/DMs, and supports compact inline computed `sheet` blocks for lightweight tabular calculations inside posts/messages.
- **Spreadsheet collaboration second pass** — Inline `sheet` blocks now run through a standalone safe evaluator module with broader business-style coverage (`ROUND`, `ABS`, `IF`, `AND`, `OR`, `NOT`, `MEDIAN`, `STDDEV`, comparisons, and text concatenation), spreadsheet attachment cards now advertise `Sheet` / `Macros disabled` status more clearly, and spreadsheet preview buttons use explicit `Open sheet` / `Hide sheet` wording.

### Fixed
- **Edited mention and inbox refresh** — Pending mention and inbox payloads now refresh in place when feed posts, channel messages, replies, and direct messages are edited, newly added local mention targets are created on edit, removed mentions are retained but marked stale, and incoming P2P DM edits rebuild or refresh recipient inbox items instead of leaving creation-time snapshots behind.
- **DM agent-contract hardening** — Group DMs now flow cleanly through the REST, UI, and MCP surfaces: group recipients are included in recent-message/search/read queries, DM edits fan out to every group member, local inbox rows are only created for real local accounts, DM delete propagation uses the correct direct-message signal, and agent catchup/inbox payloads now carry reply/group metadata consistently.

---

## [0.4.45] - 2026-03-07

### Changed
- **Documentation refresh for current Canopy surface** — Updated the README, quick start, agent onboarding, mentions guide, API reference, and Windows tray guide so the public docs now describe the current `0.4.45` behavior: canonical `/api/v1` plus legacy `/api` compatibility, current agent runtime loops, Windows tray distribution, thread reply inbox subscriptions, relay/connectivity diagnostics, and current operator-facing documentation map.
- **Spreadsheet collaboration surface** — Canopy now accepts modern spreadsheet attachments (`.csv`, `.tsv`, `.xlsx`, `.xlsm`), exposes bounded read-only preview JSON via `/files/<file_id>/preview`, renders spreadsheet previews inline across channels/feed/DMs, and supports compact inline computed `sheet` blocks for lightweight tabular calculations inside posts/messages.
- **Spreadsheet collaboration second pass** — Inline `sheet` blocks now run through a standalone safe evaluator module with broader business-style coverage (`ROUND`, `ABS`, `IF`, `AND`, `OR`, `NOT`, `MEDIAN`, `STDDEV`, comparisons, and text concatenation), spreadsheet attachment cards now advertise `Sheet`/`Macros disabled` status more clearly, and spreadsheet preview buttons use explicit `Open sheet` / `Hide sheet` wording.
- **Inline spreadsheet editor pass** — Inline `sheet` blocks now open into a branded local editor with add/remove row and column controls, live recalculation preview, copy/apply actions, and a save path that reuses the normal post/message editor rather than inventing a separate persistence flow.

### Fixed
- **Agent inbox endpoint compatibility and instruction drift** — Restored backward-compatible `/api` access alongside `/api/v1` for agent-facing inbox and message endpoints, added shorthand claim/ack aliases used by older agent clients, corrected stale MCP instruction examples to prefer `/api/v1`, and fixed the machine-readable agent instructions payload so mention claim/ack alias metadata is actually exposed instead of being overwritten by a duplicate `mentions` section.
- **Canopy tray compatibility and packaging drift** — Tray polling now prefers current peer endpoints, suppresses self-authored notifications, batches recent messages instead of only reading `limit=1`, deep-links notifications to the exact message, avoids minting placeholder tray keys before an owner account exists, and ships with refreshed Windows packaging/build documentation plus an installer path.

---

## [0.4.44] - 2026-03-07

### Fixed
- **Mesh connectivity durability and endpoint truth** — Invite imports now persist only canonical dialable endpoints, discovery preserves all advertised peer addresses, reconnect attempts try multiple discovered targets and keep retrying with capped backoff instead of entering a permanent cold state, inbound auth no longer poisons stored peer endpoints with guessed socket-origin addresses, and reconnect-time membership/key/delete repair traffic now uses the higher-volume sync rate budget. Review also caught and fixed two rollout blockers before bumping: invite endpoint sanitization now matches the note, and IPv6 discovery endpoints are rendered in a dialable bracketed form.

---

## [0.4.43] - 2026-03-05

### Fixed
- **Channel delete now works for non-origin replicas** — Node-level admins can now delete any channel replica from their local node, not just channels they originated. Previously the delete button silently failed with a 403 for channels created on other peers. The backend now passes `force=True` for node admins, bypassing the channel-admin membership check. P2P delete signals are only broadcast when the channel is locally originated; non-origin deletes are local-only replica removals. The delete button in the sidebar Tools dropdown is now visible for all channels (not just local-origin ones). Success message indicates when it's a local-only removal. Same fix applied to the REST API endpoint.

---

## [0.4.42] - 2026-03-05

### Improved
- **Channel sidebar polish** — Tools dropdown now shows compact icon-only buttons (clipboard + trash) in a horizontal pill instead of a text menu. Row action buttons (pin, tools) are always visible at soft opacity (45%), brightening to full on hover for better discoverability. Pinned channels show a persistent gold pin icon.

### Fixed
- **Tools dropdown visibility and flicker** — Removed Bootstrap auto-init (`data-bs-toggle`) and replaced with manual fixed-position dropdown using `getBoundingClientRect()`. Removed `transform: translateX(2px)` from channel row hover (CSS transforms create containing blocks that break `position: fixed` descendants). Added `tools-menu-open` class to lock row highlight while dropdown is open, eliminating hover flicker. Fixed `display: flex !important` override that was making all dropdown menus permanently visible.

---

## [0.4.41] - 2026-03-05

### Improved
- **Channel row compaction** — Reduced sidebar action clutter by keeping Pin as the only always-visible icon and moving Copy ID + Delete into a per-row overflow dropdown (three-dots "Tools" menu). Added proper channel name truncation with ellipsis for long names. Dropdown closes automatically after Copy/Delete actions. Applied to both server-rendered and dynamically generated rows.

### Fixed
- **Channel Tools dropdown not visible** — Dropdown menu was clipped by `overflow: hidden` on `#channel-sidebar` and `overflow-y: auto` on `.channel-list`. Fixed by using `position: fixed` for the dropdown menu and passing `popperConfig: { strategy: 'fixed' }` to Bootstrap's Dropdown constructor.

---

## [0.4.40] - 2026-03-05

### Improved
- **Channel row action tooltips + tap targets** — CSS-only hover tooltips (`Pin`, `Copy ID`, `Delete`) on channel sidebar action buttons using `data-action-label`, scoped to pointer/hover devices only. Increased tap target size on mobile. Copy ID and Delete actions moved from channel header to per-channel row for direct access. Delete modal now shows channel name and ID. Dynamic sidebar sync preserves quick-action state.

---

## [0.4.39] - 2026-03-05

### Added
- **Channel pinning** — Pin/unpin channels in the sidebar; pinned channels float to top with a gold border indicator. Per-user localStorage persistence, deterministic sort preserving relative order, pin state survives sidebar refresh/sync updates. Pin click uses `stopPropagation` to avoid accidental channel switching.
- **Optimistic like responsiveness** — Like/unlike across Channels, Feed, and Messages now updates instantly (optimistic UI). In-flight deduplication prevents double-clicks, button shows busy state during request, server response reconciles final state, errors roll back to prior state.

---

## [0.4.38] - 2026-03-05

### Improved
- **UI responsiveness second pass** — Comprehensive responsive audit across all major templates:
  - `base.html`: Tighter mobile navbar spacing, extra compact breakpoint at 420px, brand text hidden on very small screens.
  - `channels.html`: Tighter controls at 430px, reduced composer footprint, responsive audio player (removed forced min-width).
  - `connect.html`: Responsive peer rows, input groups, action clusters; reduced scrolling pressure on mobile.
  - `messages.html`: Responsive header, action bars, attachment rows, avatar sizing; breakpoint-specific adjustments.
  - `feed.html`: Structured responsive hooks for page header, composer, feed controls, algorithm panel; breakpoints at 768px, 576px, 430px.
  - `admin.html`: Responsive page header, summary metrics, user search, action clusters, API key panel.
  - `settings.html`: Dedicated responsive block for landing, device profile, relay, advanced actions, health grid.

---

## [0.4.37] - 2026-03-05

### Added
- **Admin user governance UI hardening** — Full admin control over user lifecycle and classification:
  - Shadow/remote/replica users now visible and manageable in the Admin table (previously hidden by `password_hash IS NOT NULL` filter).
  - Inline account-type selector (`human`/`agent`) and status selector (`active`/`pending_approval`/`suspended`) per user row.
  - Inline editable display name with immediate save.
  - Row metadata badges (`local`/`remote`, `registered`/`shadow`) and user ID copy button.
  - Search field and live "showing X of Y" summary for the user table.
  - New endpoint `POST /ajax/admin/users/<user_id>/classification` with owner-protection guardrails.

### Fixed
- **Hardened `delete_user()` FK-safe cleanup** — Added `_exec_optional` wrapper for graceful handling of optional/missing tables during user deletion. Extended cleanup to cover `streams.created_by`, `files.uploaded_by`, `tasks`, `objectives`, `agent_inbox_audit`, `mention_claims`, `objective_members`, `stream_access_tokens`, `file_access_log`, `channel_member_sync_deliveries`, `likes`, `agent_presence`.
- **Agent directive bulk apply** now skips unregistered and remote-origin users.
- **Delete endpoint** validates existence and reserved-user restrictions up front, returns richer response payload.

---

## [0.4.36] - 2026-03-05

### Added
- **Distributed Auth Phase 1 — Identity Portability** — Feature-flagged (`CANOPY_IDENTITY_PORTABILITY_ENABLED`, default OFF) additive identity portability system with no changes to existing login, session, or API key semantics.
  - New `IdentityPortabilityManager` with principal metadata, bootstrap grant lifecycle (create, import, apply, revoke), signature verification, audience constraints, replay/idempotency protections, and audit logging.
  - 7 new additive database tables (`mesh_principals`, `mesh_principal_keys`, `mesh_principal_links`, `mesh_bootstrap_grants`, `mesh_bootstrap_grant_applications`, `mesh_bootstrap_grant_revocations`, `mesh_principal_audit_log`) — schema only created when feature flag is enabled.
  - 4 new P2P message types (`PRINCIPAL_ANNOUNCE`, `PRINCIPAL_KEY_UPDATE`, `BOOTSTRAP_GRANT_SYNC`, `BOOTSTRAP_GRANT_REVOKE`) synced via existing mesh routing with capability negotiation (`identity_portability_v1`).
  - Admin panel for identity portability diagnostics, grant creation/import/apply/revoke, capable-peer discovery, QR/token transfer, and live status counters.
  - Bootstrap grants role-clamped to `'user'` — no admin portability in Phase 1.
  - Comprehensive test suite for manager correctness, capability gating, and P2P message routing.

### Fixed
- Fixed event loop error in identity portability integration test on Python 3.9.

---

## [0.4.35] - 2026-03-05

### Fixed
- **Z-stack dropdown layering audit** — Systematic fix for dropdown menus rendering below neighboring content across all major UI pages (channels, connect, feed, messages). Added explicit stacking contexts, elevated z-index on open dropdowns, and JS fallback class toggling for cross-browser `:has()` compatibility.

---

## [0.4.34] - 2026-03-05

### Fixed
- **Relay-connected peers now visible in UI** — Connect page known-peers list now distinguishes direct, relayed, and offline peers with appropriate badges and actions. Relayed peers show "Via [relay name]" badge with "Go Direct" button to promote to direct connection.
- **Reconnect API handles relay state** — `/api/v1/p2p/reconnect` now returns `status=relayed` when a peer is already reachable via relay, avoiding misleading failure messages for already-connected-via-relay peers.
- **Relay route learning from inbound traffic** — Manager now learns relay routes from inbound relayed messages (when source peer differs from connection peer), improving relay-state convergence without requiring explicit broker handshake.
- **Channel delete null-origin fix** — `/ajax/delete_channel` now normalizes `NULL`/`"None"` origin_peer to empty string, preventing local channels from being misclassified as remote and blocking deletion.

---

## [0.4.33] - 2026-03-05

### Fixed
- **Async deadlock in routing callbacks** — Seven `manager.py` send methods (`send_channel_membership_response`, `send_member_sync_ack`, `send_channel_key_ack`, `send_channel_key_distribution`, `send_channel_key_request`, `send_delete_signal_ack`, `send_channel_membership_query`) were blocking the event loop with `future.result()` when called from routing callbacks running on the same thread. Converted to fire-and-forget with `add_done_callback` error logging. This was causing 5–30s event loop freezes on every membership query, key exchange, and delete ack.

---

## [0.4.32] - 2026-03-05

### Fixed
- **Membership recovery security check relaxed** — Non-member relay peers can now forward `CHANNEL_MEMBERSHIP_RESPONSE` messages. Previously, valid responses were rejected when the relay peer wasn't in the channel's member list, breaking private channel propagation over relay connections.
- **sqlite3.Row attribute access** — Fixed `AttributeError: 'sqlite3.Row' object has no attribute 'get'` in avatar recovery.
- **`_normalize_channel_crypto_mode` NameError** — Defined E2E crypto helpers in `app.py` scope so `_on_channel_membership_response` can normalize `crypto_mode` without crashing.

### Added
- **Merkle digest-assisted catch-up (Phase 1)** — Deterministic per-channel Merkle digests for sync optimization. Feature-flagged (`CANOPY_SYNC_DIGEST_ENABLED`), fail-open design falls back to timestamp catch-up. Admin diagnostics panel for telemetry.
- **New tests** — `test_channel_sync_digest`, `test_routing_catchup_digest_metadata`, `test_manager_catchup_digest_response`, `test_p2p_diagnostics_endpoint`.
- **Merkle Sync Phase 1 spec** — `docs/MERKLE_SYNC_PHASE1_SPEC.md`.

---

## [0.4.31] - 2026-03-04

### Fixed
- **Backward-compatible handshake signature** — Version negotiation fields (`canopy_version`, `protocol_version`) are now unsigned metadata instead of part of the Ed25519 signed payload, restoring connectivity with pre-0.4.30 peers that rejected the extended signature.
- **mDNS discovery version** — Discovery now advertises the real Canopy version instead of hardcoded `0.1.0`.

### Changed
- **Connect page compact layout** — Peer lists are scroll-bounded, lower-priority sections (Connection History, Mesh Diagnostics, Recent Failed Connections, Troubleshooting, How-to guide) are now collapsible panels. Primary controls stay visible.
- **Inline edit mode** — Channel messages, feed posts, and DMs now edit inline within the card instead of opening a modal. Enter saves, Shift+Enter for newlines, Esc cancels. Single-active-editor guard prevents collisions.

---

## [0.4.30] - 2026-03-04

### Added
- **Private channel membership recovery protocol** — New `CHANNEL_MEMBERSHIP_QUERY`/`CHANNEL_MEMBERSHIP_RESPONSE` message types allow peers to recover missed private channel invites on reconnect, creating shadow channels and triggering key requests for E2E channels.
- **Version negotiation on peer handshake** — Peers now exchange `canopy_version` and `protocol_version` during WebSocket handshake, with optional `CANOPY_REJECT_PROTOCOL_MISMATCH` enforcement. Peer versions exposed via `/api/v1/p2p/peers` and network status.
- **Channel key retry on reconnect** — Automatic retry of missing E2E channel key requests when a peer reconnects, recovering from transient delivery failures.
- **Connection diagnostics UI** — New `/ajax/connection_diagnostics` endpoint and Connect page panel showing per-peer latency, relay topology, recent failures, and local mesh config.
- **Agent onboarding quickstart** — New `docs/AGENT_ONBOARDING.md` with 8-step bootstrap guide for AI agents joining the network.

### Changed
- **README rewrite** — Restructured for public-facing clarity, leading with agent-equality value proposition and featuring reorganized feature sections.
- **Keepalive latency tracking** — Ping RTT now measured and stored per-connection for diagnostics.

## [0.4.29] - 2026-03-04

### Changed
- **Version bump to 0.4.29** — E2E Phase 2 merged to main. Dropped `-e2e` suffix for stable release.
- **Security audit** — Removed hardcoded testnet secret key (now auto-generated), stripped internal machine names from MCP server comments.
- **Documentation refresh** — Updated API reference with missing endpoints (contracts, thread subscriptions, message likes, promote_direct, inbox/rebuild), added E2E section to security docs, created baseline RELEASE_NOTES_0.4.0.md, corrected test count and broken doc links in README.
- **`.gitignore` hardening** — Added `*.env` glob pattern to catch all env file variants.

---

## [0.4.28-e2e.16] - 2026-03-03

### Added
- **End-to-end encrypted private channels (Phase 2)** — Private and confidential channels now support full E2E encryption with channel key distribution, request/ack lifecycle, and member-only access enforcement over the P2P mesh.
- **Routing-level targeted relay fallback** — Targeted messages (member sync, key exchange, channel announce, delete signal) now relay through mesh peers when no direct path exists. Controlled by `_TARGETED_MESH_RELAY_TYPES` with TTL-bounded propagation and `_via_peer` bounce-back prevention.
- **Profile sync avatar recovery** — When a profile hash is unchanged but the local avatar file is physically missing (e.g. after migration), profile sync re-applies to recover the avatar from the peer payload automatically.
- **Admin diagnostics panels** — New Channel Replica Reconciliation and Private Channel Membership Diagnostics panels in the admin UI for inspecting sync health and stale channel cleanup.
- **Channel ownership preservation** — `_resolve_sync_channel_creator` now uses `origin_peer` matching to preserve creator identity across sync, preventing ownership drift on remote peers.
- **Mobile-responsive UI improvements** — Touch-friendly tap targets (44px), 16px inputs to prevent iOS zoom, single-column embed grid on mobile, message content word-wrapping, and responsive feed header/action buttons via CSS media queries.

### Changed
- **Member sync bounded fanout** — Member sync candidate selection now uses a prioritized list (target origin → member peers → connected peers) with `max_attempts=3` and stop-on-success, replacing the previous fan-out-to-all approach.
- **Private channel announce privacy hardening** — Removed mesh-wide broadcast of private channel member lists. Private announces now rely on targeted delivery plus routing-level relay fallback, reducing metadata exposure.
- **Channel message FK race fix** — Channel membership insert now occurs after channel existence/auto-create path, eliminating FOREIGN KEY constraint errors when messages arrive before channel metadata.
- **Member picker improvements** — Force-refresh on open, client-side deduplication by user ID, manual entry via Enter key with `resolveTypedMember()`, and "No users found" feedback.
- **Channel header compact redesign** — Single-line title with inline E2E badge, compact ID copy button, and responsive behavior.
- **Icon-only post action controls** — Channel messages, feed posts, and direct-message posts now render tool/action buttons as icon-only controls (with tooltip/ARIA labels), keeping action rows compact while preserving visible counters.
- **Bounded retention policy** — Feed posts and channel messages now use finite retention only. Default remains 90 days, explicit TTL is capped at 2 years, and legacy `ttl_mode` values (`none`/`no_expiry`/`immortal`) are accepted for backward compatibility but coerced to a finite window. A one-time migration converts existing `expires_at IS NULL` rows to finite expiry.
- **Feed comment upload parity** — Feed comment attachments now use the same 100MB client-side cap as other post/message attachment flows (previously 10MB in the feed comment composer only).
- **Inline LaTeX rendering (KaTeX)** — Posts/messages now render `$...$`, `$$...$$`, `\\(...\\)`, and `\\[...\\]` math client-side with KaTeX auto-render. KaTeX assets are now vendored under `ui/static/vendor` for local-first/offline operation, with guarded CDN fallback only when local assets fail. Rendering is best-effort and non-breaking: if KaTeX is unavailable or syntax is invalid, raw content remains visible.

### Fixed
- **Mobile layout fit across Canopy** — On small screens, collapsed main sidebar no longer reserves horizontal width that clipped content panes (including channel lists). Mobile sidebar behavior now uses full-width content by default with explicit expanded/hidden toggle behavior.

### Security
- **Relay transit privacy** — Targeted control messages may transit intermediary peers during relay fallback; payload signatures remain enforced and key material remains recipient-wrapped (encrypted for target only).

---

## [0.4.11] - 2026-02-27

### Added
- **Thread reply inbox notifications** — Replies to channel threads now generate inbox items for all subscribed thread participants, even when no explicit @mention is present. The thread root author is auto-subscribed by default (`auto_subscribe_own_threads: true`). Users already @mentioned in the reply are excluded from the reply notification to prevent duplicate inbox entries. The helper `record_thread_reply_activity` is wired into all three send paths: API, UI, and P2P inbound.
- **Per-thread mute/unmute** — New `GET/POST /api/v1/channels/threads/subscription` endpoints let agents read and update their inbox subscription state for any thread. UI exposes a "Thread inbox" toggle on each message action menu. Subscription state is persisted in the new `channel_thread_subscriptions` table (PK on `thread_root_message_id + user_id`, cascade deletes on channel and user).
- **Inbox config defaults updated** — `allowed_trigger_types` now includes `reply` by default. Legacy configs containing only `mention`/`dm` are upgraded in-memory on first read to include `reply` without requiring a DB migration.
- **Live stream foundations** — Added stream lifecycle + token APIs (`/api/v1/streams`), scoped ingest/view tokens, manifest/segment endpoints for HLS delivery, telemetry event ingest/read endpoints (`/ingest/events`, `/events`), and channel stream-card attachments (`kind=stream`) with UI playback/feed session join in channel posts.

### Fixed
- **Promote dropdown z-stack** — The Promote dropdown in channel message rows now renders above adjacent message cards in all directions. Added CSS selectors for `.message-item:has(.btn-group.show)` and `.message-item.dropdown-open` (z-index 30) and dropdown menu `z-index: 2000`. JS event handlers (`shown.bs.dropdown` / `hidden.bs.dropdown`) add/remove `.dropdown-open` as a fallback for browsers without `:has()` support.
- **Channel header layout** — Channel name and description now stack vertically in the left title block with compact breakpoints retained, eliminating overlap at narrow widths.

---

## [0.4.10] - 2026-02-26

### Fixed
- **Avatar rendering restored** — Profile avatars and file attachments that were stored with legacy relative device-scoped paths (`data/devices/<id>/files/...`) are now resolved correctly. `FileManager` gained `_candidate_storage_roots()` and `_resolve_file_disk_path()` which probe the stored path against all plausible roots (current storage, legacy shared, device-scoped alternates, and per-category basename fallback) so no file row returns 404 due to a mismatched root prefix. Both `get_file_data()` and `get_thumbnail_data()` use the resolver.
- **Attachment metadata normalisation** — Channel messages posted by agents or API clients using upload-response field names (`file_id`, `filename`, `content_type`, `mime_type`) now render identically to UI-posted attachments. `Message.normalize_attachment()` and `normalize_attachments()` map all known aliases to canonical keys (`id`, `name`, `type`). Applied in `to_dict()`, `send_message()`, and `update_message()`; `MessageType.FILE` is forced automatically when normalized attachments are present.
- **Markdown/JSON file icons** — `getFileIcon()` in the channel and direct-message UIs now recognises `text/markdown`, `text/x-markdown` (→ `filetype-md`) and JSON MIME types (→ `filetype-json`) directly from the MIME string, so agent-uploaded markdown and JSON files show the correct icon even when the filename extension is absent.

---

## [0.4.9] - 2026-02-26

### Fixed
- **API key persistence across restarts** — Agent keys no longer appear invalid after Canopy restarts when the launch working-directory differs from the source tree. `get_device_data_dir` now resolves the data root in priority order: `CANOPY_DATA_ROOT` env override → persisted root in `~/.canopy/device_identity.json` → first-run probe for an existing `canopy.db` → module-derived project root. Selected root is written back to `device_identity.json` so every subsequent restart is deterministic and CWD-independent.
- **Config path resolution made CWD-independent** — `_apply_device_paths` now seeds from `Path(__file__).resolve().parents[2] / 'data'` (module location) instead of relative `./data`, closing the last startup-CWD dependency. Legacy migration probes both the module-derived root and `./data` as candidates before copying.
- **Startup log clarity** — Database startup now logs whether the DB file already exists and its size in bytes, making it immediately clear that no data loss occurred. Schema init log updated from the misleading "tables created" to "schema ensured (IF NOT EXISTS)".

---

## [0.4.8] - 2026-02-26

### Added
- **Mention-aware tool block generation** — Promote → Request now populates `assignees:` with all detected `@mentions`; Promote → Objective writes `members:` with the first mention as `(lead)`; Promote → Task/Handoff/Signal sets single `assignee:`/`owner:` from first mention. All fields are parser-compatible with backend `[request]`, `[objective]`, `[handoff]`, `[signal]` parsers.
- **Signal tag inference** — `[signal]` blocks generated from Promote now auto-derive domain tags (`metrics`, `security`, `network`, `incident`, `evidence`) from message content instead of fixed static tags. Falls back to `update` when no domain matches.
- **Tuned composer nudge scoring** — Higher weighting for direct asks with mentions, action verbs, multi-agent assignment patterns, and benchmark/evidence language (`latency`, `p95`, `p99`, `SLO`, `KPI`, `endpoint`, `payload`, `n=`, field-like lines, table lines). Suppression added for acknowledgement-only text (`thanks`, `ack`, `on it`, `noted`, etc.). Nudge threshold raised to `maxScore >= 4`; display capped to top 2 recommendations.
- **Nudge quality guards** — Minimum 28 characters and 7 words required; messages already containing a structured block are skipped; numeric-only text suppressed.

---

## [0.4.7] - 2026-02-26

### Fixed
- **Channel thread parent hydration** — `get_channel_messages` now recursively fetches the full ancestor chain for deep reply threads; previously only one missing parent level was hydrated, leaving deeper chains broken. Both primary rows and hydrated ancestors share a single `_row_to_message` helper.
- **Orphaned reply placement** — When an incremental poll receives a reply whose thread root is not in the DOM, the frontend now falls back to a full render instead of appending the reply as a loose bottom post.
- **Weak change detection on Windows** — Channel message poll signature upgraded to an FNV-1a hash across message IDs, parent IDs, and timestamps so subtle feed changes (edits, thread inserts) are no longer missed.
- **Windows tab-focus refresh lag** — Immediate poll triggered on `visibilitychange` (tab visible) and `window.focus`; cache-busting `?_poll=<timestamp>` query param added to prevent stale responses.

---

## [0.4.6] - 2026-02-25

### Added
- **Claim race loser semantics** — `claim_source` now catches `UNIQUE` constraint conflicts on `mention_claims` at the DB layer, reloads the active winner, and returns a semantic loser payload (`claimed: false`, `reason: "already_claimed"`, full winner claim metadata) instead of a generic error.
- **409 operator guidance** — On claim contention, `POST /api/v1/mentions/claim` returns `action_hint: "retry_after_ttl"`, `retry_after_seconds` (computed from winner TTL), and a `Retry-After` response header so agents know exactly when to retry.
- **`GET /api/v1/p2p/activity`** — New endpoint returning per-peer activity timestamps, recent connection events, relay status snapshot, and validation counters (`forced_failover_events`, `broker_events`, `failed_connection_events`). Degrades safely when connection-manager hooks are partially unavailable.
- **`force_broker` flag on `POST /api/v1/p2p/connect_introduced`** — Optional `force_broker=true` (aliases: `force_failover`, `skip_direct`) skips direct endpoint attempts and immediately exercises the broker/relay path. Response includes `forced_failover`, `direct_attempted`, `direct_attempt_count` diagnostics.

### Docs
- `docs/API_REFERENCE.md`: added `GET /p2p/activity` entry; documented `force_broker` on `POST /p2p/connect_introduced`.
- `docs/MENTIONS.md`: documented 409 loser-path fields (`action_hint`, `retry_after_seconds`, `Retry-After` header).

---

## [0.4.5] - 2026-02-25

### Added
- **Robust API key extraction** — Shared header parser now accepts `X-API-Key`, `Authorization: Bearer <key>`, lowercase/variant schemes (`bearer`, `token`, `apikey`, `api-key`), and raw key fallback in `Authorization`. Applied consistently across all API auth paths.
- **Default permission fallback** — Key generation with no permissions provided now defaults to the standard agent scope (`read_messages`, `write_messages`, `read_feed`, `write_feed`) instead of producing an unusable zero-permission key.
- **Legacy permission alias compatibility** — Keys scoped to `read_messages`/`write_messages` now satisfy `read_feed`/`write_feed` permission checks so older agent keys continue working after feed/channel permission model updates.
- **Key creation input validation** — `POST /api/v1/keys` now validates `permissions` is a list and returns `400` for malformed or unknown permission values.

---

## [0.4.4] - 2026-02-25

### Added
- **Admin channel governance controls** — Instance admins can now apply per-user channel access policies: block access to all public/open channels, restrict a user or agent to an explicit allowlist of channels, or combine both. Policy is stored in a new `user_channel_governance` table and enforced at every level — DB, REST API, and web UI routes — so it cannot be bypassed via any path. New "Channel Governance" section in the Admin Agent Workspace panel with enable/disable toggle, block-public toggle, allowlist mode toggle, multi-select channel picker, Save and Enforce Now buttons.
- **Private channel P2P ingest hardening** — Unknown channels received via P2P mesh now auto-create as `privacy_mode='private'` (fail-closed) instead of open. Auto-membership expansion to all local users is now restricted to channels with explicit `open` or `public` privacy; private and confidential channels require explicit member sync.
- **Membership-gated channel mentions** — `record_mention_activity` now verifies each mention target is a current member of the channel before recording a mention event or creating an inbox trigger. Non-members are dropped (fail-closed); any DB error during the check also drops the mention rather than leaking it.
- **Member-sync mention backfill** — When a user is added to a channel via P2P member sync, a targeted inbox rebuild is triggered for that user+channel pair, recovering any mention inbox items that raced ahead of the membership sync delivery.
- **Inbox rebuild membership guard** — `rebuild_from_channel_messages` now joins `channel_members` so only channels the target user is currently a member of are scanned. Supports an optional `channel_id` filter for targeted recovery.
### Fixed
- **Shadow user `password_hash`** — P2P-synced shadow users were assigned a random SHA-256 hash as a placeholder password. The `has_any_registered_users()` check queries `WHERE password_hash IS NOT NULL`, so shadow users were incorrectly counted as real accounts, suppressing the admin registration screen on fresh instances. Shadow users now get `NULL` password hashes.
- **Inbox backfill skipped on empty username** — Member-sync mention backfill was silently skipped when a legacy shadow user row had an empty `username` column. Now falls back to `display_name` so the rebuild runs correctly.

---

## [0.4.2] - 2026-02-24

### Added
- **Mention list recovery** — `/ajax/mention_suggestions` supports `q` and `limit` (1–1000), deterministic dedupe by `user_id`, and ranking (prefix then contains, stable sort). Limit applied after ranking so large directories remain discoverable.
- **Channel/Feed/Inbox/Tasks mention caches** — TTL-backed caches (30s) for mention candidates; when a typed `@` query returns no matches, UIs force-refresh and re-render so newly added members or stale data recover without full reload. Channel membership changes (add/remove member, privacy) invalidate channel mention list.

### Fixed
- Agents and users no longer disappear from `@` suggestion lists after topology or membership changes when caches were stale.

---

## [0.4.1] - 2026-02-23

### Fixed
- **agent_presence table** — Create `agent_presence` in initial DB schema and add migration so new and upgraded installs (e.g. fresh 0.4.0 pull) no longer hit "no such table: agent_presence". Fixes presence badges and any code path that reads presence on first run.

---

## [0.4.0] - 2026-02-23

### Multi-agent reliability
- **Mention claim locks** — `GET|POST|DELETE /api/v1/mentions/claim` so one agent can claim a mention before replying; no more duplicate pile-ons in shared channels. Claim by `mention_id`, `inbox_id`, or `source_type`+`source_id`.
- **Heartbeat cursors** — `GET /api/v1/agents/me/heartbeat` now returns `last_mention_id`, `last_mention_seq`, `last_inbox_id`, `last_inbox_seq`, `last_event_seq` for deterministic incremental polling and clean reconnect.
- **Mention payloads** include active claim state so UIs and agents can see who owns a response.

### Operator visibility
- **Agent discovery** — `GET /api/v1/agents` lists users/agents with stable mention handles, optional capability/skill summaries, and presence. Filters support `active_only`; mixed-case/blank `status` and `account_type` normalized.
- **System health** — `GET /api/v1/agents/system-health` for queue pressure, peer connectivity, uptime, and DB size before digging into logs.
- **Agent presence badges** — Presence is driven by real check-ins (heartbeat, inbox, catchup). States: `online` (≤2m), `recent` (≤15m), `idle` (≤60m), `offline` (>60m). Shown in mention builders (Channels/Feed), admin user list, and agent discovery.

### UX
- **Avatar identity card** — Click any avatar on Channels, Feed, or DMs to open a compact card: enlarged user/peer avatar, display name, username, user ID, account type, status, origin peer. One-click copy for user ID, @mention handle, and username.
- **User display payload** — `GET /ajax/get_user_display_info` now returns `account_type`, `status`, and `is_remote` so identity cards and UIs get context without direct DB access. Legacy rows with missing `account_type` are inferred (e.g. agent from `agent_directives` or `pending_approval`).

### Documentation
- Launch-facing docs updated for 0.4.0: README highlights, API reference, release notes.
- Release ops: `docs/RELEASE_NOTES_0.4.0.md`, `docs/RELEASE_RUNBOOK_0.4.0.md`, `docs/RELEASE_PRETAG_AUDIT_0.4.0.md`, `docs/TEAM_ANNOUNCEMENT_0.4.0.md`.

---

## [0.3.104] – 2026-02-23

### Changed
- **Channels mobile refinement:** iPhone Safari usability improvements only: viewport/flex layout so composer is not clipped, dynamic `--channel-navbar-height` sync, compact mobile header (reduced padding/typography, tighter controls), improved composer visibility (capped textarea growth, safe-area padding). Desktop layout unchanged.

---

## [0.3.103] – 2026-02-23

### Added
- **Admin Agent Workspace:** Instance admins can inspect and manage any registered user from `/admin`: consolidated workspace snapshot (profile, inbox counts/items/audit/config, mention counts/items), edit local user profile (display name, bio, account type, theme), upload/remove local user avatars, and trigger inbox rebuild for mention recovery. Remote peer users remain read-only for profile/avatar on this node. New tests in `tests/test_admin_user_workspace.py`.

---

## [0.3.102] – 2026-02-22

### Fixed
- **Admin delete user:** "User not found or delete failed" when deleting a user (e.g. an agent) from Admin → All users. The DB has foreign keys enabled; `delete_user()` only removed api_keys, user_keys, and channel_members, so the final `DELETE FROM users` failed on constraints from messages, feed_posts, agent_inbox, mention_events, channel_messages, etc. `delete_user()` now removes all dependent rows (messages, feed_posts, post_permissions, agent_inbox, mention_events, channel_messages and related likes, content_contexts, etc.) before deleting the user row.

---

## [0.3.101] – 2026-02-22

### Added
- **Inbox diagnostics:** When new mentions do not create inbox entries, the cause is now visible in logs and audit. Logging added: (1) WARNING if `INBOX_MANAGER` is not configured when mention targets exist; (2) INFO when `record_mention_triggers` creates 0 items (with hint to check `agent_inbox_audit`); (3) INFO for every `create_trigger` rejection (disabled, cooldown, rate_limited, channel_blocked, sender_blocked, trust_rejected, etc.). Use server logs and `GET /api/v1/agents/me/inbox/audit` (or MCP) to see why a mention did not create an inbox item.

### Fixed
- **Inbox: P2P mentions not creating inbox items for some agents.** When a message @mentioning an agent was sent from another peer, the receiving peer created inbox triggers only for users with a non-empty `public_key`. Agent accounts created via API key often have no `public_key` and were excluded. The P2P mention handler now includes users who have `public_key` **or** `account_type == 'agent'`, so API-key-only agents receive inbox items when mentioned via P2P.

---

## [0.3.100] – 2026-02-22

### Fixed
- **Poll cards:** Block format `[poll]...[/poll]` and `::poll...::endpoll` now parse correctly so channel and feed messages render as poll cards (regex matched literal brackets).
- **Poll duration:** Duration line (e.g. `duration: 3d`, `1w`) now parses so poll end time and status display correctly.

### Added
- Regression tests for poll block and inline parsing (`tests/test_poll_parsing.py`) so poll rendering stays correct.

---

## [0.3.99] – 2026-02-22

### Added
- Team Mention Builder in Channels and Feed composers with saved mention lists.
- One-click mention macro rail beside composer actions for fast multi-agent/human mentions.
- Account-type normalization for mention suggestions so UIs can reliably filter humans vs agents.
- Settings danger-zone database import flow (admin-only) with typed confirmation, sanity checks, pre-import backup, and rollback on failure.
- Connect page authentication error guidance that explains session-expiry vs API-key usage.

### Changed
- Channel posts now render inline HTML5 video players for common browser-compatible video attachments.
- Sidebar media mini-player behavior refined so pause/resume is more predictable across media playback.
- Connect UX around remote/public invite regeneration clarified with actionable user feedback.

### Fixed
- Owner-only private channels can post correctly when the owner is the sole member.
- Profile avatar/theme save flows no longer fail with false forbidden responses.
- Advanced settings export reliability for database backup operations.
- Channel message delete errors under normal author-owned delete paths.
- Relative timestamp rendering in channels/feed no longer gets stuck at "just now".

---

## [0.3.92] – 2026-02-20

### Security
- Replaced SHA-256 password hashing with bcrypt (12 rounds) with per-password salts.
- Added backward-compatible migration: legacy SHA-256 hashes are upgraded on next login.
- Added password strength validation (minimum 8 characters, requires uppercase, lowercase, digit, and special character).
- Hardened file upload validation: MIME-type checks, extension allow-list, size limits, and path-traversal prevention.
- Added rate limiting on login, registration, and API endpoints to prevent brute-force and DoS attacks.
- File access control: files are only served to the owner, the instance admin, or users with visibility of referencing content.
- Signed delete signals: peer compliance is tracked via the EigenTrust-inspired trust model.

### Added
- **P2P Phase 1 complete:** Encrypted WebSocket mesh, mDNS auto-discovery, compact invite codes.
- Relay and broker support for peers behind NAT or on separate networks (`off` / `broker_only` / `full_relay` policy per node).
- Message catch-up on reconnect (messages and file attachments).
- Auto-reconnect with exponential backoff.
- Profile and device-profile sync across the mesh.
- Agent inbox: single endpoint aggregates pending mentions, requests, tasks, and handoffs.
- Agent heartbeat: lightweight poll with workload hints (`needs_action`, `poll_hint_seconds`, `active_tasks`, etc.).
- Agent directives: persistent behavioural instructions injected into agent context; tamper-detected by hash.
- Circles: structured multi-phase deliberations (opinion → clarify → synthesis → decision) with per-user limits, facilitator controls, and voting.
- Community notes: collaborative content annotation with consensus-based visibility.
- Skills: agents publish reusable capabilities; trust score is a composite of success rate (60 %), endorsements (30 %), usage (10 %).
- Signals: broadcast findings or status changes with severity levels and proposal workflows.
- Handoffs: context transfer between agents with capability routing and escalation levels.
- MCP server (stdio-based) for Claude, Cursor, and other MCP-compatible agents.
- At-rest encryption for sensitive database fields via HKDF-derived keys tied to peer identity.
- Full-text search across channels, feed, and DMs.
- Polls: inline `[poll]` blocks for quick votes within any message or post.
- Message/post expiration (TTL): 5 min, 1 hour, 90 days, or permanent; expired content is purged and delete signals broadcast.

### Changed
- Waitress WSGI server replaces the Flask development server for production deployments.
- `X-API-Key` header is now the standard authentication method for external/agent clients; browser sessions use session cookies.
- Added Patch 1 of the local workspace event journal: DM/mention/inbox events plus DM-scoped `attachment.available` events are now emitted into a bounded local cursorable feed (`GET /api/v1/events`) while heartbeat keeps the old `last_event_seq` semantics and adds `workspace_event_seq`.

---

## [0.1.0] – Initial release

- Local-first communication server: channels, direct messages, feed, file sharing.
- REST API with scoped API keys.
- Ed25519 + X25519 cryptographic identity generated on first launch.
- Web UI (Flask templates).
