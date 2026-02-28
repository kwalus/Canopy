"""File access authorization helpers.

Centralizes content-scoped checks so file downloads in UI/API consistently
respect channel membership, post visibility, and DM recipient constraints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FileAccessEvidence:
    source_type: str
    source_id: str
    detail: str
    can_view: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_type': self.source_type,
            'source_id': self.source_id,
            'detail': self.detail,
            'can_view': self.can_view,
        }


@dataclass
class FileAccessResult:
    allowed: bool
    reason: str
    evidences: List[FileAccessEvidence] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'allowed': self.allowed,
            'reason': self.reason,
            'evidence': [e.to_dict() for e in self.evidences],
        }


def _contains_file_reference(text: Optional[str], file_id: str) -> bool:
    if not text:
        return False
    target = f"/files/{file_id}"
    return target in str(text)


def _metadata_contains_file(metadata: Optional[Dict[str, Any]], file_id: str) -> bool:
    if not isinstance(metadata, dict):
        return False
    attachments = metadata.get('attachments') or []
    if not isinstance(attachments, list):
        return False
    for att in attachments:
        if not isinstance(att, dict):
            continue
        if (att.get('id') == file_id) or (att.get('file_id') == file_id):
            return True
    return False


def _parse_json_blob(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed
    except Exception:
        return default


def _is_dm_visible_to_user(row: Dict[str, Any], user_id: str) -> bool:
    if row.get('sender_id') == user_id or row.get('recipient_id') == user_id:
        return True

    # Group DMs carry explicit membership in metadata.group_members.
    meta = _parse_json_blob(row.get('metadata'), {})
    if isinstance(meta, dict):
        members = meta.get('group_members') or []
        if isinstance(members, list) and user_id in members:
            return True

    return False


def evaluate_file_access(
    *,
    db_manager: Any,
    file_id: str,
    viewer_user_id: str,
    file_uploaded_by: Optional[str] = None,
    is_admin: bool = False,
    trust_manager: Optional[Any] = None,
    feed_manager: Optional[Any] = None,
    max_evidence: int = 25,
) -> FileAccessResult:
    """Evaluate whether a user can access a file based on referencing content.

    The function is deny-by-default: every return path that does not find
    explicit positive evidence returns ``allowed=False``.  Callers must not
    interpret a missing/ambiguous result as a grant.
    """
    evidences: List[FileAccessEvidence] = []

    if not file_id or not viewer_user_id:
        return FileAccessResult(False, 'missing-identity')

    # Explicit guard: a missing db_manager must never silently allow access.
    if db_manager is None:
        return FileAccessResult(False, 'missing-db')

    if is_admin:
        return FileAccessResult(True, 'admin', evidences)

    if file_uploaded_by and file_uploaded_by == viewer_user_id:
        return FileAccessResult(True, 'owner', evidences)

    trust_cache: Dict[str, int] = {}

    def _trust_for_author(author_id: Optional[str]) -> int:
        if not author_id:
            return 50
        if author_id in trust_cache:
            return trust_cache[author_id]
        score = 50
        if trust_manager:
            try:
                score = int(trust_manager.get_trust_score(author_id))
            except Exception:
                score = 50
        trust_cache[author_id] = score
        return score

    try:
        with db_manager.get_connection() as conn:
            # Channel messages (attachments + content references)
            channel_rows = conn.execute(
                """
                SELECT id, channel_id, attachments, content
                FROM channel_messages
                WHERE attachments LIKE ? OR content LIKE ?
                """,
                (f'%{file_id}%', f'%/files/{file_id}%')
            ).fetchall()
            for row in channel_rows:
                attachments = _parse_json_blob(row['attachments'], [])
                referenced = False
                if isinstance(attachments, list):
                    for att in attachments:
                        if not isinstance(att, dict):
                            continue
                        if att.get('id') == file_id or att.get('file_id') == file_id:
                            referenced = True
                            break
                if not referenced and not _contains_file_reference(row['content'], file_id):
                    continue

                member = conn.execute(
                    "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (row['channel_id'], viewer_user_id)
                ).fetchone()
                can_view = bool(member)
                evidences.append(FileAccessEvidence(
                    source_type='channel_message',
                    source_id=row['id'],
                    detail=f"channel:{row['channel_id']}",
                    can_view=can_view,
                ))
                if can_view:
                    return FileAccessResult(True, 'channel-membership', evidences[:max_evidence])

            # Feed posts (metadata + content references)
            feed_rows = conn.execute(
                """
                SELECT id, author_id, metadata, content
                FROM feed_posts
                WHERE metadata LIKE ? OR content LIKE ?
                """,
                (f'%{file_id}%', f'%/files/{file_id}%')
            ).fetchall()
            for row in feed_rows:
                meta = _parse_json_blob(row['metadata'], {})
                referenced = _metadata_contains_file(meta, file_id)
                if not referenced and not _contains_file_reference(row['content'], file_id):
                    continue

                can_view = False
                if feed_manager:
                    try:
                        post = feed_manager.get_post(row['id'])
                        if post:
                            can_view = bool(post.can_view(viewer_user_id, _trust_for_author(post.author_id)))
                    except Exception:
                        can_view = False
                evidences.append(FileAccessEvidence(
                    source_type='feed_post',
                    source_id=row['id'],
                    detail=f"author:{row['author_id']}",
                    can_view=can_view,
                ))
                if can_view:
                    return FileAccessResult(True, 'feed-visibility', evidences[:max_evidence])

            # Direct messages / group DMs
            dm_rows = conn.execute(
                """
                SELECT id, sender_id, recipient_id, metadata, content
                FROM messages
                WHERE metadata LIKE ? OR content LIKE ?
                """,
                (f'%{file_id}%', f'%/files/{file_id}%')
            ).fetchall()
            for row in dm_rows:
                meta = _parse_json_blob(row['metadata'], {})
                referenced = _metadata_contains_file(meta, file_id)
                if not referenced and not _contains_file_reference(row['content'], file_id):
                    continue

                row_dict = {
                    'sender_id': row['sender_id'],
                    'recipient_id': row['recipient_id'],
                    'metadata': row['metadata'],
                }
                can_view = _is_dm_visible_to_user(row_dict, viewer_user_id)
                evidences.append(FileAccessEvidence(
                    source_type='direct_message',
                    source_id=row['id'],
                    detail=f"sender:{row['sender_id']} recipient:{row['recipient_id']}",
                    can_view=can_view,
                ))
                if can_view:
                    return FileAccessResult(True, 'direct-message-visibility', evidences[:max_evidence])

    except Exception:
        return FileAccessResult(False, 'lookup-error', evidences[:max_evidence])

    # Deny-by-default: no positive evidence was found.  All branches below
    # must remain denials so that new code paths cannot accidentally grant
    # access by falling through to this point.
    if evidences:
        return FileAccessResult(False, 'no-visible-reference', evidences[:max_evidence])
    return FileAccessResult(False, 'unreferenced', evidences)
