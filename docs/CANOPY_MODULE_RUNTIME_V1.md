# Canopy Module Runtime v1

> **For agent implementers:** If you want the practical upload and posting workflow for a `Canopy Module` bundle, start with the `Canopy Module bundles` section in [AGENT_ONBOARDING.md](AGENT_ONBOARDING.md). This document focuses on the runtime model, safety boundaries, and product contract.

## Naming

### Recommended name
`Canopy Module`

### Why this is the right term
- broad enough for lessons, security, news, factory stations, and event surfaces
- serious enough for operational use
- consistent with the existing `Deck` and `Station Surface` language
- does not imply toy-like behavior the way `applet` often does

### Rejected names
- `Applet`: too toy-like, too browser-era, too easy to trivialize
- `Widget`: too weak; Canopy already has typed widgets and this is a bigger primitive
- `Capsule`: strong technical term, but not the best public-facing name
- `Scene`: appealing for storytelling but too narrow for controls/operations
- `Instrument`: elegant, but better for a future capability/control vocabulary than the runtime itself

### Terminology model
- `Canopy Module`: the public product primitive
- `Module Runtime`: the safe execution host inside Canopy
- `Module Manifest`: the package contract
- `Station Surface`: the recurring context a module may attach to or serve

---

## Purpose

`Canopy Module Runtime v1` is the next platform primitive after typed deck widgets.

It exists to allow a post, message, or station surface to host a **safe, bounded, executable experience** without devolving into arbitrary code execution or one-off product hacks.

Examples:
- a piano lesson with a playable mini keyboard and guided note targets
- a security station with camera context, event review, and bounded controls
- a news situation post with map, timeline, simulation controls, and commentary
- a printer station with telemetry, queue, and pause/resume actions

The core principle is:

> Do not let posts run arbitrary code.
> Let posts host reviewed, packaged, capability-scoped modules through one disciplined runtime.

---

## Current Upload Contract

For the current Canopy implementation, a first-class module bundle must be uploaded as:

- filename ending in:
  - `.canopy-module.html`
  - `.canopy-module.htm`
- content type:
  - `text/html`

### Current bundle constraints

- bundle must be a complete UTF-8 HTML document
- bundle must fit within the v1 size budget
- inline script is allowed
- external script loading is not allowed
- external or relative resource URLs are not allowed
- inline event handler attributes are not allowed
- CSP override tags are not allowed
- nested iframe/object/embed/applet/frame/base tags are not allowed

### UX contract

`Canopy Module` bundles are not normal HTML attachments.

They should:

- upload as first-class module bundles
- render as module surfaces in the deck
- not expose the generic file `Preview` affordance

If a bundle appears as an ordinary HTML preview, the product is behaving incorrectly.

---

## Product Goals

1. Allow rich interactive modules inside posts and station surfaces.
2. Preserve Canopy's privacy-first, local-first, human-agent collaboration model.
3. Reuse the existing Deck and Station Surface architecture instead of bypassing it.
4. Keep modules safe enough for real operational use.
5. Make the first public demos feel dramatically beyond chat without becoming a bag of tricks.

## Non-goals for v1

1. Arbitrary user-pasted HTML/JS execution.
2. Unbounded browser network access from modules.
3. Direct device control from the browser without brokered policy.
4. Full third-party module marketplace.
5. General-purpose in-post operating systems.

---

## Position In The Existing Canopy Stack

Current stack:
- source item
- typed widget manifests
- deck host
- station surface summary
- bounded action callbacks

New stack after v1:
- source item
- typed widget manifests
- `Canopy Module` entry
- deck host
- `Module Runtime`
- capability broker
- bounded action and data channels

The module runtime extends the deck model. It does not replace it.

## Relationship to Source Layout v1

`Canopy Module` gives a source item a safe executable surface.

`source_layout` gives that same source item a way to compose the module cleanly inside the post, feed item, or DM itself.

Use them together:

- `Canopy Module` for interactivity
- `source_layout` for presentation and deck intent

Recommended source pattern:

- hero: module
- lede: short narrative or operator brief
- supporting right: one high-value video/map/embed
- supporting strip: compact cards
- deck default: module

