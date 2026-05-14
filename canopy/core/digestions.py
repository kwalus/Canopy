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
_OPENAI_EMBEDDINGS_URL = os.getenv(
    "CANOPY_DIGESTION_OPENAI_EMBEDDINGS_URL",
    "https://api.openai.com/v1/embeddings",
).strip() or "https://api.openai.com/v1/embeddings"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{2,}")


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

                CREATE INDEX IF NOT EXISTS idx_digestions_owner ON digestions(owner_user_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_acl_grantee ON digestion_acl(grantee_user_id, can_query, can_manage);
                CREATE INDEX IF NOT EXISTS idx_digestion_chunks_digestion ON digestion_chunks(digestion_id, file_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_digestion_sources_status ON digestion_sources(digestion_id, status);
                """
            )
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
                        status, extracted_chars, chunk_count, error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, NULL, ?)
                    ON CONFLICT(digestion_id, file_id) DO UPDATE SET
                        file_checksum = excluded.file_checksum,
                        file_name = excluded.file_name,
                        content_type = excluded.content_type,
                        status = 'pending',
                        error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        digestion.id,
                        info.id,
                        info.checksum,
                        info.original_name,
                        info.content_type,
                        now,
                    ),
                )
                added += 1
            conn.execute("UPDATE digestions SET status = ?, updated_at = ? WHERE id = ?", ("draft", now, digestion.id))
            conn.commit()
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
                       extracted_chars, chunk_count, error, updated_at
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
            return {
                "success": status in {"ready", "ready_with_errors"},
                "digestion_id": digestion.id,
                "status": status,
                "started_at": started,
                "built_at": self._now(),
                "chunk_count": total_chunks,
                "embedded_count": embedded_count,
                "errors": errors,
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
        query_vector = self._embed_one(
            query_text,
            provider=digestion.provider,
            model=digestion.embedding_model,
            dimensions=digestion.embedding_dimensions,
        )
        rows = self._queryable_chunk_rows(digestion.id)
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
            scored.append(
                {
                    "chunk_id": row["chunk_id"],
                    "file_id": row["file_id"],
                    "file_name": row["file_name"] or row["file_id"],
                    "content_type": row["content_type"] or "application/octet-stream",
                    "chunk_index": int(row["chunk_index"] or 0),
                    "page_label": row["page_label"] or "",
                    "score": round(float(score), 6),
                    "token_estimate": int(row["token_estimate"] or 0),
                    "snippet": self._snippet(text) if include_snippets else "",
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        results = scored[:top_k]
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO digestion_query_log (digestion_id, user_id, query, result_count, created_at) VALUES (?, ?, ?, ?, ?)",
                (digestion.id, actor_user_id, query_text[:4000], len(results), self._now()),
            )
            conn.commit()
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
