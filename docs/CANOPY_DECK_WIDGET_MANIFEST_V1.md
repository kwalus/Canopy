# Canopy Deck — Widget Manifest v1

This document describes the **sanitized widget manifest** contract used by the **Canopy Deck** when a post or message contains embeds, stream cards, or other typed deck items. It is aimed at **integrators and future feature work** (e.g. station-style surfaces), not end users.

For end-user behavior, see [QUICKSTART.md](QUICKSTART.md) (rich links and media deck) and [API_REFERENCE.md](API_REFERENCE.md) (rich media notes).

---

## Purpose

- **Safety:** Only allowlisted iframe hosts, external hosts, and callback handlers are accepted.
- **Consistency:** Every widget gets normalized **`station_surface`**, **`action_policy`**, and **`source_binding`** even if the producer omits them.
- **Trust:** The deck can show **what kind of operational surface** the user is in (policy pill + badges) without implying broader permissions than the client actually grants.

---

## Lifecycle

1. **Producers** attach JSON to DOM nodes, typically via `data-canopy-widget-manifest="..."` (e.g. rich embed HTML from `canopy-main.js`, stream cards from `channels.html`).
2. **`sanitizeDeckWidgetManifest()`** in `canopy/ui/static/js/canopy-main.js` parses and normalizes; invalid manifests are rejected (`null`).
3. **`parseDeckWidgetManifest(node)`** re-sanitizes after `JSON.parse` when building deck items from the DOM.
4. **Deck UI** renders `renderDeckStationSummary`, widget badges/details/actions, and optional iframe stage.

---

## Top-level manifest fields (after sanitize)

| Field | Notes |
|-------|--------|
| `version` | Always `1` for this contract. |
| `key` | Stable string id for selection within a source. |
| `widget_type` | One of: `map`, `chart`, `media_embed`, `story`, `media_stream`, `telemetry_panel`. |
| `render_mode` | One of: `iframe`, `card`, `stream_summary`. |
| `title` | Required; short heading. |
| `subtitle`, `body_text`, `provider_label`, `icon` | Optional UI copy; `icon` is Bootstrap Icons class (`bi-*`). |
| `embed_url`, `external_url`, `thumb_url` | URLs re-validated against allowlists. |
| `badges`, `details` | Bounded arrays for chip and key/value rows. |
| `station_surface` | See below. |
| `action_policy` | See below. |
| `source_binding` | See below. |
| `actions` | Bounded list (max 4) of allowed actions. |

---

## `station_surface`

| Field | Type | Description |
|-------|------|-------------|
| `kind` | enum | `source_bundle`, `reference_surface`, `stream_station`, `telemetry_station`, `station_surface`. Unknown values fall back to defaults per `widget_type`. |
| `domain` | enum | e.g. `media`, `sensor`, `mapping`, `market`, `general`, … |
| `label` | string | Primary station line in the deck (e.g. “Live video Surface”). |
| `summary` | string | Subtitle explaining context. |
| `recurring` | bool | `true` for live/recurring operational surfaces (e.g. streams). |
| `scope` | `source` \| `station` | Whether the surface is framed as tied to the message/post vs a broader station context. |

**Defaults:** If the producer omits `station_surface`, defaults are chosen from `widget_type` (e.g. maps → `reference_surface` / `mapping`; streams → `stream_station` or `telemetry_station`).

**Deck UI note:** For a *simple* reference surface (`kind: reference_surface`, not recurring, `scope: source`, `max_risk: view`, `human_gate: none`), the web UI **may omit** the separate **Station Surface** summary block to avoid repeating context already obvious from the map/chart stage. The manifest fields are still normalized and used elsewhere (e.g. policy). Streams and station-scoped surfaces always show the summary when relevant.

---

## `action_policy`

| Field | Type | Description |
|-------|------|-------------|
| `bounded` | bool | Intended for future policy toggles; defaults `true`. |
| `max_risk` | `view` \| `low` | Ceiling for actions: if `view`, only `risk: view` actions are kept. |
| `human_gate` | `none` \| `recommended` \| `required` | Shown as a badge when not `none` (UX hint for future flows). |
| `audit_label` | string | Short label for the policy pill (e.g. “Bounded actions”, “View-only actions”). |

---

## `source_binding`

| Field | Type | Description |
|-------|------|-------------|
| `binding_type` | string | Opaque type label (e.g. `message_attachment`). |
| `source_scope` | `source` \| `station` | Normalized scope. |
| `return_label` | string | Text for the deck **Return** control (default “Return to source”). |

---

## Actions

Each action object may include:

- `kind`: `external_link` \| `clipboard` \| `callback`
- `label`, optional `icon`
- `risk`: `view` \| `low`
- `scope`: `source` \| `station`
- `requires_confirmation`: optional bool → browser confirm before run

**`external_link`:** `url` must pass `CANOPY_DECK_EXTERNAL_HOSTS`.

**`clipboard`:** `text` length bounded.

**`callback`:** `handler` must be in the allowlist (`open_stream_workspace` today). Args are validated per handler.

**Runtime:** `canRunDeckWidgetAction(action, manifest)` enforces `action_policy.max_risk` before execution.

---

## Canonical producer: stream cards

Channel stream attachment cards in `canopy/ui/templates/channels.html` emit a full manifest including `station_surface`, `action_policy`, `source_binding`, and mixed actions (workspace callback + copy stream ID).

Use them as the reference when adding new producers.

---

## Out of scope (v1)

- Arbitrary HTML/JS widgets from untrusted authors
- New callback handlers without code review and allowlist updates
- Server-side authorization for “station” actions (future work)

---

## Related files

- `canopy/ui/static/js/canopy-main.js` — sanitization, embed `widgetManifest` builders
- `canopy/ui/templates/base.html` — deck shell, station summary block
- `canopy/ui/templates/channels.html` — stream card manifest
- `tests/test_frontend_regressions.py` — string anchors for CI