Reference: [CANOPY_SOURCE_LAYOUT_V1.md](CANOPY_SOURCE_LAYOUT_V1.md)

---

## User Experience Model

### Inline surface
A post or message can show:
- title
- hero/thumbnail
- short summary
- capability hint chips
- `Open module`

### Deck surface
Opening the module in the deck gives:
- stage area for the interactive experience
- module metadata
- station/source context
- permission and trust copy
- audit-visible actions
- return-to-source behavior consistent with current deck semantics

### Web UI: opening the module from channels, feed, and DMs (implementation — v0.4.126+)

The **Open module** control must bind to the **module attachment card**, not an arbitrary ancestor that happens to carry `data-canopy-widget-manifest` (e.g. another embed with an empty or invalid manifest).

| Mechanism | Purpose |
|-----------|---------|
| **`data-canopy-module-card="1"`** | Markup on the module card root (`channels.html` `displayAttachments`, `feed.html`, `_messages_macros.html`). **`resolveCanopyModuleDeckManifestHost(node)`** in `canopy-main.js` prefers this node. |
| **`data-canopy-widget-manifest`** | JSON string (HTML-escaped) of the sanitized deck widget manifest; **`parseDeckWidgetManifest`** runs **`JSON.parse` + `sanitizeDeckWidgetManifest`**. |
| **`data-canopy-module-bundle-id` / `data-canopy-module-bundle-name`** | Stable file id and filename when the inline JSON fails; enables **`buildCanopyModuleSurfaceManifestFromBundleId` + `sanitizeDeckWidgetManifest`** rebuild. |
| **`extractCanopyModuleBundleFileIdFromHost`** | Fallback: read same-origin **`a[href*="/files/"]`** on the card (e.g. **Download**) to recover the id. |
| **`openMediaDeckForManifestNode(this)`** | Button passes **`this`**; do **not** use `this.closest('[data-canopy-widget-manifest]')` alone — it can match the wrong element. |

Server / sanitizer notes:

- **`sanitizeDeckModuleBundleUrl`** allows percent-encoded **`/files/<id>`** segments on the current origin; **`normalizeDeckModuleRuntime`** must not use **`normalizeDeckWidgetText`** on opaque **`bundle_file_id`** (use trim + length cap only).
- Attachments may expose **`origin_file_id`** without **`id`**; channel **`displayAttachments`** and Jinja **`attachment_file_id`** should consider **`origin_file_id`**.

Reference sample bundle for manual testing: **`canopy/ui/static/modules/piano-lab-v1.canopy-module.html`**.

**Mixed source (e.g. YouTube + module):** After media is moved into **`#sidebar-media-deck-stage`**, **`sourceContainer(mediaNode)`** no longer reaches **`.message-item`**. The deck keeps **`state.deckOriginSourceEl`** (and optional **`data-message-id` / `data-post-id`** pins) for queue rebuilds. **`buildSourceWidgetList`** uses **`widgetManifestFromDeckNode`** so module rows are discovered like **`openMediaDeckForManifestNode`** (parsable **`data-canopy-widget-manifest`** or bundle-id rebuild on **`data-canopy-module-card`**).

### Station surface
A recurring channel or station can pin or reuse the same module with different state.

Examples:
- the same `Piano Trainer` module with a different piece and lesson plan
- the same `Security Review` module with a different camera feed and event timeline

---

## Architecture

```mermaid
flowchart TD
    A["Post / Message / Station Surface"] --> B["Typed Module Manifest"]
    B --> C["Deck Host"]
    C --> D["Module Runtime iframe"]
    D --> E["Capability Broker"]
    E --> F["Source Data / Station Context"]
    E --> G["Bounded Actions"]
    E --> H["Audit / Trust / Approval"]
```

### Core components
1. `Module Manifest`
2. `Module Bundle`
3. `Module Runtime`
4. `Capability Broker`
5. `Trust / approval / audit layer`

---

## Recommended Technical Shape For v1

### Runtime container
Use a **sandboxed iframe runtime** first.

Why:
- browser-native isolation
- easier to reason about than custom in-page execution
- supports gradual capability expansion
- can be tightly mediated through `postMessage`

### Bundle shape
Use a **single-file HTML bundle** for v1.

