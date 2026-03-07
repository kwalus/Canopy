"""
Identity portability manager (Phase 1).

Phase 1 scope:
- Principal metadata portability across trusted peers
- Explicit bootstrap grants for recognition/linking
- Strictly additive, feature-flagged, backward-compatible

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

logger = logging.getLogger('canopy.identity_portability')


class IdentityPortabilityManager:
    """Manage distributed principal metadata and bootstrap grants."""

    CAPABILITY = 'identity_portability_v1'
    SCHEMA_VERSION = 1
    MAX_GRANT_HOURS = 24 * 14
    LOCAL_ONLY_PRINCIPAL_METADATA_KEYS = frozenset({
        'local_user_id',
        'local_account_id',
    })
    LOCAL_ONLY_PRINCIPAL_KEY_METADATA_KEYS = frozenset({
        'local_user_id',
        'local_account_id',
        'user_id',
        'account_id',
    })

    def __init__(self, db_manager: Any, config: Any, p2p_manager: Optional[Any] = None):
        self.db = db_manager
        self.config = config
        self.p2p_manager = p2p_manager
        self.enabled = bool(
            getattr(getattr(config, 'security', None), 'identity_portability_enabled', False)
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _iso(cls, dt: datetime) -> str:
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    @classmethod
    def _parse_ts(cls, value: Any) -> Optional[datetime]:
        raw = str(value or '').strip()
        if not raw:
            return None
        try:
            if raw.endswith('Z'):
                raw = raw[:-1] + '+00:00'
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _canonical_json(data: dict[str, Any]) -> bytes:
        return json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')

    @staticmethod
    def _normalize_role(value: Optional[str]) -> str:
        role = str(value or '').strip().lower()
        return 'user' if role != 'user' else role

    def _local_peer_id(self) -> str:
        try:
            if self.p2p_manager:
                return str(self.p2p_manager.get_peer_id() or '').strip()
        except Exception:
            pass
        return ''

    def _local_identity(self) -> Optional[Any]:
        if not self.p2p_manager:
            return None
        try:
            if getattr(self.p2p_manager, 'local_identity', None):
                return self.p2p_manager.local_identity
            id_mgr = getattr(self.p2p_manager, 'identity_manager', None)
            if id_mgr:
                return getattr(id_mgr, 'local_identity', None)
        except Exception:
            return None
        return None

    def _peer_identity(self, peer_id: str) -> Optional[Any]:
        if not self.p2p_manager or not peer_id:
            return None
        try:
            local_peer = self._local_peer_id()
            local_identity = self._local_identity()
            if peer_id == local_peer and local_identity is not None:
                return local_identity
            id_mgr = getattr(self.p2p_manager, 'identity_manager', None)
            if not id_mgr:
                return None
            return id_mgr.get_peer(peer_id)
        except Exception:
            return None

    def _verify_payload_signature(
        self,
        payload: dict[str, Any],
        signature_hex: str,
        issuer_peer_id: str,
    ) -> bool:
        try:
            signature = bytes.fromhex((signature_hex or '').strip())
        except Exception:
            return False

        peer_identity = self._peer_identity(str(issuer_peer_id or '').strip())
        if not peer_identity:
            return False
        try:
            return bool(peer_identity.verify(self._canonical_json(payload), signature))
        except Exception:
            return False

    def _sign_payload(self, payload: dict[str, Any]) -> str:
        local_identity = self._local_identity()
        if not local_identity:
            raise RuntimeError("Local peer identity unavailable; cannot sign bootstrap grant")
        signature = cast(bytes, local_identity.sign(self._canonical_json(payload)))
        return signature.hex()

    def _peer_supports_capability(self, peer_id: str) -> bool:
        if not peer_id or not self.p2p_manager:
            return False
        try:
            return bool(self.p2p_manager.peer_supports_capability(peer_id, self.CAPABILITY))
        except Exception:
            return False

    @staticmethod
    def _row_get(row: Any, key: str, default: Any = None) -> Any:
        if row is None:
            return default
        try:
            if hasattr(row, 'keys') and key in row.keys():
                return row[key]
        except Exception:
            pass
        if isinstance(row, dict):
            return row.get(key, default)
        return default

    def _sanitize_principal_for_mesh(self, principal: Optional[dict[str, Any]]) -> dict[str, Any]:
        clean = dict(principal or {})
        metadata = clean.get('metadata')
        if not isinstance(metadata, dict):
            clean['metadata'] = {}
            return clean
        safe_metadata: dict[str, Any] = {}
        for raw_key, value in metadata.items():
            key = str(raw_key or '').strip()
            if not key:
                continue
            if key in self.LOCAL_ONLY_PRINCIPAL_METADATA_KEYS or key.startswith('local_'):
                continue
            safe_metadata[key] = value
        clean['metadata'] = safe_metadata
        return clean

    def _sanitize_principal_keys_for_mesh(
        self,
        keys: Optional[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for raw_key in keys or []:
            if not isinstance(raw_key, dict):
                continue
            clean_key = dict(raw_key)
            metadata = clean_key.get('metadata')
            if not isinstance(metadata, dict):
                clean_key['metadata'] = {}
                sanitized.append(clean_key)
                continue
            safe_metadata: dict[str, Any] = {}
            for raw_meta_key, value in metadata.items():
                meta_key = str(raw_meta_key or '').strip()
                if not meta_key:
                    continue
                if (
                    meta_key in self.LOCAL_ONLY_PRINCIPAL_KEY_METADATA_KEYS
                    or meta_key.startswith('local_')
                ):
                    continue
                safe_metadata[meta_key] = value
            clean_key['metadata'] = safe_metadata
            sanitized.append(clean_key)
        return sanitized

    def _list_capable_connected_peers(self) -> list[str]:
        if not self.p2p_manager:
            return []
        peers: list[str] = []
        try:
            for peer in self.p2p_manager.get_connected_peers() or []:
                pid = str(peer or '').strip()
                if pid and self._peer_supports_capability(pid):
                    peers.append(pid)
        except Exception:
            return []
        return peers

    def _audit(
        self,
        action: str,
        principal_id: Optional[str] = None,
        grant_id: Optional[str] = None,
        source_peer: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self.enabled:
            return
        payload = json.dumps(details or {}, sort_keys=True) if details else None
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO mesh_principal_audit_log (
                        principal_id, grant_id, action, source_peer, actor_user_id, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        principal_id,
                        grant_id,
                        str(action or '').strip() or 'unknown',
                        str(source_peer or '').strip() or None,
                        actor_user_id,
                        payload,
                        self._iso(self._utcnow()),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug("Identity audit insert failed: %s", e)

    def _normalize_grant_status(self, status: Optional[str], expires_at: Optional[datetime]) -> str:
        st = str(status or 'active').strip().lower()
        now = self._utcnow()
        if st == 'revoked':
            return 'revoked'
        if expires_at and expires_at <= now:
            return 'expired'
        if st in {'consumed', 'expired'}:
            return st
        return 'active'

    def _coerce_grant_payload(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize inbound grant payload to the canonical signed schema."""
        payload = dict(raw or {})
        grant_id = str(payload.get('grant_id') or '').strip()
        principal_id = str(payload.get('principal_id') or '').strip()
        if not grant_id or not principal_id:
            raise ValueError("grant_id and principal_id are required")

        max_uses = int(payload.get('max_uses') or 1)
        if max_uses < 1:
            max_uses = 1
        if max_uses > 50:
            max_uses = 50

        audience_peer_raw = str(payload.get('audience_peer') or '').strip()
        issuer_peer_id = str(payload.get('issuer_peer_id') or '').strip()
        created_by_principal_id = str(
            payload.get('created_by_principal_id') or payload.get('created_by') or ''
        ).strip()
        expires_at = self._parse_ts(payload.get('expires_at'))
        issued_at = self._parse_ts(payload.get('issued_at')) or self._utcnow()

        if not issuer_peer_id:
            raise ValueError("issuer_peer_id is required")
        if not created_by_principal_id:
            raise ValueError("created_by_principal_id is required")
        if not expires_at:
            raise ValueError("expires_at is required")

        return {
            'version': int(payload.get('version') or self.SCHEMA_VERSION),
            'grant_id': grant_id,
            'principal_id': principal_id,
            'granted_role': self._normalize_role(payload.get('granted_role')),
            'audience_peer': audience_peer_raw or None,
            'max_uses': max_uses,
            'expires_at': self._iso(expires_at),
            'created_by_principal_id': created_by_principal_id,
            'issuer_peer_id': issuer_peer_id,
            'issued_at': self._iso(issued_at),
        }

    def _validate_grant_payload(
        self,
        payload: dict[str, Any],
        signature: str,
        source_peer: Optional[str] = None,
    ) -> tuple[bool, str]:
        issuer_peer_id = str(payload.get('issuer_peer_id') or '').strip()
        if not issuer_peer_id:
            return False, 'issuer_missing'

        # Issuer identity validation: source peer must match issuer when available.
        source_peer_id = str(source_peer or '').strip()
        if source_peer_id and source_peer_id != issuer_peer_id:
            return False, 'issuer_source_mismatch'

        if not self._verify_payload_signature(payload, signature, issuer_peer_id):
            return False, 'bad_signature'

        expires_at = self._parse_ts(payload.get('expires_at'))
        if not expires_at:
            return False, 'expires_missing'
        if expires_at <= self._utcnow():
            return False, 'expired'

        role = self._normalize_role(payload.get('granted_role'))
        if role != 'user':
            return False, 'role_not_allowed'

        audience = str(payload.get('audience_peer') or '').strip()
        local_peer = self._local_peer_id()
        if audience:
            if not local_peer:
                return False, 'local_peer_unavailable'
            if audience != local_peer:
                return False, 'audience_mismatch'

        return True, 'ok'

    def _upsert_principal(
        self,
        principal_id: str,
        display_name: Optional[str],
        origin_peer: str,
        status: str = 'active',
        metadata: Optional[dict[str, Any]] = None,
        updated_at: Optional[datetime] = None,
        source_peer: Optional[str] = None,
    ) -> dict[str, Any]:
        """Upsert principal with deterministic conflict policy."""
        principal_id = str(principal_id or '').strip()
        if not principal_id:
            raise ValueError("principal_id required")
        origin_peer = str(origin_peer or '').strip() or (source_peer or '')
        if not origin_peer:
            raise ValueError("origin_peer required")

        incoming_updated = updated_at or self._utcnow()
        incoming_updated_iso = self._iso(incoming_updated)
        status_clean = str(status or 'active').strip().lower() or 'active'
        metadata_json = json.dumps(metadata or {}, sort_keys=True) if metadata else None
        created = False
        changed = False
        conflict = None

        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT principal_id, display_name, origin_peer, status, metadata_json, updated_at
                FROM mesh_principals
                WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchone()

            if not row:
                conn.execute(
                    """
                    INSERT INTO mesh_principals (
                        principal_id, display_name, origin_peer, status, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        principal_id,
                        display_name,
                        origin_peer,
                        status_clean,
                        metadata_json,
                        incoming_updated_iso,
                        incoming_updated_iso,
                    ),
                )
                created = True
                changed = True
            else:
                existing_updated = self._parse_ts(row['updated_at']) or datetime(1970, 1, 1, tzinfo=timezone.utc)
                existing_origin = str(row['origin_peer'] or '').strip()
                chosen_origin = existing_origin

                if existing_origin and origin_peer and existing_origin != origin_peer:
                    # Conflict policy: origin_peer is immutable once set.
                    conflict = {
                        'type': 'origin_peer_mismatch',
                        'existing_origin_peer': existing_origin,
                        'incoming_origin_peer': origin_peer,
                    }
                elif not existing_origin and origin_peer:
                    chosen_origin = origin_peer

                should_update = incoming_updated >= existing_updated
                next_display_name = row['display_name']
                next_status = row['status']
                next_metadata_json = row['metadata_json']
                next_updated_iso = row['updated_at']

                if should_update:
                    next_display_name = display_name if display_name is not None else row['display_name']
                    next_status = status_clean
                    next_metadata_json = metadata_json if metadata_json is not None else row['metadata_json']
                    next_updated_iso = incoming_updated_iso

                if (
                    next_display_name != row['display_name']
                    or next_status != row['status']
                    or next_metadata_json != row['metadata_json']
                    or chosen_origin != row['origin_peer']
                    or next_updated_iso != row['updated_at']
                ):
                    conn.execute(
                        """
                        UPDATE mesh_principals
                        SET display_name = ?, origin_peer = ?, status = ?, metadata_json = ?, updated_at = ?
                        WHERE principal_id = ?
                        """,
                        (
                            next_display_name,
                            chosen_origin,
                            next_status,
                            next_metadata_json,
                            next_updated_iso,
                            principal_id,
                        ),
                    )
                    changed = True

            conn.commit()

        if conflict:
            self._audit(
                action='principal_conflict',
                principal_id=principal_id,
                source_peer=source_peer,
                details=conflict,
            )

        return {
            'principal_id': principal_id,
            'created': created,
            'changed': changed,
            'conflict': conflict,
        }

    def _upsert_principal_key(
        self,
        principal_id: str,
        key_type: str,
        key_data: str,
        key_id: Optional[str] = None,
        revoked_at: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        principal_id = str(principal_id or '').strip()
        key_type = str(key_type or '').strip().lower()
        key_data = str(key_data or '').strip()
        if not principal_id or not key_type or not key_data:
            raise ValueError("principal_id, key_type, key_data required")

        kid = str(key_id or '').strip() or f"MPK{secrets.token_hex(12)}"
        metadata_json = json.dumps(metadata or {}, sort_keys=True) if metadata else None
        with self.db.get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM mesh_principal_keys
                WHERE principal_id = ? AND key_type = ? AND key_data = ?
                LIMIT 1
                """,
                (principal_id, key_type, key_data),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE mesh_principal_keys
                    SET revoked_at = COALESCE(?, revoked_at),
                        metadata_json = COALESCE(?, metadata_json)
                    WHERE id = ?
                    """,
                    (revoked_at, metadata_json, existing['id']),
                )
                conn.commit()
                return str(existing['id'])

            conn.execute(
                """
                INSERT OR REPLACE INTO mesh_principal_keys (
                    id, principal_id, key_type, key_data, created_at, revoked_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kid,
                    principal_id,
                    key_type,
                    key_data,
                    self._iso(self._utcnow()),
                    revoked_at,
                    metadata_json,
                ),
            )
            conn.commit()
        return kid

    def _serialize_principal(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        metadata_json = data.get('metadata_json')
        metadata = {}
        if metadata_json:
            try:
                metadata = json.loads(metadata_json)
            except Exception:
                metadata = {}
        data['metadata'] = metadata
        return {
            'principal_id': data.get('principal_id'),
            'display_name': data.get('display_name'),
            'origin_peer': data.get('origin_peer'),
            'status': data.get('status'),
            'created_at': data.get('created_at'),
            'updated_at': data.get('updated_at'),
            'metadata': metadata,
        }

    def _serialize_grant(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        payload = {}
        try:
            payload = json.loads(data.get('payload_json') or '{}')
        except Exception:
            payload = {}
        data['payload'] = payload
        return {
            'grant_id': data.get('grant_id'),
            'principal_id': data.get('principal_id'),
            'granted_role': data.get('granted_role'),
            'audience_peer': data.get('audience_peer'),
            'max_uses': int(data.get('max_uses') or 1),
            'uses_consumed': int(data.get('uses_consumed') or 0),
            'expires_at': data.get('expires_at'),
            'created_by': data.get('created_by'),
            'issuer_peer_id': data.get('issuer_peer_id'),
            'issued_at': data.get('issued_at'),
            'status': data.get('status'),
            'revoked_at': data.get('revoked_at'),
            'revoked_reason': data.get('revoked_reason'),
            'created_at': data.get('created_at'),
            'updated_at': data.get('updated_at'),
            'signature': data.get('signature'),
            'payload': payload,
        }

    def _sync_principal_to_mesh(self, principal_id: str, only_peer: Optional[str] = None) -> int:
        if not self.enabled or not self.p2p_manager:
            return 0
        peers = [str(only_peer or '').strip()] if only_peer else self._list_capable_connected_peers()
        peers = [peer_id for peer_id in peers if peer_id and self._peer_supports_capability(peer_id)]
        if not peers:
            return 0
        snapshot = self.get_principal_snapshot(principal_id)
        if not snapshot:
            return 0
        principal_payload = self._sanitize_principal_for_mesh(snapshot.get('principal') or {})
        key_payload = self._sanitize_principal_keys_for_mesh(snapshot.get('keys') or [])
        sent = 0
        for peer_id in peers:
            try:
                if self.p2p_manager.send_principal_announce(
                    to_peer=peer_id,
                    principal=principal_payload,
                    keys=key_payload,
                ):
                    sent += 1
            except Exception:
                continue
        return sent

    def _sync_grant_to_mesh(self, grant_id: str, only_peer: Optional[str] = None) -> int:
        if not self.enabled or not self.p2p_manager:
            return 0
        grant = self.get_grant(grant_id)
        if not grant:
            return 0
        peers = [only_peer] if only_peer else self._list_capable_connected_peers()
        peers = [p for p in peers if p]
        sent = 0
        for peer_id in peers:
            if not self._peer_supports_capability(peer_id):
                continue
            try:
                if self.p2p_manager.send_bootstrap_grant_sync(peer_id, grant):
                    sent += 1
            except Exception:
                continue
        return sent

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status_snapshot(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                'enabled': False,
                'capability': self.CAPABILITY,
                'local_peer_id': self._local_peer_id() or None,
            }
        self.reconcile_expired_grants()
        with self.db.get_connection() as conn:
            principals = conn.execute("SELECT COUNT(*) AS c FROM mesh_principals").fetchone()
            links = conn.execute("SELECT COUNT(*) AS c FROM mesh_principal_links").fetchone()
            grants = conn.execute(
                "SELECT status, COUNT(*) AS c FROM mesh_bootstrap_grants GROUP BY status"
            ).fetchall()
            revocations = conn.execute(
                "SELECT COUNT(*) AS c FROM mesh_bootstrap_grant_revocations"
            ).fetchone()
            recent_audit = conn.execute(
                """
                SELECT id, principal_id, grant_id, action, source_peer, actor_user_id, created_at
                FROM mesh_principal_audit_log
                ORDER BY id DESC
                LIMIT 20
                """
            ).fetchall()

        status_counts: dict[str, int] = {}
        for row in grants or []:
            status_counts[str(row['status'] or 'unknown')] = int(row['c'] or 0)
        return {
            'enabled': True,
            'capability': self.CAPABILITY,
            'local_peer_id': self._local_peer_id() or None,
            'counts': {
                'principals': int(self._row_get(principals, 'c', 0) or 0),
                'links': int(self._row_get(links, 'c', 0) or 0),
                'grants': int(sum(status_counts.values())),
                'grants_by_status': status_counts,
                'revocations': int(self._row_get(revocations, 'c', 0) or 0),
            },
            'connected_capable_peers': self._list_capable_connected_peers(),
            'recent_audit': [dict(r) for r in (recent_audit or [])],
        }

    def list_principals(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        lim = max(1, min(int(limit or 200), 1000))
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT principal_id, display_name, origin_peer, status, created_at, updated_at, metadata_json
                FROM mesh_principals
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        return [self._serialize_principal(row) for row in rows or []]

    def list_grants(self, limit: int = 200, status: Optional[str] = None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        self.reconcile_expired_grants()
        lim = max(1, min(int(limit or 200), 1000))
        status_clean = str(status or '').strip().lower()
        with self.db.get_connection() as conn:
            if status_clean:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM mesh_bootstrap_grants
                    WHERE lower(status) = ?
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    (status_clean, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM mesh_bootstrap_grants
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
        return [self._serialize_grant(row) for row in rows or []]

    def get_principal_snapshot(self, principal_id: str) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        pid = str(principal_id or '').strip()
        if not pid:
            return None
        with self.db.get_connection() as conn:
            principal = conn.execute(
                """
                SELECT principal_id, display_name, origin_peer, status, created_at, updated_at, metadata_json
                FROM mesh_principals
                WHERE principal_id = ?
                """,
                (pid,),
            ).fetchone()
            if not principal:
                return None
            keys = conn.execute(
                """
                SELECT id, principal_id, key_type, key_data, created_at, revoked_at, metadata_json
                FROM mesh_principal_keys
                WHERE principal_id = ?
                ORDER BY created_at ASC
                """,
                (pid,),
            ).fetchall()
        return {
            'principal': self._serialize_principal(principal),
            'keys': [dict(k) for k in (keys or [])],
        }

    def get_grant(self, grant_id: str) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        gid = str(grant_id or '').strip()
        if not gid:
            return None
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM mesh_bootstrap_grants WHERE grant_id = ?",
                (gid,),
            ).fetchone()
        return self._serialize_grant(row) if row else None

    def reconcile_expired_grants(self) -> int:
        if not self.enabled:
            return 0
        now_iso = self._iso(self._utcnow())
        with self.db.get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE mesh_bootstrap_grants
                SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <= ?
                """,
                (now_iso, now_iso),
            )
            conn.commit()
            changed = int(cur.rowcount or 0)
        if changed > 0:
            self._audit(action='grant_expired_batch', details={'count': changed})
        return changed

    def ensure_local_principal(
        self,
        local_user_id: str,
        acting_user_id: Optional[str] = None,
        sync_to_mesh: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Identity portability is disabled")
        uid = str(local_user_id or '').strip()
        if not uid:
            raise ValueError("local_user_id required")

        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT mpl.principal_id, u.display_name, u.username, u.public_key
                FROM mesh_principal_links mpl
                JOIN users u ON u.id = mpl.local_user_id
                WHERE mpl.local_user_id = ?
                ORDER BY mpl.linked_at ASC
                LIMIT 1
                """,
                (uid,),
            ).fetchone()
            if row:
                principal_id = str(row['principal_id'])
                principal_snapshot = self.get_principal_snapshot(principal_id)
                return {
                    'principal_id': principal_id,
                    'created': False,
                    'principal': (principal_snapshot or {}).get('principal'),
                }

            user_row = conn.execute(
                """
                SELECT id, display_name, username, public_key
                FROM users
                WHERE id = ?
                """,
                (uid,),
            ).fetchone()
            if not user_row:
                raise ValueError(f"Unknown local user: {uid}")

        principal_id = f"PRN{secrets.token_hex(12)}"
        display_name = str(user_row['display_name'] or user_row['username'] or uid)
        origin_peer = self._local_peer_id() or 'local'
        self._upsert_principal(
            principal_id=principal_id,
            display_name=display_name,
            origin_peer=origin_peer,
            status='active',
            metadata={'source': 'local_user_link', 'local_user_id': uid},
            updated_at=self._utcnow(),
            source_peer=origin_peer,
        )

        public_key = str(user_row['public_key'] or '').strip()
        if public_key:
            try:
                self._upsert_principal_key(
                    principal_id=principal_id,
                    key_type='ed25519_public',
                    key_data=public_key,
                    metadata={'source': 'users.public_key', 'user_id': uid},
                )
            except Exception as e:
                logger.debug("Principal key insert skipped: %s", e)

        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO mesh_principal_links (
                    principal_id, local_user_id, linked_at, linked_by, source
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    principal_id,
                    uid,
                    self._iso(self._utcnow()),
                    acting_user_id or uid,
                    'local',
                ),
            )
            conn.commit()

        self._audit(
            action='principal_link_created',
            principal_id=principal_id,
            actor_user_id=acting_user_id or uid,
            details={'local_user_id': uid, 'source': 'local'},
        )

        if sync_to_mesh:
            synced = self._sync_principal_to_mesh(principal_id)
            self._audit(
                action='principal_sync_sent',
                principal_id=principal_id,
                actor_user_id=acting_user_id or uid,
                details={'sent_peers': synced},
            )

        principal_snapshot = self.get_principal_snapshot(principal_id)
        return {
            'principal_id': principal_id,
            'created': True,
            'principal': (principal_snapshot or {}).get('principal'),
        }

    def create_bootstrap_grant(
        self,
        local_user_id: str,
        *,
        acting_user_id: Optional[str] = None,
        audience_peer: Optional[str] = None,
        expires_in_hours: int = 24,
        max_uses: int = 1,
        sync_to_mesh: bool = True,
        target_peer_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Identity portability is disabled")
        local_peer_id = self._local_peer_id()
        if not local_peer_id:
            raise RuntimeError("Local peer identity unavailable; cannot create bootstrap grant")

        principal_info = self.ensure_local_principal(
            local_user_id=local_user_id,
            acting_user_id=acting_user_id or local_user_id,
            sync_to_mesh=False,
        )
        principal_id = str(principal_info['principal_id'])
        issued_at = self._utcnow()
        hours = max(1, min(int(expires_in_hours or 24), self.MAX_GRANT_HOURS))
        max_uses_clean = max(1, min(int(max_uses or 1), 50))
        grant_id = f"MGR{secrets.token_hex(10)}"

        payload = {
            'version': self.SCHEMA_VERSION,
            'grant_id': grant_id,
            'principal_id': principal_id,
            'granted_role': 'user',
            'audience_peer': str(audience_peer or '').strip() or None,
            'max_uses': max_uses_clean,
            'expires_at': self._iso(issued_at + timedelta(hours=hours)),
            'created_by_principal_id': principal_id,
            'issuer_peer_id': local_peer_id,
            'issued_at': self._iso(issued_at),
        }
        signature = self._sign_payload(payload)
        now_iso = self._iso(self._utcnow())

        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO mesh_bootstrap_grants (
                    grant_id, principal_id, granted_role, audience_peer, max_uses, uses_consumed,
                    expires_at, created_by, issuer_peer_id, issued_at, signature, payload_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, 'user', ?, ?, 0, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    grant_id,
                    principal_id,
                    payload.get('audience_peer'),
                    max_uses_clean,
                    payload['expires_at'],
                    principal_id,
                    payload['issuer_peer_id'],
                    payload['issued_at'],
                    signature,
                    json.dumps(payload, sort_keys=True),
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()

        self._audit(
            action='grant_created',
            principal_id=principal_id,
            grant_id=grant_id,
            actor_user_id=acting_user_id or local_user_id,
            source_peer=self._local_peer_id(),
            details={
                'audience_peer': payload.get('audience_peer'),
                'expires_at': payload.get('expires_at'),
                'max_uses': max_uses_clean,
            },
        )

        principal_synced = 0
        grant_synced = 0
        if sync_to_mesh:
            principal_synced = self._sync_principal_to_mesh(principal_id, only_peer=target_peer_id)
            grant_synced = self._sync_grant_to_mesh(grant_id, only_peer=target_peer_id)

        grant = self.get_grant(grant_id) or {}
        artifact = dict(payload)
        artifact['signature'] = signature
        artifact['issuer_peer_id'] = payload['issuer_peer_id']
        return {
            'grant': grant,
            'artifact': artifact,
            'synced': {
                'principal_peers': principal_synced,
                'grant_peers': grant_synced,
            },
        }

    def import_bootstrap_grant(
        self,
        artifact: dict[str, Any],
        *,
        source_peer: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        sync_to_mesh: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Identity portability is disabled")

        signature = str(artifact.get('signature') or '').strip()
        payload = self._coerce_grant_payload(artifact)
        ok, reason = self._validate_grant_payload(payload, signature, source_peer=source_peer)
        if not ok:
            self._audit(
                action='grant_import_rejected',
                principal_id=payload.get('principal_id'),
                grant_id=payload.get('grant_id'),
                source_peer=source_peer,
                actor_user_id=actor_user_id,
                details={'reason': reason},
            )
            return {'imported': False, 'reason': reason}

        principal_id = str(payload['principal_id'])
        issuer_peer_id = str(payload['issuer_peer_id'])
        self._upsert_principal(
            principal_id=principal_id,
            display_name=None,
            origin_peer=issuer_peer_id,
            source_peer=source_peer or issuer_peer_id,
            updated_at=self._parse_ts(payload.get('issued_at')) or self._utcnow(),
            metadata={'source': 'grant_import'},
        )

        now_iso = self._iso(self._utcnow())
        payload_json = json.dumps(payload, sort_keys=True)
        with self.db.get_connection() as conn:
            revoked_marker = conn.execute(
                """
                SELECT grant_id FROM mesh_bootstrap_grant_revocations
                WHERE grant_id = ?
                """,
                (payload['grant_id'],),
            ).fetchone()
            existing = conn.execute(
                """
                SELECT grant_id, uses_consumed, max_uses, status, issuer_peer_id, expires_at
                FROM mesh_bootstrap_grants
                WHERE grant_id = ?
                """,
                (payload['grant_id'],),
            ).fetchone()

            computed_status = 'revoked' if revoked_marker else 'active'
            expires_at_dt = self._parse_ts(payload.get('expires_at'))
            computed_status = self._normalize_grant_status(computed_status, expires_at_dt)

            if not existing:
                conn.execute(
                    """
                    INSERT INTO mesh_bootstrap_grants (
                        grant_id, principal_id, granted_role, audience_peer, max_uses, uses_consumed,
                        expires_at, created_by, issuer_peer_id, issued_at, signature, payload_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, 'user', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload['grant_id'],
                        principal_id,
                        payload.get('audience_peer'),
                        int(payload.get('max_uses') or 1),
                        payload['expires_at'],
                        payload['created_by_principal_id'],
                        issuer_peer_id,
                        payload['issued_at'],
                        signature,
                        payload_json,
                        computed_status,
                        now_iso,
                        now_iso,
                    ),
                )
                imported = True
            else:
                if str(existing['issuer_peer_id'] or '').strip() != issuer_peer_id:
                    conn.rollback()
                    self._audit(
                        action='grant_import_rejected',
                        principal_id=principal_id,
                        grant_id=payload.get('grant_id'),
                        source_peer=source_peer,
                        actor_user_id=actor_user_id,
                        details={'reason': 'issuer_mismatch_existing'},
                    )
                    return {'imported': False, 'reason': 'issuer_mismatch_existing'}

                existing_uses = int(existing['uses_consumed'] or 0)
                existing_max = int(existing['max_uses'] or 1)
                merged_uses = max(existing_uses, 0)
                merged_max = max(existing_max, int(payload.get('max_uses') or 1))
                if merged_uses >= merged_max:
                    computed_status = 'consumed'
                conn.execute(
                    """
                    UPDATE mesh_bootstrap_grants
                    SET principal_id = ?,
                        granted_role = 'user',
                        audience_peer = ?,
                        max_uses = ?,
                        uses_consumed = ?,
                        expires_at = ?,
                        created_by = ?,
                        issuer_peer_id = ?,
                        issued_at = ?,
                        signature = ?,
                        payload_json = ?,
                        status = ?,
                        updated_at = ?
                    WHERE grant_id = ?
                    """,
                    (
                        principal_id,
                        payload.get('audience_peer'),
                        merged_max,
                        merged_uses,
                        payload['expires_at'],
                        payload['created_by_principal_id'],
                        issuer_peer_id,
                        payload['issued_at'],
                        signature,
                        payload_json,
                        computed_status,
                        now_iso,
                        payload['grant_id'],
                    ),
                )
                imported = False

            conn.commit()

        self._audit(
            action='grant_imported' if imported else 'grant_synced',
            principal_id=principal_id,
            grant_id=payload['grant_id'],
            source_peer=source_peer or issuer_peer_id,
            actor_user_id=actor_user_id,
            details={'status': 'imported' if imported else 'updated'},
        )

        sync_count = self._sync_grant_to_mesh(payload['grant_id']) if sync_to_mesh else 0
        return {'imported': True, 'grant_id': payload['grant_id'], 'synced_peers': sync_count}

    def apply_bootstrap_grant(
        self,
        grant_id: str,
        local_user_id: str,
        *,
        actor_user_id: Optional[str] = None,
        source_peer: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Identity portability is disabled")

        gid = str(grant_id or '').strip()
        uid = str(local_user_id or '').strip()
        if not gid or not uid:
            raise ValueError("grant_id and local_user_id are required")
        self.reconcile_expired_grants()
        now_iso = self._iso(self._utcnow())

        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user_row = conn.execute(
                "SELECT id, display_name, username FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
            if not user_row:
                conn.rollback()
                return {'applied': False, 'reason': 'unknown_local_user'}

            idempotent = conn.execute(
                """
                SELECT grant_id
                FROM mesh_bootstrap_grant_applications
                WHERE grant_id = ? AND local_user_id = ?
                LIMIT 1
                """,
                (gid, uid),
            ).fetchone()
            if idempotent:
                conn.commit()
                return {'applied': True, 'idempotent': True, 'reason': 'already_applied'}

            row = conn.execute(
                "SELECT * FROM mesh_bootstrap_grants WHERE grant_id = ?",
                (gid,),
            ).fetchone()
            if not row:
                conn.rollback()
                return {'applied': False, 'reason': 'grant_not_found'}

            row_expires = self._parse_ts(row['expires_at'])
            row_status = self._normalize_grant_status(row['status'], row_expires)
            if row_status != row['status']:
                conn.execute(
                    "UPDATE mesh_bootstrap_grants SET status = ?, updated_at = ? WHERE grant_id = ?",
                    (row_status, now_iso, gid),
                )
            if row_status == 'revoked':
                conn.commit()
                return {'applied': False, 'reason': 'revoked'}
            if row_status == 'expired':
                conn.commit()
                return {'applied': False, 'reason': 'expired'}
            if row_status == 'consumed':
                conn.commit()
                return {'applied': False, 'reason': 'grant_consumed'}

            payload = {}
            try:
                payload = json.loads(row['payload_json'] or '{}')
            except Exception:
                payload = {}
            signature = str(row['signature'] or '').strip()
            normalized_payload = self._coerce_grant_payload(payload)
            ok, reason = self._validate_grant_payload(
                normalized_payload,
                signature,
                source_peer=source_peer or normalized_payload.get('issuer_peer_id'),
            )
            if not ok:
                conn.rollback()
                self._audit(
                    action='grant_apply_rejected',
                    principal_id=normalized_payload.get('principal_id'),
                    grant_id=gid,
                    source_peer=source_peer,
                    actor_user_id=actor_user_id or uid,
                    details={'reason': reason},
                )
                return {'applied': False, 'reason': reason}

            max_uses = int(row['max_uses'] or normalized_payload.get('max_uses') or 1)
            uses = int(row['uses_consumed'] or 0)
            if uses >= max_uses:
                next_status = 'consumed'
                conn.execute(
                    """
                    UPDATE mesh_bootstrap_grants
                    SET status = ?, updated_at = ?
                    WHERE grant_id = ?
                    """,
                    (next_status, now_iso, gid),
                )
                conn.commit()
                return {'applied': False, 'reason': 'grant_consumed'}

            principal_id = str(normalized_payload['principal_id'])
            principal = conn.execute(
                """
                SELECT principal_id, display_name, origin_peer
                FROM mesh_principals WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchone()
            if not principal:
                # Ensure principal exists for link target without creating auth semantics.
                fallback_name = str(user_row['display_name'] or user_row['username'] or uid)
                conn.execute(
                    """
                    INSERT INTO mesh_principals (
                        principal_id, display_name, origin_peer, status, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        principal_id,
                        fallback_name,
                        str(normalized_payload.get('issuer_peer_id') or '').strip() or 'unknown',
                        now_iso,
                        now_iso,
                        json.dumps({'source': 'grant_apply_placeholder'}, sort_keys=True),
                    ),
                )

            conn.execute(
                """
                INSERT OR IGNORE INTO mesh_principal_links (
                    principal_id, local_user_id, linked_at, linked_by, source
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    principal_id,
                    uid,
                    now_iso,
                    actor_user_id or uid,
                    'grant_apply',
                ),
            )
            conn.execute(
                """
                INSERT INTO mesh_bootstrap_grant_applications (
                    grant_id, local_user_id, applied_at, applied_by, source_peer
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    gid,
                    uid,
                    now_iso,
                    actor_user_id or uid,
                    source_peer,
                ),
            )

            uses_after = uses + 1
            status_after = 'consumed' if uses_after >= max_uses else 'active'
            conn.execute(
                """
                UPDATE mesh_bootstrap_grants
                SET uses_consumed = ?, status = ?, updated_at = ?
                WHERE grant_id = ?
                """,
                (uses_after, status_after, now_iso, gid),
            )
            conn.commit()

        self._audit(
            action='grant_applied',
            principal_id=principal_id,
            grant_id=gid,
            source_peer=source_peer,
            actor_user_id=actor_user_id or uid,
            details={'local_user_id': uid, 'uses_after': uses_after, 'status_after': status_after},
        )
        self._sync_grant_to_mesh(gid)
        return {
            'applied': True,
            'principal_id': principal_id,
            'local_user_id': uid,
            'uses_consumed': uses_after,
            'status': status_after,
        }

    def revoke_bootstrap_grant(
        self,
        grant_id: str,
        *,
        actor_user_id: Optional[str] = None,
        reason: Optional[str] = None,
        sync_to_mesh: bool = True,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Identity portability is disabled")
        gid = str(grant_id or '').strip()
        if not gid:
            raise ValueError("grant_id is required")

        now_iso = self._iso(self._utcnow())
        local_peer = self._local_peer_id()
        reason_clean = str(reason or '').strip() or 'revoked_by_admin'
        if not local_peer:
            return {'revoked': False, 'reason': 'local_peer_unavailable'}
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT grant_id, issuer_peer_id, principal_id, status FROM mesh_bootstrap_grants WHERE grant_id = ?",
                (gid,),
            ).fetchone()
            if not row:
                return {'revoked': False, 'reason': 'grant_not_found'}

            issuer_peer_id = str(row['issuer_peer_id'] or '').strip()
            if issuer_peer_id and local_peer and issuer_peer_id != local_peer:
                return {'revoked': False, 'reason': 'issuer_mismatch_local'}

            conn.execute(
                """
                UPDATE mesh_bootstrap_grants
                SET status = 'revoked', revoked_at = ?, revoked_reason = ?, updated_at = ?
                WHERE grant_id = ?
                """,
                (now_iso, reason_clean, now_iso, gid),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO mesh_bootstrap_grant_revocations (
                    grant_id, issuer_peer_id, revoked_at, reason, created_at
                ) VALUES (?, ?, ?, ?, COALESCE(
                    (SELECT created_at FROM mesh_bootstrap_grant_revocations WHERE grant_id = ?),
                    ?
                ))
                """,
                (gid, issuer_peer_id or local_peer or 'unknown', now_iso, reason_clean, gid, now_iso),
            )
            conn.commit()
            principal_id = str(row['principal_id'] or '').strip() or None

        self._audit(
            action='grant_revoked',
            principal_id=principal_id,
            grant_id=gid,
            source_peer=local_peer,
            actor_user_id=actor_user_id,
            details={'reason': reason_clean},
        )

        synced = 0
        if sync_to_mesh and self.p2p_manager:
            peers = self._list_capable_connected_peers()
            for peer_id in peers:
                try:
                    if self.p2p_manager.send_bootstrap_grant_revoke(
                        to_peer=peer_id,
                        grant_id=gid,
                        revoked_at=now_iso,
                        reason=reason_clean,
                        issuer_peer_id=local_peer,
                    ):
                        synced += 1
                except Exception:
                    continue
        return {'revoked': True, 'grant_id': gid, 'synced_peers': synced}

    # ------------------------------------------------------------------
    # P2P callbacks
    # ------------------------------------------------------------------

    def handle_principal_announce(
        self,
        principal: dict[str, Any],
        keys: list[dict[str, Any]],
        *,
        from_peer: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {'handled': False, 'reason': 'disabled'}
        principal_id = str((principal or {}).get('principal_id') or '').strip()
        if not principal_id:
            return {'handled': False, 'reason': 'principal_id_missing'}

        origin_peer = str((principal or {}).get('origin_peer') or '').strip() or str(from_peer or '').strip()
        updated_at = self._parse_ts((principal or {}).get('updated_at')) or self._utcnow()
        upsert = self._upsert_principal(
            principal_id=principal_id,
            display_name=(principal or {}).get('display_name'),
            origin_peer=origin_peer,
            status=(principal or {}).get('status') or 'active',
            metadata=(principal or {}).get('metadata') if isinstance((principal or {}).get('metadata'), dict) else None,
            updated_at=updated_at,
            source_peer=from_peer,
        )
        imported_keys = 0
        for key in keys or []:
            try:
                self._upsert_principal_key(
                    principal_id=principal_id,
                    key_type=str(key.get('key_type') or ''),
                    key_data=str(key.get('key_data') or ''),
                    key_id=key.get('id'),
                    revoked_at=key.get('revoked_at'),
                    metadata=key.get('metadata') if isinstance(key.get('metadata'), dict) else None,
                )
                imported_keys += 1
            except Exception:
                continue

        self._audit(
            action='principal_announced',
            principal_id=principal_id,
            source_peer=from_peer,
            details={'imported_keys': imported_keys, 'upsert': upsert},
        )
        return {'handled': True, 'principal_id': principal_id, 'imported_keys': imported_keys}

    def handle_principal_key_update(
        self,
        principal_id: str,
        key: dict[str, Any],
        *,
        from_peer: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {'handled': False, 'reason': 'disabled'}
        pid = str(principal_id or '').strip()
        if not pid:
            return {'handled': False, 'reason': 'principal_id_missing'}
        try:
            key_id = self._upsert_principal_key(
                principal_id=pid,
                key_type=str((key or {}).get('key_type') or ''),
                key_data=str((key or {}).get('key_data') or ''),
                key_id=(key or {}).get('id'),
                revoked_at=(key or {}).get('revoked_at'),
                metadata=(key or {}).get('metadata') if isinstance((key or {}).get('metadata'), dict) else None,
            )
        except Exception as e:
            return {'handled': False, 'reason': f'key_upsert_failed:{e}'}
        self._audit(
            action='principal_key_updated',
            principal_id=pid,
            source_peer=from_peer,
            details={'key_id': key_id},
        )
        return {'handled': True, 'principal_id': pid, 'key_id': key_id}

    def handle_bootstrap_grant_sync(
        self,
        grant: dict[str, Any],
        *,
        from_peer: Optional[str] = None,
        actor_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {'handled': False, 'reason': 'disabled'}
        result = self.import_bootstrap_grant(
            artifact=grant or {},
            source_peer=from_peer,
            actor_user_id=actor_user_id,
            sync_to_mesh=False,
        )
        return {'handled': bool(result.get('imported')), **result}

    def handle_bootstrap_grant_revoke(
        self,
        grant_id: str,
        revoked_at: str,
        reason: Optional[str],
        issuer_peer_id: Optional[str],
        *,
        from_peer: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {'handled': False, 'reason': 'disabled'}
        gid = str(grant_id or '').strip()
        if not gid:
            return {'handled': False, 'reason': 'grant_id_missing'}
        issuer = str(issuer_peer_id or '').strip() or str(from_peer or '').strip()
        source = str(from_peer or '').strip()
        if source and issuer and source != issuer:
            return {'handled': False, 'reason': 'issuer_source_mismatch'}
        revoked_ts = self._parse_ts(revoked_at) or self._utcnow()
        revoked_iso = self._iso(revoked_ts)
        reason_clean = str(reason or '').strip() or 'revoked_remote'
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO mesh_bootstrap_grant_revocations (
                    grant_id, issuer_peer_id, revoked_at, reason, created_at
                ) VALUES (?, ?, ?, ?, COALESCE(
                    (SELECT created_at FROM mesh_bootstrap_grant_revocations WHERE grant_id = ?),
                    ?
                ))
                """,
                (gid, issuer or 'unknown', revoked_iso, reason_clean, gid, revoked_iso),
            )
            conn.execute(
                """
                UPDATE mesh_bootstrap_grants
                SET status = 'revoked', revoked_at = ?, revoked_reason = ?, updated_at = ?
                WHERE grant_id = ? AND (? = '' OR issuer_peer_id = ?)
                """,
                (revoked_iso, reason_clean, revoked_iso, gid, issuer or '', issuer or ''),
            )
            conn.commit()

        self._audit(
            action='grant_revocation_synced',
            grant_id=gid,
            source_peer=source or issuer,
            details={'issuer_peer_id': issuer, 'reason': reason_clean},
        )
        return {'handled': True, 'grant_id': gid}
