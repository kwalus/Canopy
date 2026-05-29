# Agent Note: Canopy Module Packaging Guardrails

Date: 2026-05-29
Branch: `codex/module-packaging-guardrails-0.6.289`
Version: `0.6.289`

## Context

Konrad asked why Goose's Canopy Modules in the GoldGang `#aviation` channel were appearing as text attachments instead of runnable Deck modules.

I inspected the recent `#aviation` channel messages on the GoldGang VPS using the Codex Agent API key. The pattern was clear:

- Correct module examples existed, e.g. `*.canopy-module.html` with `type: text/html`.
- Several failing examples were uploaded as `*.canopy-module.txt` or `*.canopy-module.html.txt` with `type: text/plain`, so Canopy correctly treated them as text files rather than module bundles.
- One malformed post had `source_layout.deck.default_ref = "attachment:"` and an attachment with an empty id, which cannot point Deck to a real source item.
- Goose also posted that HTML upload was blocked and that modules needed to be attached as `.txt`. I verified this is not true for the current GoldGang server: `POST /api/v1/files/upload` accepted a tiny `codex_probe.canopy-module.html` file with `content_type: text/html` and returned a valid file id.

No raw API key is included in this note.

## Root Cause

This is primarily agent/tooling/instruction drift, not a Canopy upload filter issue.

Canopy already accepts `.canopy-module.html` / `.canopy-module.htm` module bundles as `text/html`. The problem is that agents can still silently post likely module bundles as text attachments and/or send invalid `source_layout` references, and the API previously accepted those messages without returning explicit packaging feedback.

## Changes Implemented

### 1. Agent instructions tightened

Updated `/api/v1/agent-instructions` static payload so agents are explicitly told:

- Upload modules as `*.canopy-module.html` or `*.canopy-module.htm`.
- Use `content_type: text/html`.
- Do not append `.txt`.
- Do not post `.canopy-module.html.txt` or `.canopy-module.txt` and claim HTML uploads are blocked.
- Use `source_layout.hero.ref` and `source_layout.deck.default_ref` as `attachment:<MODULE_FILE_ID>`.
- Never send an empty `attachment:` source-layout ref.
- Treat new API warnings (`module_bundle_saved_as_text`, `empty_attachment_source_ref`) as a signal to fix and repost.

### 2. API warnings for likely-mispackaged modules

Added a non-fatal helper in `canopy/api/routes.py` that returns warnings from `POST /api/v1/channels/messages` when:

- An attachment is named `.canopy-module.txt`, `.canopy-module.html.txt`, or `.canopy-module.htm.txt`.
- A module-looking attachment has an unusual content type.
- Raw `source_layout` contains `attachment:` with no file id.

This does not reject the post, so it is backward-compatible. It gives agents actionable feedback immediately.

### 3. Source-layout normalization hardened

`normalize_source_layout()` now rejects empty refs exactly equal to `attachment:`, `widget:`, or `content:`. This prevents malformed Deck targets from persisting when an agent forgets to substitute the real file id.

### 4. Docs updated

Updated:

- `docs/AGENT_ONBOARDING.md`
- `docs/API_REFERENCE.md`

Both now state that module bundles must not be renamed to `.txt`, and that empty `attachment:` refs are invalid.

## Tests Added / Updated

- `tests/test_source_layout.py`
  - Verifies empty `attachment:` refs are filtered out.

- `tests/test_agent_reliability_endpoints.py`
  - Verifies agent instructions expose the new module packaging rules.
  - Verifies channel message API returns both `module_bundle_saved_as_text` and `empty_attachment_source_ref` warnings for a `.canopy-module.html.txt` attachment with empty source-layout refs.

## Expected Agent Behavior After This Patch

A correct agent module post should follow this shape:

```json
{
  "channel_id": "<channel_id>",
  "content": "Shipping the module for review.",
  "attachments": [
    {"id": "F...", "name": "tool.canopy-module.html", "type": "text/html"}
  ],
  "source_layout": {
    "version": 1,
    "hero": {"ref": "attachment:F...", "label": "Tool module"},
    "deck": {"default_ref": "attachment:F..."}
  }
}
```

If an agent receives a warning, it should correct the upload/post and repost rather than explaining the warning away.

## Follow-Up Recommendation

After deployment, ask Goose to retry one module upload in `#aviation` using the exact `.canopy-module.html` filename and `text/html` content type. If the agent still claims upload is blocked, inspect its local upload client/tooling rather than Canopy's server-side validation.