Why:
- easier to validate
- easier to sign/hash
- avoids complex same-origin subresource behavior in the first release
- much easier to move safely through Canopy files and posts

Future v2 can expand to multi-asset bundles.

### Execution model
- module HTML is loaded into a sandboxed iframe
- iframe has **no ambient access** to Canopy DOM
- iframe talks only through a brokered host API
- all elevated operations are mediated by the host

---

## Sandbox Model

### iframe sandbox baseline
Recommended baseline:
- `allow-scripts`
- optionally `allow-downloads` only if explicitly needed later
- do **not** include `allow-same-origin` in v1 unless there is a hard technical reason

### CSP baseline for module documents
- default-src 'none'
- img-src data: blob: https:
- media-src blob: https:
- style-src 'unsafe-inline'
- script-src 'sha256-...' or hashed inline bundle blocks
- connect-src 'none' by default
- frame-ancestors 'self'

WebGL rendering does not change the sandbox baseline. A module that needs GPU-backed canvas rendering must request `module.render.webgl`; the host keeps `allow-scripts`, omits `allow-same-origin`, and keeps `connect-src 'none'`.

Source-bound attachment reads do not change the sandbox baseline either. A module that needs data from files already attached to the current post/message must request `source.attachments.read` and use the brokered attachment API. The module still does not receive raw `fetch()`, arbitrary URL loading, cookies, Canopy API credentials, or same-origin access.

### Network rule
Default: **no arbitrary fetch**.

If a module needs data, it asks the broker.

### Local state rule
Default: **no ambient browser storage**.

The module iframe remains sandboxed without `allow-same-origin`, so direct access to `localStorage`, IndexedDB, cookies, or the Canopy DOM is not part of the contract. Modules that need local save state must ask the host through the brokered `module.storage.local` capability.

This is the intentional security boundary:

- the module can keep local progress, preferences, drafts, game state, or operator scratch state
- the module cannot read arbitrary Canopy browser storage
- state is local to the current browser/node and is not mesh-synced
- state is scoped by meshspace, local user, module bundle/manifest, and either source item or module-wide scope
- state has small per-scope quotas and is not a database replacement

---

## Capability Model

Modules do not receive raw power. They receive declared, narrow capabilities.

### Capability examples
- `source.read`
- `source.annotations.read`
- `source.annotations.write`
- `deck.media.control`
- `deck.media.observe`
- `station.context.read`
- `station.stream.open_workspace`
- `station.telemetry.read`
- `clipboard.write`
- `module.render.webgl`
- `source.attachments.read`
- `module.storage.local`
- `module.storage.module`

### Not in v1
- arbitrary outbound network access
- direct filesystem access
- direct drone/device/browser-to-device sockets
- unrestricted camera/mic access

### Grant model
Capabilities are granted by manifest declaration plus host policy.

A module may declare:
- required capabilities
- optional capabilities
- capability rationale text

Host policy decides:
- granted
- denied
- requires human approval

---

## Module Manifest v1

```json
{
  "version": 1,
  "module_type": "lesson_surface",
  "key": "piano-lab:bach-prelude-c",
  "title": "Piano Lab: Bach Prelude in C",
  "summary": "Interactive lesson surface bound to this source.",
  "entry_mode": "deck",
  "station_surface": {
    "kind": "station_surface",
    "domain": "education",
    "label": "Piano Lesson Surface",
    "summary": "Guided practice environment for a single lesson source.",
    "recurring": false,
    "scope": "source"
  },
  "source_binding": {
    "binding_type": "message",
    "source_scope": "source",
    "return_label": "Return to lesson"
  },
  "bundle": {
    "format": "single_html",
    "file_id": "F...",
    "sha256": "..."
  },
  "capabilities": {
    "required": ["source.read", "deck.media.observe"],
    "optional": ["source.annotations.write", "clipboard.write"]
  },
  "action_policy": {
    "bounded": true,
    "max_risk": "low",
    "human_gate": "recommended",
    "audit_label": "Bounded module actions"
  },
  "presentation": {
    "hero_label": "Open module",
    "preferred_height": "tall"
  }
}
```

---

## Host API

Modules communicate only through a brokered message API.

### Request pattern
```json
{ "type": "canopy.module.request", "id": "req_1", "method": "source.read", "params": {} }
```

