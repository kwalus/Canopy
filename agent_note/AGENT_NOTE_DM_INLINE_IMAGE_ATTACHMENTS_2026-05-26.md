# Agent Note: DM Inline Image Attachments From File IDs

Date: 2026-05-26
Branch: `codex/dm-inline-image-attachments-0.6.237`
Version: `0.6.237`

## Problem
Agents can retrieve Digestion figures, upload the PNG bytes successfully, and include the resulting file IDs in direct-message `attachments`, but the DM UI only rendered image previews when the attachment payload already included a `url`. API-created and agent-created DM attachments often contain `id` / `file_id` plus name/type metadata, so users saw an empty card/dash instead of the inline image. Channel rendering already tolerated this better, so the behavior looked like a DM-only rendering gap.

## Implementation
- Updated `canopy/ui/templates/_messages_macros.html` so DM attachments derive an authenticated browser URL from a local file ID:
  - `attachment_url = attachment.url || /files/<local_file_id>`
  - `attachment_thumb_url = attachment.thumb_url || /files/<local_file_id>/thumb` for images
- Kept remote-large behavior intact by distinguishing local file IDs from `origin_file_id`; only local file IDs produce browser preview/download URLs.
- Extended the shared media-gallery macro used by DM layout hints so id-only images still render in grouped image layouts.
- Updated `canopy/ui/templates/messages.html` so the optimistic/live DM renderer also derives URLs from `id`, `file_id`, or `vault_file_id`, and shows a proper image preview before the next snapshot refresh.
- Updated `canopy/api/agent_instructions_data.py` and `canopy/mcp/server.py` so agents are explicitly told that uploaded images/media/documents can be attached to DMs as well as channel messages.
- Added static regression coverage in `tests/test_frontend_regressions.py` for derived DM attachment URLs and updated version references.

## User-Facing Behavior
- A DM attachment like `{ "id": "Fabc...", "name": "figure.png", "type": "image/png" }` should now render inline for logged-in users.
- PDF/document/code previews continue to use existing `/ajax/files/<id>/preview` routes when a local file ID is available.
- API raw file URLs still require API-key auth; browser UI rendering uses the existing authenticated session route `/files/<id>`.

## Compatibility / Risk
- This is intentionally a UI/template normalization patch, not a storage or permission change.
- File access is still enforced by the existing `/files/<id>` and `/files/<id>/thumb` routes.
- Remote metadata-only attachments without a local file ID continue to show the fetch/save controls rather than pretending a local preview exists.

## Verification
Recommended checks:
- `venv/bin/python scripts/check_jinja_templates.py`
- `venv/bin/python -m pytest tests/test_frontend_regressions.py -q`
- Manual: upload an image with `POST /api/v1/files/upload`, send a DM with `attachments: [{"id":"<file_id>","name":"figure.png","type":"image/png"}]`, and confirm the image renders inline in Messages and Deck Inbox.
