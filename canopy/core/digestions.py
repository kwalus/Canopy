"""
User-owned semantic digestions over File Vault content.

A Digestion is a local, permissioned retrieval index derived from selected
Vault files. It does not mesh-sync source files or vectors by default; it gives
humans and agents a bounded way to query user-approved material with citations.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .database import DatabaseManager
from .file_preview import build_file_preview
from .files import FileInfo, FileManager

logger = logging.getLogger("canopy.digestions")

DEFAULT_OPENAI_EMBEDDING_MODEL = os.getenv(
    "CANOPY_DIGESTION_OPENAI_MODEL",
    "text-embedding-3-small",
).strip() or "text-embedding-3-small"
DEFAULT_LOCAL_EMBEDDING_MODEL = "local-hash-v1"
DEFAULT_LOCAL_DIMENSIONS = 256
DEFAULT_CHUNK_SIZE = 1800
DEFAULT_CHUNK_OVERLAP = 220
MAX_CHUNK_SIZE = 8000
MAX_CHUNK_OVERLAP = 2000
MAX_QUERY_TOP_K = 20
MAX_SNIPPET_CHARS = 1400
MAX_FILE_BYTES = int(os.getenv("CANOPY_DIGESTION_MAX_FILE_BYTES", str(64 * 1024 * 1024)))
MAX_FILE_CHARS = int(os.getenv("CANOPY_DIGESTION_MAX_FILE_CHARS", "2000000"))
MAX_CHUNKS_PER_BUILD = int(os.getenv("CANOPY_DIGESTION_MAX_CHUNKS_PER_BUILD", "5000"))
MAX_MATERIALS_PER_INGEST = int(os.getenv("CANOPY_DIGESTION_MAX_MATERIALS_PER_INGEST", "100"))
MIN_LOCAL_HASH_QUERY_SCORE = float(os.getenv("CANOPY_DIGESTION_LOCAL_HASH_MIN_SCORE", "0.08") or "0.08")
_SOURCE_REVEALING_OUTPUT_KINDS = {"manifest", "human_brief"}
_OPENAI_EMBEDDINGS_URL = os.getenv(
    "CANOPY_DIGESTION_OPENAI_EMBEDDINGS_URL",
    "https://api.openai.com/v1/embeddings",
).strip() or "https://api.openai.com/v1/embeddings"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{2,}")
_MATERIAL_KIND_RE = re.compile(r"[^a-z0-9_\-]+")
_COMMON_TERMS = {
    "about", "after", "again", "against", "also", "because", "before", "being", "between", "could",
    "does", "each", "from", "have", "into", "more", "most", "other", "over", "same", "should",
    "such", "than", "that", "their", "there", "these", "this", "those", "through", "under", "using",
    "very", "were", "what", "when", "where", "which", "while", "with", "would", "your",
}


class DigestionError(RuntimeError):
    """User-facing digestion failure with an HTTP-appropriate status code."""

    def __init__(self, message: str, *, status_code: int = 400, reason: str = "digestion_error") -> None:
        super().__init__(message)
        self.status_code = int(status_code or 400)
        self.reason = reason


@dataclass
class Digestion:
    id: str
    owner_user_id: str
    name: str
    description: str
    purpose: str
    status: str
    provider: str
    embedding_model: str
    embedding_dimensions: int
    chunk_size: int
    chunk_overlap: int
    settings: dict[str, Any]
    created_at: str
    updated_at: str
    built_at: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self, *, access: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        data = asdict(self)
        data["settings"] = dict(self.settings or {})
        if access is not None:
            data["access"] = access
        return data


@dataclass
class ExtractedSegment:
    text: str
    page_label: str = ""


class DigestionManager:
    """Builds and queries local semantic indexes over user Vault files."""

    def __init__(self, db: DatabaseManager, file_manager: FileManager, config: Any = None):
        self.db = db
        self.file_manager = file_manager
        self.config = config
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _ensure_tables(self) -> None:
        with self.db.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS digestions (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    purpose TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    provider TEXT NOT NULL DEFAULT 'openai',
                    embedding_model TEXT NOT NULL,
                    embedding_dimensions INTEGER NOT NULL DEFAULT 0,
                    chunk_size INTEGER NOT NULL DEFAULT 1800,
                    chunk_overlap INTEGER NOT NULL DEFAULT 220,
                    settings_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    built_at TIMESTAMP,
                    error TEXT,
                    FOREIGN KEY (owner_user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS digestion_sources (
                    digestion_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_checksum TEXT,
                    file_name TEXT,
                    content_type TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    extracted_chars INTEGER NOT NULL DEFAULT 0,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (digestion_id, file_id),
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS digestion_embeddings (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL DEFAULT 0,
                    chunk_hash TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(provider, model, dimensions, chunk_hash)
                );

                CREATE TABLE IF NOT EXISTS digestion_chunks (
                    id TEXT PRIMARY KEY,
                    digestion_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    page_label TEXT,
                    chunk_hash TEXT NOT NULL,
                    embedding_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(digestion_id, file_id, chunk_index),
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                    FOREIGN KEY (embedding_id) REFERENCES digestion_embeddings(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS digestion_acl (
                    digestion_id TEXT NOT NULL,
                    grantee_user_id TEXT NOT NULL,
                    grantee_kind TEXT NOT NULL DEFAULT 'user',
                    can_query INTEGER NOT NULL DEFAULT 1,
                    can_manage INTEGER NOT NULL DEFAULT 0,
                    can_read_sources INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (digestion_id, grantee_user_id),
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                    FOREIGN KEY (grantee_user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS digestion_query_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    digestion_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    result_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS digestion_outputs (
                    id TEXT PRIMARY KEY,
                    digestion_id TEXT NOT NULL,
                    output_kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'text/markdown',
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(digestion_id, output_kind),
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_digestions_owner ON digestions(owner_user_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_acl_grantee ON digestion_acl(grantee_user_id, can_query, can_manage);
                CREATE INDEX IF NOT EXISTS idx_digestion_chunks_digestion ON digestion_chunks(digestion_id, file_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_digestion_sources_status ON digestion_sources(digestion_id, status);
                CREATE INDEX IF NOT EXISTS idx_digestion_outputs ON digestion_outputs(digestion_id, output_kind);
                """
            )
            source_columns = {
                str(row["name"] if hasattr(row, "keys") else row[1])
                for row in conn.execute("PRAGMA table_info(digestion_sources)").fetchall()
            }
            source_additions = {
                "source_kind": "ALTER TABLE digestion_sources ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'vault_file'",
                "source_label": "ALTER TABLE digestion_sources ADD COLUMN source_label TEXT",
                "source_uri": "ALTER TABLE digestion_sources ADD COLUMN source_uri TEXT",
                "source_metadata_json": "ALTER TABLE digestion_sources ADD COLUMN source_metadata_json TEXT",
            }
            for column, ddl in source_additions.items():
                if column not in source_columns:
                    conn.execute(ddl)
            conn.commit()

    # ------------------------------------------------------------------
    # Public metadata and ACLs
    # ------------------------------------------------------------------
    def create_digestion(
        self,
        owner_user_id: str,
        *,
        name: str,
        description: str = "",
        purpose: str = "",
        source_file_ids: Optional[Iterable[str]] = None,
        provider: Optional[str] = None,
        embedding_model: Optional[str] = None,
        embedding_dimensions: Optional[int] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        settings: Optional[dict[str, Any]] = None,
        source_materials: Optional[Iterable[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        owner_user_id = self._clean_id(owner_user_id)
        if not owner_user_id:
            raise DigestionError("owner_user_id is required", status_code=400, reason="missing_owner")
        name_clean = str(name or "").strip()
        if not name_clean:
            raise DigestionError("Digestion name is required", status_code=400, reason="missing_name")
        provider_clean = self._normalize_provider(provider)
        model_clean = str(
            embedding_model
            or (DEFAULT_LOCAL_EMBEDDING_MODEL if provider_clean == "local_hash" else DEFAULT_OPENAI_EMBEDDING_MODEL)
        ).strip()
        if not model_clean:
            raise DigestionError("embedding_model is required", status_code=400, reason="missing_model")
        dims = self._normalize_dimensions(embedding_dimensions, provider_clean)
        chunk_size = max(240, min(int(chunk_size or DEFAULT_CHUNK_SIZE), MAX_CHUNK_SIZE))
        chunk_overlap = max(0, min(int(chunk_overlap or 0), min(MAX_CHUNK_OVERLAP, chunk_size // 2)))
        now = self._now()
        digestion_id = f"Dg{secrets.token_hex(12)}"
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO digestions (
                    id, owner_user_id, name, description, purpose, status,
                    provider, embedding_model, embedding_dimensions,
                    chunk_size, chunk_overlap, settings_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    digestion_id,
                    owner_user_id,
                    name_clean[:180],
                    str(description or "").strip()[:4000],
                    str(purpose or "").strip()[:4000],
                    provider_clean,
                    model_clean[:160],
                    dims,
                    chunk_size,
                    chunk_overlap,
                    json.dumps(settings or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.commit()
        if source_file_ids:
            self.add_sources(digestion_id, owner_user_id, list(source_file_ids))
        if source_materials:
            self.add_materials(digestion_id, owner_user_id, list(source_materials))
        return self.get_digestion(digestion_id, owner_user_id=owner_user_id) or {}

    def add_sources(self, digestion_id: str, owner_user_id: str, source_file_ids: Iterable[str]) -> dict[str, Any]:
        digestion = self._require_digestion(digestion_id, owner_user_id, manage=True)
        unique_file_ids = []
        seen: set[str] = set()
        for raw_id in source_file_ids or []:
            file_id = self._clean_id(raw_id)
            if file_id and file_id not in seen:
                unique_file_ids.append(file_id)
                seen.add(file_id)
        added = 0
        skipped: list[dict[str, str]] = []
        now = self._now()
        with self.db.get_connection() as conn:
            for file_id in unique_file_ids:
                info = self.file_manager.get_file(file_id)
                if not info or str(info.uploaded_by) != str(digestion.owner_user_id):
                    skipped.append({"file_id": file_id, "reason": "not_found_or_not_owned"})
                    continue
                conn.execute(
                    """
                    INSERT INTO digestion_sources (
                        digestion_id, file_id, file_checksum, file_name, content_type,
                        status, extracted_chars, chunk_count, error, updated_at,
                        source_kind, source_label, source_uri, source_metadata_json
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, NULL, ?, 'vault_file', ?, NULL, ?)
                    ON CONFLICT(digestion_id, file_id) DO UPDATE SET
                        file_checksum = excluded.file_checksum,
                        file_name = excluded.file_name,
                        content_type = excluded.content_type,
                        status = 'pending',
                        error = NULL,
                        source_kind = COALESCE(digestion_sources.source_kind, excluded.source_kind),
                        source_label = COALESCE(digestion_sources.source_label, excluded.source_label),
                        source_uri = COALESCE(digestion_sources.source_uri, excluded.source_uri),
                        source_metadata_json = COALESCE(digestion_sources.source_metadata_json, excluded.source_metadata_json),
                        updated_at = excluded.updated_at
                    """,
                    (
                        digestion.id,
                        info.id,
                        info.checksum,
                        info.original_name,
                        info.content_type,
                        now,
                        info.original_name,
                        json.dumps({"ingest_path": "vault_file"}, sort_keys=True),
                    ),
                )
                added += 1
            conn.execute("UPDATE digestions SET status = ?, updated_at = ? WHERE id = ?", ("draft", now, digestion.id))
            conn.commit()
        return {"success": True, "added": added, "skipped": skipped, "digestion_id": digestion.id}

    def add_materials(self, digestion_id: str, actor_user_id: str, materials: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Add inline/source materials by normalizing them into Vault-backed source files.

        This gives Digestions a generic ingestion path without adding mesh-synced
        raw corpora or bypassing the existing Vault/file permission boundary.
        """
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        added = 0
        skipped: list[dict[str, str]] = []
        now = self._now()
        normalized: list[tuple[FileInfo, dict[str, Any]]] = []
        all_materials = list(materials or [])
        material_list = all_materials[:MAX_MATERIALS_PER_INGEST]
        skipped_extra = max(0, len(all_materials) - len(material_list))
        for index, material in enumerate(material_list, start=1):
            if not isinstance(material, dict):
                skipped.append({"index": str(index), "reason": "material_not_object"})
                continue
            try:
                file_info, material_meta = self._material_to_vault_file(digestion, actor_user_id, material, index=index)
                normalized.append((file_info, material_meta))
            except DigestionError as exc:
                skipped.append({"index": str(index), "reason": exc.reason, "error": str(exc)})
            except Exception as exc:
                skipped.append({"index": str(index), "reason": "material_ingest_failed", "error": str(exc)[:300]})
        with self.db.get_connection() as conn:
            for info, material_meta in normalized:
                conn.execute(
                    """
                    INSERT INTO digestion_sources (
                        digestion_id, file_id, file_checksum, file_name, content_type,
                        status, extracted_chars, chunk_count, error, updated_at,
                        source_kind, source_label, source_uri, source_metadata_json
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, NULL, ?, ?, ?, ?, ?)
                    ON CONFLICT(digestion_id, file_id) DO UPDATE SET
                        file_checksum = excluded.file_checksum,
                        file_name = excluded.file_name,
                        content_type = excluded.content_type,
                        status = 'pending',
                        error = NULL,
                        source_kind = excluded.source_kind,
                        source_label = excluded.source_label,
                        source_uri = excluded.source_uri,
                        source_metadata_json = excluded.source_metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        digestion.id,
                        info.id,
                        info.checksum,
                        info.original_name,
                        info.content_type,
                        now,
                        material_meta.get("source_kind") or "inline_text",
                        material_meta.get("source_label") or info.original_name,
                        material_meta.get("source_uri") or "",
                        json.dumps(material_meta, sort_keys=True),
                    ),
                )
                added += 1
            if added:
                conn.execute("UPDATE digestions SET status = ?, updated_at = ? WHERE id = ?", ("draft", now, digestion.id))
            conn.commit()
        if skipped_extra:
            skipped.append({"reason": "material_limit_reached", "count": str(skipped_extra)})
        return {"success": True, "added": added, "skipped": skipped, "digestion_id": digestion.id}

    def list_digestions(self, user_id: str, *, include_sources: bool = False) -> list[dict[str, Any]]:
        user_id = self._clean_id(user_id)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT d.*, a.can_query, a.can_manage, a.can_read_sources
                FROM digestions d
                LEFT JOIN digestion_acl a ON a.digestion_id = d.id AND a.grantee_user_id = ?
                WHERE d.owner_user_id = ? OR COALESCE(a.can_query, 0) = 1 OR COALESCE(a.can_manage, 0) = 1
                ORDER BY d.updated_at DESC, d.created_at DESC
                """,
                (user_id, user_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._digestion_from_row(row).to_dict(access=self._access_from_row(row, user_id))
            if include_sources and item.get("access", {}).get("can_read_sources"):
                item["sources"] = self.list_sources(item["id"], user_id=user_id)
            elif include_sources:
                item["sources"] = []
            result.append(item)
        return result

    def get_digestion(self, digestion_id: str, *, owner_user_id: Optional[str] = None, user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        viewer = self._clean_id(owner_user_id or user_id or "")
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT d.*, a.can_query, a.can_manage, a.can_read_sources
                FROM digestions d
                LEFT JOIN digestion_acl a ON a.digestion_id = d.id AND a.grantee_user_id = ?
                WHERE d.id = ?
                """,
                (viewer, self._clean_id(digestion_id)),
            ).fetchone()
        if not row:
            return None
        digestion = self._digestion_from_row(row)
        access = self._access_from_row(row, viewer)
        if viewer and not access.get("can_query") and not access.get("can_manage"):
            return None
        data = digestion.to_dict(access=access)
        data["sources"] = self.list_sources(digestion.id, user_id=viewer) if viewer and access.get("can_read_sources") else []
        data["stats"] = self.stats(digestion.id)
        return data

    def list_sources(self, digestion_id: str, *, user_id: str = "") -> list[dict[str, Any]]:
        if user_id:
            digestion = self._get_digestion_obj(digestion_id)
            if not digestion:
                raise DigestionError("Digestion not found", status_code=404, reason="not_found")
            access = self._access_for(digestion, user_id)
            if not (access.get("can_read_sources") or access.get("can_manage")):
                raise DigestionError(
                    "You do not have source metadata access to this Digestion.",
                    status_code=403,
                    reason="source_metadata_denied",
                )
        else:
            digestion = self._get_digestion_obj(digestion_id)
        if not digestion:
            return []
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT file_id, file_checksum, file_name, content_type, status,
                       extracted_chars, chunk_count, error, updated_at,
                       source_kind, source_label, source_uri, source_metadata_json
                FROM digestion_sources
                WHERE digestion_id = ?
                ORDER BY file_name COLLATE NOCASE, file_id
                """,
                (digestion.id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def grant_access(
        self,
        digestion_id: str,
        actor_user_id: str,
        grantee_user_id: str,
        *,
        can_query: bool = True,
        can_manage: bool = False,
        can_read_sources: bool = False,
    ) -> dict[str, Any]:
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        grantee = self._clean_id(grantee_user_id)
        if not grantee:
            raise DigestionError("grantee_user_id is required", status_code=400, reason="missing_grantee")
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO digestion_acl (
                    digestion_id, grantee_user_id, grantee_kind, can_query,
                    can_manage, can_read_sources, created_at
                ) VALUES (?, ?, 'user', ?, ?, ?, ?)
                ON CONFLICT(digestion_id, grantee_user_id) DO UPDATE SET
                    can_query = excluded.can_query,
                    can_manage = excluded.can_manage,
                    can_read_sources = excluded.can_read_sources
                """,
                (
                    digestion.id,
                    grantee,
                    1 if can_query else 0,
                    1 if can_manage else 0,
                    1 if can_read_sources else 0,
                    self._now(),
                ),
            )
            conn.commit()
        return {"success": True, "digestion_id": digestion.id, "grantee_user_id": grantee}

    # ------------------------------------------------------------------
    # Build and query
    # ------------------------------------------------------------------
    def build_digestion(self, digestion_id: str, actor_user_id: str, *, rebuild: bool = False) -> dict[str, Any]:
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        source_rows = self._source_rows(digestion.id)
        if not source_rows:
            raise DigestionError("Add at least one Vault file before building a Digestion.", status_code=400, reason="no_sources")

        started = self._now()
        self._set_status(digestion.id, "building", error=None)
        total_chunks = 0
        embedded_count = 0
        errors: list[dict[str, str]] = []
        try:
            if rebuild:
                with self.db.get_connection() as conn:
                    conn.execute("DELETE FROM digestion_chunks WHERE digestion_id = ?", (digestion.id,))
                    conn.commit()

            for source in source_rows:
                if total_chunks >= MAX_CHUNKS_PER_BUILD:
                    errors.append({"file_id": str(source["file_id"]), "error": "build_chunk_limit_reached"})
                    break
                try:
                    file_chunks = self._index_source(digestion, source, remaining_chunks=MAX_CHUNKS_PER_BUILD - total_chunks)
                    total_chunks += file_chunks["chunk_count"]
                    embedded_count += file_chunks["embedded_count"]
                except Exception as exc:
                    message = str(exc)[:1000]
                    errors.append({"file_id": str(source["file_id"]), "error": message})
                    self._mark_source_error(digestion.id, str(source["file_id"]), message)

            status = "ready" if total_chunks > 0 else "error"
            error_text = None if status == "ready" else "No extractable chunks were indexed."
            if errors and status == "ready":
                status = "ready_with_errors"
                error_text = json.dumps(errors[:8])
            self._set_status(digestion.id, status, built_at=self._now(), error=error_text)
            digestion = self._get_digestion_obj(digestion.id) or digestion
            try:
                outputs = self.generate_outputs(digestion.id, actor_user_id) if total_chunks > 0 else {"outputs": []}
            except Exception as exc:
                logger.warning("Digestion output generation failed for %s: %s", digestion.id, exc, exc_info=True)
                outputs = {"outputs": [], "error": str(exc)[:500]}
            return {
                "success": status in {"ready", "ready_with_errors"},
                "digestion_id": digestion.id,
                "status": status,
                "started_at": started,
                "built_at": self._now(),
                "chunk_count": total_chunks,
                "embedded_count": embedded_count,
                "errors": errors,
                "outputs": outputs.get("outputs") or [],
                "stats": self.stats(digestion.id),
            }
        except Exception as exc:
            message = str(exc)[:1000]
            self._set_status(digestion.id, "error", error=message)
            raise

    def query(
        self,
        digestion_id: str,
        actor_user_id: str,
        query: str,
        *,
        top_k: int = 8,
        include_snippets: bool = True,
    ) -> dict[str, Any]:
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        query_text = str(query or "").strip()
        if not query_text:
            raise DigestionError("query is required", status_code=400, reason="missing_query")
        top_k = max(1, min(int(top_k or 8), MAX_QUERY_TOP_K))
        rows = self._queryable_chunk_rows(digestion.id)
        stats = self.stats(digestion.id)
        if not rows:
            self._log_query(digestion.id, actor_user_id, query_text, 0)
            return {
                "success": True,
                "digestion_id": digestion.id,
                "query": query_text,
                "top_k": top_k,
                "result_count": 0,
                "results": [],
                "status": digestion.status,
                "provider": digestion.provider,
                "embedding_model": digestion.embedding_model,
                "indexed_chunks": int(stats.get("chunks") or 0),
                "retrieval_ready": False,
                "stats": stats,
                "warning": "This Digestion has no indexed chunks yet. Build or rebuild it before expecting RAG results.",
            }
        query_vector = self._embed_one(
            query_text,
            provider=digestion.provider,
            model=digestion.embedding_model,
            dimensions=digestion.embedding_dimensions,
        )
        query_terms = self._query_terms(query_text) if digestion.provider == "local_hash" else set()
        scored: list[dict[str, Any]] = []
        for row in rows:
            try:
                vector = json.loads(row["vector_json"] or "[]")
            except Exception:
                continue
            score = self._cosine(query_vector, vector)
            if score <= 0:
                continue
            text = str(row["text"] or "")
            term_overlap = 0
            if digestion.provider == "local_hash":
                term_overlap = len(query_terms & self._query_terms(text))
                if term_overlap <= 0 or score < MIN_LOCAL_HASH_QUERY_SCORE:
                    continue
            scored.append(
                {
                    "chunk_id": row["chunk_id"],
                    "file_id": row["file_id"],
                    "file_name": row["file_name"] or row["file_id"],
                    "content_type": row["content_type"] or "application/octet-stream",
                    "chunk_index": int(row["chunk_index"] or 0),
                    "page_label": row["page_label"] or "",
                    "score": round(float(score), 6),
                    "term_overlap": term_overlap,
                    "token_estimate": int(row["token_estimate"] or 0),
                    "snippet": self._snippet(text) if include_snippets else "",
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        results = scored[:top_k]
        self._log_query(digestion.id, actor_user_id, query_text, len(results))
        return {
            "success": True,
            "digestion_id": digestion.id,
            "query": query_text,
            "top_k": top_k,
            "result_count": len(results),
            "results": results,
            "status": digestion.status,
            "provider": digestion.provider,
            "embedding_model": digestion.embedding_model,
            "indexed_chunks": int(stats.get("chunks") or len(rows) or 0),
            "retrieval_ready": True,
            "stats": stats,
        }

    def stats(self, digestion_id: str) -> dict[str, Any]:
        with self.db.get_connection() as conn:
            chunk_row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(token_estimate), 0) AS tokens FROM digestion_chunks WHERE digestion_id = ?",
                (digestion_id,),
            ).fetchone()
            source_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM digestion_sources WHERE digestion_id = ? GROUP BY status",
                (digestion_id,),
            ).fetchall()
        return {
            "chunks": int((chunk_row["count"] if chunk_row else 0) or 0),
            "token_estimate": int((chunk_row["tokens"] if chunk_row else 0) or 0),
            "sources_by_status": {str(row["status"]): int(row["count"] or 0) for row in source_rows},
        }

    def generate_outputs(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        kinds: Optional[Iterable[str]] = None,
    ) -> dict[str, Any]:
        """Generate reusable human/agent artifacts from the normalized Digestion."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        requested = {str(kind or "").strip().lower() for kind in (kinds or []) if str(kind or "").strip()}
        if not requested:
            requested = {"manifest", "human_brief", "agent_context"}
        allowed = {"manifest", "human_brief", "agent_context"}
        requested = requested & allowed
        if not requested:
            raise DigestionError("No supported output kinds requested.", status_code=400, reason="unsupported_output_kind")

        outputs = []
        for kind in sorted(requested):
            if kind == "manifest":
                outputs.append(self._upsert_output(digestion, actor_user_id, *self._build_manifest_output(digestion)))
            elif kind == "human_brief":
                outputs.append(self._upsert_output(digestion, actor_user_id, *self._build_human_brief_output(digestion)))
            elif kind == "agent_context":
                outputs.append(self._upsert_output(digestion, actor_user_id, *self._build_agent_context_output(digestion)))
        return {"success": True, "digestion_id": digestion.id, "outputs": outputs, "count": len(outputs)}

    def list_outputs(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        access = self._access_for(digestion, actor_user_id)
        source_outputs_allowed = bool(access.get("can_read_sources") or access.get("can_manage"))
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, digestion_id, output_kind, title, content_type, content,
                       metadata_json, created_by, created_at, updated_at
                FROM digestion_outputs
                WHERE digestion_id = ?
                ORDER BY
                    CASE output_kind
                        WHEN 'human_brief' THEN 1
                        WHEN 'agent_context' THEN 2
                        WHEN 'manifest' THEN 3
                        ELSE 9
                    END,
                    updated_at DESC
                """,
                (digestion.id,),
            ).fetchall()
        outputs = []
        for row in rows:
            output_kind = str(row["output_kind"] or "")
            if output_kind in _SOURCE_REVEALING_OUTPUT_KINDS and not source_outputs_allowed:
                continue
            outputs.append(self._output_row_to_dict(row, include_content=include_content))
        return outputs

    def get_output(self, digestion_id: str, actor_user_id: str, output_ref: str) -> dict[str, Any]:
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        access = self._access_for(digestion, actor_user_id)
        ref = str(output_ref or "").strip()
        if not ref:
            raise DigestionError("output_id or output_kind is required", status_code=400, reason="missing_output")
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, digestion_id, output_kind, title, content_type, content,
                       metadata_json, created_by, created_at, updated_at
                FROM digestion_outputs
                WHERE digestion_id = ? AND (id = ? OR output_kind = ?)
                """,
                (digestion.id, ref, ref),
            ).fetchone()
        if not row:
            raise DigestionError("Digestion output not found. Generate outputs first.", status_code=404, reason="output_not_found")
        if str(row["output_kind"] or "") in _SOURCE_REVEALING_OUTPUT_KINDS and not (access.get("can_read_sources") or access.get("can_manage")):
            raise DigestionError(
                "This Digestion output includes source metadata. Source metadata access is required.",
                status_code=403,
                reason="output_source_metadata_denied",
            )
        return self._output_row_to_dict(row, include_content=True)

    def export_output_to_vault(self, digestion_id: str, actor_user_id: str, output_ref: str) -> dict[str, Any]:
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        output = self.get_output(digestion.id, actor_user_id, output_ref)
        ext = ".json" if output.get("content_type") == "application/json" else ".md"
        filename = f"{self._slugify(digestion.name or 'digestion')}-{output.get('output_kind') or 'output'}{ext}"
        content = str(output.get("content") or "").encode("utf-8")
        file_info = self.file_manager.save_file(content, filename, str(output.get("content_type") or "text/markdown"), actor_user_id)
        if not file_info:
            raise DigestionError("Could not export Digestion output to Vault.", status_code=500, reason="output_export_failed")
        return {"success": True, "digestion_id": digestion.id, "output": output, "file": file_info.to_dict()}

    def context_pack(
        self,
        digestion_id: str,
        actor_user_id: str,
        query: str,
        *,
        top_k: int = 8,
    ) -> dict[str, Any]:
        """Return a compact, citable context package for agents or human drafts."""
        result = self.query(digestion_id, actor_user_id, query, top_k=top_k, include_snippets=True)
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        citations = []
        lines = [
            f"Digestion: {digestion.name} ({digestion.id})",
            f"Query: {result['query']}",
            "Use only the cited snippets below unless the user supplies additional context.",
            "",
        ]
        warning = str(result.get("warning") or "").strip()
        if warning:
            lines.append(f"Retrieval warning: {warning}")
            lines.append("")
        for index, item in enumerate(result.get("results") or [], start=1):
            citation = {
                "index": index,
                "file_id": item.get("file_id") or "",
                "file_name": item.get("file_name") or "",
                "page_label": item.get("page_label") or "",
                "chunk_index": item.get("chunk_index"),
                "score": item.get("score"),
            }
            citations.append(citation)
            label = f"[{index}] {citation['file_name']}"
            if citation["page_label"]:
                label += f" {citation['page_label']}"
            lines.append(f"{label}: {item.get('snippet') or ''}")
        if not citations:
            lines.append("No cited snippets matched this query. Build/rebuild the Digestion or try a more specific query before using it as evidence.")
        prompt_context = "\n\n".join(lines).strip()
        return {
            "success": True,
            "digestion_id": digestion.id,
            "digestion_name": digestion.name,
            "query": result["query"],
            "citations": citations,
            "prompt_context": prompt_context,
            "results": result.get("results") or [],
            "retrieval_ready": bool(result.get("retrieval_ready")),
            "indexed_chunks": int(result.get("indexed_chunks") or 0),
            "warning": warning,
            "stats": result.get("stats") or {},
        }

    # ------------------------------------------------------------------
    # Index internals
    # ------------------------------------------------------------------
    def _index_source(self, digestion: Digestion, source_row: Any, *, remaining_chunks: int) -> dict[str, int]:
        file_id = str(source_row["file_id"])
        info = self.file_manager.get_file(file_id)
        if not info or str(info.uploaded_by) != str(digestion.owner_user_id):
            raise DigestionError("Source file is no longer available in the owner Vault.", status_code=404, reason="source_missing")
        file_data_tuple = self.file_manager.get_file_data(file_id)
        if not file_data_tuple:
            raise DigestionError("Source file bytes are not available on this node.", status_code=404, reason="source_bytes_missing")
        file_data, info = file_data_tuple
        if len(file_data) > MAX_FILE_BYTES:
            raise DigestionError(
                f"Source file exceeds Digestion extraction limit ({MAX_FILE_BYTES} bytes).",
                status_code=413,
                reason="source_too_large",
            )
        segments = self.extract_text_segments(file_data, info)
        if not segments:
            raise DigestionError("No extractable text found in source file.", status_code=415, reason="no_extractable_text")
        chunks = self._chunk_segments(segments, digestion.chunk_size, digestion.chunk_overlap, remaining_chunks=remaining_chunks)
        if not chunks:
            raise DigestionError("No indexable chunks were produced from source file.", status_code=415, reason="no_chunks")
        vectors = self._embed_chunks(chunks, provider=digestion.provider, model=digestion.embedding_model, dimensions=digestion.embedding_dimensions)
        now = self._now()
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM digestion_chunks WHERE digestion_id = ? AND file_id = ?", (digestion.id, info.id))
            for idx, chunk in enumerate(chunks):
                embedding_id = vectors.get(chunk["chunk_hash"])
                conn.execute(
                    """
                    INSERT INTO digestion_chunks (
                        id, digestion_id, file_id, chunk_index, text, token_estimate,
                        page_label, chunk_hash, embedding_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"Dgc{secrets.token_hex(12)}",
                        digestion.id,
                        info.id,
                        idx,
                        chunk["text"],
                        chunk["token_estimate"],
                        chunk.get("page_label") or "",
                        chunk["chunk_hash"],
                        embedding_id,
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE digestion_sources
                SET file_checksum = ?, file_name = ?, content_type = ?, status = 'indexed',
                    extracted_chars = ?, chunk_count = ?, error = NULL, updated_at = ?
                WHERE digestion_id = ? AND file_id = ?
                """,
                (
                    info.checksum,
                    info.original_name,
                    info.content_type,
                    sum(len(segment.text) for segment in segments),
                    len(chunks),
                    now,
                    digestion.id,
                    info.id,
                ),
            )
            conn.commit()
        return {"chunk_count": len(chunks), "embedded_count": len(vectors)}

    def extract_text_segments(self, file_data: bytes, info: FileInfo) -> list[ExtractedSegment]:
        filename = str(info.original_name or "")
        content_type = str(info.content_type or "")
        ext = Path(filename).suffix.lower()
        if content_type.split(";", 1)[0].strip().lower() == "application/pdf" or ext == ".pdf":
            return self._extract_pdf_segments(file_data)
        preview = build_file_preview(file_data[:MAX_FILE_BYTES], filename, content_type)
        if not preview.get("previewable"):
            raise DigestionError(
                str(preview.get("error") or "This file type is not extractable for Digestions."),
                status_code=415,
                reason="unsupported_source_type",
            )
        if "text" in preview:
            text = self._normalize_text(str(preview.get("text") or ""))
            return [ExtractedSegment(text=text[:MAX_FILE_CHARS])] if text else []
        if preview.get("kind") == "spreadsheet":
            text = self._spreadsheet_preview_to_text(preview)
            return [ExtractedSegment(text=text[:MAX_FILE_CHARS])] if text else []
        return []

    def _extract_pdf_segments(self, file_data: bytes) -> list[ExtractedSegment]:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency may be absent in minimal envs
            raise DigestionError(
                "PDF Digestions require the optional pypdf dependency on this node.",
                status_code=503,
                reason="pdf_dependency_missing",
            ) from exc
        try:
            reader = PdfReader(io.BytesIO(file_data))
        except Exception as exc:
            raise DigestionError("PDF could not be read for text extraction.", status_code=415, reason="pdf_unreadable") from exc
        segments: list[ExtractedSegment] = []
        chars = 0
        for index, page in enumerate(reader.pages, start=1):
            if chars >= MAX_FILE_CHARS:
                break
            try:
                text = self._normalize_text(page.extract_text() or "")
            except Exception:
                text = ""
            if not text:
                continue
            remaining = MAX_FILE_CHARS - chars
            segment_text = text[:remaining]
            chars += len(segment_text)
            segments.append(ExtractedSegment(text=segment_text, page_label=f"p. {index}"))
        return segments

    def _chunk_segments(
        self,
        segments: list[ExtractedSegment],
        chunk_size: int,
        chunk_overlap: int,
        *,
        remaining_chunks: int,
    ) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for segment in segments:
            text = self._normalize_text(segment.text)
            if not text:
                continue
            start = 0
            while start < len(text) and len(chunks) < remaining_chunks:
                end = min(len(text), start + chunk_size)
                if end < len(text):
                    boundary = max(text.rfind("\n", start + max(120, chunk_size // 2), end), text.rfind(". ", start + max(120, chunk_size // 2), end))
                    if boundary > start:
                        end = min(len(text), boundary + 1)
                chunk_text = text[start:end].strip()
                if chunk_text:
                    chunk_hash = hashlib.sha256(chunk_text.encode("utf-8", errors="replace")).hexdigest()
                    chunks.append(
                        {
                            "text": chunk_text,
                            "token_estimate": max(1, math.ceil(len(chunk_text) / 4)),
                            "page_label": segment.page_label,
                            "chunk_hash": chunk_hash,
                        }
                    )
                if end >= len(text):
                    break
                start = max(end - chunk_overlap, start + 1)
        return chunks

    def _embed_chunks(self, chunks: list[dict[str, Any]], *, provider: str, model: str, dimensions: int) -> dict[str, str]:
        result: dict[str, str] = {}
        missing: list[dict[str, Any]] = []
        cache_dimensions = int(dimensions or 0)
        with self.db.get_connection() as conn:
            for chunk in chunks:
                row = conn.execute(
                    """
                    SELECT id FROM digestion_embeddings
                    WHERE provider = ? AND model = ? AND dimensions = ? AND chunk_hash = ?
                    """,
                    (provider, model, cache_dimensions, chunk["chunk_hash"]),
                ).fetchone()
                if row:
                    result[chunk["chunk_hash"]] = str(row["id"])
                else:
                    missing.append(chunk)
        if not missing:
            return result
        vectors = self._embed_texts(
            [chunk["text"] for chunk in missing],
            provider=provider,
            model=model,
            dimensions=dimensions,
        )
        now = self._now()
        with self.db.get_connection() as conn:
            for chunk, vector in zip(missing, vectors):
                embedding_id = f"Dge{secrets.token_hex(12)}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO digestion_embeddings (
                        id, provider, model, dimensions, chunk_hash, vector_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        embedding_id,
                        provider,
                        model,
                        cache_dimensions,
                        chunk["chunk_hash"],
                        json.dumps(vector, separators=(",", ":")),
                        now,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT id FROM digestion_embeddings
                    WHERE provider = ? AND model = ? AND dimensions = ? AND chunk_hash = ?
                    """,
                    (provider, model, cache_dimensions, chunk["chunk_hash"]),
                ).fetchone()
                if row:
                    result[chunk["chunk_hash"]] = str(row["id"])
            conn.commit()
        return result

    def _embed_one(self, text: str, *, provider: str, model: str, dimensions: int) -> list[float]:
        return self._embed_texts([text], provider=provider, model=model, dimensions=dimensions)[0]

    def _embed_texts(self, texts: list[str], *, provider: str, model: str, dimensions: int) -> list[list[float]]:
        provider = self._normalize_provider(provider)
        if provider == "local_hash":
            dims = dimensions or DEFAULT_LOCAL_DIMENSIONS
            return [self._local_hash_embedding(text, dims) for text in texts]
        if provider == "openai":
            return self._openai_embeddings(texts, model=model, dimensions=dimensions)
        raise DigestionError(f"Unsupported embedding provider: {provider}", status_code=400, reason="unsupported_provider")

    def _openai_embeddings(self, texts: list[str], *, model: str, dimensions: int) -> list[list[float]]:
        api_key = self._openai_api_key()
        if not api_key:
            raise DigestionError(
                "OpenAI embedding key is not configured. Set OPENAI_API_KEY or CANOPY_OPENAI_API_KEY, or use provider=local_hash for local testing.",
                status_code=503,
                reason="embedding_key_missing",
            )
        try:
            batch_size = int(os.getenv("CANOPY_DIGESTION_EMBEDDING_BATCH_SIZE", "64") or 64)
        except Exception:
            batch_size = 64
        batch_size = max(1, min(batch_size, 128))
        if len(texts) > batch_size:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), batch_size):
                vectors.extend(self._openai_embeddings(texts[start:start + batch_size], model=model, dimensions=dimensions))
            return vectors
        payload: dict[str, Any] = {"model": model or DEFAULT_OPENAI_EMBEDDING_MODEL, "input": texts}
        if int(dimensions or 0) > 0:
            payload["dimensions"] = int(dimensions)
        body = json.dumps(payload).encode("utf-8")
        req = Request(
            _OPENAI_EMBEDDINGS_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Canopy-Digestions/1.0",
            },
            method="POST",
        )
        try:
            timeout = float(os.getenv("CANOPY_DIGESTION_OPENAI_TIMEOUT", "60"))
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                pass
            raise DigestionError(
                f"OpenAI embeddings request failed ({exc.code}): {detail or exc.reason}",
                status_code=502,
                reason="embedding_provider_http_error",
            ) from exc
        except URLError as exc:
            raise DigestionError(
                f"Could not reach OpenAI embeddings endpoint: {exc}",
                status_code=502,
                reason="embedding_provider_unreachable",
            ) from exc
        except Exception as exc:
            raise DigestionError("OpenAI embeddings returned an unreadable response.", status_code=502, reason="embedding_provider_bad_response") from exc
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise DigestionError("OpenAI embeddings response did not include data[].", status_code=502, reason="embedding_provider_bad_response")
        ordered: list[Optional[list[float]]] = [None] * len(texts)
        for item in rows:
            try:
                idx = int(item.get("index"))
                vector = [float(v) for v in item.get("embedding") or []]
                if 0 <= idx < len(ordered) and vector:
                    ordered[idx] = vector
            except Exception:
                continue
        if any(vector is None for vector in ordered):
            raise DigestionError("OpenAI embeddings response was incomplete.", status_code=502, reason="embedding_provider_incomplete")
        return [vector or [] for vector in ordered]

    @staticmethod
    def _local_hash_embedding(text: str, dimensions: int) -> list[float]:
        dims = max(32, min(int(dimensions or DEFAULT_LOCAL_DIMENSIONS), 4096))
        vector = [0.0] * dims
        for token in _TOKEN_RE.findall(str(text or "").lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "big")
            index = raw % dims
            sign = -1.0 if (raw >> 8) & 1 else 1.0
            weight = 1.0 + min(len(token), 24) / 24.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [round(value / norm, 8) for value in vector]

    # ------------------------------------------------------------------
    # Row and permission helpers
    # ------------------------------------------------------------------
    def _require_digestion(self, digestion_id: str, actor_user_id: str, *, query: bool = False, manage: bool = False) -> Digestion:
        actor_user_id = self._clean_id(actor_user_id)
        digestion = self._get_digestion_obj(digestion_id)
        if not digestion:
            raise DigestionError("Digestion not found", status_code=404, reason="not_found")
        access = self._access_for(digestion, actor_user_id)
        if manage and not access.get("can_manage"):
            raise DigestionError("You do not have management access to this Digestion.", status_code=403, reason="manage_denied")
        if query and not (access.get("can_query") or access.get("can_manage")):
            raise DigestionError("You do not have query access to this Digestion.", status_code=403, reason="query_denied")
        return digestion

    def _get_digestion_obj(self, digestion_id: str) -> Optional[Digestion]:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM digestions WHERE id = ?", (self._clean_id(digestion_id),)).fetchone()
        return self._digestion_from_row(row) if row else None

    def _access_for(self, digestion: Digestion, user_id: str) -> dict[str, Any]:
        if str(user_id) == str(digestion.owner_user_id):
            return {"role": "owner", "can_query": True, "can_manage": True, "can_read_sources": True}
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT can_query, can_manage, can_read_sources FROM digestion_acl WHERE digestion_id = ? AND grantee_user_id = ?",
                (digestion.id, user_id),
            ).fetchone()
        if not row:
            return {"role": "none", "can_query": False, "can_manage": False, "can_read_sources": False}
        return {
            "role": "manager" if bool(row["can_manage"]) else "reader",
            "can_query": bool(row["can_query"]),
            "can_manage": bool(row["can_manage"]),
            "can_read_sources": bool(row["can_read_sources"]),
        }

    def _access_from_row(self, row: Any, viewer_user_id: str) -> dict[str, Any]:
        owner = str(row["owner_user_id"] or "")
        if viewer_user_id and str(viewer_user_id) == owner:
            return {"role": "owner", "can_query": True, "can_manage": True, "can_read_sources": True}
        can_manage = bool(row["can_manage"] if "can_manage" in row.keys() else 0)
        can_query = bool(row["can_query"] if "can_query" in row.keys() else 0)
        can_read_sources = bool(row["can_read_sources"] if "can_read_sources" in row.keys() else 0)
        return {
            "role": "manager" if can_manage else ("reader" if can_query else "none"),
            "can_query": can_query,
            "can_manage": can_manage,
            "can_read_sources": can_read_sources,
        }

    def _digestion_from_row(self, row: Any) -> Digestion:
        settings_raw = row["settings_json"] if "settings_json" in row.keys() else "{}"
        try:
            settings = json.loads(settings_raw or "{}")
        except Exception:
            settings = {}
        return Digestion(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            name=str(row["name"] or ""),
            description=str(row["description"] or ""),
            purpose=str(row["purpose"] or ""),
            status=str(row["status"] or "draft"),
            provider=str(row["provider"] or "openai"),
            embedding_model=str(row["embedding_model"] or DEFAULT_OPENAI_EMBEDDING_MODEL),
            embedding_dimensions=int(row["embedding_dimensions"] or 0),
            chunk_size=int(row["chunk_size"] or DEFAULT_CHUNK_SIZE),
            chunk_overlap=int(row["chunk_overlap"] or DEFAULT_CHUNK_OVERLAP),
            settings=settings if isinstance(settings, dict) else {},
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            built_at=str(row["built_at"] or "") or None,
            error=str(row["error"] or "") or None,
        )

    def _source_rows(self, digestion_id: str) -> list[Any]:
        with self.db.get_connection() as conn:
            return conn.execute(
                "SELECT * FROM digestion_sources WHERE digestion_id = ? ORDER BY file_name COLLATE NOCASE, file_id",
                (digestion_id,),
            ).fetchall()

    def _queryable_chunk_rows(self, digestion_id: str) -> list[Any]:
        with self.db.get_connection() as conn:
            return conn.execute(
                """
                SELECT c.id AS chunk_id, c.file_id, c.chunk_index, c.text, c.token_estimate,
                       c.page_label, s.file_name, s.content_type, e.vector_json
                FROM digestion_chunks c
                JOIN digestion_embeddings e ON e.id = c.embedding_id
                LEFT JOIN digestion_sources s ON s.digestion_id = c.digestion_id AND s.file_id = c.file_id
                WHERE c.digestion_id = ?
                """,
                (digestion_id,),
            ).fetchall()

    def _log_query(self, digestion_id: str, user_id: str, query_text: str, result_count: int) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO digestion_query_log (digestion_id, user_id, query, result_count, created_at) VALUES (?, ?, ?, ?, ?)",
                (digestion_id, user_id, str(query_text or "")[:4000], int(result_count or 0), self._now()),
            )
            conn.commit()

    @staticmethod
    def _query_terms(text: str) -> set[str]:
        return {
            token
            for token in _TOKEN_RE.findall(str(text or "").lower())
            if len(token) > 2 and token not in _COMMON_TERMS
        }

    def _source_summary_rows(self, digestion_id: str) -> list[dict[str, Any]]:
        rows = self._source_rows(digestion_id)
        summaries: list[dict[str, Any]] = []
        for row in rows:
            metadata_raw = self._row_get(row, "source_metadata_json", "{}")
            try:
                metadata = json.loads(metadata_raw or "{}")
            except Exception:
                metadata = {}
            summaries.append({
                "file_id": str(row["file_id"] or ""),
                "file_name": str(row["file_name"] or row["file_id"] or ""),
                "content_type": str(row["content_type"] or ""),
                "source_kind": str(self._row_get(row, "source_kind", "vault_file") or "vault_file"),
                "source_label": str(self._row_get(row, "source_label", "") or row["file_name"] or ""),
                "source_uri": str(self._row_get(row, "source_uri", "") or ""),
                "status": str(row["status"] or ""),
                "extracted_chars": int(row["extracted_chars"] or 0),
                "chunk_count": int(row["chunk_count"] or 0),
                "error": str(row["error"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "metadata": metadata if isinstance(metadata, dict) else {},
            })
        return summaries

    def _build_manifest_output(self, digestion: Digestion) -> tuple[str, str, str, str, dict[str, Any]]:
        sources = self._source_summary_rows(digestion.id)
        stats = self.stats(digestion.id)
        manifest = {
            "kind": "canopy_digestion_manifest_v2",
            "digestion": {
                "id": digestion.id,
                "name": digestion.name,
                "description": digestion.description,
                "purpose": digestion.purpose,
                "status": digestion.status,
                "provider": digestion.provider,
                "embedding_model": digestion.embedding_model,
                "embedding_dimensions": digestion.embedding_dimensions,
                "chunk_size": digestion.chunk_size,
                "chunk_overlap": digestion.chunk_overlap,
                "built_at": digestion.built_at,
                "updated_at": digestion.updated_at,
            },
            "processing": {
                "pipeline": [
                    "source_registration",
                    "type_detection",
                    "text_extraction",
                    "normalization",
                    "semantic_chunking",
                    "embedding",
                    "cited_retrieval",
                    "output_artifact_generation",
                ],
                "local_by_default": True,
                "source_files_mesh_synced": False,
                "raw_vectors_mesh_synced": False,
            },
            "stats": stats,
            "sources": sources,
            "source_kinds": self._count_by_key(sources, "source_kind"),
            "generated_at": self._now(),
        }
        return (
            "manifest",
            f"{digestion.name or 'Digestion'} Manifest",
            "application/json",
            json.dumps(manifest, indent=2, sort_keys=True),
            {"source_count": len(sources), "stats": stats},
        )

    def _build_human_brief_output(self, digestion: Digestion) -> tuple[str, str, str, str, dict[str, Any]]:
        sources = self._source_summary_rows(digestion.id)
        stats = self.stats(digestion.id)
        terms = self._top_terms(digestion.id, limit=12)
        source_lines = [
            f"- {src['source_label'] or src['file_name']} ({src['source_kind']}; {src['chunk_count']} chunks; {src['status']})"
            for src in sources[:30]
        ]
        if len(sources) > 30:
            source_lines.append(f"- ... plus {len(sources) - 30} additional sources")
        body = f"""# {digestion.name or 'Canopy Digestion'} Brief

{digestion.description or 'Reusable Canopy Digestion generated from approved source material.'}

## Purpose
{digestion.purpose or 'Query and reuse this corpus with cited retrieval inside Canopy.'}

## What was digested
{chr(10).join(source_lines) if source_lines else '- No sources are registered yet.'}

## Processing state
- Status: {digestion.status}
- Indexed chunks: {stats.get('chunks', 0)}
- Estimated tokens: {stats.get('token_estimate', 0)}
- Pipeline: type detection -> text extraction -> normalization -> chunking -> embeddings -> cited retrieval -> reusable outputs

## Useful terms
{', '.join(terms) if terms else 'No stable terms extracted yet.'}

## Reuse guidance
Humans and agents should query this Digestion for grounded answers and cite returned source snippets. Query access does not grant unrestricted raw Vault access.
""".strip()
        return (
            "human_brief",
            f"{digestion.name or 'Digestion'} Brief",
            "text/markdown",
            body,
            {"source_count": len(sources), "top_terms": terms, "stats": stats},
        )

    def _build_agent_context_output(self, digestion: Digestion) -> tuple[str, str, str, str, dict[str, Any]]:
        stats = self.stats(digestion.id)
        body = f"""# Agent Context: {digestion.name or 'Canopy Digestion'}

Digestion ID: `{digestion.id}`

Use this as a permissioned retrieval capability, not as raw file access.

## Operating rules
- Query this Digestion before answering questions about its corpus.
- Use cited snippets from query results as the grounding source.
- Do not claim the full source files were read unless source access was explicitly granted.
- Preserve citation labels such as file name, page label, and chunk index in work products.
- If results are weak or absent, say that the Digestion did not contain enough evidence.

## Current state
- Status: {digestion.status}
- Provider/model: {digestion.provider} / {digestion.embedding_model}
- Chunks: {stats.get('chunks', 0)}
- Token estimate: {stats.get('token_estimate', 0)}

## Suggested API flow
1. `POST /api/v1/digestions/{digestion.id}/query` with `query` and `top_k`.
2. Use `/api/v1/digestions/{digestion.id}/context` when you need a compact prompt-ready context pack.
3. Write durable synthesis back to Canopy posts, cards, or Vault files as requested.
""".strip()
        return (
            "agent_context",
            f"{digestion.name or 'Digestion'} Agent Context",
            "text/markdown",
            body,
            {"stats": stats},
        )

    def _upsert_output(
        self,
        digestion: Digestion,
        actor_user_id: str,
        output_kind: str,
        title: str,
        content_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        now = self._now()
        with self.db.get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM digestion_outputs WHERE digestion_id = ? AND output_kind = ?",
                (digestion.id, output_kind),
            ).fetchone()
            output_id = str(existing["id"]) if existing else f"Dgo{secrets.token_hex(12)}"
            conn.execute(
                """
                INSERT INTO digestion_outputs (
                    id, digestion_id, output_kind, title, content_type, content,
                    metadata_json, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(digestion_id, output_kind) DO UPDATE SET
                    title = excluded.title,
                    content_type = excluded.content_type,
                    content = excluded.content,
                    metadata_json = excluded.metadata_json,
                    created_by = excluded.created_by,
                    updated_at = excluded.updated_at
                """,
                (
                    output_id,
                    digestion.id,
                    output_kind,
                    title[:240],
                    content_type,
                    content,
                    json.dumps(metadata or {}, sort_keys=True),
                    actor_user_id,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id, digestion_id, output_kind, title, content_type, content,
                       metadata_json, created_by, created_at, updated_at
                FROM digestion_outputs
                WHERE digestion_id = ? AND output_kind = ?
                """,
                (digestion.id, output_kind),
            ).fetchone()
            conn.commit()
        return self._output_row_to_dict(row, include_content=False) if row else {}

    def _output_row_to_dict(self, row: Any, *, include_content: bool = False) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        content = str(row["content"] or "")
        data = {
            "id": str(row["id"] or ""),
            "digestion_id": str(row["digestion_id"] or ""),
            "output_kind": str(row["output_kind"] or ""),
            "title": str(row["title"] or ""),
            "content_type": str(row["content_type"] or "text/markdown"),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_by": str(row["created_by"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "size_chars": len(content),
            "preview": self._snippet(content),
        }
        if include_content:
            data["content"] = content
        return data

    @staticmethod
    def _count_by_key(items: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "unknown")
            counts[value] = counts.get(value, 0) + 1
        return counts

    def _top_terms(self, digestion_id: str, *, limit: int = 12) -> list[str]:
        counts: dict[str, int] = {}
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT text FROM digestion_chunks WHERE digestion_id = ? LIMIT 500", (digestion_id,)).fetchall()
        for row in rows:
            for token in _TOKEN_RE.findall(str(row["text"] or "").lower()):
                if len(token) < 4 or token in _COMMON_TERMS or token.isdigit():
                    continue
                counts[token] = counts.get(token, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [term for term, _ in ranked[:limit]]

    def _set_status(self, digestion_id: str, status: str, *, built_at: Optional[str] = None, error: Optional[str] = None) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE digestions SET status = ?, built_at = COALESCE(?, built_at), error = ?, updated_at = ? WHERE id = ?",
                (status, built_at, error, self._now(), digestion_id),
            )
            conn.commit()

    def _mark_source_error(self, digestion_id: str, file_id: str, message: str) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                """
                UPDATE digestion_sources
                SET status = 'error', error = ?, updated_at = ?
                WHERE digestion_id = ? AND file_id = ?
                """,
                (message[:1000], self._now(), digestion_id, file_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _material_to_vault_file(
        self,
        digestion: Digestion,
        actor_user_id: str,
        material: dict[str, Any],
        *,
        index: int,
    ) -> tuple[FileInfo, dict[str, Any]]:
        source_kind = self._normalize_material_kind(material.get("source_kind") or material.get("kind") or "inline_text")
        title = str(material.get("title") or material.get("name") or f"{source_kind} material {index}").strip()
        if not title:
            title = f"{source_kind} material {index}"
        content = str(material.get("content") or material.get("text") or material.get("body") or "").strip()
        if not content:
            raise DigestionError("Material content is required.", status_code=400, reason="missing_material_content")
        source_uri = str(material.get("source_uri") or material.get("uri") or material.get("url") or "").strip()
        content_type = str(material.get("content_type") or material.get("mime_type") or "").strip().lower()
        if not content_type:
            content_type = "text/markdown" if source_kind in {"markdown", "note", "post", "inline_text"} else "text/plain"
        ext = ".md" if content_type in {"text/markdown", "text/x-markdown"} else ".txt"
        filename = f"digestion-{self._slugify(title)}-{secrets.token_hex(3)}{ext}"
        metadata = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
        header = {
            "digestion_id": digestion.id,
            "digestion_name": digestion.name,
            "source_kind": source_kind,
            "source_label": title,
            "source_uri": source_uri,
            "ingested_at": self._now(),
            "metadata": metadata,
        }
        normalized_text = self._normalize_material_text(content, title=title, header=header, content_type=content_type)
        # Managed material ingestion becomes part of the Digestion owner's local
        # corpus so later builds do not depend on the manager/agent's Vault.
        file_info = self.file_manager.save_file(normalized_text.encode("utf-8"), filename, content_type, digestion.owner_user_id)
        if not file_info:
            raise DigestionError("Could not persist normalized material to Vault.", status_code=500, reason="material_vault_save_failed")
        material_meta = dict(header)
        material_meta.update({
            "vault_file_id": file_info.id,
            "vault_file_name": file_info.original_name,
            "content_type": file_info.content_type,
            "checksum": file_info.checksum,
            "submitted_by": actor_user_id,
            "ingest_path": "normalized_material_to_vault",
        })
        return file_info, material_meta

    @staticmethod
    def _normalize_material_text(content: str, *, title: str, header: dict[str, Any], content_type: str) -> str:
        clean = DigestionManager._normalize_text(content)[:MAX_FILE_CHARS]
        if content_type in {"text/markdown", "text/x-markdown"}:
            frontmatter = "\n".join([
                "---",
                f"canopy_digestion_id: {header.get('digestion_id') or ''}",
                f"source_kind: {header.get('source_kind') or ''}",
                f"source_label: {header.get('source_label') or ''}",
                f"source_uri: {header.get('source_uri') or ''}",
                "---",
                "",
            ])
            if not clean.lstrip().startswith("#"):
                clean = f"# {title}\n\n{clean}"
            return f"{frontmatter}{clean}".strip() + "\n"
        prefix = textwrap.dedent(f"""\
            Canopy Digestion Source
            Digestion ID: {header.get('digestion_id') or ''}
            Source kind: {header.get('source_kind') or ''}
            Source label: {header.get('source_label') or ''}
            Source URI: {header.get('source_uri') or ''}

        """)
        return f"{prefix}{clean}".strip() + "\n"

    @staticmethod
    def _normalize_material_kind(value: Any) -> str:
        raw = str(value or "inline_text").strip().lower().replace(" ", "_")
        raw = _MATERIAL_KIND_RE.sub("_", raw).strip("_")
        return (raw or "inline_text")[:80]

    @staticmethod
    def _slugify(value: Any) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower()).strip("-")
        return (slug or "digestion")[:80]

    @staticmethod
    def _row_get(row: Any, key: str, default: Any = None) -> Any:
        try:
            if hasattr(row, "keys") and key in row.keys():
                return row[key]
        except Exception:
            pass
        try:
            return row[key]
        except Exception:
            return default

    @staticmethod
    def _normalize_provider(provider: Optional[str]) -> str:
        raw = str(provider or os.getenv("CANOPY_DIGESTION_PROVIDER") or "").strip().lower().replace("-", "_")
        if not raw:
            raw = "openai" if (os.getenv("CANOPY_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")) else "local_hash"
        aliases = {"local": "local_hash", "hash": "local_hash", "localhash": "local_hash", "openai_embeddings": "openai"}
        raw = aliases.get(raw, raw)
        if raw not in {"openai", "local_hash"}:
            raise DigestionError("Unsupported Digestion embedding provider.", status_code=400, reason="unsupported_provider")
        return raw

    @staticmethod
    def _normalize_dimensions(value: Optional[int], provider: str) -> int:
        try:
            parsed = int(value or 0)
        except Exception:
            parsed = 0
        if parsed <= 0 and provider == "local_hash":
            return DEFAULT_LOCAL_DIMENSIONS
        return max(0, min(parsed, 4096))

    @staticmethod
    def _clean_id(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalize_text(value: str) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()

    @staticmethod
    def _spreadsheet_preview_to_text(preview: dict[str, Any]) -> str:
        sections: list[str] = []
        for sheet in preview.get("sheets") or []:
            title = str(sheet.get("name") or "Sheet").strip()
            rows_text: list[str] = []
            for row in sheet.get("rows") or []:
                cells = []
                for cell in row or []:
                    if isinstance(cell, dict):
                        cells.append(str(cell.get("display") or ""))
                    else:
                        cells.append(str(cell or ""))
                row_text = "\t".join(cell for cell in cells if cell != "")
                if row_text.strip():
                    rows_text.append(row_text)
            if rows_text:
                sections.append(f"Sheet: {title}\n" + "\n".join(rows_text))
        return DigestionManager._normalize_text("\n\n".join(sections))

    @staticmethod
    def _snippet(text: str) -> str:
        clean = DigestionManager._normalize_text(text)
        if len(clean) <= MAX_SNIPPET_CHARS:
            return clean
        return clean[:MAX_SNIPPET_CHARS].rstrip() + "..."

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(float(x) * float(y) for x, y in zip(a, b))
        norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
        norm_b = math.sqrt(sum(float(y) * float(y) for y in b))
        if norm_a <= 0 or norm_b <= 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _openai_api_key() -> str:
        return str(os.getenv("CANOPY_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