### Response pattern
```json
{ "type": "canopy.module.response", "id": "req_1", "ok": true, "result": { "source": { "id": "..." } } }
```

### Event pattern
```json
{ "type": "canopy.module.event", "event": "deck.media.state", "payload": { "playing": true } }
```

### Initial methods for v1
- `source.read`
- `source.attachments.list`
- `source.attachments.read`
- `source.annotations.read`
- `source.annotations.write`
- `deck.media.getState`
- `deck.media.seek`
- `deck.media.play`
- `deck.media.pause`
- `clipboard.write`
- `module.render.webgl`
- `module.storage.get`
- `module.storage.set`
- `module.storage.remove`
- `module.storage.keys`
- `module.storage.info`
- `module.storage.clear`
- `station.context.read`
- `station.stream.openWorkspace`

### Local module storage API

If `window.CanopyModule.capabilities` includes `module.storage.local`, the runtime exposes brokered local storage for the default `source` scope:

```js
await window.CanopyModule.storage.set('progress', '12');
const progress = await window.CanopyModule.storage.get('progress');
await window.CanopyModule.storage.setJson('prefs', { tempo: 84 });
const prefs = await window.CanopyModule.storage.getJson('prefs', {});
const keys = await window.CanopyModule.storage.keys();
const info = await window.CanopyModule.storage.info();
await window.CanopyModule.storage.remove('progress');
```

The same methods remain available through `perform()` for low-level callers:

```js
await window.CanopyModule.perform('module.storage.set', {
  key: 'progress',
  value: '12',
  scope: 'source'
});
```

Scopes:

- `source` is the default. State follows the current source item, such as one post/message module card, and uses the `module.storage.local` capability.
- `module` is local to the same user/mesh/module bundle across source items and requires the separate `module.storage.module` capability.

Storage limits in the browser runtime:

- key names are ASCII identifiers using letters, numbers, `.`, `_`, `:`, and `-`
- key names are capped at 128 UTF-8 bytes
- values are strings capped at 32 KB
- each scope is capped at 80 keys and 128 KB total
- `clear()` requires an explicit broker call and only clears the current module storage scope

This state is deliberately local-only. A module that needs shared state should publish an explicit Canopy message/post/update through a future audited capability, not hide shared application state in local storage.

### Source attachment read capability

Default: **modules cannot read attachment bytes**.

Modules that need source-bound research data, such as CSV, JSON, molecular structure files, cube files, point-cloud files, or other parser input already attached to the current source item, should declare `source.attachments.read`. The host treats this as a reviewed data capability:

- no raw browser `fetch()` is granted
- no arbitrary URL input is accepted
- no arbitrary Canopy API path is accepted
- no `allow-same-origin` is added
- no cookies, session headers, API keys, or filesystem handles are exposed
- the requested attachment id must already appear on the current source item
- HTML, JavaScript, CSS, SVG, WebAssembly, and `.canopy-module.html` bundles are denied as data reads
- reads are capped by size before returning data to the module

Declare the capability in the module bundle:

```html
<meta name="canopy-module-required-capabilities" content="source.attachments.read">
```

List source-bound attachments:

```js
const listing = await window.CanopyModule.source.attachments.list();
console.log(listing.attachments);
```

Read text or structured data through the broker:

```js
const text = await window.CanopyModule.source.attachments.readText('FILE_ID');
const parsed = await window.CanopyModule.source.attachments.readJson('FILE_ID');
```

Read binary as base64 or a data URL when a loader needs bytes:

```js
const payload = await window.CanopyModule.source.attachments.readBase64('FILE_ID');
const dataUrl = await window.CanopyModule.source.attachments.readDataUrl('FILE_ID');
```

The lower-level broker method is also available:

```js
await window.CanopyModule.perform('source.attachments.read', {
  attachment_id: 'FILE_ID',
  mode: 'text' // text, json, base64, or data_url
});
```

Initial browser-runtime limits:

- text/JSON reads: 1 MB
- base64/data URL reads: 4 MB
- attachment list: first 32 source-bound attachments

These limits are deliberately conservative. Larger streaming/chunked scientific data should be designed as a separate reviewed capability rather than relaxing the module iframe CSP.

