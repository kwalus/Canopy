"""
Contract management for Canopy.

Contracts are deterministic coordination objects extracted from inline
[contract] blocks in posts/messages. They provide explicit ownership,
participants, lifecycle state, and bounded retention.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, cast

from .database import DatabaseManager

logger = logging.getLogger('canopy.contracts')

CONTRACT_STATUSES = (
    'proposed',
    'accepted',
    'active',
    'fulfilled',
    'breached',
    'void',
    'archived',
)

_CONTRACT_BLOCK_PATTERNS = [
    re.compile(r"(?is)\[contract\](.*?)\[/contract\]"),
    re.compile(r"(?is)::contract\s*(.*?)\s*::endcontract"),
    re.compile(r"(?s)\[contract:\s*(.*?)\n\]"),
]

_MAX_CONTRACT_BLOCKS = 20
_MAX_CONTRACT_INPUT_SIZE = 1_000_000
_DEFAULT_TTL_SECONDS = 30 * 24 * 3600
_CONFIRM_FALSE = {'false', 'no', 'off', '0'}
_CONFIRM_TRUE = {'true', 'yes', 'on', '1'}


@dataclass
class ContractSpec:
    title: str
    summary: Optional[str] = None
    terms: Optional[str] = None
    owner: Optional[str] = None
    counterparties: List[str] = field(default_factory=list)
    status: Optional[str] = None
    contract_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    ttl_seconds: Optional[int] = None
    ttl_mode: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    confirmed: bool = True
    raw: Optional[str] = None
    fields: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload['expires_at'] = self.expires_at.isoformat() if self.expires_at else None
        return payload


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        raw = str(value).strip()
        if not raw:
            return None
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_ttl(value: Any) -> Optional[int]:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    try:
        if raw.isdigit():
            return int(raw)
        m = re.match(r"^(\d+)\s*([smhdw]|mo|q|y)$", raw)
        if not m:
            return None
        amount = int(m.group(1))
        unit = m.group(2)
        seconds = amount
        if unit == 'm':
            seconds *= 60
        elif unit == 'h':
            seconds *= 3600
        elif unit == 'd':
            seconds *= 86400
        elif unit == 'w':
            seconds *= 7 * 86400
        elif unit == 'mo':
            seconds *= 30 * 86400
        elif unit == 'q':
            seconds *= 90 * 86400
        elif unit == 'y':
            seconds *= 365 * 86400
        return seconds
    except Exception:
        return None


def _mask_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", lambda m: "\x00" * len(m.group(0)), text, flags=re.S)


def _sanitize_text(text: str) -> str:
    if not text:
        return text
    text = text.replace('\x00', '')
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", '', text)
    text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]", '', text)
    return text


def _split_csv(raw: str) -> List[str]:
    if not raw:
        return []
    out: List[str] = []
    for token in re.split(r"[,;]", raw):
        item = token.strip()
        if item:
            out.append(item)
    seen = set()
    deduped: List[str] = []
    for item in out:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def derive_contract_id(source_type: str, source_id: str, index: int = 0,
                       total: int = 1, override: Optional[str] = None) -> str:
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned if cleaned.startswith('contract_') else f"contract_{cleaned}"
    base = f"contract_{source_type}_{source_id}"
    if total > 1:
        return f"{base}_{index + 1}"
    return base


def parse_contract_blocks(text: str) -> List[ContractSpec]:
    if not text:
        return []
    if len(text) > _MAX_CONTRACT_INPUT_SIZE:
        logger.warning("Rejecting oversized input for contract parsing (%s bytes)", len(text))
        return []

    text = _sanitize_text(text)
    masked = _mask_code_fences(text)
    specs: List[ContractSpec] = []

    for pattern in _CONTRACT_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            if len(specs) >= _MAX_CONTRACT_BLOCKS:
                logger.warning("Contract block limit reached; ignoring extras")
                break

            block = text[match.start(1):match.end(1)] if match.group(1) else ''
            raw_block = text[match.start():match.end()]

            title = None
            summary_lines: List[str] = []
            terms_lines: List[str] = []
            notes_lines: List[str] = []
            owner = None
            counterparties: List[str] = []
            status = None
            contract_id = None
            expires_at = None
            ttl_seconds = None
            ttl_mode = None
            metadata: Dict[str, Any] = {}
            confirmed = True
            fields: Set[str] = set()
            section = None

            for line in block.splitlines():
                stripped = line.strip()
                if not stripped:
                    if section in ('summary', 'terms', 'notes'):
                        if section == 'summary':
                            summary_lines.append('')
                        elif section == 'terms':
                            terms_lines.append('')
                        else:
                            notes_lines.append('')
                    continue

                m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)", stripped)
                if m:
                    key = m.group(1).lower()
                    val = (m.group(2) or '').strip()

                    if key in ('title', 'name'):
                        title = val or title
                        fields.add('title')
                        section = None
                        continue
                    if key in ('summary', 'description', 'desc'):
                        if val:
                            summary_lines.append(val)
                        fields.add('summary')
                        section = 'summary'
                        continue
                    if key in ('terms', 'obligations', 'body', 'scope'):
                        if val:
                            terms_lines.append(val)
                        fields.add('terms')
                        section = 'terms'
                        continue
                    if key in ('owner', 'lead', 'facilitator'):
                        owner = val or owner
                        fields.add('owner')
                        section = None
                        continue
                    if key in ('counterparties', 'participants', 'members', 'parties'):
                        counterparties.extend(_split_csv(val))
                        fields.add('counterparties')
                        section = None
                        continue
                    if key in ('status',):
                        status = val.lower() or status
                        fields.add('status')
                        section = None
                        continue
                    if key in ('id', 'contract_id'):
                        contract_id = val or contract_id
                        fields.add('contract_id')
                        section = None
                        continue
                    if key in ('expires', 'expires_at', 'deadline', 'end'):
                        expires_at = _parse_dt(val) or expires_at
                        fields.add('expires_at')
                        section = None
                        continue
                    if key in ('ttl', 'ttl_seconds'):
                        ttl_seconds = _parse_ttl(val) or ttl_seconds
                        fields.add('ttl_seconds')
                        section = None
                        continue
                    if key in ('ttl_mode',):
                        ttl_mode = val.lower() or ttl_mode
                        fields.add('ttl_mode')
                        section = None
                        continue
                    if key in ('notes',):
                        if val:
                            notes_lines.append(val)
                        fields.add('notes')
                        section = 'notes'
                        continue
                    if key in ('confirm', 'enabled'):
                        raw = val.lower()
                        if raw in _CONFIRM_FALSE:
                            confirmed = False
                        elif raw in _CONFIRM_TRUE:
                            confirmed = True
                        fields.add('confirm')
                        section = None
                        continue

                    # Preserve unknown keys as metadata for deterministic hashing.
                    metadata[key] = val
                    fields.add('metadata')
                    section = None
                    continue

                if section == 'summary':
                    summary_lines.append(stripped.lstrip('-* ').strip())
                elif section == 'terms':
                    terms_lines.append(stripped)
                elif section == 'notes':
                    notes_lines.append(stripped)

            summary = "\n".join([l for l in summary_lines if l is not None]).strip() or None
            terms = "\n".join([l for l in terms_lines if l is not None]).strip() or None
            notes = "\n".join([l for l in notes_lines if l is not None]).strip() or None

            if not title:
                if summary:
                    title = summary.splitlines()[0][:160]
                elif terms:
                    title = terms.splitlines()[0][:160]

            if not title:
                continue

            specs.append(
                ContractSpec(
                    title=title,
                    summary=summary,
                    terms=terms,
                    owner=owner,
                    counterparties=counterparties,
                    status=status,
                    contract_id=contract_id,
                    expires_at=expires_at,
                    ttl_seconds=ttl_seconds,
                    ttl_mode=ttl_mode,
                    metadata=metadata or None,
                    notes=notes,
                    confirmed=confirmed,
                    raw=raw_block,
                    fields=fields,
                )
            )

    return specs


def strip_contract_blocks(text: str, remove_unconfirmed: bool = False) -> str:
    if not text:
        return text

    masked = _mask_code_fences(text)
    out = text
    for pattern in _CONTRACT_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            raw_block = text[match.start():match.end()]
            remove = True
            if not remove_unconfirmed:
                try:
                    spec = parse_contract_blocks(raw_block)
                    if spec and not spec[0].confirmed:
                        remove = False
                except Exception:
                    remove = True
            if remove:
                out = out.replace(raw_block, '').strip()
    return out


class ContractManager:
    """Manages structured contract records."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        logger.info("Initializing ContractManager")
        self._ensure_tables()
        logger.info("ContractManager initialized successfully")

    def _ensure_tables(self) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contracts (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT,
                    terms TEXT,
                    status TEXT DEFAULT 'proposed',
                    owner_id TEXT,
                    counterparties TEXT,
                    fingerprint TEXT,
                    revision INTEGER DEFAULT 1,
                    metadata TEXT,
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    visibility TEXT DEFAULT 'network',
                    origin_peer TEXT,
                    source_type TEXT,
                    source_id TEXT,
                    expires_at TIMESTAMP,
                    ttl_seconds INTEGER,
                    ttl_mode TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contracts_owner ON contracts(owner_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contracts_source ON contracts(source_type, source_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contracts_updated ON contracts(updated_at)")
            conn.commit()

    def _normalize_status(self, status: Optional[str]) -> str:
        value = (status or 'proposed').strip().lower()
        return value if value in CONTRACT_STATUSES else 'proposed'

    def _resolve_expiry(self,
                        expires_at: Optional[Any],
                        ttl_seconds: Optional[int],
                        ttl_mode: Optional[str],
                        base_time: Optional[datetime] = None) -> Optional[datetime]:
        if ttl_mode in ('none', 'no_expiry', 'immortal'):
            return None
        if expires_at:
            return _parse_dt(expires_at)

        base = base_time or _now_utc()
        if ttl_seconds is not None:
            try:
                ttl_val = int(ttl_seconds)
            except (TypeError, ValueError):
                ttl_val = None
            if ttl_val is not None:
                if ttl_val <= 0:
                    return None
                return base + timedelta(seconds=ttl_val)

        return base + timedelta(seconds=_DEFAULT_TTL_SECONDS)

    def _normalize_counterparties(self, counterparties: Optional[List[str]]) -> List[str]:
        out: List[str] = []
        for token in counterparties or []:
            item = str(token or '').strip()
            if not item:
                continue
            out.append(item)
        seen = set()
        deduped: List[str] = []
        for token in out:
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(token)
        return deduped

    def _compute_fingerprint(self,
                             title: str,
                             summary: Optional[str],
                             terms: Optional[str],
                             status: str,
                             owner_id: Optional[str],
                             counterparties: List[str],
                             metadata: Optional[Dict[str, Any]]) -> str:
        payload = {
            'title': (title or '').strip(),
            'summary': (summary or '').strip(),
            'terms': (terms or '').strip(),
            'status': status,
            'owner_id': owner_id or '',
            'counterparties': sorted([c.strip() for c in (counterparties or [])]),
            'metadata': metadata or {},
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    def _row_to_contract(self, row: Any) -> Dict[str, Any]:
        data = dict(row)
        try:
            data['counterparties'] = json.loads(data.get('counterparties') or '[]')
        except Exception:
            data['counterparties'] = []
        try:
            metadata_raw = data.get('metadata')
            if isinstance(metadata_raw, (str, bytes, bytearray)):
                data['metadata'] = json.loads(metadata_raw)
            else:
                data['metadata'] = None
        except Exception:
            data['metadata'] = None
        return data

    def _is_admin(self, actor_id: Optional[str], admin_user_id: Optional[str] = None) -> bool:
        if not actor_id:
            return False
        if admin_user_id:
            return actor_id == admin_user_id
        try:
            owner = self.db.get_instance_owner_user_id()
        except Exception:
            owner = None
        return bool(owner and owner == actor_id)

    def _can_manage(self, contract: Dict[str, Any], actor_id: Optional[str], admin_user_id: Optional[str] = None) -> bool:
        if not actor_id:
            return False
        if self._is_admin(actor_id, admin_user_id=admin_user_id):
            return True
        return actor_id in {
            contract.get('owner_id'),
            contract.get('created_by'),
        }

    def _can_participate(self, contract: Dict[str, Any], actor_id: Optional[str], admin_user_id: Optional[str] = None) -> bool:
        if not actor_id:
            return False
        if self._can_manage(contract, actor_id, admin_user_id=admin_user_id):
            return True
        return actor_id in set(contract.get('counterparties') or [])

    def _validate_participant_transition(self, current_status: str, next_status: str) -> bool:
        transitions = {
            'proposed': {'accepted'},
            'accepted': {'active'},
            'active': {'fulfilled', 'breached'},
        }
        allowed = transitions.get(current_status, set())
        return next_status in allowed

    def get_contract(self, contract_id: str) -> Optional[Dict[str, Any]]:
        if not contract_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
            if not row:
                return None
            return self._row_to_contract(row)
        except Exception as e:
            logger.error("Failed to get contract %s: %s", contract_id, e)
            return None

    def upsert_contract(
        self,
        contract_id: str,
        title: str,
        created_by: str,
        summary: Optional[str] = None,
        terms: Optional[str] = None,
        status: Optional[str] = None,
        owner_id: Optional[str] = None,
        counterparties: Optional[List[str]] = None,
        visibility: Optional[str] = None,
        origin_peer: Optional[str] = None,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        expires_at: Optional[Any] = None,
        ttl_seconds: Optional[int] = None,
        ttl_mode: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[Any] = None,
        updated_at: Optional[Any] = None,
        actor_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not contract_id or not title:
            return None

        created_dt = _parse_dt(created_at) or _now_utc()
        updated_dt = _parse_dt(updated_at) or _now_utc()
        status_val = self._normalize_status(status)
        owner_val = (owner_id or created_by or '').strip() or created_by
        counterparties_val = self._normalize_counterparties(counterparties)
        expires_dt = self._resolve_expiry(expires_at, ttl_seconds, ttl_mode, base_time=created_dt)
        metadata_val = metadata if isinstance(metadata, dict) else None
        metadata_json = json.dumps(metadata_val) if metadata_val is not None else None
        fingerprint = self._compute_fingerprint(
            title=title,
            summary=summary,
            terms=terms,
            status=status_val,
            owner_id=owner_val,
            counterparties=counterparties_val,
            metadata=metadata_val,
        )

        try:
            with self.db.get_connection() as conn:
                existing_row = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
                if existing_row:
                    existing = self._row_to_contract(existing_row)
                    prev_fp = existing.get('fingerprint') or ''
                    changed = any([
                        (existing.get('title') or '') != (title or ''),
                        (existing.get('summary') or '') != (summary or ''),
                        (existing.get('terms') or '') != (terms or ''),
                        (existing.get('status') or 'proposed') != status_val,
                        (existing.get('owner_id') or '') != (owner_val or ''),
                        (existing.get('counterparties') or []) != counterparties_val,
                        (existing.get('visibility') or 'network') != (visibility or existing.get('visibility') or 'network'),
                        (existing.get('origin_peer') or '') != (origin_peer or existing.get('origin_peer') or ''),
                        (existing.get('source_type') or '') != (source_type or existing.get('source_type') or ''),
                        (existing.get('source_id') or '') != (source_id or existing.get('source_id') or ''),
                        (existing.get('expires_at') or '') != (expires_dt.isoformat() if expires_dt else ''),
                        (existing.get('ttl_seconds')) != ttl_seconds,
                        (existing.get('ttl_mode') or '') != (ttl_mode or ''),
                        (existing.get('metadata') or {}) != (metadata_val or None),
                        prev_fp != fingerprint,
                    ])
                    if not changed:
                        return existing

                    revision = int(existing.get('revision') or 1)
                    if prev_fp != fingerprint:
                        revision += 1

                    conn.execute(
                        """
                        UPDATE contracts
                        SET title = ?, summary = ?, terms = ?, status = ?, owner_id = ?,
                            counterparties = ?, fingerprint = ?, revision = ?, metadata = ?,
                            visibility = ?, origin_peer = ?, source_type = ?, source_id = ?,
                            expires_at = ?, ttl_seconds = ?, ttl_mode = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            title,
                            summary,
                            terms,
                            status_val,
                            owner_val,
                            json.dumps(counterparties_val),
                            fingerprint,
                            revision,
                            metadata_json,
                            visibility or existing.get('visibility') or 'network',
                            origin_peer or existing.get('origin_peer'),
                            source_type or existing.get('source_type'),
                            source_id or existing.get('source_id'),
                            expires_dt.isoformat() if expires_dt else None,
                            ttl_seconds,
                            ttl_mode,
                            updated_dt.isoformat(),
                            contract_id,
                        )
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO contracts
                        (id, title, summary, terms, status, owner_id, counterparties,
                         fingerprint, revision, metadata, created_by, created_at, updated_at,
                         visibility, origin_peer, source_type, source_id, expires_at,
                         ttl_seconds, ttl_mode)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            contract_id,
                            title,
                            summary,
                            terms,
                            status_val,
                            owner_val,
                            json.dumps(counterparties_val),
                            fingerprint,
                            metadata_json,
                            created_by,
                            created_dt.isoformat(),
                            updated_dt.isoformat(),
                            visibility or 'network',
                            origin_peer,
                            source_type,
                            source_id,
                            expires_dt.isoformat() if expires_dt else None,
                            ttl_seconds,
                            ttl_mode,
                        )
                    )
                conn.commit()
            return self.get_contract(contract_id)
        except Exception as e:
            logger.error("Failed to upsert contract %s: %s", contract_id, e, exc_info=True)
            return None

    def list_contracts(self,
                       limit: int = 50,
                       status: Optional[str] = None,
                       owner_id: Optional[str] = None,
                       source_type: Optional[str] = None,
                       source_id: Optional[str] = None,
                       visibility: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            limit_val = max(1, min(int(limit or 50), 200))
            clauses = []
            params: List[Any] = []
            if status:
                clauses.append("status = ?")
                params.append(self._normalize_status(status))
            if owner_id:
                clauses.append("owner_id = ?")
                params.append(owner_id)
            if source_type:
                clauses.append("source_type = ?")
                params.append(source_type)
            if source_id:
                clauses.append("source_id = ?")
                params.append(source_id)
            if visibility:
                clauses.append("visibility = ?")
                params.append(str(visibility).strip().lower())

            query = "SELECT * FROM contracts"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit_val)

            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
            return [self._row_to_contract(r) for r in rows]
        except Exception as e:
            logger.error("Failed to list contracts: %s", e)
            return []

    def update_contract(self,
                        contract_id: str,
                        updates: Dict[str, Any],
                        actor_id: Optional[str] = None,
                        admin_user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not contract_id:
            return None
        if not updates:
            return self.get_contract(contract_id)

        current = self.get_contract(contract_id)
        if not current:
            return None

        can_manage = self._can_manage(current, actor_id, admin_user_id=admin_user_id)
        can_participate = self._can_participate(current, actor_id, admin_user_id=admin_user_id)
        if not can_participate:
            raise PermissionError("Not authorized to update contract")

        normalized_updates = dict(updates or {})
        if 'counterparties' in normalized_updates:
            cp = normalized_updates.get('counterparties') or []
            if isinstance(cp, str):
                cp = _split_csv(cp)
            normalized_updates['counterparties'] = cp
        if 'status' in normalized_updates and normalized_updates.get('status') is not None:
            normalized_updates['status'] = self._normalize_status(normalized_updates.get('status'))

        # Non-managers can only advance status transitions on contracts where they participate.
        if not can_manage:
            allowed_keys = {'status'}
            provided_keys = {k for k in normalized_updates.keys() if k in (
                'title', 'summary', 'terms', 'status', 'owner_id', 'counterparties',
                'expires_at', 'ttl_seconds', 'ttl_mode', 'metadata', 'visibility'
            )}
            if not provided_keys or not provided_keys.issubset(allowed_keys):
                raise PermissionError("Only owner/creator can edit contract fields")
            next_status = normalized_updates.get('status')
            if not next_status or not self._validate_participant_transition(
                (current.get('status') or 'proposed').lower(),
                next_status,
            ):
                raise PermissionError("Status transition is not allowed")

        next_title = cast(str, normalized_updates.get('title', current.get('title')))
        next_summary = normalized_updates.get('summary', current.get('summary'))
        next_terms = normalized_updates.get('terms', current.get('terms'))
        next_status = normalized_updates.get('status', current.get('status') or 'proposed')
        next_owner = normalized_updates.get('owner_id', current.get('owner_id'))
        next_counterparties = self._normalize_counterparties(
            normalized_updates.get('counterparties', current.get('counterparties') or [])
        )
        next_visibility = normalized_updates.get('visibility', current.get('visibility') or 'network')

        meta_current = current.get('metadata') if isinstance(current.get('metadata'), dict) else None
        next_metadata = normalized_updates.get('metadata', meta_current)
        if next_metadata is not None and not isinstance(next_metadata, dict):
            next_metadata = meta_current

        expires_dt = self._resolve_expiry(
            normalized_updates.get('expires_at', current.get('expires_at')),
            normalized_updates.get('ttl_seconds', current.get('ttl_seconds')),
            normalized_updates.get('ttl_mode', current.get('ttl_mode')),
            base_time=_parse_dt(current.get('created_at')) or _now_utc(),
        )

        fingerprint = self._compute_fingerprint(
            title=next_title,
            summary=next_summary,
            terms=next_terms,
            status=next_status,
            owner_id=next_owner,
            counterparties=next_counterparties,
            metadata=next_metadata,
        )
        prev_fp = current.get('fingerprint') or ''
        revision = int(current.get('revision') or 1)
        if fingerprint != prev_fp:
            revision += 1

        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE contracts
                    SET title = ?, summary = ?, terms = ?, status = ?, owner_id = ?,
                        counterparties = ?, fingerprint = ?, revision = ?, metadata = ?,
                        visibility = ?, expires_at = ?, ttl_seconds = ?, ttl_mode = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_title,
                        next_summary,
                        next_terms,
                        next_status,
                        next_owner,
                        json.dumps(next_counterparties),
                        fingerprint,
                        revision,
                        json.dumps(next_metadata) if next_metadata is not None else None,
                        next_visibility,
                        expires_dt.isoformat() if expires_dt else None,
                        normalized_updates.get('ttl_seconds', current.get('ttl_seconds')),
                        normalized_updates.get('ttl_mode', current.get('ttl_mode')),
                        _now_utc().isoformat(),
                        contract_id,
                    )
                )
                conn.commit()
            return self.get_contract(contract_id)
        except Exception as e:
            logger.error("Failed to update contract %s: %s", contract_id, e, exc_info=True)
            return None
