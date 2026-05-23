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
from threading import RLock
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
MIN_PARTIAL_QUERY_TERM_CHARS = int(os.getenv("CANOPY_DIGESTION_PARTIAL_QUERY_TERM_MIN_CHARS", "3") or "3")
STRUCTURED_DATAPOINT_OUTPUT_KIND = "structured_datapoints"
STRUCTURED_DATAPOINT_SCHEMA_VERSION = "canopy_structured_datapoints_v1"
AGENT_CONTRIBUTION_SCHEMA_VERSION = "canopy_agent_digestion_contribution_v1"
DIGESTION_CONTRIBUTION_LEDGER_SCHEMA_VERSION = "canopy_digestion_contribution_ledger_v1"
CONTRIBUTION_STATUS_PENDING = "pending"
CONTRIBUTION_STATUS_ACCEPTED = "accepted"
CONTRIBUTION_STATUS_REVIEWED = "reviewed"
CONTRIBUTION_STATUS_REJECTED = "rejected"
CONTRIBUTION_STATUSES = {
    CONTRIBUTION_STATUS_PENDING,
    CONTRIBUTION_STATUS_ACCEPTED,
    CONTRIBUTION_STATUS_REVIEWED,
    CONTRIBUTION_STATUS_REJECTED,
}
DEFAULT_STRUCTURED_DATAPOINT_CHUNKS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_DEFAULT_CHUNKS", "80"))
MAX_STRUCTURED_DATAPOINT_CHUNKS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_MAX_CHUNKS", "240"))
DEFAULT_STRUCTURED_DATAPOINTS_PER_RUN = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_DEFAULT_RECORDS", "400"))
MAX_STRUCTURED_DATAPOINTS_PER_RUN = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_MAX_RECORDS", "1200"))
MAX_AGENT_CONTRIBUTIONS_PER_APPEND = int(os.getenv("CANOPY_DIGESTION_MAX_AGENT_CONTRIBUTIONS_PER_APPEND", "50"))
MAX_AGENT_DATAPOINTS_PER_APPEND = int(os.getenv("CANOPY_DIGESTION_MAX_AGENT_DATAPOINTS_PER_APPEND", "500"))
MAX_STRUCTURED_DATAPOINT_LLM_BATCH_CHUNKS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_BATCH_CHUNKS", "6"))
MAX_STRUCTURED_DATAPOINT_LLM_BATCH_CHARS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_BATCH_CHARS", "18000"))
MAX_STRUCTURED_DATAPOINT_LLM_CHUNK_CHARS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_CHUNK_CHARS", "2800"))
MAX_STRUCTURED_DATAPOINTS_PER_LLM_BATCH = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_BATCH_RECORDS", "40"))
MAX_STRUCTURED_DATAPOINT_LLM_OUTPUT_TOKENS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_OUTPUT_TOKENS", "7000"))
DATAPOINT_MIN_TERM_OVERLAP = float(os.getenv("CANOPY_DIGESTION_DATAPOINT_MIN_TERM_OVERLAP", "0.75"))
PDF_FIGURE_OUTPUT_KIND = "pdf_figures"
PDF_FIGURE_SCHEMA_VERSION = "canopy_pdf_figures_v1"
MAX_PDF_FIGURES_PER_SOURCE = int(os.getenv("CANOPY_DIGESTION_MAX_PDF_FIGURES_PER_SOURCE", "80"))
MAX_PDF_FIGURE_BYTES = int(os.getenv("CANOPY_DIGESTION_MAX_PDF_FIGURE_BYTES", str(8 * 1024 * 1024)))
MIN_PDF_FIGURE_DIMENSION = int(os.getenv("CANOPY_DIGESTION_MIN_PDF_FIGURE_DIMENSION", "64"))
_SOURCE_REVEALING_OUTPUT_KINDS = {"manifest", "human_brief", STRUCTURED_DATAPOINT_OUTPUT_KIND, PDF_FIGURE_OUTPUT_KIND}
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
        self._progress_lock = RLock()
        self._operation_progress: dict[str, dict[str, dict[str, Any]]] = {}
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

                CREATE TABLE IF NOT EXISTS digestion_pdf_figures (
                    id TEXT PRIMARY KEY,
                    digestion_id TEXT NOT NULL,
                    source_file_id TEXT NOT NULL,
                    source_checksum TEXT,
                    figure_index INTEGER NOT NULL,
                    page_number INTEGER NOT NULL DEFAULT 0,
                    page_label TEXT,
                    image_file_id TEXT,
                    image_name TEXT,
                    content_type TEXT,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    byte_size INTEGER NOT NULL DEFAULT 0,
                    caption TEXT,
                    context_text TEXT,
                    vision_description TEXT,
                    extraction_method TEXT,
                    metadata_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(digestion_id, source_file_id, figure_index),
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                    FOREIGN KEY (source_file_id) REFERENCES files(id) ON DELETE CASCADE,
                    FOREIGN KEY (image_file_id) REFERENCES files(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS digestion_contributions (
                    id TEXT PRIMARY KEY,
                    digestion_id TEXT NOT NULL,
                    contributor_user_id TEXT,
                    contribution_kind TEXT NOT NULL DEFAULT 'agent_output',
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'accepted',
                    payload_json TEXT,
                    summary TEXT,
                    tags_json TEXT,
                    confidence REAL,
                    source_file_ids_json TEXT,
                    material_file_ids_json TEXT,
                    added_source_file_ids_json TEXT,
                    datapoint_count INTEGER NOT NULL DEFAULT 0,
                    skipped_json TEXT,
                    result_json TEXT,
                    metadata_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reviewed_by TEXT,
                    reviewed_at TIMESTAMP,
                    review_note TEXT,
                    accepted_at TIMESTAMP,
                    rejected_at TIMESTAMP,
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                    FOREIGN KEY (contributor_user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_digestions_owner ON digestions(owner_user_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_acl_grantee ON digestion_acl(grantee_user_id, can_query, can_manage);
                CREATE INDEX IF NOT EXISTS idx_digestion_chunks_digestion ON digestion_chunks(digestion_id, file_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_digestion_sources_status ON digestion_sources(digestion_id, status);
                CREATE INDEX IF NOT EXISTS idx_digestion_outputs ON digestion_outputs(digestion_id, output_kind);
                CREATE INDEX IF NOT EXISTS idx_digestion_pdf_figures ON digestion_pdf_figures(digestion_id, source_file_id, page_number);
                CREATE INDEX IF NOT EXISTS idx_digestion_contributions_digestion ON digestion_contributions(digestion_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_contributions_contributor ON digestion_contributions(contributor_user_id, created_at);
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
        """Add Vault files to a Digestion, copying manager-owned files into the owner-bound corpus."""
        actor_user_id = self._clean_id(owner_user_id)
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        digestion_owner_id = str(digestion.owner_user_id)
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
        source_results: list[dict[str, Any]] = []
        intake_folder_id: Optional[str] = None
        with self.db.get_connection() as conn:
            for file_id in unique_file_ids:
                original_info = self.file_manager.get_file(file_id)
                if not original_info:
                    skipped.append({"file_id": file_id, "reason": "not_found"})
                    continue
                original_owner = str(original_info.uploaded_by or "")
                if original_owner not in {actor_user_id, digestion_owner_id}:
                    skipped.append({"file_id": file_id, "reason": "not_owned_by_actor_or_digestion_owner"})
                    continue

                copied_to_owner = False
                source_info = original_info
                existing_source_id = self._existing_source_file_for_original(
                    digestion.id,
                    original_info.id,
                    checksum=original_info.checksum,
                )
                if existing_source_id:
                    existing_info = self.file_manager.get_file(existing_source_id)
                    if existing_info:
                        source_info = existing_info
                elif original_owner != digestion_owner_id:
                    source_path = self._resolved_source_file_path(original_info)
                    if not source_path or not source_path.exists():
                        skipped.append({
                            "file_id": file_id,
                            "reason": "source_file_unavailable_for_owner_copy",
                            "source_owner_user_id": original_owner,
                            "submitted_by": actor_user_id,
                            "digestion_owner_user_id": digestion_owner_id,
                            "hint": (
                                "The file record is visible, but this node does not have the local bytes yet. "
                                "Wait for transfer, re-upload the file here, or ask the owner to add an available Vault copy."
                            ),
                        })
                        continue
                    if intake_folder_id is None:
                        intake_folder_id = self._digestion_intake_folder_id(digestion)
                    if not intake_folder_id:
                        skipped.append({
                            "file_id": file_id,
                            "reason": "intake_folder_unavailable",
                            "source_owner_user_id": original_owner,
                            "submitted_by": actor_user_id,
                            "digestion_owner_user_id": digestion_owner_id,
                            "hint": (
                                "Manage access passed, but Canopy could not create the owner's Digestion Intake folder. "
                                "Check Vault folder database permissions and retry."
                            ),
                        })
                        continue
                    copied_info = self.file_manager.copy_file_to_user_vault(
                        original_info.id,
                        digestion_owner_id,
                        vault_folder_id=intake_folder_id,
                        duplicate_if_owned=True,
                    )
                    if not copied_info:
                        skipped.append({
                            "file_id": file_id,
                            "reason": "copy_to_digestion_owner_failed",
                            "source_owner_user_id": original_owner,
                            "submitted_by": actor_user_id,
                            "digestion_owner_user_id": digestion_owner_id,
                            "hint": (
                                "Manage access passed, but Canopy could not persist an owner-side Vault copy. "
                                "Check local storage permissions and retry, or have the owner add the source directly."
                            ),
                        })
                        continue
                    source_info = copied_info
                    copied_to_owner = True

                metadata = {
                    "ingest_path": "vault_file",
                    "submitted_by": actor_user_id,
                    "source_owner_user_id": str(source_info.uploaded_by or ""),
                    "original_file_id": original_info.id,
                }
                if source_info.id != original_info.id:
                    metadata.update({
                        "original_file_name": original_info.original_name,
                        "original_uploaded_by": original_owner,
                        "original_checksum": original_info.checksum,
                        "copied_to_owner_vault": True,
                        "owner_intake_folder_id": intake_folder_id or source_info.vault_folder_id,
                        "owner_intake_folder": self._digestion_intake_folder_name(digestion),
                    })
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
                        source_info.id,
                        source_info.checksum,
                        source_info.original_name,
                        source_info.content_type,
                        now,
                        source_info.original_name,
                        json.dumps(metadata, sort_keys=True),
                    ),
                )
                # Release the Digestion write lock before the next manager-owned
                # source is copied through FileManager, which performs its own
                # Vault/file-table write on a separate SQLite connection.
                conn.commit()
                added += 1
                source_results.append({
                    "input_file_id": original_info.id,
                    "file_id": source_info.id,
                    "file_name": source_info.original_name,
                    "copied_to_owner_vault": copied_to_owner,
                    "submitted_by": actor_user_id,
                })
            conn.execute("UPDATE digestions SET status = ?, updated_at = ? WHERE id = ?", ("draft", now, digestion.id))
            conn.commit()
        return {"success": True, "added": added, "skipped": skipped, "digestion_id": digestion.id, "sources": source_results}

    @staticmethod
    def _safe_vault_folder_segment(value: Any, *, fallback: str, limit: int = 80) -> str:
        clean = re.sub(r"[\r\n\t/\\]+", " ", str(value or "").strip())
        clean = " ".join(clean.split()).strip(" .") or fallback
        return clean[:limit].rstrip(" .") or fallback

    def _find_or_create_vault_folder(self, user_id: str, name: str, parent_id: Optional[str] = None) -> Optional[str]:
        parent_clean = str(parent_id or "").strip() or None
        try:
            for folder in self.file_manager.list_user_folders(user_id, parent_clean):
                if str(folder.name or "").strip().lower() == str(name or "").strip().lower():
                    return folder.id
            return self.file_manager.create_user_folder(user_id, name, parent_clean).id
        except Exception:
            logger.warning("Could not create/find Vault folder %s for user %s", name, user_id, exc_info=True)
            return None

    def _resolved_source_file_path(self, file_info: FileInfo) -> Optional[Path]:
        raw_path = str(getattr(file_info, "file_path", "") or "").strip()
        if not raw_path:
            return None
        resolver = getattr(self.file_manager, "_resolve_file_disk_path", None)
        try:
            return resolver(raw_path) if callable(resolver) else Path(raw_path).expanduser()
        except Exception:
            logger.debug("Could not resolve Digestion source file path for %s", file_info.id, exc_info=True)
            return Path(raw_path).expanduser()

    def _digestion_intake_folder_name(self, digestion: Digestion) -> str:
        label = self._safe_vault_folder_segment(digestion.name, fallback="Digestion", limit=64)
        return self._safe_vault_folder_segment(f"{digestion.id} {label}", fallback=digestion.id, limit=120)

    def _digestion_intake_folder_id(self, digestion: Digestion) -> Optional[str]:
        owner_id = str(digestion.owner_user_id or "").strip()
        if not owner_id:
            return None
        root_id = self._find_or_create_vault_folder(owner_id, "Digestion Intake")
        if not root_id:
            return None
        return self._find_or_create_vault_folder(owner_id, self._digestion_intake_folder_name(digestion), root_id)

    def merge_sources_from_digestion(
        self,
        target_digestion_id: str,
        source_digestion_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        """Copy source references from one accessible Digestion into another.

        This intentionally does not merge or delete Digestion records. It makes the
        target dirty so the owner can rebuild with the expanded source set.
        """
        target = self._require_digestion(target_digestion_id, actor_user_id, manage=True)
        source = self._require_digestion(source_digestion_id, actor_user_id, query=True)
        if target.id == source.id:
            raise DigestionError("Drop a different Digestion to merge sources.", status_code=400, reason="same_digestion")
        source_access = self._access_for(source, actor_user_id)
        if not source_access.get("can_read_sources"):
            raise DigestionError(
                "Merging Digestions requires source metadata access to the source Digestion.",
                status_code=403,
                reason="source_metadata_denied",
            )

        now = self._now()
        added = 0
        updated = 0
        skipped: list[dict[str, str]] = []
        with self.db.get_connection() as conn:
            source_rows = conn.execute(
                """
                SELECT file_id, file_checksum, file_name, content_type, source_kind,
                       source_label, source_uri, source_metadata_json
                FROM digestion_sources
                WHERE digestion_id = ?
                ORDER BY file_name COLLATE NOCASE, file_id
                """,
                (source.id,),
            ).fetchall()
            existing_ids = {
                str(row["file_id"] or "")
                for row in conn.execute(
                    "SELECT file_id FROM digestion_sources WHERE digestion_id = ?",
                    (target.id,),
                ).fetchall()
            }
            for row in source_rows:
                file_id = self._clean_id(row["file_id"] if "file_id" in row.keys() else "")
                if not file_id:
                    skipped.append({"file_id": "", "reason": "missing_file_id"})
                    continue
                info = self.file_manager.get_file(file_id)
                if not info:
                    skipped.append({"file_id": file_id, "reason": "file_not_found"})
                    continue
                if str(info.uploaded_by) != str(target.owner_user_id):
                    skipped.append({"file_id": file_id, "reason": "not_owned_by_target_owner"})
                    continue
                try:
                    metadata = json.loads(row["source_metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.update({
                    "ingest_path": "digestion_merge",
                    "merged_from_digestion_id": source.id,
                    "merged_from_digestion_name": source.name,
                })
                was_existing = file_id in existing_ids
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
                        target.id,
                        info.id,
                        info.checksum,
                        info.original_name,
                        info.content_type,
                        now,
                        str(row["source_kind"] or "vault_file"),
                        str(row["source_label"] or info.original_name),
                        str(row["source_uri"] or ""),
                        json.dumps(metadata, sort_keys=True),
                    ),
                )
                if was_existing:
                    updated += 1
                else:
                    added += 1
                    existing_ids.add(file_id)
            if added or updated:
                conn.execute("UPDATE digestions SET status = ?, updated_at = ? WHERE id = ?", ("draft", now, target.id))
            conn.commit()
        return {
            "success": True,
            "digestion_id": target.id,
            "target_digestion_id": target.id,
            "source_digestion_id": source.id,
            "added": added,
            "updated": updated,
            "skipped": skipped,
        }

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
        source_results: list[dict[str, Any]] = []
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
                source_results.append({
                    "file_id": info.id,
                    "file_name": info.original_name,
                    "source_kind": material_meta.get("source_kind") or "inline_text",
                    "source_label": material_meta.get("source_label") or info.original_name,
                    "source_uri": material_meta.get("source_uri") or "",
                    "submitted_by": actor_user_id,
                })
            if added:
                conn.execute("UPDATE digestions SET status = ?, updated_at = ? WHERE id = ?", ("draft", now, digestion.id))
            conn.commit()
        if skipped_extra:
            skipped.append({"reason": "material_limit_reached", "count": str(skipped_extra)})
        return {
            "success": True,
            "added": added,
            "skipped": skipped,
            "digestion_id": digestion.id,
            "sources": source_results,
        }

    def append_contributions(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        contributions: Optional[Iterable[dict[str, Any]]] = None,
        source_file_ids: Optional[Iterable[str]] = None,
        datapoints: Optional[Iterable[dict[str, Any]]] = None,
        build_after: bool = False,
        review_required: bool = False,
        _record_ledger: bool = True,
    ) -> dict[str, Any]:
        """Append durable agent/human work product to a managed Digestion.

        Contributions are intentionally additive. Inline work product becomes
        owner-bound Vault material, referenced files use the existing source
        add/copy path, and explicit datapoints are appended to the reusable
        structured_datapoints snapshot only when source-metadata access exists.
        """
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        material_items: list[dict[str, Any]] = []
        direct_datapoints: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        top_level_file_ids = self._clean_id_list(source_file_ids)
        contribution_file_ids = list(top_level_file_ids)
        valid_contributions: list[dict[str, Any]] = []

        if isinstance(contributions, dict):
            raw_contributions = [contributions]
        else:
            raw_contributions = list(contributions or [])
        contribution_list = raw_contributions[:MAX_AGENT_CONTRIBUTIONS_PER_APPEND]
        skipped_extra = max(0, len(raw_contributions) - len(contribution_list))
        for index, contribution in enumerate(contribution_list, start=1):
            if not isinstance(contribution, dict):
                skipped.append({"index": str(index), "reason": "contribution_not_object"})
                continue
            contribution_payload = dict(contribution)
            valid_contributions.append(contribution_payload)
            contribution_file_ids.extend(self._contribution_file_ids(contribution))
            material = self._contribution_to_material(digestion, actor_user_id, contribution_payload, index=index)
            if material:
                material_items.append(material)
            contribution_datapoints = contribution_payload.get("datapoints") or contribution_payload.get("structured_datapoints") or []
            if isinstance(contribution_datapoints, dict):
                contribution_datapoints = [contribution_datapoints]
            if isinstance(contribution_datapoints, list):
                for item in contribution_datapoints:
                    if isinstance(item, dict):
                        item_copy = dict(item)
                        item_copy.setdefault("_contribution_title", material.get("title") if material else contribution_payload.get("title"))
                        item_copy.setdefault("_contribution_kind", contribution_payload.get("kind") or contribution_payload.get("type"))
                        item_copy.setdefault("_contribution_tags", contribution_payload.get("tags"))
                        direct_datapoints.append(item_copy)

        top_level_datapoints = datapoints or []
        if isinstance(top_level_datapoints, dict):
            top_level_datapoints = [top_level_datapoints]
        top_level_datapoint_list: list[dict[str, Any]] = []
        if isinstance(top_level_datapoints, list):
            top_level_datapoint_list = [dict(item) for item in top_level_datapoints if isinstance(item, dict)]
            direct_datapoints.extend(top_level_datapoint_list)

        contribution_file_ids = self._clean_id_list(contribution_file_ids)
        if skipped_extra:
            skipped.append({"reason": "contribution_limit_reached", "count": str(skipped_extra)})
        if review_required:
            pending_rows = self._record_pending_contribution_ledger(
                digestion,
                actor_user_id,
                contributions=valid_contributions,
                source_file_ids=top_level_file_ids,
                datapoints=top_level_datapoint_list,
            )
            if not pending_rows:
                raise DigestionError(
                    "Provide at least one contribution, source_file_id, or datapoint to append.",
                    status_code=400,
                    reason="missing_contribution_payload",
                )
            return {
                "success": True,
                "digestion_id": digestion.id,
                "schema_version": AGENT_CONTRIBUTION_SCHEMA_VERSION,
                "ledger_schema_version": DIGESTION_CONTRIBUTION_LEDGER_SCHEMA_VERSION,
                "review_required": True,
                "pending_contributions": len(pending_rows),
                "contributions": pending_rows,
                "materials_added": 0,
                "source_files_added": 0,
                "datapoints_added": 0,
                "skipped": skipped,
                "build_result": None,
                "stats": self.stats(digestion.id),
            }
        if not material_items and not contribution_file_ids and not direct_datapoints:
            raise DigestionError(
                "Provide at least one contribution, source_file_id, or datapoint to append.",
                status_code=400,
                reason="missing_contribution_payload",
            )

        material_result = {"success": True, "added": 0, "skipped": [], "sources": []}
        if material_items:
            material_result = self.add_materials(digestion.id, actor_user_id, material_items)

        source_result = {"success": True, "added": 0, "skipped": [], "sources": []}
        if contribution_file_ids:
            source_result = self.add_sources(digestion.id, actor_user_id, contribution_file_ids)

        build_result: Optional[dict[str, Any]] = None
        if build_after and (int(material_result.get("added") or 0) or int(source_result.get("added") or 0)):
            build_result = self.build_digestion(digestion.id, actor_user_id, rebuild=False)

        datapoint_result: dict[str, Any] = {"success": True, "added": 0, "skipped": [], "output": {}}
        if direct_datapoints:
            access = self._access_for(digestion, actor_user_id)
            if access.get("can_read_sources"):
                datapoint_result = self._append_agent_structured_datapoints(
                    digestion,
                    actor_user_id,
                    direct_datapoints,
                )
            else:
                datapoint_result = {
                    "success": False,
                    "added": 0,
                    "skipped": [
                        {
                            "reason": "source_metadata_access_required",
                            "count": str(min(len(direct_datapoints), MAX_AGENT_DATAPOINTS_PER_APPEND)),
                        }
                    ],
                    "output": {},
                    "message": (
                        "Explicit structured datapoints were not appended because source-metadata "
                        "access is required for source-revealing datapoint outputs."
                    ),
                }
                if not material_items and not contribution_file_ids:
                    raise DigestionError(
                        datapoint_result["message"],
                        status_code=403,
                        reason="datapoint_source_metadata_denied",
                    )

        result = {
            "success": True,
            "digestion_id": digestion.id,
            "schema_version": AGENT_CONTRIBUTION_SCHEMA_VERSION,
            "ledger_schema_version": DIGESTION_CONTRIBUTION_LEDGER_SCHEMA_VERSION,
            "materials_added": int(material_result.get("added") or 0),
            "source_files_added": int(source_result.get("added") or 0),
            "datapoints_added": int(datapoint_result.get("added") or 0),
            "materials": material_result,
            "source_files": source_result,
            "datapoints": datapoint_result,
            "skipped": skipped,
            "build_result": build_result,
            "stats": self.stats(digestion.id),
        }
        if _record_ledger:
            accepted_rows = self._record_accepted_contribution_ledger(
                digestion,
                actor_user_id,
                contributions=valid_contributions,
                source_file_ids=top_level_file_ids,
                datapoints=top_level_datapoint_list,
                result=result,
            )
            result["contributions_recorded"] = len(accepted_rows)
            result["contributions"] = accepted_rows
            result["stats"] = self.stats(digestion.id)
        return result

    def list_contributions(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        status: str = "",
        include_payload: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List the durable contribution ledger for a managed Digestion."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        access = self._access_for(digestion, actor_user_id)
        requested_status = self._normalize_contribution_status(status)
        max_rows = max(1, min(int(limit or 100), 250))
        params: list[Any] = [digestion.id]
        status_clause = ""
        if requested_status:
            status_clause = "AND c.status = ?"
            params.append(requested_status)
        params.append(max_rows)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    c.*,
                    u.username AS contributor_username,
                    u.avatar_file_id AS contributor_avatar_file_id
                FROM digestion_contributions c
                LEFT JOIN users u ON u.id = c.contributor_user_id
                WHERE c.digestion_id = ?
                {status_clause}
                ORDER BY
                    CASE c.status
                        WHEN 'pending' THEN 0
                        WHEN 'accepted' THEN 1
                        WHEN 'reviewed' THEN 2
                        ELSE 3
                    END,
                    c.created_at DESC,
                    c.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        include_sensitive = bool(access.get("can_read_sources")) and bool(include_payload)
        contributions = [
            self._contribution_row_to_dict(row, include_payload=include_sensitive)
            for row in rows
        ]
        pending_count = sum(1 for item in contributions if item.get("status") == CONTRIBUTION_STATUS_PENDING)
        return {
            "success": True,
            "digestion_id": digestion.id,
            "schema_version": DIGESTION_CONTRIBUTION_LEDGER_SCHEMA_VERSION,
            "contributions": contributions,
            "count": len(contributions),
            "pending_count": pending_count,
            "access": access,
        }

    def review_contribution(
        self,
        digestion_id: str,
        contribution_id: str,
        actor_user_id: str,
        *,
        action: str,
        note: str = "",
        build_after: bool = False,
    ) -> dict[str, Any]:
        """Accept, reject, or mark a contribution ledger row as reviewed."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        contribution_id = self._clean_id(contribution_id)
        action_clean = str(action or "").strip().lower()
        if action_clean not in {"accept", "reject", "review", "mark_reviewed"}:
            raise DigestionError("Invalid contribution review action.", status_code=400, reason="invalid_contribution_action")
        row = self._get_contribution_row(digestion.id, contribution_id)
        if not row:
            raise DigestionError("Contribution not found.", status_code=404, reason="contribution_not_found")
        status = str(row["status"] or "").strip().lower()
        now = self._now()
        apply_result: Optional[dict[str, Any]] = None
        if action_clean == "accept":
            if status != CONTRIBUTION_STATUS_PENDING:
                raise DigestionError("Only pending contributions can be accepted.", status_code=400, reason="contribution_not_pending")
            payload = self._json_loads(row["payload_json"], {})
            if not isinstance(payload, dict):
                payload = {}
            apply_actor_id = str(row["contributor_user_id"] or actor_user_id).strip() or actor_user_id
            def _apply_pending(actor_id: str) -> dict[str, Any]:
                if str(payload.get("_ledger_payload_kind") or "") == "source_datapoint_bundle":
                    return self.append_contributions(
                        digestion.id,
                        actor_id,
                        contributions=[],
                        source_file_ids=payload.get("source_file_ids") if isinstance(payload.get("source_file_ids"), list) else [],
                        datapoints=payload.get("datapoints") if isinstance(payload.get("datapoints"), list) else [],
                        build_after=build_after,
                        _record_ledger=False,
                    )
                return self.append_contributions(
                    digestion.id,
                    actor_id,
                    contributions=[payload],
                    build_after=build_after,
                    _record_ledger=False,
                )

            try:
                apply_result = _apply_pending(apply_actor_id)
            except DigestionError as exc:
                if apply_actor_id != actor_user_id and getattr(exc, "reason", "") == "manage_denied":
                    apply_result = _apply_pending(actor_user_id)
                else:
                    raise
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE digestion_contributions
                    SET status = ?, accepted_at = ?, reviewed_by = ?, reviewed_at = ?,
                        review_note = ?, result_json = ?, updated_at = ?
                    WHERE id = ? AND digestion_id = ?
                    """,
                    (
                        CONTRIBUTION_STATUS_ACCEPTED,
                        now,
                        actor_user_id,
                        now,
                        str(note or "").strip()[:2000],
                        json.dumps(apply_result or {}, sort_keys=True),
                        now,
                        contribution_id,
                        digestion.id,
                    ),
                )
                conn.commit()
        elif action_clean == "reject":
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE digestion_contributions
                    SET status = ?, rejected_at = ?, reviewed_by = ?, reviewed_at = ?,
                        review_note = ?, updated_at = ?
                    WHERE id = ? AND digestion_id = ?
                    """,
                    (
                        CONTRIBUTION_STATUS_REJECTED,
                        now,
                        actor_user_id,
                        now,
                        str(note or "").strip()[:2000],
                        now,
                        contribution_id,
                        digestion.id,
                    ),
                )
                conn.commit()
        else:
            if status == CONTRIBUTION_STATUS_REJECTED:
                raise DigestionError("Rejected contributions cannot be marked reviewed.", status_code=400, reason="contribution_rejected")
            if status == CONTRIBUTION_STATUS_PENDING:
                raise DigestionError("Pending contributions must be accepted or rejected before they can be marked reviewed.", status_code=400, reason="contribution_pending")
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE digestion_contributions
                    SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?, updated_at = ?
                    WHERE id = ? AND digestion_id = ?
                    """,
                    (
                        CONTRIBUTION_STATUS_REVIEWED,
                        actor_user_id,
                        now,
                        str(note or "").strip()[:2000],
                        now,
                        contribution_id,
                        digestion.id,
                    ),
                )
                conn.commit()
        updated = self._get_contribution_row(digestion.id, contribution_id)
        return {
            "success": True,
            "digestion_id": digestion.id,
            "action": action_clean,
            "contribution": self._contribution_row_to_dict(updated, include_payload=True) if updated else {},
            "apply_result": apply_result,
            "stats": self.stats(digestion.id),
        }

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
        stats_by_id = self.stats_many([str(row["id"] or "") for row in rows])
        for row in rows:
            item = self._digestion_from_row(row).to_dict(access=self._access_from_row(row, user_id))
            item["stats"] = stats_by_id.get(item["id"], self._empty_stats())
            item["operation_progress"] = self._progress_snapshot(
                item["id"],
                include_source_details=bool(item.get("access", {}).get("can_read_sources")),
            )
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
        data["operation_progress"] = self._progress_snapshot(
            digestion.id,
            include_source_details=bool(access.get("can_read_sources")),
        )
        return data

    def get_operation_progress(self, digestion_id: str, actor_user_id: str) -> dict[str, Any]:
        """Return live, non-sensitive progress for Digestion build/extraction operations."""
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        access = self._access_for(digestion, actor_user_id)
        return {
            "success": True,
            "digestion_id": digestion.id,
            "status": digestion.status,
            "stats": self.stats(digestion.id),
            "operations": self._progress_snapshot(
                digestion.id,
                include_source_details=bool(access.get("can_read_sources")),
            ),
        }

    def list_sources(self, digestion_id: str, *, user_id: str = "") -> list[dict[str, Any]]:
        if user_id:
            digestion = self._get_digestion_obj(digestion_id)
            if not digestion:
                raise DigestionError("Digestion not found", status_code=404, reason="not_found")
            access = self._access_for(digestion, user_id)
            if not access.get("can_read_sources"):
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

    def list_figures(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        limit: int = 120,
    ) -> dict[str, Any]:
        """List extracted PDF figures and captions for a source-readable Digestion."""
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        access = self._access_for(digestion, actor_user_id)
        if not access.get("can_read_sources"):
            raise DigestionError(
                "PDF figure previews include source-derived images and captions. Source metadata access is required.",
                status_code=403,
                reason="source_metadata_denied",
            )
        figure_limit = max(1, min(int(limit or 120), 240))
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    f.*,
                    s.file_name AS source_file_name,
                    s.content_type AS source_content_type,
                    img.original_name AS vault_image_name,
                    img.size AS vault_image_size
                FROM digestion_pdf_figures f
                LEFT JOIN digestion_sources s
                  ON s.digestion_id = f.digestion_id
                 AND s.file_id = f.source_file_id
                LEFT JOIN files img ON img.id = f.image_file_id
                WHERE f.digestion_id = ?
                ORDER BY COALESCE(s.file_name, f.source_file_id) COLLATE NOCASE,
                         f.source_file_id, f.page_number, f.figure_index
                LIMIT ?
                """,
                (digestion.id, figure_limit),
            ).fetchall()
        figures = [self._figure_row_to_dict(row) for row in rows]
        return {
            "success": True,
            "digestion_id": digestion.id,
            "schema_version": PDF_FIGURE_SCHEMA_VERSION,
            "count": len(figures),
            "figures": figures,
            "stats": self.stats(digestion.id),
        }

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
        if grantee == digestion.owner_user_id:
            raise DigestionError(
                "The Digestion owner already has full access and cannot be added as a separate grantee.",
                status_code=400,
                reason="owner_not_grantable",
            )
        with self.db.get_connection() as conn:
            grantee_row = conn.execute("SELECT * FROM users WHERE id = ?", (grantee,)).fetchone()
            if not grantee_row:
                raise DigestionError(
                    "Digestion live query access can only be granted to local users or agents on this node.",
                    status_code=400,
                    reason="grantee_not_eligible",
                )
            row_keys = set(grantee_row.keys()) if hasattr(grantee_row, "keys") else set()
            origin_peer = str((grantee_row["origin_peer"] if "origin_peer" in row_keys else "") or "").strip()
            if origin_peer:
                raise DigestionError(
                    "Digestion live query access can only be granted to local users or agents on this node.",
                    status_code=400,
                    reason="grantee_not_eligible",
                )
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
        username = grantee_row["username"] if "username" in row_keys else grantee
        display_name = grantee_row["display_name"] if "display_name" in row_keys else username
        account_type = grantee_row["account_type"] if "account_type" in row_keys else ""
        return {
            "success": True,
            "digestion_id": digestion.id,
            "grantee_user_id": grantee,
            "can_query": bool(can_query),
            "can_manage": bool(can_manage),
            "can_read_sources": bool(can_read_sources),
            "grantee": {
                "user_id": grantee,
                "username": username or grantee,
                "display_name": display_name or username or grantee,
                "account_type": account_type or "",
            },
        }

    def list_access(self, digestion_id: str, actor_user_id: str) -> dict[str, Any]:
        """List local users and agents with explicit live access to a Digestion."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    a.grantee_user_id,
                    a.grantee_kind,
                    a.can_query,
                    a.can_manage,
                    a.can_read_sources,
                    a.created_at AS acl_created_at,
                    u.*
                FROM digestion_acl a
                LEFT JOIN users u ON u.id = a.grantee_user_id
                WHERE a.digestion_id = ?
                ORDER BY a.created_at ASC, a.grantee_user_id COLLATE NOCASE
                """,
                (digestion.id,),
            ).fetchall()
        entries = []
        for row in rows:
            row_keys = set(row.keys()) if hasattr(row, "keys") else set()
            user_id = str(row["grantee_user_id"] or "").strip()
            username = str((row["username"] if "username" in row_keys else "") or user_id).strip() or user_id
            display_name = str((row["display_name"] if "display_name" in row_keys else "") or username or user_id).strip()
            avatar_file_id = str((row["avatar_file_id"] if "avatar_file_id" in row_keys else "") or "").strip()
            account_type = str((row["account_type"] if "account_type" in row_keys else "") or "").strip()
            origin_peer = str((row["origin_peer"] if "origin_peer" in row_keys else "") or "").strip()
            entries.append(
                {
                    "user_id": user_id,
                    "grantee_user_id": user_id,
                    "grantee_kind": str(row["grantee_kind"] or "user"),
                    "username": username,
                    "display_name": display_name or username or user_id,
                    "account_type": account_type,
                    "origin_peer": origin_peer,
                    "avatar_file_id": avatar_file_id,
                    "avatar_url": f"/files/{avatar_file_id}" if avatar_file_id else "",
                    "can_query": bool(row["can_query"]),
                    "can_manage": bool(row["can_manage"]),
                    "can_read_sources": bool(row["can_read_sources"]),
                    "created_at": str(row["acl_created_at"] or ""),
                    "missing_local_user": "id" not in row_keys or not bool(row["id"] if "id" in row_keys else ""),
                }
            )
        return {
            "success": True,
            "digestion_id": digestion.id,
            "owner_user_id": digestion.owner_user_id,
            "entries": entries,
            "count": len(entries),
        }

    def revoke_access(self, digestion_id: str, actor_user_id: str, grantee_user_id: str) -> dict[str, Any]:
        """Remove a local user's explicit live access without touching other grantees."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        grantee = self._clean_id(grantee_user_id)
        if not grantee:
            raise DigestionError("grantee_user_id is required", status_code=400, reason="missing_grantee")
        if grantee == digestion.owner_user_id:
            raise DigestionError("The Digestion owner cannot be removed from access.", status_code=400, reason="owner_not_revocable")
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM digestion_acl WHERE digestion_id = ? AND grantee_user_id = ?",
                (digestion.id, grantee),
            )
            conn.commit()
        return {
            "success": True,
            "digestion_id": digestion.id,
            "grantee_user_id": grantee,
            "revoked": bool(getattr(cursor, "rowcount", 0)),
        }

    # ------------------------------------------------------------------
    # Build and query
    # ------------------------------------------------------------------
    def build_digestion(self, digestion_id: str, actor_user_id: str, *, rebuild: bool = False) -> dict[str, Any]:
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        source_rows = self._source_rows(digestion.id)
        if not source_rows:
            self._set_operation_progress(
                digestion.id,
                "build",
                status="failed",
                phase="no_sources",
                percent=0,
                processed=0,
                total=0,
                message="Add at least one source before building this Digestion.",
            )
            raise DigestionError("Add at least one Vault file before building a Digestion.", status_code=400, reason="no_sources")

        started = self._now()
        self._set_status(digestion.id, "building", error=None)
        source_total = len(source_rows)
        self._set_operation_progress(
            digestion.id,
            "build",
            status="running",
            phase="preparing",
            percent=2,
            processed=0,
            total=source_total,
            message=f"Preparing {source_total} source{'' if source_total == 1 else 's'} for indexing.",
            details={
                "rebuild": bool(rebuild),
                "chunk_size": digestion.chunk_size,
                "chunk_overlap": digestion.chunk_overlap,
                "provider": digestion.provider,
                "embedding_model": digestion.embedding_model,
            },
        )
        total_chunks = 0
        embedded_count = 0
        total_figures = 0
        errors: list[dict[str, str]] = []
        try:
            if rebuild:
                with self.db.get_connection() as conn:
                    conn.execute(
                        """
                        DELETE FROM digestion_chunks
                        WHERE digestion_id = ?
                          AND file_id NOT IN (
                            SELECT file_id FROM digestion_sources WHERE digestion_id = ?
                          )
                        """,
                        (digestion.id, digestion.id),
                    )
                    conn.commit()

            for source_index, source in enumerate(source_rows, start=1):
                if total_chunks >= MAX_CHUNKS_PER_BUILD:
                    errors.append({"file_id": str(source["file_id"]), "error": "build_chunk_limit_reached"})
                    self._set_operation_progress(
                        digestion.id,
                        "build",
                        status="running",
                        phase="limit_reached",
                        percent=92,
                        processed=source_index - 1,
                        total=source_total,
                        message=f"Chunk limit reached at {MAX_CHUNKS_PER_BUILD} chunks.",
                        details={
                            "chunk_count": total_chunks,
                            "embedded_count": embedded_count,
                            "errors": errors[:8],
                        },
                    )
                    break
                try:
                    source_label = str(
                        self._row_get(source, "source_label", "")
                        or self._row_get(source, "file_name", "")
                        or source["file_id"]
                    )

                    def source_progress(stage: str, message: str, fraction: float, extra: Optional[dict[str, Any]] = None) -> None:
                        base = (source_index - 1) / max(1, source_total)
                        step = max(0.0, min(float(fraction or 0), 1.0)) / max(1, source_total)
                        percent = 4 + int((base + step) * 88)
                        self._set_operation_progress(
                            digestion.id,
                            "build",
                            status="running",
                            phase=stage,
                            percent=min(92, max(4, percent)),
                            processed=source_index - 1,
                            total=source_total,
                            current_label=source_label,
                            message=message,
                            details={
                                "chunk_count": total_chunks,
                                "embedded_count": embedded_count,
                                "source_index": source_index,
                                "source_total": source_total,
                                **(extra or {}),
                            },
                        )

                    source_progress("reading_source", f"Reading {source_label}.", 0.05)
                    file_chunks = self._index_source(
                        digestion,
                        source,
                        remaining_chunks=MAX_CHUNKS_PER_BUILD - total_chunks,
                        progress_callback=source_progress,
                    )
                    total_chunks += file_chunks["chunk_count"]
                    embedded_count += file_chunks["embedded_count"]
                    total_figures += int(file_chunks.get("figure_count") or 0)
                    self._set_operation_progress(
                        digestion.id,
                        "build",
                        status="running",
                        phase="source_indexed",
                        percent=min(92, 4 + int((source_index / max(1, source_total)) * 88)),
                        processed=source_index,
                        total=source_total,
                        current_label=source_label,
                        message=f"Indexed {source_label}: {file_chunks['chunk_count']} chunk{'' if file_chunks['chunk_count'] == 1 else 's'}.",
                        details={
                            "chunk_count": total_chunks,
                            "embedded_count": embedded_count,
                            "figure_count": total_figures,
                            "source_index": source_index,
                            "source_total": source_total,
                        },
                    )
                except Exception as exc:
                    message = str(exc)[:1000]
                    errors.append({"file_id": str(source["file_id"]), "error": message})
                    self._mark_source_error(digestion.id, str(source["file_id"]), message)
                    self._set_operation_progress(
                        digestion.id,
                        "build",
                        status="running",
                        phase="source_error",
                        percent=min(92, 4 + int((source_index / max(1, source_total)) * 88)),
                        processed=source_index,
                        total=source_total,
                        current_label=str(self._row_get(source, "file_name", "") or source["file_id"]),
                        message=f"Source issue: {message}",
                        details={
                            "chunk_count": total_chunks,
                            "embedded_count": embedded_count,
                            "figure_count": total_figures,
                            "errors": errors[:8],
                        },
                    )

            status = "ready" if total_chunks > 0 else "error"
            error_text = None if status == "ready" else "No extractable chunks were indexed."
            if errors and status == "ready":
                status = "ready_with_errors"
                error_text = json.dumps(errors[:8])
            self._set_status(digestion.id, status, built_at=self._now(), error=error_text)
            digestion = self._get_digestion_obj(digestion.id) or digestion
            try:
                if total_chunks > 0:
                    self._set_operation_progress(
                        digestion.id,
                        "build",
                        status="running",
                        phase="generating_outputs",
                        percent=95,
                        processed=source_total,
                        total=source_total,
                        message="Creating manifest, brief, and agent context outputs.",
                        details={
                            "chunk_count": total_chunks,
                            "embedded_count": embedded_count,
                            "figure_count": total_figures,
                            "errors": errors[:8],
                        },
                    )
                outputs = self.generate_outputs(digestion.id, actor_user_id) if total_chunks > 0 else {"outputs": []}
            except Exception as exc:
                logger.warning("Digestion output generation failed for %s: %s", digestion.id, exc, exc_info=True)
                outputs = {"outputs": [], "error": str(exc)[:500]}
            self._set_operation_progress(
                digestion.id,
                "build",
                status="completed" if status in {"ready", "ready_with_errors"} else "failed",
                phase=status,
                percent=100 if status in {"ready", "ready_with_errors"} else 0,
                processed=source_total,
                total=source_total,
                message=(
                    f"Build finished with {total_chunks} indexed chunk{'' if total_chunks == 1 else 's'}."
                    if status in {"ready", "ready_with_errors"}
                    else "Build finished without indexed chunks."
                ),
                details={
                    "chunk_count": total_chunks,
                    "embedded_count": embedded_count,
                    "figure_count": total_figures,
                    "errors": errors[:8],
                    "final_status": status,
                },
            )
            return {
                "success": status in {"ready", "ready_with_errors"},
                "digestion_id": digestion.id,
                "status": status,
                "started_at": started,
                "built_at": self._now(),
                "chunk_count": total_chunks,
                "embedded_count": embedded_count,
                "figure_count": total_figures,
                "errors": errors,
                "outputs": outputs.get("outputs") or [],
                "stats": self.stats(digestion.id),
                "progress": self._progress_snapshot(digestion.id).get("build", {}),
            }
        except Exception as exc:
            message = str(exc)[:1000]
            self._set_status(digestion.id, "error", error=message)
            self._set_operation_progress(
                digestion.id,
                "build",
                status="failed",
                phase="error",
                percent=0,
                message=message,
                details={"errors": [{"error": message}]},
            )
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
        query_terms = self._query_terms(query_text)
        scored: list[dict[str, Any]] = []
        for row in rows:
            try:
                vector = json.loads(row["vector_json"] or "[]")
            except Exception:
                continue
            score = self._cosine(query_vector, vector)
            text = str(row["text"] or "")
            term_overlap = self._query_term_overlap(query_terms, self._query_terms(text))
            if digestion.provider == "local_hash":
                if term_overlap <= 0:
                    continue
                if score < MIN_LOCAL_HASH_QUERY_SCORE:
                    score = MIN_LOCAL_HASH_QUERY_SCORE + (0.01 * min(term_overlap, 5))
            else:
                if score <= 0 and term_overlap <= 0:
                    continue
                if term_overlap > 0:
                    score = max(score, 0.05 + (0.01 * min(term_overlap, 5)))
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

    def search_structured_datapoints(
        self,
        digestion_id: str,
        actor_user_id: str,
        query: str,
        *,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Search the extracted structured datapoint output with source-gated access."""
        query_text = str(query or "").strip()
        if not query_text:
            raise DigestionError("query is required", status_code=400, reason="missing_query")
        try:
            result_limit = int(limit or 25)
        except (TypeError, ValueError):
            result_limit = 25
        limit = max(1, min(result_limit, 80))
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        stats = self.stats(digestion.id)
        try:
            output = self.get_output(digestion.id, actor_user_id, STRUCTURED_DATAPOINT_OUTPUT_KIND)
        except DigestionError as exc:
            if getattr(exc, "reason", "") == "output_not_found":
                return {
                    "success": True,
                    "digestion_id": digestion.id,
                    "query": query_text,
                    "mode": "structured_datapoints",
                    "result_count": 0,
                    "results": [],
                    "stats": stats,
                    "warning": "No structured datapoints output exists yet. Extract datapoints before searching this mode.",
                    "datapoints_ready": False,
                }
            raise
        try:
            payload = json.loads(str(output.get("content") or "{}"))
        except Exception:
            payload = {}
        datapoints = payload.get("datapoints") if isinstance(payload, dict) else []
        if not isinstance(datapoints, list):
            datapoints = []
        query_terms = self._query_terms(query_text)
        query_lower = query_text.lower()
        results: list[dict[str, Any]] = []
        for index, item in enumerate(datapoints, start=1):
            if not isinstance(item, dict):
                continue
            haystack = json.dumps(item, ensure_ascii=False, sort_keys=True).lower()
            terms = self._query_terms(haystack)
            overlap = len(query_terms & terms)
            phrase_match = query_lower in haystack
            if query_terms:
                if overlap <= 0 and not phrase_match:
                    continue
            elif not phrase_match:
                continue
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
            evidence_quotes = [
                str(entry.get("quote") or entry.get("text") or entry.get("evidence_sentence") or "")[:280]
                for entry in evidence
                if isinstance(entry, dict) and str(entry.get("quote") or entry.get("text") or entry.get("evidence_sentence") or "").strip()
            ]
            title = str(item.get("claim") or item.get("subject") or "Structured datapoint").strip()
            field_counts = {
                "materials": len(item.get("materials") or []) if isinstance(item.get("materials"), list) else 0,
                "methods": len(item.get("methods") or []) if isinstance(item.get("methods"), list) else 0,
                "measurements": len(item.get("measurements") or []) if isinstance(item.get("measurements"), list) else 0,
                "numerical_results": len(item.get("numerical_results") or []) if isinstance(item.get("numerical_results"), list) else 0,
                "relationships": len(item.get("relationships") or []) if isinstance(item.get("relationships"), list) else 0,
                "limitations_or_uncertainty": (
                    len(item.get("limitations_or_uncertainty") or [])
                    if isinstance(item.get("limitations_or_uncertainty"), list)
                    else 0
                ),
            }
            structured_fields: dict[str, list[str]] = {}
            for field_name in field_counts:
                values = item.get(field_name)
                if not isinstance(values, list):
                    continue
                structured_fields[field_name] = [
                    str(value)[:360]
                    for value in values
                    if str(value or "").strip()
                ][:6]
            score = 1.0 if phrase_match else (overlap / max(1, len(query_terms)))
            results.append(
                {
                    "datapoint_index": index,
                    "score": round(float(score), 6),
                    "term_overlap": overlap,
                    "subject": str(item.get("subject") or ""),
                    "claim": title,
                    "tags": [str(tag) for tag in (item.get("tags") or [])[:8]] if isinstance(item.get("tags"), list) else [],
                    "source": {
                        "file_id": str(source.get("file_id") or ""),
                        "file_name": str(source.get("file_name") or ""),
                        "content_type": str(source.get("content_type") or ""),
                        "page_label": str(source.get("page_label") or ""),
                        "chunk_index": source.get("chunk_index"),
                    },
                    "field_counts": field_counts,
                    "structured_fields": structured_fields,
                    "quantitative_results": item.get("quantitative_results")[:4]
                    if isinstance(item.get("quantitative_results"), list)
                    else [],
                    "evidence": evidence_quotes[:3],
                    "snippet": self._snippet(" ".join(
                        part
                        for part in [
                            str(item.get("subject") or ""),
                            str(item.get("claim") or ""),
                            " ".join(str(value) for value in (item.get("numerical_results") or []) if value),
                            " ".join(evidence_quotes),
                        ]
                        if part
                    )),
                }
            )
        results.sort(key=lambda item: (item["score"], item["term_overlap"]), reverse=True)
        results = results[:limit]
        return {
            "success": True,
            "digestion_id": digestion.id,
            "query": query_text,
            "mode": "structured_datapoints",
            "result_count": len(results),
            "results": results,
            "stats": stats,
            "datapoints_ready": True,
            "datapoint_count": len(datapoints),
            "output": {
                "id": output.get("id") or "",
                "title": output.get("title") or "",
                "updated_at": output.get("updated_at") or "",
                "metadata": output.get("metadata") or {},
            },
        }

    def stats(self, digestion_id: str) -> dict[str, Any]:
        return self.stats_many([digestion_id]).get(str(digestion_id or ""), self._empty_stats())

    @staticmethod
    def _empty_stats() -> dict[str, Any]:
        return {
            "chunks": 0,
            "token_estimate": 0,
            "figures": 0,
            "outputs": 0,
            "source_count": 0,
            "datapoint_count": 0,
            "quantitative_result_count": 0,
            "contribution_count": 0,
            "pending_contribution_count": 0,
            "sources_by_status": {},
        }

    def stats_many(self, digestion_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids: list[str] = []
        seen: set[str] = set()
        for raw_id in digestion_ids or []:
            digestion_id = self._clean_id(raw_id)
            if not digestion_id or digestion_id in seen:
                continue
            seen.add(digestion_id)
            ids.append(digestion_id)
        if not ids:
            return {}
        stats_by_id = {digestion_id: self._empty_stats() for digestion_id in ids}
        placeholders = ",".join("?" for _ in ids)
        with self.db.get_connection() as conn:
            chunk_rows = conn.execute(
                f"""
                SELECT digestion_id, COUNT(*) AS count, COALESCE(SUM(token_estimate), 0) AS tokens
                FROM digestion_chunks
                WHERE digestion_id IN ({placeholders})
                GROUP BY digestion_id
                """,
                ids,
            ).fetchall()
            source_rows = conn.execute(
                f"""
                SELECT digestion_id, status, COUNT(*) AS count
                FROM digestion_sources
                WHERE digestion_id IN ({placeholders})
                GROUP BY digestion_id, status
                """,
                ids,
            ).fetchall()
            figure_rows = conn.execute(
                f"""
                SELECT digestion_id, COUNT(*) AS count
                FROM digestion_pdf_figures
                WHERE digestion_id IN ({placeholders})
                GROUP BY digestion_id
                """,
                ids,
            ).fetchall()
            output_rows = conn.execute(
                f"""
                SELECT digestion_id, output_kind, metadata_json
                FROM digestion_outputs
                WHERE digestion_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            contribution_rows = conn.execute(
                f"""
                SELECT digestion_id, status, COUNT(*) AS count
                FROM digestion_contributions
                WHERE digestion_id IN ({placeholders})
                GROUP BY digestion_id, status
                """,
                ids,
            ).fetchall()
        for row in chunk_rows:
            digestion_id = str(row["digestion_id"] or "")
            if digestion_id not in stats_by_id:
                continue
            stats_by_id[digestion_id]["chunks"] = int((row["count"] if row else 0) or 0)
            stats_by_id[digestion_id]["token_estimate"] = int((row["tokens"] if row else 0) or 0)
        for row in source_rows:
            digestion_id = str(row["digestion_id"] or "")
            if digestion_id not in stats_by_id:
                continue
            source_count = int(row["count"] or 0)
            stats_by_id[digestion_id]["sources_by_status"][str(row["status"])] = source_count
            stats_by_id[digestion_id]["source_count"] = int(stats_by_id[digestion_id].get("source_count") or 0) + source_count
        for row in figure_rows:
            digestion_id = str(row["digestion_id"] or "")
            if digestion_id not in stats_by_id:
                continue
            stats_by_id[digestion_id]["figures"] = int(row["count"] or 0)
        for row in output_rows:
            digestion_id = str(row["digestion_id"] or "")
            if digestion_id not in stats_by_id:
                continue
            stats_by_id[digestion_id]["outputs"] = int(stats_by_id[digestion_id].get("outputs") or 0) + 1
            if str(row["output_kind"] or "") != STRUCTURED_DATAPOINT_OUTPUT_KIND:
                continue
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            if isinstance(metadata, dict):
                stats_by_id[digestion_id]["datapoint_count"] = self._bounded_int(
                    metadata.get("datapoint_count"),
                    0,
                    0,
                    1_000_000_000,
                )
                stats_by_id[digestion_id]["quantitative_result_count"] = self._bounded_int(
                    metadata.get("quantitative_result_count"),
                    0,
                    0,
                    1_000_000_000,
                )
        for row in contribution_rows:
            digestion_id = str(row["digestion_id"] or "")
            if digestion_id not in stats_by_id:
                continue
            count = int(row["count"] or 0)
            stats_by_id[digestion_id]["contribution_count"] = int(
                stats_by_id[digestion_id].get("contribution_count") or 0
            ) + count
            if str(row["status"] or "").strip().lower() == CONTRIBUTION_STATUS_PENDING:
                stats_by_id[digestion_id]["pending_contribution_count"] = count
        return stats_by_id

    def generate_outputs(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        kinds: Optional[Iterable[str]] = None,
    ) -> dict[str, Any]:
        """Generate reusable human/agent artifacts from the normalized Digestion."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        access = self._access_for(digestion, actor_user_id)
        source_outputs_allowed = bool(access.get("can_read_sources"))
        requested = {str(kind or "").strip().lower() for kind in (kinds or []) if str(kind or "").strip()}
        if not requested:
            requested = {"manifest", "human_brief", "agent_context"}
            if int(self.stats(digestion.id).get("figures") or 0) > 0:
                requested.add(PDF_FIGURE_OUTPUT_KIND)
        allowed = {"manifest", "human_brief", "agent_context", STRUCTURED_DATAPOINT_OUTPUT_KIND, PDF_FIGURE_OUTPUT_KIND}
        requested = requested & allowed
        if not requested:
            raise DigestionError("No supported output kinds requested.", status_code=400, reason="unsupported_output_kind")

        outputs = []
        for kind in sorted(requested):
            if kind in _SOURCE_REVEALING_OUTPUT_KINDS and not source_outputs_allowed:
                continue
            if kind == "manifest":
                outputs.append(self._upsert_output(digestion, actor_user_id, *self._build_manifest_output(digestion)))
            elif kind == "human_brief":
                outputs.append(self._upsert_output(digestion, actor_user_id, *self._build_human_brief_output(digestion)))
            elif kind == "agent_context":
                outputs.append(self._upsert_output(digestion, actor_user_id, *self._build_agent_context_output(digestion)))
            elif kind == PDF_FIGURE_OUTPUT_KIND:
                outputs.append(self._upsert_output(digestion, actor_user_id, *self._build_pdf_figures_output(digestion)))
            elif kind == STRUCTURED_DATAPOINT_OUTPUT_KIND:
                outputs.append(self.generate_structured_datapoints(digestion.id, actor_user_id)["output"])
        return {"success": True, "digestion_id": digestion.id, "outputs": outputs, "count": len(outputs)}

    def generate_structured_datapoints(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        max_chunks: Optional[int] = None,
        max_datapoints: Optional[int] = None,
        lens: str = "",
        extraction_scope: str = "new",
    ) -> dict[str, Any]:
        """Extract source-grounded structured datapoints from indexed chunks using an LLM."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        access = self._access_for(digestion, actor_user_id)
        self._set_operation_progress(
            digestion.id,
            "datapoints",
            status="running",
            phase="preflight",
            percent=2,
            processed=0,
            total=0,
            message="Checking source access and Digestion AI extraction settings.",
            details={},
        )
        if not access.get("can_read_sources"):
            self._set_operation_progress(
                digestion.id,
                "datapoints",
                status="failed",
                phase="source_access_denied",
                percent=0,
                message="Source-read access is required for structured datapoint extraction.",
            )
            raise DigestionError(
                "Structured datapoint extraction sends indexed source chunks to the configured LLM provider. Source metadata access is required.",
                status_code=403,
                reason="datapoint_source_metadata_denied",
            )
        try:
            llm_context = self._resolve_datapoint_llm_context(actor_user_id)
        except DigestionError as exc:
            self._set_operation_progress(
                digestion.id,
                "datapoints",
                status="failed",
                phase="llm_unavailable",
                percent=0,
                message=str(exc),
                details={"reason": getattr(exc, "reason", "datapoint_llm_unavailable")},
            )
            raise
        parameters = llm_context.get("parameters") if isinstance(llm_context.get("parameters"), dict) else {}
        provider = str(llm_context.get("provider") or "openai").strip().lower()
        max_output_tokens = self._datapoint_max_output_tokens(provider=provider, parameters=parameters)
        default_lens = str(llm_context.get("default_lens") or "").strip()
        effective_lens = str(lens or "").strip() or default_lens
        chunk_limit = self._bounded_int(
            max_chunks,
            int(parameters.get("max_chunks") or DEFAULT_STRUCTURED_DATAPOINT_CHUNKS),
            1,
            MAX_STRUCTURED_DATAPOINT_CHUNKS,
        )
        datapoint_limit = self._bounded_int(
            max_datapoints,
            int(parameters.get("max_datapoints") or DEFAULT_STRUCTURED_DATAPOINTS_PER_RUN),
            1,
            MAX_STRUCTURED_DATAPOINTS_PER_RUN,
        )
        scope = self._normalize_datapoint_extraction_scope(extraction_scope)
        existing_payload = self._structured_datapoint_payload(digestion.id)
        existing_datapoints = (
            existing_payload.get("datapoints")
            if isinstance(existing_payload, dict) and isinstance(existing_payload.get("datapoints"), list)
            else []
        )
        existing_file_ids = self._datapoint_source_file_ids(existing_datapoints)
        rows = self._datapoint_chunk_rows(
            digestion.id,
            limit=chunk_limit,
            exclude_file_ids=existing_file_ids if scope == "new" and existing_file_ids else None,
        )
        if scope == "new" and existing_datapoints and not rows:
            output = self._structured_datapoint_output_row(digestion.id)
            quantitative_result_count = sum(
                len(item.get("quantitative_results") or [])
                for item in existing_datapoints
                if isinstance(item, dict)
            )
            self._set_operation_progress(
                digestion.id,
                "datapoints",
                status="completed",
                phase="no_new_chunks",
                percent=100,
                processed=0,
                total=0,
                message="No newly indexed sources need datapoint extraction; existing structured datapoints were kept.",
                details={
                    "datapoint_count": len(existing_datapoints),
                    "quantitative_result_count": quantitative_result_count,
                    "existing_datapoints_preserved": len(existing_datapoints),
                    "extraction_scope": scope,
                    "max_chunks": chunk_limit,
                    "max_datapoints": datapoint_limit,
                },
            )
            return {
                "success": True,
                "digestion_id": digestion.id,
                "output": output,
                "datapoint_count": len(existing_datapoints),
                "quantitative_result_count": quantitative_result_count,
                "stats": (existing_payload.get("stats") if isinstance(existing_payload, dict) else {}) or {},
                "preview": existing_datapoints[:3],
                "progress": self._progress_snapshot(digestion.id).get("datapoints", {}),
                "skipped": True,
                "reason": "no_new_chunks",
                "extraction_scope": scope,
            }
        if not rows:
            self._set_operation_progress(
                digestion.id,
                "datapoints",
                status="failed",
                phase="no_indexed_chunks",
                percent=0,
                message="Build this Digestion before extracting structured datapoints; no indexed chunks are available.",
                details={"max_chunks": chunk_limit, "max_datapoints": datapoint_limit},
            )
            raise DigestionError(
                "Build this Digestion before extracting structured datapoints; no indexed chunks are available.",
                status_code=400,
                reason="no_indexed_chunks",
            )
        estimated_batches = len(self._datapoint_llm_batches(rows, parameters=parameters))
        self._set_operation_progress(
            digestion.id,
            "datapoints",
            status="running",
            phase="starting_batches",
            percent=6,
            processed=0,
            total=estimated_batches,
            message=(
                f"Preparing to scan {len(rows)} indexed chunk{'' if len(rows) == 1 else 's'} "
                f"across {estimated_batches} LLM batch{'' if estimated_batches == 1 else 'es'}."
            ),
            details={
                "max_chunks": chunk_limit,
                "max_datapoints": datapoint_limit,
                "provider": provider,
                "model": llm_context.get("model") or "",
                "credential_source": llm_context.get("credential_source") or "",
                "lens": effective_lens[:800],
                "extraction_scope": scope,
                "estimated_batches": estimated_batches,
            },
        )

        try:
            extraction = self._extract_structured_datapoints_with_llm(
                digestion,
                rows,
                llm_context=llm_context,
                lens=effective_lens,
                datapoint_limit=datapoint_limit,
                progress_callback=lambda payload: self._set_operation_progress(
                    digestion.id,
                    "datapoints",
                    status="running",
                    phase=str(payload.get("phase") or "extracting"),
                    percent=int(payload.get("percent") or 0),
                    processed=payload.get("processed"),
                    total=payload.get("total"),
                    current_label=str(payload.get("current_label") or ""),
                    message=str(payload.get("message") or ""),
                    details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
                ),
            )
        except Exception as exc:
            self._set_operation_progress(
                digestion.id,
                "datapoints",
                status="failed",
                phase="extraction_failed",
                percent=0,
                message=str(exc)[:1000],
                details={"reason": getattr(exc, "reason", "datapoint_extraction_failed")},
            )
            raise
        scoped_file_ids = {str(row["file_id"] or "") for row in rows if str(row["file_id"] or "")}
        extracted_datapoints = extraction["datapoints"]
        preserved_datapoints: list[dict[str, Any]] = []
        if scope == "new" and existing_datapoints:
            preserved_datapoints = [
                item
                for item in existing_datapoints
                if isinstance(item, dict)
                and not (self._datapoint_source_file_ids([item]) & scoped_file_ids)
            ]
        datapoints = [*preserved_datapoints, *extracted_datapoints]
        quantitative_result_count = sum(len(item.get("quantitative_results") or []) for item in datapoints)

        field_counts = self._datapoint_field_counts(datapoints)
        sources = self._source_summary_rows(digestion.id)
        digestion_stats = self.stats(digestion.id)
        payload = {
            "kind": STRUCTURED_DATAPOINT_SCHEMA_VERSION,
            "schema_version": STRUCTURED_DATAPOINT_SCHEMA_VERSION,
            "digestion": {
                "id": digestion.id,
                "name": digestion.name,
                "purpose": digestion.purpose or digestion.description,
                "status": digestion.status,
                "built_at": digestion.built_at,
            },
            "extractor": {
                "name": "canopy_llm_structured_datapoint_extractor",
                "version": "2",
                "mode": "source_grounded_llm",
                "provider": llm_context.get("provider") or "",
                "model": llm_context.get("model") or "",
                "credential_source": llm_context.get("credential_source") or "",
                "network_calls": True,
                "lens": effective_lens[:800],
                "source_boundary": "only indexed Digestion chunks are sent to the configured LLM provider; raw Vault files are not exported",
            },
            "limits": {
                "max_chunks": chunk_limit,
                "max_datapoints": datapoint_limit,
                "batch_chunks": extraction["stats"].get("batch_chunk_limit"),
                "batch_chars": extraction["stats"].get("batch_char_limit"),
                "chunk_chars": extraction["stats"].get("chunk_char_limit"),
                "batch_records": extraction["stats"].get("batch_record_limit"),
                "max_output_tokens": max_output_tokens,
                "extraction_scope": scope,
            },
            "stats": {
                "datapoint_count": len(datapoints),
                "new_datapoint_count": len(extracted_datapoints),
                "preserved_datapoint_count": len(preserved_datapoints),
                "quantitative_result_count": quantitative_result_count,
                "source_count": len(sources),
                "chunks_considered": len(rows),
                "total_indexed_chunks": int(digestion_stats.get("chunks") or 0),
                "batches_considered": extraction["stats"].get("batches_considered", 0),
                "failed_batches": extraction["stats"].get("failed_batches", 0),
                "chunks_without_datapoints": extraction["stats"].get("chunks_without_datapoints", 0),
                "field_counts": field_counts,
                "errors": extraction["errors"][:8],
                "extraction_scope": scope,
                "scoped_source_file_ids": sorted(scoped_file_ids),
            },
            "sources": [
                {
                    "file_id": item.get("file_id") or "",
                    "file_name": item.get("file_name") or "",
                    "source_kind": item.get("source_kind") or "",
                    "source_label": item.get("source_label") or "",
                    "chunk_count": item.get("chunk_count") or 0,
                }
                for item in sources
            ],
            "datapoints": datapoints,
            "reuse_guidance": [
                "Treat each datapoint as a cited record, not as a complete reading of the underlying source.",
                "Use source.file_name, source.page_label, source.chunk_index, and evidence[] when citing or challenging a datapoint.",
                "If a field is empty, the extractor did not find source-grounded evidence for that field in the supplied chunks.",
                "For live semantic retrieval, keep using the Digestion query/context endpoints; this output is a reusable structured snapshot.",
            ],
            "generated_at": self._now(),
        }
        output = self._upsert_output(
            digestion,
            actor_user_id,
            STRUCTURED_DATAPOINT_OUTPUT_KIND,
            f"{digestion.name or 'Digestion'} Structured Datapoints",
            "application/json",
            json.dumps(payload, indent=2, sort_keys=True),
            {
                "schema_version": STRUCTURED_DATAPOINT_SCHEMA_VERSION,
                "extractor": payload["extractor"],
                "datapoint_count": len(datapoints),
                "quantitative_result_count": quantitative_result_count,
                "chunks_considered": len(rows),
                "batches_considered": extraction["stats"].get("batches_considered", 0),
                "failed_batches": extraction["stats"].get("failed_batches", 0),
                "chunks_without_datapoints": extraction["stats"].get("chunks_without_datapoints", 0),
                "field_counts": field_counts,
                "source_count": len(sources),
                "source_revealing": True,
                "extraction_scope": scope,
                "new_datapoint_count": len(extracted_datapoints),
                "preserved_datapoint_count": len(preserved_datapoints),
            },
        )
        self._set_operation_progress(
            digestion.id,
            "datapoints",
            status="completed",
            phase="completed",
            percent=100,
            processed=int(extraction["stats"].get("batches_considered", 0) or 0),
            total=int(extraction["stats"].get("batches_considered", 0) or estimated_batches or 0),
            message=(
                f"Extracted {len(extracted_datapoints)} new structured datapoint"
                f"{'' if len(extracted_datapoints) == 1 else 's'}; "
                f"{len(datapoints)} total now retained."
            ),
            details={
                "datapoint_count": len(datapoints),
                "new_datapoint_count": len(extracted_datapoints),
                "preserved_datapoint_count": len(preserved_datapoints),
                "quantitative_result_count": quantitative_result_count,
                "chunks_considered": len(rows),
                "batches_considered": extraction["stats"].get("batches_considered", 0),
                "failed_batches": extraction["stats"].get("failed_batches", 0),
                "max_chunks": chunk_limit,
                "max_datapoints": datapoint_limit,
                "provider": provider,
                "model": llm_context.get("model") or "",
                "extraction_scope": scope,
            },
        )
        return {
            "success": True,
            "digestion_id": digestion.id,
            "output": output,
            "datapoint_count": len(datapoints),
            "quantitative_result_count": quantitative_result_count,
            "stats": payload["stats"],
            "preview": datapoints[:3],
            "progress": self._progress_snapshot(digestion.id).get("datapoints", {}),
            "extraction_scope": scope,
            "new_datapoint_count": len(extracted_datapoints),
            "preserved_datapoint_count": len(preserved_datapoints),
        }

    def list_outputs(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        access = self._access_for(digestion, actor_user_id)
        source_outputs_allowed = bool(access.get("can_read_sources"))
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
                        WHEN 'pdf_figures' THEN 3
                        WHEN 'structured_datapoints' THEN 4
                        WHEN 'manifest' THEN 5
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
        if str(row["output_kind"] or "") in _SOURCE_REVEALING_OUTPUT_KINDS and not access.get("can_read_sources"):
            raise DigestionError(
                "This Digestion output includes source metadata. Source metadata access is required.",
                status_code=403,
                reason="output_source_metadata_denied",
            )
        return self._output_row_to_dict(row, include_content=True)

    def export_output_to_vault(self, digestion_id: str, actor_user_id: str, output_ref: str) -> dict[str, Any]:
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        output = self.get_output(digestion.id, actor_user_id, output_ref)
        content_type = str(output.get("content_type") or "text/markdown").split(";", 1)[0].strip().lower()
        ext = ".json" if content_type == "application/json" else ".md"
        save_type = "application/json" if content_type == "application/json" else "text/markdown"
        filename = f"{self._slugify(digestion.name or 'digestion')}-{output.get('output_kind') or 'output'}{ext}"
        content = str(output.get("content") or "").encode("utf-8")
        file_info = self.file_manager.save_file(content, filename, save_type, actor_user_id)
        if not file_info:
            raise DigestionError("Could not export Digestion output to Vault.", status_code=500, reason="output_export_failed")
        return {
            "success": True,
            "digestion_id": digestion.id,
            "output": output,
            "file": file_info.to_dict(),
            "agent_reference": self.agent_reference(digestion.id, actor_user_id),
        }

    def agent_reference(self, digestion_id: str, actor_user_id: str) -> dict[str, Any]:
        """Return a compact, copyable reference agents can use without raw Vault access."""
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        stats = self.stats(digestion.id)
        api_base = f"/api/v1/digestions/{digestion.id}"
        api = {
            "get": f"GET {api_base}",
            "sources": f"GET {api_base}/sources",
            "add_sources": f"POST {api_base}/sources",
            "merge_sources": f"POST {api_base}/merge",
            "add_materials": f"POST {api_base}/materials",
            "list_contributions": f"GET {api_base}/contributions",
            "append_contributions": f"POST {api_base}/contributions",
            "review_contribution": f"POST {api_base}/contributions/<contribution_id>",
            "build": f"POST {api_base}/build",
            "progress": f"GET {api_base}/progress",
            "query": f"POST {api_base}/query",
            "context": f"POST {api_base}/context",
            "datapoints_extract": f"POST {api_base}/datapoints/extract",
            "datapoints_search": f"POST {api_base}/datapoints/search",
            "figures": f"GET {api_base}/figures",
            "outputs": f"GET|POST {api_base}/outputs",
            "get_output": f"GET {api_base}/outputs/<output_ref>",
            "export_output": f"POST {api_base}/outputs/<output_ref>/export",
            "package": f"GET {api_base}/package",
            "export_package": f"POST {api_base}/package/export",
            "access_request": f"GET {api_base}/access-request",
            "acl_list": f"GET {api_base}/acl",
            "acl_grant": f"POST {api_base}/acl",
            "acl_revoke": f"DELETE {api_base}/acl/<grantee_user_id>",
        }
        mcp = {
            "list": "canopy_digest_list",
            "create": "canopy_digest_create",
            "build": "canopy_digest_build",
            "query": "canopy_digest_query",
            "context": "canopy_digest_context",
            "sources": "canopy_digest_sources",
            "add_sources": "canopy_digest_add_sources",
            "add_materials": "canopy_digest_add_materials",
            "append_contributions": "canopy_digest_append_contributions",
            "contributions": "canopy_digest_contributions",
            "datapoints_extract": "canopy_digest_datapoints_extract",
            "datapoints_search": "canopy_digest_datapoints_search",
            "figures": "canopy_digest_figures",
            "outputs": "canopy_digest_outputs",
            "request_access": "canopy_digest_request_access",
        }
        return {
            "kind": "canopy_digestion_reference_v1",
            "digestion_id": digestion.id,
            "name": digestion.name,
            "purpose": digestion.purpose or digestion.description,
            "status": digestion.status,
            "provider": digestion.provider,
            "embedding_model": digestion.embedding_model,
            "stats": stats,
            "query_endpoint": f"{api_base}/query",
            "context_endpoint": f"{api_base}/context",
            "package_endpoint": f"{api_base}/package",
            "access_request_endpoint": f"{api_base}/access-request",
            "api": api,
            "body_templates": {
                "query": {"query": "What should I know about this corpus?", "top_k": 8},
                "context": {"query": "Build a grounded context pack for this task.", "top_k": 8},
                "add_sources": {"source_file_ids": ["<vault_file_id>"], "build_after": False},
                "add_materials": {
                    "materials": [
                        {
                            "title": "Agent note or post excerpt",
                            "kind": "note",
                            "content": "...",
                            "source_uri": "canopy://...",
                        }
                    ]
                },
                "append_contributions": {
                    "contributions": [
                        {
                            "kind": "agent_note",
                            "title": "Synthesis note",
                            "content": "...",
                            "claims": ["..."],
                            "references": ["..."],
                            "source_file_ids": ["<vault_file_id>"],
                        }
                    ],
                    "build_after": False,
                    "review_required": False,
                },
                "review_contribution": {
                    "action": "accept|reject|review",
                    "note": "optional owner or manager review note",
                    "build_after": False,
                },
                "datapoints_extract": {
                    "lens": "optional extraction focus",
                    "max_chunks": 80,
                    "max_datapoints": 400,
                    "scope": "new",
                },
                "datapoints_search": {"query": "metric, material, method, claim, tag, or evidence term", "limit": 25},
                "acl_grant": {
                    "grantee_user_id": "<agent_or_user_id>",
                    "can_query": True,
                    "can_read_sources": False,
                    "can_manage": False,
                },
            },
            "mcp": mcp,
            "workflow": [
                "Start with query/context for cited RAG retrieval; cite file_name/page_label/snippet from returned results.",
                "Use sources/figures/datapoints only when source-metadata access is granted.",
                "Use add_sources or add_materials only when the human wants this Digestion expanded.",
                "Use append_contributions to preserve durable agent work product: notes, claims, facts, references, files, and optional datapoints.",
                "Use list_contributions/review_contribution or canopy_digest_contributions when additions should be staged, accepted, rejected, or marked owner-reviewed.",
                "Use datapoints_extract with scope='new' after adding documents; use scope='all' only for an explicit full refresh.",
                "Use package/export_package for portable handoffs; package snapshots do not grant live query access by themselves.",
                "When receiving a package from another user, treat its counts as a snapshot and call access_request/request_access first if live query fails.",
            ],
            "live_access": {
                "self_check": f"GET {api_base}/access-request",
                "package_is_snapshot": True,
                "package_does_not_grant_acl": True,
                "acl_grant_template": {
                    "grantee_user_id": "<agent_or_user_id>",
                    "can_query": True,
                    "can_read_sources": False,
                    "can_manage": False,
                },
            },
            "permissions": {
                "query_context": "read_files plus Digestion query access",
                "sources_figures_datapoints": "read_files plus query access plus can_read_sources",
                "build_add_sources_add_materials_contributions": "write_files plus Digestion manage access",
                "explicit_datapoint_append": "write_files plus manage access plus can_read_sources",
                "acl_management": "write_files plus Digestion manage access",
            },
            "note": (
                "Use this Digestion as a permissioned retrieval capability. "
                "Query access returns cited snippets; it does not grant raw File Vault access. "
                "If this reference came from an attached package but live query returns 403/query_denied, "
                "call access_request/canopy_digest_request_access and ask the owner to grant Digestion query access. "
                "For a rendered Canopy card, export the Digestion package and attach the package JSON to a post or DM."
            ),
        }

    def package_payload(self, digestion_id: str, actor_user_id: str, *, include_content: bool = True) -> dict[str, Any]:
        """Build a reusable machine package for humans or agents to attach/share."""
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        access = self._access_for(digestion, actor_user_id)
        generated_at = self._now()
        outputs = self.list_outputs(digestion.id, actor_user_id, include_content=include_content)
        if not outputs and access.get("can_manage"):
            try:
                self.generate_outputs(digestion.id, actor_user_id)
                outputs = self.list_outputs(digestion.id, actor_user_id, include_content=include_content)
            except DigestionError:
                outputs = []
        sources: list[dict[str, Any]] = []
        figures: list[dict[str, Any]] = []
        if access.get("can_read_sources"):
            sources = self.list_sources(digestion.id, user_id=actor_user_id)
            figures = self.list_figures(digestion.id, actor_user_id, limit=80).get("figures") or []
        digestion_payload = digestion.to_dict(access=access)
        digestion_payload["access_subject_user_id"] = actor_user_id
        digestion_payload["access_scope"] = "exporting_user"
        stats = self.stats(digestion.id)
        return {
            "kind": "canopy_digestion_package_v1",
            "generated_at": generated_at,
            "digestion": digestion_payload,
            "stats": stats,
            "agent_reference": self.agent_reference(digestion.id, actor_user_id),
            "snapshot": {
                "kind": "static_package_snapshot",
                "generated_at": generated_at,
                "source_count": int(stats.get("source_count") or 0),
                "chunks": int(stats.get("chunks") or 0),
                "token_estimate": int(stats.get("token_estimate") or 0),
                "status_at_export": digestion.status,
                "exporting_user_id": actor_user_id,
                "package_access_reflects": "exporting_user",
                "live_query_access_not_implied": True,
                "live_access_check_endpoint": f"GET /api/v1/digestions/{digestion.id}/access-request",
            },
            "access_subject": {
                "user_id": actor_user_id,
                "scope": "exporting_user",
                "access": access,
                "recipient_live_query_implied": False,
            },
            "live_query_access": {
                "package_access_reflects": "exporting_user",
                "recipient_live_query_implied": False,
                "recipient_query_requires_acl": True,
                "acl_endpoint": f"POST /api/v1/digestions/{digestion.id}/acl",
                "acl_body_template": {
                    "grantee_user_id": "<recipient_local_user_id>",
                    "can_query": True,
                    "can_read_sources": False,
                    "can_manage": False,
                },
            },
            "sources_included": bool(sources),
            "sources": sources,
            "figures_included": bool(figures),
            "figures": figures,
            "outputs": outputs,
            "reuse_guidance": [
                "Attach this package to a post, DM, task, or agent request when you want another consumer to understand what the Digestion is.",
                "The access/can_query values in this package describe only the exporting user/API key, not the recipient.",
                "Grant Digestion ACL access separately when a local agent or user should query the live index. In the Vault UI, use Share access on the Digestion card and paste the recipient's exact local user_id if needed; via API use POST /api/v1/digestions/<digestion_id>/acl.",
                "Exported packages are snapshots; the live Digestion may continue to change as files are added and rebuilt.",
            ],
        }

    def request_access_info(self, digestion_id: str, requester_user_id: str) -> dict[str, Any]:
        """Return non-sensitive guidance for requesting live Digestion access."""
        requester = self._clean_id(requester_user_id)
        digestion = self._get_digestion_obj(digestion_id)
        if not digestion:
            raise DigestionError("Digestion not found", status_code=404, reason="not_found")
        access = self._access_for(digestion, requester)
        acl_endpoint = f"POST /api/v1/digestions/{digestion.id}/acl"
        acl_body: dict[str, Any] = {
            "grantee_user_id": requester,
            "can_query": True,
            "can_read_sources": False,
            "can_manage": False,
        }
        return {
            "success": True,
            "digestion_id": digestion.id,
            "name": digestion.name,
            "status": digestion.status,
            "owner_user_id": digestion.owner_user_id,
            "your_user_id": requester,
            "your_access": access,
            "already_has_query_access": bool(access.get("can_query") or access.get("can_manage")),
            "acl_grant_endpoint": acl_endpoint,
            "acl_grant_body": acl_body,
            "guidance": (
                f"You already have live query access to Digestion '{digestion.name}' ({digestion.id})."
                if bool(access.get("can_query") or access.get("can_manage"))
                else (
                    f"You do not currently have query access to Digestion '{digestion.name}' ({digestion.id}). "
                    f"Ask the owner ({digestion.owner_user_id}) to grant your user id ({requester}) live query access. "
                    "In the Vault UI, the owner can use Share access on the Digestion card."
                )
            ),
        }

    def export_package_to_vault(self, digestion_id: str, actor_user_id: str) -> dict[str, Any]:
        """Save a whole Digestion package into the caller's Vault as one artifact."""
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        package = self.package_payload(digestion.id, actor_user_id)
        filename = f"{self._slugify(digestion.name or 'digestion')}-canopy-digestion-package.json"
        content = json.dumps(package, indent=2, sort_keys=True).encode("utf-8")
        file_info = self.file_manager.save_file(content, filename, "application/json", actor_user_id)
        if not file_info:
            raise DigestionError("Could not export Digestion package to Vault.", status_code=500, reason="package_export_failed")
        return {
            "success": True,
            "digestion_id": digestion.id,
            "package": package,
            "file": file_info.to_dict(),
            "agent_reference": package.get("agent_reference") or self.agent_reference(digestion.id, actor_user_id),
        }

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
    def _index_source(
        self,
        digestion: Digestion,
        source_row: Any,
        *,
        remaining_chunks: int,
        progress_callback: Optional[Any] = None,
    ) -> dict[str, int]:
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
        figure_segments: list[ExtractedSegment] = []
        figure_count = 0
        if self._is_pdf_file(info):
            if callable(progress_callback):
                progress_callback(
                    "figures_scanning",
                    f"Scanning {info.original_name} for embedded PDF figures.",
                    0.28,
                    {"file_size": len(file_data)},
                )
            try:
                figure_result = self._extract_pdf_figures_for_source(
                    digestion,
                    info,
                    file_data,
                    text_segments=segments,
                )
                figure_count = int(figure_result.get("figure_count") or 0)
                figure_segments = figure_result.get("segments") if isinstance(figure_result.get("segments"), list) else []
                if callable(progress_callback):
                    progress_callback(
                        "figures_extracted",
                        (
                            f"Captured {figure_count} PDF figure preview{'' if figure_count == 1 else 's'} from {info.original_name}."
                            if figure_count
                            else f"No reusable embedded figures were detected in {info.original_name}."
                        ),
                        0.32,
                        {"figure_count": figure_count},
                    )
            except Exception as exc:
                logger.warning("PDF figure extraction failed for %s in %s: %s", info.id, digestion.id, exc, exc_info=True)
                if callable(progress_callback):
                    progress_callback(
                        "figures_unavailable",
                        "PDF text indexing will continue; figure extraction was unavailable for this source.",
                        0.32,
                        {"figure_error": str(exc)[:500]},
                    )
        else:
            with self.db.get_connection() as conn:
                conn.execute(
                    "DELETE FROM digestion_pdf_figures WHERE digestion_id = ? AND source_file_id = ?",
                    (digestion.id, info.id),
                )
                conn.commit()
        if figure_segments:
            segments = [*segments, *figure_segments]
        if not segments:
            raise DigestionError("No extractable text found in source file.", status_code=415, reason="no_extractable_text")
        extracted_chars = sum(len(segment.text) for segment in segments)
        if callable(progress_callback):
            progress_callback(
                "text_extracted",
                f"Extracted {extracted_chars:,} characters from {info.original_name}.",
                0.34,
                {"extracted_chars": extracted_chars, "file_size": len(file_data), "figure_count": figure_count},
            )
        chunks = self._chunk_segments(segments, digestion.chunk_size, digestion.chunk_overlap, remaining_chunks=remaining_chunks)
        if not chunks:
            raise DigestionError("No indexable chunks were produced from source file.", status_code=415, reason="no_chunks")
        if callable(progress_callback):
            progress_callback(
                "chunking",
                f"Prepared {len(chunks)} semantic chunk{'' if len(chunks) == 1 else 's'} from {info.original_name}.",
                0.58,
                {"source_chunk_count": len(chunks), "extracted_chars": extracted_chars, "figure_count": figure_count},
            )
            progress_callback(
                "embedding",
                f"Embedding {len(chunks)} chunk{'' if len(chunks) == 1 else 's'} for retrieval.",
                0.72,
                {"source_chunk_count": len(chunks), "provider": digestion.provider, "embedding_model": digestion.embedding_model},
            )
        vectors = self._embed_chunks(chunks, provider=digestion.provider, model=digestion.embedding_model, dimensions=digestion.embedding_dimensions)
        if callable(progress_callback):
            progress_callback(
                "writing_index",
                f"Writing {len(chunks)} indexed chunk{'' if len(chunks) == 1 else 's'} to the local retrieval store.",
                0.9,
                {"source_chunk_count": len(chunks), "embedded_count": len(vectors)},
            )
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
                    extracted_chars,
                    len(chunks),
                    now,
                    digestion.id,
                    info.id,
                ),
            )
            conn.commit()
        return {"chunk_count": len(chunks), "embedded_count": len(vectors), "figure_count": figure_count}

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
        pypdf_error: Optional[Exception] = None
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency may be absent in minimal envs
            pypdf_error = exc
        else:
            try:
                reader = PdfReader(io.BytesIO(file_data))
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
                if segments:
                    return segments
            except Exception as exc:
                pypdf_error = exc

        try:
            fallback_segments = self._extract_pdfminer_segments(file_data)
            if fallback_segments:
                return fallback_segments
        except DigestionError:
            if pypdf_error is None:
                raise
        except Exception as exc:
            logger.debug("pdfminer PDF fallback failed: %s", exc, exc_info=True)

        if pypdf_error is not None:
            raise DigestionError("PDF could not be read for text extraction.", status_code=415, reason="pdf_unreadable") from pypdf_error
        return []

    def _extract_pdfminer_segments(self, file_data: bytes) -> list[ExtractedSegment]:
        try:
            from pdfminer.high_level import extract_pages, extract_text  # type: ignore
            from pdfminer.layout import LTTextContainer  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency may be absent in minimal envs
            raise DigestionError(
                "PDF fallback extraction requires the pdfminer.six dependency on this node.",
                status_code=503,
                reason="pdfminer_dependency_missing",
            ) from exc

        segments: list[ExtractedSegment] = []
        chars = 0
        try:
            for index, page_layout in enumerate(extract_pages(io.BytesIO(file_data)), start=1):
                if chars >= MAX_FILE_CHARS:
                    break
                page_parts: list[str] = []
                stack = list(page_layout)
                while stack:
                    element = stack.pop(0)
                    if isinstance(element, LTTextContainer):
                        page_parts.append(element.get_text())
                    elif hasattr(element, "__iter__"):
                        try:
                            stack[0:0] = list(element)
                        except TypeError:
                            pass
                text = self._normalize_text("\n".join(page_parts))
                if not text:
                    continue
                remaining = MAX_FILE_CHARS - chars
                segment_text = text[:remaining]
                chars += len(segment_text)
                segments.append(ExtractedSegment(text=segment_text, page_label=f"p. {index}"))
        except Exception as exc:
            logger.debug("pdfminer page-layout extraction failed: %s", exc, exc_info=True)

        if segments:
            return segments

        try:
            text = self._normalize_text(extract_text(io.BytesIO(file_data)) or "")
        except Exception as exc:
            raise DigestionError("PDF could not be read for text extraction.", status_code=415, reason="pdf_unreadable") from exc
        return [ExtractedSegment(text=text[:MAX_FILE_CHARS])] if text else []

    def _extract_pdf_figures_for_source(
        self,
        digestion: Digestion,
        info: FileInfo,
        file_data: bytes,
        *,
        text_segments: list[ExtractedSegment],
    ) -> dict[str, Any]:
        cached = self._cached_pdf_figure_rows(digestion.id, info.id, info.checksum)
        if cached:
            return {
                "figure_count": len(cached),
                "figures": [self._figure_row_to_dict(row) for row in cached],
                "segments": self._figure_rows_to_segments(cached, source_name=info.original_name),
                "cached": True,
            }

        captions_by_page = self._pdf_caption_candidates_by_page(text_segments)
        with self.db.get_connection() as conn:
            conn.execute(
                "DELETE FROM digestion_pdf_figures WHERE digestion_id = ? AND source_file_id = ?",
                (digestion.id, info.id),
            )
            conn.commit()

        pymupdf_rows = self._extract_pdf_figures_with_pymupdf(digestion, info, file_data, captions_by_page)
        if pymupdf_rows:
            rows = self._cached_pdf_figure_rows(digestion.id, info.id, info.checksum)
            return {
                "figure_count": len(rows),
                "figures": [self._figure_row_to_dict(row) for row in rows],
                "segments": self._figure_rows_to_segments(rows, source_name=info.original_name),
                "cached": False,
            }

        try:
            from pypdf import PdfReader  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency may be absent in minimal envs
            logger.debug("pypdf image extraction unavailable: %s", exc)
            return {"figure_count": 0, "figures": [], "segments": [], "cached": False}

        seen_hashes: set[str] = set()
        try:
            reader = PdfReader(io.BytesIO(file_data))
            figure_index = 0
            for page_number, page in enumerate(reader.pages, start=1):
                if figure_index >= MAX_PDF_FIGURES_PER_SOURCE:
                    break
                page_images = list(getattr(page, "images", []) or [])
                page_figure_order = 0
                for image in page_images:
                    if figure_index >= MAX_PDF_FIGURES_PER_SOURCE:
                        break
                    image_bytes = self._pdf_image_bytes(image)
                    if not image_bytes:
                        continue
                    image_hash = hashlib.sha256(image_bytes).hexdigest()
                    if image_hash in seen_hashes:
                        continue
                    page_label = f"p. {page_number}"
                    row_payload = self._persist_pdf_figure_image(
                        digestion,
                        info,
                        image_bytes,
                        figure_index=figure_index + 1,
                        page_number=page_number,
                        page_label=page_label,
                        page_figure_order=page_figure_order + 1,
                        captions_by_page=captions_by_page,
                        image_name=str(getattr(image, "name", "") or ""),
                        extraction_method="pypdf.embedded_image",
                        image_hash=image_hash,
                        metadata={"original_pdf_image_name": str(getattr(image, "name", "") or "")},
                    )
                    if row_payload:
                        seen_hashes.add(image_hash)
                        figure_index += 1
                        page_figure_order += 1
        except Exception as exc:
            logger.debug("PDF image extraction failed for %s: %s", info.original_name, exc, exc_info=True)

        rows = self._cached_pdf_figure_rows(digestion.id, info.id, info.checksum)
        return {
            "figure_count": len(rows),
            "figures": [self._figure_row_to_dict(row) for row in rows],
            "segments": self._figure_rows_to_segments(rows, source_name=info.original_name),
            "cached": False,
        }

    def _extract_pdf_figures_with_pymupdf(
        self,
        digestion: Digestion,
        info: FileInfo,
        file_data: bytes,
        captions_by_page: dict[str, list[str]],
    ) -> list[dict[str, Any]]:
        try:
            try:
                import pymupdf as fitz  # type: ignore
            except Exception:
                import fitz  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency may be absent in current envs
            logger.debug("PyMuPDF PDF figure extraction unavailable: %s", exc)
            return []

        rows: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        figure_index = 0
        try:
            with fitz.open(stream=file_data, filetype="pdf") as doc:
                for page_number, page in enumerate(doc, start=1):
                    if figure_index >= MAX_PDF_FIGURES_PER_SOURCE:
                        break
                    page_figure_order = 0
                    for image_info in page.get_images(full=True) or []:
                        if figure_index >= MAX_PDF_FIGURES_PER_SOURCE:
                            break
                        try:
                            xref = int(image_info[0])
                        except Exception:
                            continue
                        extracted = doc.extract_image(xref) or {}
                        image_bytes = extracted.get("image")
                        if not isinstance(image_bytes, bytes) or not image_bytes:
                            continue
                        image_hash = hashlib.sha256(image_bytes).hexdigest()
                        if image_hash in seen_hashes:
                            continue
                        ext = str(extracted.get("ext") or "").strip().lstrip(".")
                        page_label = f"p. {page_number}"
                        row_payload = self._persist_pdf_figure_image(
                            digestion,
                            info,
                            image_bytes,
                            figure_index=figure_index + 1,
                            page_number=page_number,
                            page_label=page_label,
                            page_figure_order=page_figure_order + 1,
                            captions_by_page=captions_by_page,
                            image_name=f"xref-{xref}.{ext}" if ext else f"xref-{xref}",
                            extraction_method="pymupdf.extract_image",
                            image_hash=image_hash,
                            metadata={
                                "pdf_xref": xref,
                                "pdf_image_ext": ext,
                                "pdf_image_width": extracted.get("width"),
                                "pdf_image_height": extracted.get("height"),
                            },
                        )
                        if row_payload:
                            seen_hashes.add(image_hash)
                            rows.append(row_payload)
                            figure_index += 1
                            page_figure_order += 1
        except Exception as exc:
            logger.debug("PyMuPDF PDF figure extraction failed for %s: %s", info.original_name, exc, exc_info=True)
        return rows

    def _persist_pdf_figure_image(
        self,
        digestion: Digestion,
        info: FileInfo,
        image_bytes: bytes,
        *,
        figure_index: int,
        page_number: int,
        page_label: str,
        page_figure_order: int,
        captions_by_page: dict[str, list[str]],
        image_name: str,
        extraction_method: str,
        image_hash: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        if len(image_bytes) > MAX_PDF_FIGURE_BYTES:
            return None
        content_type, ext = self._image_content_type_from_bytes(image_bytes, image_name)
        if not content_type:
            return None
        width, height = self._image_dimensions(image_bytes)
        if width and height and max(width, height) < MIN_PDF_FIGURE_DIMENSION:
            return None
        caption = self._caption_for_figure(captions_by_page, page_label, page_figure_order)
        context_text = self._figure_context_text(caption, page_label=page_label, source_name=info.original_name)
        filename = f"{self._slugify(Path(info.original_name or 'pdf').stem)}-figure-{figure_index:03d}{ext}"
        saved = self.file_manager.save_file(image_bytes, filename, content_type, digestion.owner_user_id)
        if not saved:
            return None
        row_payload = {
            "id": f"Dgf{secrets.token_hex(12)}",
            "digestion_id": digestion.id,
            "source_file_id": info.id,
            "source_checksum": info.checksum,
            "figure_index": figure_index,
            "page_number": page_number,
            "page_label": page_label,
            "image_file_id": saved.id,
            "image_name": saved.original_name,
            "content_type": saved.content_type,
            "width": width,
            "height": height,
            "byte_size": len(image_bytes),
            "caption": caption,
            "context_text": context_text,
            "vision_description": "",
            "extraction_method": extraction_method,
            "metadata": {
                "source_file_name": info.original_name,
                "image_hash": image_hash,
                "vision_status": "not_run",
                **(metadata or {}),
            },
        }
        self._insert_pdf_figure(row_payload)
        return row_payload

    def _insert_pdf_figure(self, payload: dict[str, Any]) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO digestion_pdf_figures (
                    id, digestion_id, source_file_id, source_checksum, figure_index,
                    page_number, page_label, image_file_id, image_name, content_type,
                    width, height, byte_size, caption, context_text, vision_description,
                    extraction_method, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(digestion_id, source_file_id, figure_index) DO UPDATE SET
                    source_checksum = excluded.source_checksum,
                    page_number = excluded.page_number,
                    page_label = excluded.page_label,
                    image_file_id = excluded.image_file_id,
                    image_name = excluded.image_name,
                    content_type = excluded.content_type,
                    width = excluded.width,
                    height = excluded.height,
                    byte_size = excluded.byte_size,
                    caption = excluded.caption,
                    context_text = excluded.context_text,
                    vision_description = excluded.vision_description,
                    extraction_method = excluded.extraction_method,
                    metadata_json = excluded.metadata_json
                """,
                (
                    payload.get("id") or f"Dgf{secrets.token_hex(12)}",
                    payload.get("digestion_id") or "",
                    payload.get("source_file_id") or "",
                    payload.get("source_checksum") or "",
                    int(payload.get("figure_index") or 0),
                    int(payload.get("page_number") or 0),
                    payload.get("page_label") or "",
                    payload.get("image_file_id") or "",
                    payload.get("image_name") or "",
                    payload.get("content_type") or "",
                    int(payload.get("width") or 0),
                    int(payload.get("height") or 0),
                    int(payload.get("byte_size") or 0),
                    payload.get("caption") or "",
                    payload.get("context_text") or "",
                    payload.get("vision_description") or "",
                    payload.get("extraction_method") or "",
                    json.dumps(payload.get("metadata") or {}, sort_keys=True),
                    self._now(),
                ),
            )
            conn.commit()

    def _cached_pdf_figure_rows(self, digestion_id: str, source_file_id: str, source_checksum: str) -> list[Any]:
        with self.db.get_connection() as conn:
            return conn.execute(
                """
                SELECT
                    f.*,
                    s.file_name AS source_file_name,
                    s.content_type AS source_content_type,
                    img.original_name AS vault_image_name,
                    img.size AS vault_image_size
                FROM digestion_pdf_figures f
                LEFT JOIN digestion_sources s
                  ON s.digestion_id = f.digestion_id
                 AND s.file_id = f.source_file_id
                LEFT JOIN files img ON img.id = f.image_file_id
                WHERE f.digestion_id = ?
                  AND f.source_file_id = ?
                  AND COALESCE(f.source_checksum, '') = COALESCE(?, '')
                ORDER BY f.page_number, f.figure_index
                """,
                (digestion_id, source_file_id, source_checksum or ""),
            ).fetchall()

    def _figure_rows_to_segments(self, rows: list[Any], *, source_name: str) -> list[ExtractedSegment]:
        segments: list[ExtractedSegment] = []
        for row in rows:
            data = self._figure_row_to_dict(row)
            text = self._figure_context_text(
                str(data.get("caption") or data.get("context_text") or ""),
                page_label=str(data.get("page_label") or ""),
                source_name=source_name or str(data.get("source_file_name") or ""),
                figure_index=int(data.get("figure_index") or 0),
                image_file_id=str(data.get("image_file_id") or ""),
                vision_description=str(data.get("vision_description") or ""),
            )
            if text:
                segments.append(ExtractedSegment(text=text, page_label=str(data.get("page_label") or "")))
        return segments

    def _figure_row_to_dict(self, row: Any) -> dict[str, Any]:
        try:
            metadata = json.loads(self._row_get(row, "metadata_json", "{}") or "{}")
        except Exception:
            metadata = {}
        image_file_id = str(self._row_get(row, "image_file_id", "") or "")
        source_file_id = str(self._row_get(row, "source_file_id", "") or "")
        return {
            "id": str(self._row_get(row, "id", "") or ""),
            "digestion_id": str(self._row_get(row, "digestion_id", "") or ""),
            "source_file_id": source_file_id,
            "source_file_name": str(self._row_get(row, "source_file_name", "") or source_file_id),
            "source_content_type": str(self._row_get(row, "source_content_type", "") or ""),
            "figure_index": int(self._row_get(row, "figure_index", 0) or 0),
            "page_number": int(self._row_get(row, "page_number", 0) or 0),
            "page_label": str(self._row_get(row, "page_label", "") or ""),
            "image_file_id": image_file_id,
            "image_name": str(self._row_get(row, "vault_image_name", "") or self._row_get(row, "image_name", "") or image_file_id),
            "image_url": f"/files/{image_file_id}" if image_file_id else "",
            "thumb_url": f"/files/{image_file_id}/thumb" if image_file_id else "",
            "content_type": str(self._row_get(row, "content_type", "") or ""),
            "width": int(self._row_get(row, "width", 0) or 0),
            "height": int(self._row_get(row, "height", 0) or 0),
            "byte_size": int(self._row_get(row, "byte_size", 0) or self._row_get(row, "vault_image_size", 0) or 0),
            "caption": str(self._row_get(row, "caption", "") or ""),
            "context_text": str(self._row_get(row, "context_text", "") or ""),
            "vision_description": str(self._row_get(row, "vision_description", "") or ""),
            "extraction_method": str(self._row_get(row, "extraction_method", "") or ""),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": str(self._row_get(row, "created_at", "") or ""),
        }

    @staticmethod
    def _is_pdf_file(info: FileInfo) -> bool:
        filename = str(info.original_name or "")
        content_type = str(info.content_type or "").split(";", 1)[0].strip().lower()
        return content_type == "application/pdf" or Path(filename).suffix.lower() == ".pdf"

    @staticmethod
    def _pdf_image_bytes(image: Any) -> bytes:
        data = getattr(image, "data", None)
        if isinstance(data, bytes) and data:
            return data
        pil_image = getattr(image, "image", None)
        if pil_image is not None:
            try:
                out = io.BytesIO()
                pil_image.save(out, format="PNG")
                return out.getvalue()
            except Exception:
                return b""
        return b""

    @staticmethod
    def _image_content_type_from_bytes(data: bytes, name: Any = "") -> tuple[str, str]:
        head = data[:16]
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if head.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", ".jpg"
        if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
            return "image/gif", ".gif"
        if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp", ".webp"
        ext = Path(str(name or "")).suffix.lower()
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            return {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }[ext], ".jpg" if ext == ".jpeg" else ext
        return "", ""

    @staticmethod
    def _image_dimensions(data: bytes) -> tuple[int, int]:
        try:
            from PIL import Image  # type: ignore
            with Image.open(io.BytesIO(data)) as image:
                return int(image.width or 0), int(image.height or 0)
        except Exception:
            return 0, 0

    @staticmethod
    def _pdf_caption_candidates_by_page(segments: list[ExtractedSegment]) -> dict[str, list[str]]:
        captions: dict[str, list[str]] = {}
        pattern = re.compile(
            r"\b(?:fig(?:ure)?\.?|table|chart|diagram)\s*(?:\d+[A-Za-z]?|[IVXLC]+)?\b",
            re.IGNORECASE,
        )
        for segment in segments or []:
            page_label = str(segment.page_label or "")
            text = str(segment.text or "")
            if not text:
                continue
            page_captions = captions.setdefault(page_label, [])
            for match in pattern.finditer(text):
                paragraph_start = text.rfind("\n\n", 0, match.start())
                paragraph_end = text.find("\n\n", match.start())
                start = paragraph_start + 2 if paragraph_start >= 0 else max(0, match.start() - 40)
                end = paragraph_end if paragraph_end >= 0 else min(len(text), match.start() + 620)
                snippet = text[start:end].strip()
                snippet = DigestionManager._normalize_text(snippet)
                if snippet and snippet not in page_captions:
                    page_captions.append(snippet[:700])
                if len(page_captions) >= 16:
                    break
        return captions

    @staticmethod
    def _caption_for_figure(captions_by_page: dict[str, list[str]], page_label: str, order: int) -> str:
        candidates = captions_by_page.get(page_label) or []
        if not candidates:
            return ""
        index = max(0, min(len(candidates) - 1, int(order or 1) - 1))
        return str(candidates[index] or "")[:900]

    @staticmethod
    def _figure_context_text(
        caption: str,
        *,
        page_label: str,
        source_name: str,
        figure_index: int = 0,
        image_file_id: str = "",
        vision_description: str = "",
    ) -> str:
        parts = [
            "PDF figure extracted for Canopy Digestion.",
            f"Source: {source_name}." if source_name else "",
            f"Figure index: {figure_index}." if figure_index else "",
            f"Page: {page_label}." if page_label else "",
            f"Caption/context: {caption}." if caption else "Caption/context: no nearby figure caption was detected.",
            f"Image file id: {image_file_id}." if image_file_id else "",
            f"Vision description: {vision_description}." if vision_description else "Vision description: not yet generated by an image-capable model.",
        ]
        return DigestionManager._normalize_text(" ".join(part for part in parts if part))

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
        if chunks:
            chunk_hashes = [chunk["chunk_hash"] for chunk in chunks]
            cached: dict[str, str] = {}
            batch_size = 500
            with self.db.get_connection() as conn:
                for start in range(0, len(chunk_hashes), batch_size):
                    batch = chunk_hashes[start:start + batch_size]
                    placeholders = ",".join(["?"] * len(batch))
                    rows = conn.execute(
                        f"""
                        SELECT chunk_hash, id FROM digestion_embeddings
                        WHERE provider = ? AND model = ? AND dimensions = ?
                          AND chunk_hash IN ({placeholders})
                        """,
                        (provider, model, cache_dimensions, *batch),
                    ).fetchall()
                    cached.update({str(row["chunk_hash"]): str(row["id"]) for row in rows})
            for chunk in chunks:
                chunk_hash = str(chunk["chunk_hash"])
                if chunk_hash in cached:
                    result[chunk_hash] = cached[chunk_hash]
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
                cursor = conn.execute(
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
                chunk_hash = str(chunk["chunk_hash"])
                if cursor.rowcount == 1:
                    result[chunk_hash] = embedding_id
                else:
                    row = conn.execute(
                        """
                        SELECT id FROM digestion_embeddings
                        WHERE provider = ? AND model = ? AND dimensions = ? AND chunk_hash = ?
                        """,
                        (provider, model, cache_dimensions, chunk_hash),
                    ).fetchone()
                    if row:
                        result[chunk_hash] = str(row["id"])
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

    def _existing_source_file_for_original(self, digestion_id: str, original_file_id: str, *, checksum: str = "") -> str:
        """Return the owner-bound source file already registered for a contributed file."""
        original_file_id = self._clean_id(original_file_id)
        if not original_file_id:
            return ""
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT file_id, file_checksum, source_metadata_json
                FROM digestion_sources
                WHERE digestion_id = ?
                """,
                (self._clean_id(digestion_id),),
            ).fetchall()
        for row in rows:
            file_id = str(row["file_id"] or "")
            if file_id == original_file_id:
                return file_id
            try:
                metadata = json.loads(row["source_metadata_json"] or "{}")
            except Exception:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            if str(metadata.get("original_file_id") or "") == original_file_id:
                return file_id
        checksum = str(checksum or "").strip()
        if checksum:
            for row in rows:
                try:
                    metadata = json.loads(row["source_metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                if str(metadata.get("original_checksum") or "") == checksum:
                    return str(row["file_id"] or "")
        return ""

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

    def _datapoint_chunk_rows(
        self,
        digestion_id: str,
        *,
        limit: int,
        file_ids: Optional[Iterable[str]] = None,
        exclude_file_ids: Optional[Iterable[str]] = None,
    ) -> list[Any]:
        clauses = ["c.digestion_id = ?"]
        params: list[Any] = [digestion_id]
        include_ids = [self._clean_id(item) for item in (file_ids or []) if self._clean_id(item)]
        exclude_ids = [self._clean_id(item) for item in (exclude_file_ids or []) if self._clean_id(item)]
        if include_ids:
            clauses.append(f"c.file_id IN ({','.join('?' for _ in include_ids)})")
            params.extend(include_ids)
        if exclude_ids:
            clauses.append(f"c.file_id NOT IN ({','.join('?' for _ in exclude_ids)})")
            params.extend(exclude_ids)
        params.append(max(1, int(limit or 1)))
        where_sql = " AND ".join(clauses)
        with self.db.get_connection() as conn:
            return conn.execute(
                f"""
                SELECT c.id AS chunk_id, c.file_id, c.chunk_index, c.text, c.token_estimate,
                       c.page_label, s.file_name, s.content_type, s.source_kind,
                       s.source_label, s.source_uri
                FROM digestion_chunks c
                LEFT JOIN digestion_sources s ON s.digestion_id = c.digestion_id AND s.file_id = c.file_id
                WHERE {where_sql}
                ORDER BY COALESCE(s.file_name, c.file_id) COLLATE NOCASE, c.file_id, c.chunk_index
                LIMIT ?
                """,
                params,
            ).fetchall()

    @staticmethod
    def _normalize_datapoint_extraction_scope(value: str) -> str:
        scope = str(value or "").strip().lower().replace("-", "_")
        if scope in {"all", "full", "rebuild", "refresh_all", "all_sources"}:
            return "all"
        return "new"

    def _structured_datapoint_output_row(self, digestion_id: str) -> dict[str, Any]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, digestion_id, output_kind, title, content_type, content,
                       metadata_json, created_by, created_at, updated_at
                FROM digestion_outputs
                WHERE digestion_id = ? AND output_kind = ?
                """,
                (digestion_id, STRUCTURED_DATAPOINT_OUTPUT_KIND),
            ).fetchone()
        return self._output_row_to_dict(row, include_content=False) if row else {}

    def _structured_datapoint_payload(self, digestion_id: str) -> dict[str, Any]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT content
                FROM digestion_outputs
                WHERE digestion_id = ? AND output_kind = ?
                """,
                (digestion_id, STRUCTURED_DATAPOINT_OUTPUT_KIND),
            ).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(row["content"] or "{}")
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _datapoint_source_file_ids(datapoints: Iterable[Any]) -> set[str]:
        file_ids: set[str] = set()
        for item in datapoints or []:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if isinstance(source, dict) and str(source.get("file_id") or "").strip():
                file_ids.add(str(source.get("file_id") or "").strip())
            source_chunks = item.get("source_chunks")
            if isinstance(source_chunks, list):
                for chunk in source_chunks:
                    if isinstance(chunk, dict) and str(chunk.get("file_id") or "").strip():
                        file_ids.add(str(chunk.get("file_id") or "").strip())
        return file_ids

    @staticmethod
    def _datapoint_field_counts(datapoints: Iterable[dict[str, Any]]) -> dict[str, int]:
        fields = (
            "materials",
            "methods",
            "measurements",
            "numerical_results",
            "relationships",
            "limitations_or_uncertainty",
        )
        return {
            field: sum(
                len(item.get(field) or [])
                for item in datapoints
                if isinstance(item, dict) and isinstance(item.get(field), list)
            )
            for field in fields
        }

    def _resolve_datapoint_llm_context(self, actor_user_id: str) -> dict[str, Any]:
        from .canopy_ai import CanopyLLMError, CanopyLLMManager

        try:
            secret_key = getattr(self.config, "secret_key", "") if self.config is not None else ""
            manager = CanopyLLMManager(self.db, secret_key or os.getenv("CANOPY_SECRET_KEY", ""))
            settings = manager._resolve_effective_digestion_settings(actor_user_id)
        except CanopyLLMError as exc:
            raise DigestionError(
                f"LLM-backed datapoint extraction is not configured: {exc}",
                status_code=int(getattr(exc, "status_code", 400) or 400),
                reason=f"datapoint_{getattr(exc, 'reason', 'llm_unavailable')}",
            ) from exc
        provider = str(settings.get("provider") or "openai").strip().lower()
        if provider not in {"openai", "bedrock"}:
            raise DigestionError(
                "Structured datapoint extraction requires an OpenAI Responses or AWS Bedrock provider.",
                status_code=400,
                reason="datapoint_unsupported_llm_provider",
            )
        return {
            "manager": manager,
            "provider": provider,
            "api_key": str(settings.get("api_key") or ""),
            "model": str(settings.get("model") or ""),
            "credential_source": str(settings.get("credential_source") or "user"),
            "default_lens": str(settings.get("default_lens") or ""),
            "parameters": settings.get("parameters") if isinstance(settings.get("parameters"), dict) else {},
        }

    def _extract_structured_datapoints_with_llm(
        self,
        digestion: Digestion,
        rows: list[Any],
        *,
        llm_context: dict[str, Any],
        lens: str,
        datapoint_limit: int,
        progress_callback: Optional[Any] = None,
    ) -> dict[str, Any]:
        parameters = llm_context.get("parameters") if isinstance(llm_context.get("parameters"), dict) else {}
        batch_record_limit = self._bounded_int(
            parameters.get("batch_records"),
            MAX_STRUCTURED_DATAPOINTS_PER_LLM_BATCH,
            1,
            120,
        )
        batches = self._datapoint_llm_batches(rows, parameters=parameters)
        if not batches:
            raise DigestionError("No indexed chunk text is available for LLM extraction.", status_code=400, reason="no_indexed_chunks")

        datapoints: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        touched_refs: set[str] = set()
        processed_refs: set[str] = set()
        system_prompt = self._structured_datapoint_system_prompt()
        total_batches = len(batches)
        for batch_index, batch in enumerate(batches, start=1):
            batch_refs = {str(entry["source_ref"]) for entry in batch}
            processed_refs.update(batch_refs)
            source_map = {str(entry["source_ref"]): entry for entry in batch}
            if callable(progress_callback):
                progress_callback({
                    "phase": "llm_batch",
                    "percent": min(94, 8 + int(((batch_index - 1) / max(1, total_batches)) * 84)),
                    "processed": batch_index - 1,
                    "total": total_batches,
                    "current_label": f"Batch {batch_index} of {total_batches}",
                    "message": (
                        f"Scanning batch {batch_index} of {total_batches} "
                        f"({len(batch)} chunk{'' if len(batch) == 1 else 's'})."
                    ),
                    "details": {
                        "batch_index": batch_index,
                        "batch_count": total_batches,
                        "datapoint_count": len(datapoints),
                        "remaining_datapoints": max(0, int(datapoint_limit) - len(datapoints)),
                    },
                })
            try:
                prompt = self._structured_datapoint_user_prompt(
                    digestion,
                    batch,
                    lens=lens,
                    batch_index=batch_index,
                    batch_count=total_batches,
                    remaining=max(1, int(datapoint_limit) - len(datapoints)),
                    batch_record_limit=batch_record_limit,
                )
                raw = self._call_datapoint_llm(llm_context, system_prompt=system_prompt, prompt=prompt)
                parsed = self._parse_datapoint_llm_json(raw, llm_context=llm_context, system_prompt=system_prompt)
                normalized, record_refs = self._normalize_llm_datapoints(
                    parsed.get("datapoints") if isinstance(parsed, dict) else [],
                    source_map=source_map,
                    digestion=digestion,
                    remaining=max(0, int(datapoint_limit) - len(datapoints)),
                )
                datapoints.extend(normalized)
                touched_refs.update(record_refs)
                if callable(progress_callback):
                    progress_callback({
                        "phase": "batch_normalized",
                        "percent": min(96, 8 + int((batch_index / max(1, total_batches)) * 84)),
                        "processed": batch_index,
                        "total": total_batches,
                        "current_label": f"Batch {batch_index} of {total_batches}",
                        "message": (
                            f"Batch {batch_index} normalized; {len(datapoints)} datapoint"
                            f"{'' if len(datapoints) == 1 else 's'} retained so far."
                        ),
                        "details": {
                            "batch_index": batch_index,
                            "batch_count": total_batches,
                            "datapoint_count": len(datapoints),
                            "failed_batches": len(errors),
                        },
                    })
            except DigestionError as exc:
                errors.append({
                    "batch_index": batch_index,
                    "source_refs": sorted(batch_refs),
                    "reason": getattr(exc, "reason", "datapoint_batch_failed"),
                    "error": str(exc)[:500],
                })
                if callable(progress_callback):
                    progress_callback({
                        "phase": "batch_error",
                        "percent": min(96, 8 + int((batch_index / max(1, total_batches)) * 84)),
                        "processed": batch_index,
                        "total": total_batches,
                        "current_label": f"Batch {batch_index} of {total_batches}",
                        "message": f"Batch {batch_index} had an extraction issue and was skipped.",
                        "details": {
                            "batch_index": batch_index,
                            "batch_count": total_batches,
                            "datapoint_count": len(datapoints),
                            "failed_batches": len(errors),
                            "last_error": str(exc)[:500],
                        },
                    })
            if len(datapoints) >= int(datapoint_limit):
                break

        if not datapoints and errors:
            first = errors[0]
            raise DigestionError(
                f"LLM datapoint extraction failed before producing usable records: {first.get('error') or 'unknown error'}",
                status_code=502,
                reason="datapoint_llm_extraction_failed",
            )
        return {
            "datapoints": datapoints[:int(datapoint_limit)],
            "errors": errors,
            "stats": {
                "batches_considered": len(batches),
                "failed_batches": len(errors),
                "chunks_without_datapoints": len(processed_refs - touched_refs),
                "batch_chunk_limit": self._bounded_int(
                    parameters.get("batch_chunks"),
                    6,
                    1,
                    24,
                ),
                "batch_char_limit": self._bounded_int(
                    parameters.get("batch_chars"),
                    18000,
                    4000,
                    60000,
                ),
                "chunk_char_limit": self._bounded_int(
                    parameters.get("chunk_chars"),
                    2800,
                    800,
                    8000,
                ),
                "batch_record_limit": batch_record_limit,
            },
        }

    def _datapoint_llm_batches(self, rows: list[Any], *, parameters: Optional[dict[str, Any]] = None) -> list[list[dict[str, Any]]]:
        parameters = parameters if isinstance(parameters, dict) else {}
        batch_chunk_limit = self._bounded_int(parameters.get("batch_chunks"), 6, 1, 24)
        batch_char_limit = self._bounded_int(parameters.get("batch_chars"), 18000, 4000, 60000)
        chunk_char_limit = self._bounded_int(parameters.get("chunk_chars"), 2800, 800, 8000)
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for index, row in enumerate(rows, start=1):
            text = self._normalize_text(str(row["text"] or ""))[:chunk_char_limit]
            if not text:
                continue
            entry = {
                "source_ref": f"chunk_{index:04d}",
                "row": row,
                "file_id": str(row["file_id"] or ""),
                "file_name": str(row["file_name"] or row["file_id"] or ""),
                "content_type": str(row["content_type"] or ""),
                "source_kind": str(self._row_get(row, "source_kind", "vault_file") or "vault_file"),
                "source_label": str(self._row_get(row, "source_label", "") or row["file_name"] or ""),
                "source_uri": str(self._row_get(row, "source_uri", "") or ""),
                "chunk_id": str(row["chunk_id"] or ""),
                "chunk_index": int(row["chunk_index"] or 0),
                "page_label": str(row["page_label"] or ""),
                "token_estimate": int(row["token_estimate"] or 0),
                "text": text,
            }
            entry_chars = len(text) + 600
            if current and (len(current) >= batch_chunk_limit or current_chars + entry_chars > batch_char_limit):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(entry)
            current_chars += entry_chars
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _structured_datapoint_system_prompt() -> str:
        return """You are Canopy's LLM structured datapoint extraction engine.

Extract reusable datapoints from supplied source chunks. Use intelligence to normalize concepts across chunks, but stay strictly source-grounded.

Rules:
- Return one valid JSON object only. No markdown fences, prose, comments, or trailing text.
- Use only facts present in the supplied chunks. Do not infer values, units, materials, methods, or relationships that are not in evidence.
- Every datapoint must include at least one evidence item with source_ref and a short exact quote from the source chunk.
- Normalize labels and terminology when the source supports it, but preserve original value_text and unit strings.
- Prefer fewer, higher-quality datapoints over noisy extraction.
- If no datapoints are supported, return {"datapoints":[]}.

Required JSON shape:
{
  "datapoints": [
    {
      "subject": "short normalized subject",
      "claim": "one source-grounded factual assertion",
      "materials": ["material, system, dataset, entity, or sample"],
      "methods": ["method, protocol, setup, model, or procedure"],
      "measurements": ["measured or observed property"],
      "numerical_results": ["source-grounded sentence or clause containing a number"],
      "relationships": ["comparison, trend, causal/associative relationship, or dependency"],
      "quantitative_results": [
        {"measurement_label":"label", "value_text":"42", "unit":"%", "evidence_sentence":"quote or close source sentence"}
      ],
      "limitations_or_uncertainty": ["limitations, uncertainty, caveats, or negative evidence"],
      "evidence": [
        {"source_ref":"chunk_0001", "quote":"short exact quote from that chunk"}
      ],
      "tags": ["short", "lowercase", "tags"],
      "confidence": 0.0
    }
  ]
}
""".strip()

    def _structured_datapoint_user_prompt(
        self,
        digestion: Digestion,
        batch: list[dict[str, Any]],
        *,
        lens: str,
        batch_index: int,
        batch_count: int,
        remaining: int,
        batch_record_limit: int = MAX_STRUCTURED_DATAPOINTS_PER_LLM_BATCH,
    ) -> str:
        chunks = [
            {
                "source_ref": entry["source_ref"],
                "file_name": entry["file_name"],
                "source_kind": entry["source_kind"],
                "source_label": entry["source_label"],
                "page_label": entry["page_label"],
                "chunk_index": entry["chunk_index"],
                "text": entry["text"],
            }
            for entry in batch
        ]
        return (
            f"Digestion: {digestion.name or digestion.id}\n"
            f"Purpose: {digestion.purpose or digestion.description or 'Extract reusable source-grounded datapoints.'}\n"
            f"Extraction lens: {str(lens or '').strip()[:500] or 'general reusable scientific/technical datapoints'}\n"
            f"Batch: {batch_index} of {batch_count}\n"
            f"Maximum datapoints to return for this batch: {min(max(1, int(batch_record_limit or 1)), max(1, int(remaining or 1)))}\n\n"
            "Source chunks JSON:\n"
            f"{json.dumps(chunks, ensure_ascii=False, indent=2)}\n\n"
            "Return the required JSON object now."
        )

    def _call_datapoint_llm(self, llm_context: dict[str, Any], *, system_prompt: str, prompt: str) -> str:
        from .canopy_ai import CanopyLLMError

        manager = llm_context.get("manager")
        provider = str(llm_context.get("provider") or "openai").strip().lower()
        parameters = llm_context.get("parameters") if isinstance(llm_context.get("parameters"), dict) else {}
        max_output_tokens = self._datapoint_max_output_tokens(provider=provider, parameters=parameters)
        try:
            if provider == "openai":
                return manager._call_openai(
                    api_key=str(llm_context.get("api_key") or ""),
                    model=str(llm_context.get("model") or "gpt-5-mini"),
                    system_prompt=system_prompt,
                    prompt=prompt,
                    web_search_enabled=False,
                    max_output_tokens=max_output_tokens,
                )
            if provider == "bedrock":
                return manager._call_bedrock(
                    credential_secret=str(llm_context.get("api_key") or ""),
                    model=str(llm_context.get("model") or ""),
                    system_prompt=system_prompt,
                    prompt=prompt,
                    max_output_tokens=max_output_tokens,
                )
        except CanopyLLMError as exc:
            raise DigestionError(
                f"LLM datapoint extraction failed: {exc}",
                status_code=int(getattr(exc, "status_code", 502) or 502),
                reason=f"datapoint_{getattr(exc, 'reason', 'llm_error')}",
            ) from exc
        raise DigestionError("Unsupported datapoint LLM provider.", status_code=400, reason="datapoint_unsupported_llm_provider")

    def _datapoint_max_output_tokens(self, *, provider: str, parameters: dict[str, Any]) -> int:
        max_limit = 12000 if str(provider or "").strip().lower() == "bedrock" else 20000
        return self._bounded_int(
            parameters.get("max_output_tokens"),
            MAX_STRUCTURED_DATAPOINT_LLM_OUTPUT_TOKENS,
            1200,
            max_limit,
        )

    def _parse_datapoint_llm_json(
        self,
        raw_text: str,
        *,
        llm_context: dict[str, Any],
        system_prompt: str,
    ) -> dict[str, Any]:
        try:
            parsed = self._extract_json_object(str(raw_text or ""))
            if isinstance(parsed.get("datapoints"), list):
                return parsed
            raise ValueError("JSON object did not contain datapoints[]")
        except Exception as exc:
            repair_prompt = (
                "The previous response was not valid for Canopy structured datapoint extraction.\n"
                "Repair it into exactly one valid JSON object with a top-level datapoints array.\n"
                "Do not invent new datapoints. Preserve only information present in the original response.\n\n"
                "Invalid response:\n"
                f"<<<\n{str(raw_text or '')[:50000]}\n>>>\n\n"
                "Return repaired JSON only."
            )
            try:
                repaired = self._call_datapoint_llm(llm_context, system_prompt=system_prompt, prompt=repair_prompt)
                parsed = self._extract_json_object(repaired)
                if isinstance(parsed.get("datapoints"), list):
                    return parsed
            except Exception as repair_exc:
                raise DigestionError(
                    f"LLM returned unparsable datapoint JSON and repair failed: {repair_exc}",
                    status_code=502,
                    reason="datapoint_llm_invalid_json",
                ) from repair_exc
            raise DigestionError(
                f"LLM returned datapoint JSON without datapoints[]: {exc}",
                status_code=502,
                reason="datapoint_llm_invalid_schema",
            ) from exc

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw).strip()
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", raw):
            try:
                obj, _ = decoder.raw_decode(raw[match.start():])
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj
        raise ValueError("No JSON object found")

    def _normalize_llm_datapoints(
        self,
        raw_items: Any,
        *,
        source_map: dict[str, dict[str, Any]],
        digestion: Digestion,
        remaining: int,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        if not isinstance(raw_items, list):
            raise DigestionError("LLM datapoint response must include datapoints[].", status_code=502, reason="datapoint_llm_invalid_schema")
        normalized: list[dict[str, Any]] = []
        touched_refs: set[str] = set()
        for raw in raw_items:
            if len(normalized) >= int(remaining):
                break
            record = self._normalize_llm_datapoint_record(raw, source_map=source_map, digestion=digestion)
            if not record:
                continue
            normalized.append(record)
            touched_refs.update(str(ref) for ref in record.get("source_refs") or [])
        return normalized, touched_refs

    def _normalize_llm_datapoint_record(
        self,
        raw: Any,
        *,
        source_map: dict[str, dict[str, Any]],
        digestion: Digestion,
    ) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        evidence = self._normalize_llm_evidence(raw.get("evidence") or raw.get("citations"), source_map=source_map)
        if not evidence:
            return None
        source_refs = []
        source_chunks = []
        source_texts: list[str] = []
        for item in evidence:
            ref = str(item.get("source_ref") or "")
            if ref and ref not in source_refs:
                source_refs.append(ref)
                entry = source_map.get(ref)
                if entry:
                    source_chunks.append(self._datapoint_source_from_entry(entry, digestion=digestion))
                    source_texts.append(str(entry.get("text") or ""))
        if not source_chunks:
            return None
        materials = self._llm_string_list(raw.get("materials"), limit=12)
        methods = self._llm_string_list(raw.get("methods"), limit=8)
        measurements = self._llm_string_list(raw.get("measurements"), limit=8)
        numerical_results = [
            item
            for item in self._llm_string_list(raw.get("numerical_results"), limit=10)
            if self._datapoint_statement_supported(item, evidence=evidence, source_texts=source_texts)
        ]
        relationships = [
            item
            for item in self._llm_string_list(raw.get("relationships"), limit=8)
            if self._datapoint_statement_supported(item, evidence=evidence, source_texts=source_texts)
        ]
        uncertainty = [
            item
            for item in self._llm_string_list(raw.get("limitations_or_uncertainty") or raw.get("limitations"), limit=6)
            if self._datapoint_statement_supported(item, evidence=evidence, source_texts=source_texts)
        ]
        quantitative_results = self._normalize_llm_quantitative_results(
            raw.get("quantitative_results") or raw.get("quantities") or [],
            evidence=evidence,
        )
        claim = self._llm_scalar(raw.get("claim") or raw.get("assertion") or raw.get("summary"), limit=900)
        if claim and not self._datapoint_statement_supported(claim, evidence=evidence, source_texts=source_texts):
            claim = ""
        subject = self._llm_scalar(raw.get("subject") or raw.get("label") or raw.get("name"), limit=240)
        tags = [
            tag.lower()
            for tag in self._llm_string_list(raw.get("tags"), limit=12, item_limit=48)
            if tag and not tag.isdigit()
        ]
        confidence = self._normalize_confidence(raw.get("confidence"))
        if confidence is None:
            confidence = self._datapoint_confidence(
                materials=materials,
                methods=methods,
                measurements=measurements,
                numerical_results=numerical_results,
                relationships=relationships,
            )
        fingerprint_basis = json.dumps(
            {
                "digestion_id": digestion.id,
                "subject": subject,
                "claim": claim,
                "source_refs": source_refs,
                "evidence": [item.get("text") or item.get("quote") or "" for item in evidence[:3]],
            },
            sort_keys=True,
        )
        primary_source = source_chunks[0]
        return {
            "id": f"Dp{hashlib.sha256(fingerprint_basis.encode('utf-8')).hexdigest()[:18]}",
            "schema_version": "canopy_structured_datapoint_record_v1",
            "subject": subject,
            "claim": claim,
            "source": primary_source,
            "source_refs": source_refs,
            "source_chunks": source_chunks,
            "materials": materials,
            "methods": methods,
            "measurements": measurements,
            "numerical_results": numerical_results,
            "relationships": relationships,
            "quantitative_results": quantitative_results,
            "limitations_or_uncertainty": uncertainty,
            "evidence": evidence,
            "tags": tags,
            "confidence": confidence,
            "notes": "Generated by LLM extraction from supplied indexed chunks only; verify evidence quotes before downstream use.",
        }

    def _datapoint_statement_supported(
        self,
        statement: str,
        *,
        evidence: list[dict[str, Any]],
        source_texts: list[str],
    ) -> bool:
        candidate = self._llm_scalar(statement, limit=900)
        if not candidate:
            return False
        supporting_texts = [
            text
            for text in (
                [str(item.get("text") or "") for item in evidence]
                + [str(text or "") for text in source_texts]
            )
            if text
        ]
        for source_text in supporting_texts:
            if self._datapoint_quote_supported(candidate, source_text):
                return True
        candidate_terms = self._query_terms(candidate)
        if len(candidate_terms) < 4:
            return False
        for source_text in supporting_texts:
            source_terms = self._query_terms(source_text)
            if not source_terms:
                continue
            overlap = len(candidate_terms & source_terms) / max(1, len(candidate_terms))
            if overlap >= DATAPOINT_MIN_TERM_OVERLAP:
                return True
        return False

    def _normalize_llm_evidence(self, raw: Any, *, source_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        evidence: list[dict[str, Any]] = []
        for item in items[:12]:
            if not isinstance(item, dict):
                continue
            ref = self._llm_scalar(
                item.get("source_ref") or item.get("source") or item.get("chunk_ref") or item.get("chunk"),
                limit=80,
            )
            if not ref and len(source_map) == 1:
                ref = next(iter(source_map.keys()))
            if ref not in source_map:
                continue
            quote = self._llm_scalar(item.get("quote") or item.get("text") or item.get("evidence"), limit=900)
            if not quote:
                continue
            entry = source_map[ref]
            if not self._datapoint_quote_supported(quote, str(entry.get("text") or "")):
                continue
            evidence.append({
                "source_ref": ref,
                "field": self._llm_scalar(item.get("field"), limit=80),
                "text": quote,
                "source_chunk_id": entry.get("chunk_id") or "",
                "chunk_index": entry.get("chunk_index") or 0,
                "page_label": entry.get("page_label") or "",
            })
        return evidence

    @staticmethod
    def _datapoint_quote_supported(quote: str, source_text: str) -> bool:
        needle = re.sub(r"\s+", " ", str(quote or "")).strip().lower()
        haystack = re.sub(r"\s+", " ", str(source_text or "")).strip().lower()
        if not needle or not haystack:
            return False
        if needle in haystack:
            return True
        # Allow minor punctuation/formatting drift, but require most informative
        # tokens to be present in the supplied chunk text.
        quote_terms = {
            token
            for token in _TOKEN_RE.findall(needle)
            if len(token) > 2 and token not in _COMMON_TERMS
        }
        if len(quote_terms) < 4:
            return False
        source_terms = {
            token
            for token in _TOKEN_RE.findall(haystack)
            if len(token) > 2 and token not in _COMMON_TERMS
        }
        overlap = len(quote_terms & source_terms) / max(1, len(quote_terms))
        return overlap >= 0.85

    def _datapoint_source_from_entry(self, entry: dict[str, Any], *, digestion: Digestion) -> dict[str, Any]:
        return {
            "digestion_id": digestion.id,
            "source_ref": entry.get("source_ref") or "",
            "file_id": entry.get("file_id") or "",
            "file_name": entry.get("file_name") or "",
            "content_type": entry.get("content_type") or "",
            "source_kind": entry.get("source_kind") or "vault_file",
            "source_label": entry.get("source_label") or entry.get("file_name") or "",
            "source_uri": entry.get("source_uri") or "",
            "chunk_id": entry.get("chunk_id") or "",
            "chunk_index": int(entry.get("chunk_index") or 0),
            "page_label": entry.get("page_label") or "",
            "token_estimate": int(entry.get("token_estimate") or 0),
        }

    def _normalize_llm_quantitative_results(self, raw: Any, *, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = raw if isinstance(raw, list) else ([raw] if raw else [])
        normalized: list[dict[str, Any]] = []
        for item in items[:24]:
            if isinstance(item, dict):
                value_text = self._llm_scalar(item.get("value_text") or item.get("value") or item.get("number"), limit=120)
                unit = self._llm_scalar(item.get("unit"), limit=60)
                label = self._llm_scalar(item.get("measurement_label") or item.get("label") or item.get("metric"), limit=160)
                sentence = self._llm_scalar(item.get("evidence_sentence") or item.get("sentence") or item.get("context"), limit=900)
            else:
                value_text = self._llm_scalar(item, limit=240)
                unit = ""
                label = ""
                sentence = ""
            if not value_text:
                continue
            if not sentence and evidence:
                sentence = str(evidence[0].get("text") or "")[:900]
            normalized.append({
                "value_text": value_text,
                "unit": unit,
                "measurement_label": label or "value",
                "evidence_sentence": sentence,
            })
        return normalized

    @staticmethod
    def _llm_scalar(value: Any, *, limit: int = 500) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            text = str(value)
        return re.sub(r"\s+", " ", text).strip()[:limit]

    def _llm_string_list(self, value: Any, *, limit: int = 8, item_limit: int = 500) -> list[str]:
        if value is None:
            return []
        raw_items = value if isinstance(value, list) else [value]
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            text = self._llm_scalar(item, limit=item_limit)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= int(limit):
                break
        return out

    @staticmethod
    def _normalize_confidence(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except Exception:
            return None
        if parsed > 1.0 and parsed <= 100.0:
            parsed = parsed / 100.0
        return round(max(0.0, min(parsed, 1.0)), 2)

    @staticmethod
    def _datapoint_confidence(
        *,
        materials: list[str],
        methods: list[str],
        measurements: list[str],
        numerical_results: list[str],
        relationships: list[str],
    ) -> float:
        score = 0.25
        if materials:
            score += 0.10
        if methods:
            score += 0.16
        if measurements:
            score += 0.16
        if numerical_results:
            score += 0.22
        if relationships:
            score += 0.11
        return round(min(score, 0.95), 2)

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

    @staticmethod
    def _query_term_overlap(query_terms: set[str], candidate_terms: set[str]) -> int:
        if not query_terms or not candidate_terms:
            return 0
        overlap = 0
        partial_min = max(3, int(MIN_PARTIAL_QUERY_TERM_CHARS or 3))
        for query_term in query_terms:
            if query_term in candidate_terms:
                overlap += 1
                continue
            if len(query_term) < partial_min:
                continue
            if any(
                candidate.startswith(query_term)
                or (len(candidate) >= partial_min and query_term.startswith(candidate))
                for candidate in candidate_terms
            ):
                overlap += 1
        return overlap

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
                    "pdf_figure_extraction",
                    "caption_context_binding",
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

    def _build_pdf_figures_output(self, digestion: Digestion) -> tuple[str, str, str, str, dict[str, Any]]:
        figures = self.list_figures(digestion.id, digestion.owner_user_id, limit=240).get("figures") or []
        payload = {
            "kind": PDF_FIGURE_SCHEMA_VERSION,
            "schema_version": PDF_FIGURE_SCHEMA_VERSION,
            "digestion": {
                "id": digestion.id,
                "name": digestion.name,
                "status": digestion.status,
                "built_at": digestion.built_at,
            },
            "stats": self.stats(digestion.id),
            "figures": figures,
            "reuse_guidance": [
                "Use caption, context_text, page_label, and image_file_id together; extracted figure images are source-derived artifacts.",
                "Agents with source metadata access can call the figures endpoint or fetch image_file_id through approved file endpoints for visual analysis.",
                "Vision descriptions are empty until an image-capable model pass is explicitly run in a later pipeline stage.",
            ],
            "generated_at": self._now(),
        }
        return (
            PDF_FIGURE_OUTPUT_KIND,
            f"{digestion.name or 'Digestion'} PDF Figures",
            "application/json",
            json.dumps(payload, indent=2, sort_keys=True),
            {
                "schema_version": PDF_FIGURE_SCHEMA_VERSION,
                "figure_count": len(figures),
                "source_revealing": True,
            },
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

    def _set_operation_progress(
        self,
        digestion_id: str,
        operation: str,
        *,
        status: str,
        phase: str = "",
        percent: int = 0,
        processed: Optional[int] = None,
        total: Optional[int] = None,
        current_label: str = "",
        message: str = "",
        details: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        digestion_id = self._clean_id(digestion_id)
        operation = str(operation or "").strip().lower() or "operation"
        now = self._now()
        bounded_percent = max(0, min(int(percent or 0), 100))
        with self._progress_lock:
            by_operation = self._operation_progress.setdefault(digestion_id, {})
            existing = dict(by_operation.get(operation) or {})
            started_at = str(existing.get("started_at") or now)
            finished_at = now if str(status or "").lower() in {"completed", "failed", "cancelled"} else ""
            payload = {
                "operation": operation,
                "status": str(status or "running"),
                "phase": str(phase or ""),
                "percent": bounded_percent,
                "processed": 0 if processed is None else max(0, int(processed or 0)),
                "total": 0 if total is None else max(0, int(total or 0)),
                "current_label": str(current_label or ""),
                "message": str(message or ""),
                "started_at": started_at,
                "updated_at": now,
                "finished_at": finished_at or str(existing.get("finished_at") or ""),
                "elapsed_seconds": self._elapsed_seconds(started_at, now),
                "details": details if isinstance(details, dict) else dict(existing.get("details") or {}),
            }
            by_operation[operation] = payload
            return dict(payload)

    def _progress_snapshot(self, digestion_id: str, *, include_source_details: bool = True) -> dict[str, Any]:
        digestion_id = self._clean_id(digestion_id)
        with self._progress_lock:
            operations = {
                str(operation): dict(payload or {})
                for operation, payload in (self._operation_progress.get(digestion_id) or {}).items()
            }
        for operation in ("build", "datapoints"):
            operations.setdefault(operation, self._idle_progress(operation))
        if not include_source_details:
            operations = {
                operation: self._public_progress_payload(payload)
                for operation, payload in operations.items()
            }
        return operations

    def _idle_progress(self, operation: str) -> dict[str, Any]:
        return {
            "operation": operation,
            "status": "idle",
            "phase": "idle",
            "percent": 0,
            "processed": 0,
            "total": 0,
            "current_label": "",
            "message": "",
            "started_at": "",
            "updated_at": "",
            "finished_at": "",
            "elapsed_seconds": 0,
            "details": {},
        }

    @staticmethod
    def _public_progress_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Strip source metadata from progress visible to query-only grantees."""
        public = dict(payload or {})
        public["current_label"] = ""
        public["message"] = DigestionManager._public_progress_message(public)
        details = public.get("details") if isinstance(public.get("details"), dict) else {}
        allowed_keys = {
            "chunk_count",
            "embedded_count",
            "figure_count",
            "datapoint_count",
            "quantitative_result_count",
            "chunks_considered",
            "batches_considered",
            "failed_batches",
            "max_chunks",
            "max_datapoints",
            "estimated_batches",
            "provider",
            "model",
            "credential_source",
            "final_status",
            "reason",
        }
        public["details"] = {key: details[key] for key in allowed_keys if key in details}
        return public

    @staticmethod
    def _public_progress_message(payload: dict[str, Any]) -> str:
        operation = str(payload.get("operation") or "operation")
        status = str(payload.get("status") or "idle")
        phase = str(payload.get("phase") or "")
        processed = int(payload.get("processed") or 0)
        total = int(payload.get("total") or 0)
        if status == "idle":
            return ""
        if status == "completed":
            return "Operation completed."
        if status == "failed":
            return "Operation failed."
        if operation == "build":
            return f"Building Digestion source {processed + 1} of {total}." if total else "Building Digestion."
        if operation == "datapoints":
            if phase in {"llm_batch", "batch_normalized", "batch_error"} and total:
                return f"Extracting datapoints batch {min(processed + 1, total)} of {total}."
            return "Extracting structured datapoints."
        return "Operation running."

    @staticmethod
    def _elapsed_seconds(started_at: str, now: str) -> int:
        try:
            started = datetime.fromisoformat(str(started_at or ""))
            current = datetime.fromisoformat(str(now or ""))
            return max(0, int((current - started).total_seconds()))
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Contribution ledger helpers
    # ------------------------------------------------------------------
    def _record_pending_contribution_ledger(
        self,
        digestion: Digestion,
        actor_user_id: str,
        *,
        contributions: list[dict[str, Any]],
        source_file_ids: list[str],
        datapoints: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, contribution in enumerate(contributions or [], start=1):
            rows.append(
                self._insert_contribution_ledger(
                    digestion,
                    actor_user_id,
                    contribution,
                    status=CONTRIBUTION_STATUS_PENDING,
                    index=index,
                    source_file_ids=self._contribution_file_ids(contribution),
                    datapoint_count=self._contribution_datapoint_count(contribution),
                )
            )
        if source_file_ids or datapoints:
            bundle = {
                "_ledger_payload_kind": "source_datapoint_bundle",
                "kind": "source_bundle",
                "title": "Referenced files and structured datapoints",
                "summary": "Top-level source_file_ids and/or structured datapoints submitted for owner review.",
                "source_file_ids": list(source_file_ids or []),
                "datapoints": list(datapoints or []),
            }
            rows.append(
                self._insert_contribution_ledger(
                    digestion,
                    actor_user_id,
                    bundle,
                    status=CONTRIBUTION_STATUS_PENDING,
                    index=len(rows) + 1,
                    source_file_ids=source_file_ids,
                    datapoint_count=len(datapoints or []),
                )
            )
        return rows

    def _record_accepted_contribution_ledger(
        self,
        digestion: Digestion,
        actor_user_id: str,
        *,
        contributions: list[dict[str, Any]],
        source_file_ids: list[str],
        datapoints: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        aggregate_material_ids = self._result_source_file_ids(result.get("materials"))
        aggregate_source_ids = self._result_source_file_ids(result.get("source_files"))
        skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
        for index, contribution in enumerate(contributions or [], start=1):
            rows.append(
                self._insert_contribution_ledger(
                    digestion,
                    actor_user_id,
                    contribution,
                    status=CONTRIBUTION_STATUS_ACCEPTED,
                    index=index,
                    source_file_ids=self._contribution_file_ids(contribution),
                    datapoint_count=self._contribution_datapoint_count(contribution),
                    material_file_ids=aggregate_material_ids,
                    added_source_file_ids=aggregate_source_ids,
                    skipped=skipped,
                    result=result,
                )
            )
        if source_file_ids or datapoints:
            bundle = {
                "_ledger_payload_kind": "source_datapoint_bundle",
                "kind": "source_bundle",
                "title": "Referenced files and structured datapoints",
                "summary": "Top-level source_file_ids and/or structured datapoints appended to the Digestion.",
                "source_file_ids": list(source_file_ids or []),
                "datapoints": list(datapoints or []),
            }
            rows.append(
                self._insert_contribution_ledger(
                    digestion,
                    actor_user_id,
                    bundle,
                    status=CONTRIBUTION_STATUS_ACCEPTED,
                    index=len(rows) + 1,
                    source_file_ids=source_file_ids,
                    datapoint_count=len(datapoints or []),
                    material_file_ids=aggregate_material_ids,
                    added_source_file_ids=aggregate_source_ids,
                    skipped=skipped,
                    result=result,
                )
            )
        return rows

    def _insert_contribution_ledger(
        self,
        digestion: Digestion,
        actor_user_id: str,
        payload: dict[str, Any],
        *,
        status: str,
        index: int,
        source_file_ids: Optional[Iterable[str]] = None,
        datapoint_count: int = 0,
        material_file_ids: Optional[Iterable[str]] = None,
        added_source_file_ids: Optional[Iterable[str]] = None,
        skipped: Optional[Iterable[dict[str, Any]]] = None,
        result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        title, contribution_kind = self._contribution_title_kind(payload, index=index)
        contribution_id = f"Dc{secrets.token_hex(12)}"
        now = self._now()
        tags = self._llm_string_list(payload.get("tags"), limit=24, item_limit=80) if isinstance(payload, dict) else []
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO digestion_contributions (
                    id, digestion_id, contributor_user_id, contribution_kind, title,
                    status, payload_json, summary, tags_json, confidence,
                    source_file_ids_json, material_file_ids_json, added_source_file_ids_json,
                    datapoint_count, skipped_json, result_json, metadata_json,
                    created_at, updated_at, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contribution_id,
                    digestion.id,
                    self._clean_id(actor_user_id),
                    contribution_kind,
                    title,
                    self._normalize_contribution_status(status) or CONTRIBUTION_STATUS_ACCEPTED,
                    self._json_dumps(payload),
                    self._contribution_summary(payload),
                    self._json_dumps(tags),
                    self._normalize_confidence(payload.get("confidence") if isinstance(payload, dict) else None),
                    self._json_dumps(self._clean_id_list(source_file_ids)),
                    self._json_dumps(self._clean_id_list(material_file_ids)),
                    self._json_dumps(self._clean_id_list(added_source_file_ids)),
                    max(0, int(datapoint_count or 0)),
                    self._json_dumps(list(skipped or [])),
                    self._json_dumps(result or {}),
                    self._json_dumps(metadata or {}),
                    now,
                    now,
                    now if self._normalize_contribution_status(status) == CONTRIBUTION_STATUS_ACCEPTED else None,
                ),
            )
            conn.commit()
        row = self._get_contribution_row(digestion.id, contribution_id)
        return self._contribution_row_to_dict(row, include_payload=False) if row else {"id": contribution_id, "status": status}

    def _get_contribution_row(self, digestion_id: str, contribution_id: str) -> Any:
        with self.db.get_connection() as conn:
            return conn.execute(
                """
                SELECT
                    c.*,
                    u.username AS contributor_username,
                    u.avatar_file_id AS contributor_avatar_file_id
                FROM digestion_contributions c
                LEFT JOIN users u ON u.id = c.contributor_user_id
                WHERE c.digestion_id = ? AND c.id = ?
                """,
                (self._clean_id(digestion_id), self._clean_id(contribution_id)),
            ).fetchone()

    def _contribution_row_to_dict(self, row: Any, *, include_payload: bool = False) -> dict[str, Any]:
        if not row:
            return {}
        source_file_ids = self._json_loads(row["source_file_ids_json"], [])
        material_file_ids = self._json_loads(row["material_file_ids_json"], [])
        added_source_file_ids = self._json_loads(row["added_source_file_ids_json"], [])
        tags = self._json_loads(row["tags_json"], [])
        skipped = self._json_loads(row["skipped_json"], [])
        result = self._json_loads(row["result_json"], {})
        contributor_id = str(row["contributor_user_id"] or "").strip()
        contributor_username = str(self._row_get(row, "contributor_username", "") or contributor_id).strip()
        avatar_file_id = str(self._row_get(row, "contributor_avatar_file_id", "") or "").strip()
        item = {
            "id": str(row["id"] or ""),
            "digestion_id": str(row["digestion_id"] or ""),
            "schema_version": DIGESTION_CONTRIBUTION_LEDGER_SCHEMA_VERSION,
            "contributor_user_id": contributor_id,
            "contributor": {
                "user_id": contributor_id,
                "username": contributor_username or "unknown contributor",
                "display_name": contributor_username or contributor_id or "Unknown contributor",
                "account_type": "",
                "avatar_file_id": avatar_file_id,
                "avatar_url": f"/files/{avatar_file_id}" if avatar_file_id else "",
            },
            "contribution_kind": str(row["contribution_kind"] or "agent_output"),
            "title": str(row["title"] or "Contribution"),
            "status": str(row["status"] or CONTRIBUTION_STATUS_ACCEPTED),
            "summary": str(row["summary"] or ""),
            "tags": tags if isinstance(tags, list) else [],
            "confidence": row["confidence"],
            "source_file_ids": source_file_ids if isinstance(source_file_ids, list) else [],
            "material_file_ids": material_file_ids if isinstance(material_file_ids, list) else [],
            "added_source_file_ids": added_source_file_ids if isinstance(added_source_file_ids, list) else [],
            "datapoint_count": int(row["datapoint_count"] or 0),
            "skipped": skipped if isinstance(skipped, list) else [],
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "reviewed_by": str(row["reviewed_by"] or ""),
            "reviewed_at": str(row["reviewed_at"] or ""),
            "review_note": str(row["review_note"] or ""),
            "accepted_at": str(row["accepted_at"] or ""),
            "rejected_at": str(row["rejected_at"] or ""),
        }
        if include_payload:
            item["payload"] = self._json_loads(row["payload_json"], {})
            item["result"] = result if isinstance(result, dict) else {}
            item["metadata"] = self._json_loads(row["metadata_json"], {})
        return item

    def _contribution_title_kind(self, payload: dict[str, Any], *, index: int) -> tuple[str, str]:
        title = str(
            payload.get("title")
            or payload.get("name")
            or payload.get("label")
            or payload.get("summary")
            or f"Contribution {index}"
        ).strip()
        if len(title) > 220:
            title = title[:217].rstrip() + "..."
        contribution_kind = self._normalize_material_kind(
            payload.get("contribution_kind")
            or payload.get("kind")
            or payload.get("type")
            or "agent_output"
        )
        return title or f"Contribution {index}", contribution_kind

    def _contribution_summary(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("summary", "content", "text", "body", "notes", "claims", "facts", "useful_facts"):
            value = payload.get(key)
            if value:
                text = self._markdownish_value(value)
                text = re.sub(r"\s+", " ", str(text or "")).strip()
                if len(text) > 520:
                    text = text[:517].rstrip() + "..."
                return text
        file_ids = self._contribution_file_ids(payload)
        if file_ids:
            return f"References {len(file_ids)} Vault file{'s' if len(file_ids) != 1 else ''}."
        datapoint_count = self._contribution_datapoint_count(payload)
        if datapoint_count:
            return f"Includes {datapoint_count} structured datapoint{'s' if datapoint_count != 1 else ''}."
        return ""

    def _contribution_datapoint_count(self, payload: dict[str, Any]) -> int:
        if not isinstance(payload, dict):
            return 0
        raw = payload.get("datapoints") or payload.get("structured_datapoints") or []
        if isinstance(raw, dict):
            return 1
        if isinstance(raw, list):
            return sum(1 for item in raw if isinstance(item, dict))
        return 0

    def _result_source_file_ids(self, result: Any) -> list[str]:
        if not isinstance(result, dict):
            return []
        sources = result.get("sources") if isinstance(result.get("sources"), list) else []
        return self._clean_id_list(
            item.get("file_id") or item.get("id")
            for item in sources
            if isinstance(item, dict)
        )

    def _normalize_contribution_status(self, status: Any) -> str:
        clean = str(status or "").strip().lower()
        return clean if clean in CONTRIBUTION_STATUSES else ""

    def _json_dumps(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def _json_loads(self, value: Any, default: Any) -> Any:
        if value is None or value == "":
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            parsed = json.loads(str(value))
            return parsed
        except Exception:
            return default

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _clean_id_list(self, values: Optional[Iterable[Any]]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in values or []:
            file_id = self._clean_id(raw)
            if file_id and file_id not in seen:
                out.append(file_id)
                seen.add(file_id)
        return out

    def _contribution_file_ids(self, contribution: dict[str, Any]) -> list[str]:
        values: list[Any] = []
        for key in ("source_file_ids", "file_ids", "attachment_file_ids", "attachments", "files"):
            raw = contribution.get(key)
            if raw is None:
                continue
            raw_items = raw if isinstance(raw, list) else [raw]
            for item in raw_items:
                if isinstance(item, dict):
                    values.append(item.get("file_id") or item.get("id") or item.get("vault_file_id"))
                else:
                    values.append(item)
        return self._clean_id_list(values)

    def _contribution_to_material(
        self,
        digestion: Digestion,
        actor_user_id: str,
        contribution: dict[str, Any],
        *,
        index: int,
    ) -> Optional[dict[str, Any]]:
        contribution_kind = self._normalize_material_kind(
            contribution.get("contribution_kind")
            or contribution.get("kind")
            or contribution.get("type")
            or "agent_output"
        )
        title = str(
            contribution.get("title")
            or contribution.get("name")
            or contribution.get("label")
            or f"Agent contribution {index}"
        ).strip()[:220]
        meaningful_keys = (
            "content", "text", "body", "summary", "notes", "claims", "facts", "useful_facts",
            "connections", "references", "images", "tables", "spreadsheets", "data_files",
            "limitations", "next_steps", "datapoints", "structured_datapoints",
        )
        if not any(contribution.get(key) for key in meaningful_keys) and not self._contribution_file_ids(contribution):
            return None
        rendered = self._render_contribution_markdown(
            contribution,
            title=title,
            contribution_kind=contribution_kind,
            actor_user_id=actor_user_id,
        )
        if not rendered.strip():
            return None
        metadata = contribution.get("metadata") if isinstance(contribution.get("metadata"), dict) else {}
        material_metadata = dict(metadata or {})
        material_metadata.update({
            "schema_version": AGENT_CONTRIBUTION_SCHEMA_VERSION,
            "contribution_kind": contribution_kind,
            "contributed_by": actor_user_id,
            "contribution_index": index,
            "tags": self._llm_string_list(contribution.get("tags"), limit=24, item_limit=80),
            "source_file_ids": self._contribution_file_ids(contribution),
            "confidence": self._normalize_confidence(contribution.get("confidence")),
        })
        return {
            "title": title,
            "source_kind": "agent_contribution",
            "source_uri": str(contribution.get("source_uri") or contribution.get("uri") or contribution.get("url") or "").strip(),
            "content_type": "text/markdown",
            "content": rendered,
            "metadata": material_metadata,
        }

    def _render_contribution_markdown(
        self,
        contribution: dict[str, Any],
        *,
        title: str,
        contribution_kind: str,
        actor_user_id: str,
    ) -> str:
        lines = [
            f"# {title}",
            "",
            f"- Schema: `{AGENT_CONTRIBUTION_SCHEMA_VERSION}`",
            f"- Contribution kind: `{contribution_kind}`",
            f"- Contributed by: `{actor_user_id}`",
            f"- Contributed at: `{self._now()}`",
        ]
        confidence = self._normalize_confidence(contribution.get("confidence"))
        if confidence is not None:
            lines.append(f"- Confidence: `{confidence}`")
        source_uri = str(contribution.get("source_uri") or contribution.get("uri") or contribution.get("url") or "").strip()
        if source_uri:
            lines.append(f"- Source URI: {source_uri}")
        tags = self._llm_string_list(contribution.get("tags"), limit=24, item_limit=80)
        if tags:
            lines.append(f"- Tags: {', '.join(tags)}")
        content = str(contribution.get("content") or contribution.get("text") or contribution.get("body") or "").strip()
        if content:
            lines.extend(["", "## Contribution", "", content])
        field_labels = (
            ("summary", "Summary"),
            ("notes", "Notes"),
            ("claims", "Claims"),
            ("facts", "Useful Facts"),
            ("useful_facts", "Useful Facts"),
            ("connections", "Connections"),
            ("references", "Additional References"),
            ("images", "Images / Figures"),
            ("tables", "Tables"),
            ("spreadsheets", "Spreadsheets / Data Files"),
            ("data_files", "Data Files"),
            ("limitations", "Limitations"),
            ("next_steps", "Suggested Next Steps"),
        )
        for key, label in field_labels:
            if key not in contribution:
                continue
            rendered = self._markdownish_value(contribution.get(key))
            if rendered:
                lines.extend(["", f"## {label}", "", rendered])
        file_ids = self._contribution_file_ids(contribution)
        if file_ids:
            lines.extend(["", "## Referenced Vault Files", ""])
            lines.extend(f"- `{file_id}`" for file_id in file_ids)
        return "\n".join(lines).strip() + "\n"

    def _markdownish_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            lines: list[str] = []
            for key, item in value.items():
                text = self._llm_scalar(item, limit=1600)
                if text:
                    lines.append(f"- **{key}:** {text}")
            return "\n".join(lines)
        if isinstance(value, list):
            lines = []
            for item in value[:80]:
                if isinstance(item, dict):
                    label = self._llm_scalar(
                        item.get("title") or item.get("label") or item.get("claim") or item.get("name"),
                        limit=160,
                    )
                    text = self._llm_scalar(item, limit=1600)
                    if label and text and label not in text[:220]:
                        lines.append(f"- **{label}:** {text}")
                    elif text:
                        lines.append(f"- {text}")
                else:
                    text = self._llm_scalar(item, limit=1600)
                    if text:
                        lines.append(f"- {text}")
            return "\n".join(lines)
        return self._llm_scalar(value, limit=2000)

    def _append_agent_structured_datapoints(
        self,
        digestion: Digestion,
        actor_user_id: str,
        raw_datapoints: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        existing_payload = self._structured_datapoint_payload(digestion.id)
        existing_datapoints = (
            existing_payload.get("datapoints")
            if isinstance(existing_payload, dict) and isinstance(existing_payload.get("datapoints"), list)
            else []
        )
        normalized: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        raw_all = list(raw_datapoints or [])
        raw_items = raw_all[:MAX_AGENT_DATAPOINTS_PER_APPEND]
        for index, item in enumerate(raw_items, start=1):
            if not isinstance(item, dict):
                skipped.append({"index": str(index), "reason": "datapoint_not_object"})
                continue
            record = self._normalize_agent_structured_datapoint(digestion, actor_user_id, item, index=index)
            if not record:
                skipped.append({"index": str(index), "reason": "empty_datapoint"})
                continue
            normalized.append(record)
        extra_count = max(0, len(raw_all) - len(raw_items))
        if extra_count:
            skipped.append({"reason": "datapoint_limit_reached", "count": str(extra_count)})

        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in [*existing_datapoints, *normalized]:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                item_id = self._agent_datapoint_identity(digestion.id, actor_user_id, item)
                item["id"] = item_id
            if item_id in seen_ids:
                if item in normalized:
                    skipped.append({"id": item_id, "reason": "duplicate_datapoint"})
                continue
            seen_ids.add(item_id)
            merged.append(item)
        added = max(0, len(merged) - len(existing_datapoints))
        quantitative_result_count = sum(
            len(item.get("quantitative_results") or [])
            for item in merged
            if isinstance(item, dict)
        )
        field_counts = self._datapoint_field_counts(merged)
        sources = self._source_summary_rows(digestion.id)
        existing_stats = existing_payload.get("stats") if isinstance(existing_payload.get("stats"), dict) else {}
        payload = dict(existing_payload or {})
        payload.update({
            "kind": STRUCTURED_DATAPOINT_SCHEMA_VERSION,
            "schema_version": STRUCTURED_DATAPOINT_SCHEMA_VERSION,
            "digestion": {
                "id": digestion.id,
                "name": digestion.name,
                "purpose": digestion.purpose or digestion.description,
                "status": digestion.status,
                "built_at": digestion.built_at,
            },
            "extractor": payload.get("extractor") or {
                "name": "canopy_structured_datapoint_aggregate",
                "version": "1",
                "mode": "agent_contributed",
                "network_calls": False,
            },
            "stats": {
                **existing_stats,
                "datapoint_count": len(merged),
                "new_datapoint_count": added,
                "agent_contributed_datapoint_count": sum(
                    1
                    for item in merged
                    if isinstance(item, dict) and isinstance(item.get("agent_contribution"), dict)
                ),
                "quantitative_result_count": quantitative_result_count,
                "source_count": len(sources),
                "field_counts": field_counts,
            },
            "sources": [
                {
                    "file_id": item.get("file_id") or "",
                    "file_name": item.get("file_name") or "",
                    "source_kind": item.get("source_kind") or "",
                    "source_label": item.get("source_label") or "",
                    "chunk_count": item.get("chunk_count") or 0,
                }
                for item in sources
            ],
            "datapoints": merged,
            "reuse_guidance": payload.get("reuse_guidance") or [
                "Treat each datapoint as a cited record, not as a complete reading of the underlying source.",
                "Agent-contributed datapoints may summarize work product outside indexed chunks; verify provenance before relying on them.",
                "Use semantic query/context endpoints for source-grounded chunk retrieval and datapoints/search for normalized records.",
            ],
            "updated_at": self._now(),
        })
        output = self._upsert_output(
            digestion,
            actor_user_id,
            STRUCTURED_DATAPOINT_OUTPUT_KIND,
            f"{digestion.name or 'Digestion'} Structured Datapoints",
            "application/json",
            json.dumps(payload, indent=2, sort_keys=True),
            {
                "schema_version": STRUCTURED_DATAPOINT_SCHEMA_VERSION,
                "extractor": payload.get("extractor") or {},
                "datapoint_count": len(merged),
                "agent_contributed_datapoint_count": payload["stats"].get("agent_contributed_datapoint_count", 0),
                "quantitative_result_count": quantitative_result_count,
                "field_counts": field_counts,
                "source_count": len(sources),
                "source_revealing": True,
                "append_schema_version": AGENT_CONTRIBUTION_SCHEMA_VERSION,
            },
        )
        return {
            "success": True,
            "digestion_id": digestion.id,
            "added": added,
            "skipped": skipped,
            "datapoint_count": len(merged),
            "quantitative_result_count": quantitative_result_count,
            "output": output,
            "preview": normalized[:3],
        }

    def _normalize_agent_structured_datapoint(
        self,
        digestion: Digestion,
        actor_user_id: str,
        item: dict[str, Any],
        *,
        index: int,
    ) -> Optional[dict[str, Any]]:
        subject = self._llm_scalar(item.get("subject") or item.get("title") or item.get("label"), limit=300)
        claim = self._llm_scalar(
            item.get("claim") or item.get("statement") or item.get("content") or item.get("text") or item.get("summary"),
            limit=1200,
        )
        materials = self._llm_string_list(item.get("materials") or item.get("material"), limit=24, item_limit=240)
        methods = self._llm_string_list(item.get("methods") or item.get("method"), limit=24, item_limit=300)
        measurements = self._llm_string_list(item.get("measurements") or item.get("metrics"), limit=24, item_limit=300)
        numerical_results = self._llm_string_list(item.get("numerical_results") or item.get("results"), limit=32, item_limit=500)
        relationships = self._llm_string_list(item.get("relationships") or item.get("connections"), limit=32, item_limit=500)
        limitations = self._llm_string_list(
            item.get("limitations_or_uncertainty") or item.get("limitations") or item.get("uncertainty"),
            limit=16,
            item_limit=500,
        )
        if not any([subject, claim, materials, methods, measurements, numerical_results, relationships, limitations]):
            return None
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        source_ref = self._llm_scalar(
            source.get("source_ref") or item.get("source_ref") or f"agent_contribution_{index:04d}",
            limit=120,
        )
        source_file_id = self._clean_id(
            source.get("file_id") or item.get("file_id") or item.get("source_file_id") or item.get("vault_file_id")
        )
        source_record = {
            "digestion_id": digestion.id,
            "source_ref": source_ref,
            "file_id": source_file_id,
            "file_name": self._llm_scalar(source.get("file_name") or item.get("file_name"), limit=240),
            "content_type": self._llm_scalar(source.get("content_type") or item.get("content_type"), limit=120),
            "source_kind": self._llm_scalar(source.get("source_kind") or item.get("source_kind") or "agent_contribution", limit=120),
            "source_label": self._llm_scalar(
                source.get("source_label") or item.get("_contribution_title") or item.get("source_label"),
                limit=240,
            ),
            "source_uri": self._llm_scalar(source.get("source_uri") or item.get("source_uri") or item.get("url"), limit=500),
            "chunk_id": self._llm_scalar(source.get("chunk_id") or item.get("chunk_id"), limit=120),
            "chunk_index": source.get("chunk_index") if isinstance(source.get("chunk_index"), int) else item.get("chunk_index"),
            "page_label": self._llm_scalar(source.get("page_label") or item.get("page_label") or item.get("page"), limit=80),
            "token_estimate": 0,
        }
        evidence = self._normalize_agent_evidence(item.get("evidence"), source_ref=source_ref, fallback_text=claim)
        quantitative_results = self._normalize_llm_quantitative_results(
            item.get("quantitative_results") or item.get("quantitative") or item.get("values"),
            evidence=evidence,
        )
        tags = self._llm_string_list(item.get("tags"), limit=24, item_limit=80)
        contribution_tags = self._llm_string_list(item.get("_contribution_tags"), limit=24, item_limit=80)
        for tag in [*contribution_tags, "agent_contributed"]:
            if tag and tag not in tags:
                tags.append(tag)
        record = {
            "id": self._llm_scalar(item.get("id") or item.get("datapoint_id"), limit=120),
            "subject": subject,
            "claim": claim or subject or "Agent-contributed datapoint",
            "materials": materials,
            "methods": methods,
            "measurements": measurements,
            "numerical_results": numerical_results,
            "relationships": relationships,
            "quantitative_results": quantitative_results,
            "limitations_or_uncertainty": limitations,
            "evidence": evidence,
            "source": source_record,
            "source_chunks": [source_record],
            "source_refs": [source_ref],
            "tags": tags[:24],
            "confidence": self._normalize_confidence(item.get("confidence")),
            "agent_contribution": {
                "schema_version": AGENT_CONTRIBUTION_SCHEMA_VERSION,
                "contributed_by": actor_user_id,
                "contributed_at": self._now(),
                "contribution_kind": self._normalize_material_kind(item.get("_contribution_kind") or item.get("kind") or "datapoint"),
                "notes": "Agent-contributed structured datapoint; verify provenance before downstream use.",
            },
        }
        if not record["id"]:
            record["id"] = self._agent_datapoint_identity(digestion.id, actor_user_id, record)
        return record

    def _normalize_agent_evidence(self, raw: Any, *, source_ref: str, fallback_text: str = "") -> list[dict[str, Any]]:
        raw_items = raw if isinstance(raw, list) else ([raw] if raw else [])
        evidence: list[dict[str, Any]] = []
        for item in raw_items[:12]:
            if isinstance(item, dict):
                text = self._llm_scalar(item.get("text") or item.get("quote") or item.get("evidence_sentence"), limit=900)
                ref = self._llm_scalar(item.get("source_ref") or item.get("source") or source_ref, limit=120) or source_ref
                field = self._llm_scalar(item.get("field") or "claim", limit=80)
            else:
                text = self._llm_scalar(item, limit=900)
                ref = source_ref
                field = "claim"
            if text:
                evidence.append({"source_ref": ref, "field": field, "text": text})
        if not evidence and fallback_text:
            evidence.append({"source_ref": source_ref, "field": "claim", "text": fallback_text[:900]})
        return evidence

    @staticmethod
    def _agent_datapoint_identity(digestion_id: str, actor_user_id: str, item: dict[str, Any]) -> str:
        seed = json.dumps(
            {
                "digestion_id": digestion_id,
                "actor_user_id": actor_user_id,
                "subject": item.get("subject") or "",
                "claim": item.get("claim") or "",
                "source": item.get("source") or {},
                "evidence": item.get("evidence") or [],
                "measurements": item.get("measurements") or [],
                "numerical_results": item.get("numerical_results") or [],
                "quantitative_results": item.get("quantitative_results") or [],
                "tags": item.get("tags") or [],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return f"adp_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"

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
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value if value is not None else default)
        except Exception:
            parsed = int(default)
        return max(int(minimum), min(parsed, int(maximum)))

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