### WebGL rendering capability

Default: **WebGL is blocked for ordinary modules**.

Modules that need GPU-backed rendering, such as molecular isosurfaces, volumetric data views, large point clouds, or scientific 3D scenes, should declare `module.render.webgl` explicitly. The host treats it as a reviewed rendering capability:

- no raw `fetch()` is granted
- no `allow-same-origin` is added
- no Canopy API credential is exposed
- the module still uses brokered APIs for source context and storage
- the operator must approve the elevated rendering capability before the iframe is started for that session

Declare the capability in the module bundle:

```html
<meta name="canopy-module-required-capabilities" content="module.render.webgl">
```

or in an inert JSON declaration:

```html
<script type="application/json" data-canopy-module-manifest>
{
  "capabilities": {
    "required": ["module.render.webgl"]
  }
}
</script>
```

Inside the module, feature-detect the grant before relying on WebGL:

```js
const webglEnabled = !!(
  window.CanopyModule
  && window.CanopyModule.render
  && window.CanopyModule.render.webgl
  && window.CanopyModule.render.webgl.enabled
);
```

If `module.render.webgl` is not granted, the runtime makes `canvas.getContext('webgl')`, `webgl2`, and `experimental-webgl` return `null` while leaving 2D canvas available.

---

## Trust, Privacy, And Approval

### Core rule
A module does not inherit the full trust of the post author.

A module is evaluated on:
- bundle provenance
- manifest declaration
- granted capabilities
- surface context
- user trust posture

### Approval tiers
- `none`
- `recommended`
- `required`

### Audit requirements
Every low-risk action should log:
- module key
- source/station id
- actor user id
- timestamp
- method
- params summary
- approval state

### Trust posture examples
- unknown module bundle: view-only until approved
- local signed module: granted declared low-risk capabilities
- peer-shared module from untrusted peer: blocked or narrowed

---

## Packaging And Review

### v1 authoring model
A module bundle should be:
- built offline
- packaged as one reviewed HTML file
- uploaded as a real Canopy file asset
- referenced by manifest

### Review requirements
Before a bundle is runnable:
- manifest parses and validates
- file hash recorded
- size within configured bounds
- capability list reviewed
- no forbidden runtime calls or network patterns in static validation pass

### Suggested limits for v1
- max bundle size: 500 KB compressed-equivalent target, 1 MB absolute cap
- max CPU wall time before warning: browser-side watchdog
- max persistent local module storage: small per-module quota
- local module storage should remain per-user/per-mesh/per-module scoped and should not be used for secrets, keys, or mesh-visible records

---

## First Implementations

### First public wow demo
`Piano Lab Module`

It should include:
- synced lesson media reference
- playable mini keyboard
- highlighted target notes
- section loop controls
- practice progress marks
- agent coaching panel

This is the right first demo because it is:
- surprising
- safe
- understandable
- educational
- emotionally resonant

### First serious station surface
`Canopy-Security`

It should include:
- event timeline
- bounded review actions
- map/camera/media context
- operator acknowledgment flow

This is the right serious embodiment because it proves Canopy is not just a media deck.

---

## Rollout Plan

### Phase A: foundation
- module manifest parser
- bundle validation
- sandbox runtime shell
- broker skeleton
- deck open/render path

### Phase B: safe view + lesson demo
- `source.read`
- `deck.media.observe`
- `deck.media.control`
- `clipboard.write`
- `Piano Lab Module`

### Phase C: bounded write actions
- source annotations write
- station context read
- explicit approval prompts
- audit surface

### Phase D: operational station modules
- `Canopy-Security`
- scoreboard/event stations
- printer/sensor station trials

---

## Non-negotiable Product Boundary

If this slips into arbitrary code execution inside posts, it becomes novelty theater and a security problem.

If it stays:
- packaged
- typed
- capability-scoped
- audited
- trust-gated

then it becomes a real platform primitive.

---

## Implementation Recommendation

Build `Canopy Module Runtime v1` next, but keep the first implementation narrow:
- deck-only open path
- single-file HTML bundles
- brokered host API only
- no arbitrary fetch
- one polished `Piano Lab Module` as the proof

That is enough to move Canopy into a new category without losing architectural discipline.
