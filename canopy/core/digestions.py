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
DEFAULT_OPERATION_STALE_SECONDS = 2 * 60 * 60
CANCELLABLE_OPERATIONS = {"build", "datapoints", "structured_records", "figure_vision"}
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
STRUCTURED_RECORD_OUTPUT_KIND = "structured_records"
STRUCTURED_RECORD_SCHEMA_VERSION = "canopy_structured_records_v1"
AGENT_CONTRIBUTION_SCHEMA_VERSION = "canopy_agent_digestion_contribution_v1"
DIGESTION_CONTRIBUTION_LEDGER_SCHEMA_VERSION = "canopy_digestion_contribution_ledger_v1"
DIGESTION_EVIDENCE_SCHEMA_VERSION = "canopy_digestion_evidence_v1"
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
EVIDENCE_STATUS_CANDIDATE = "candidate"
EVIDENCE_STATUS_STABLE = "stable"
EVIDENCE_STATUS_CONTESTED = "contested"
EVIDENCE_STATUS_NEEDS_SOURCE = "needs_source"
EVIDENCE_STATUS_STALE = "stale"
EVIDENCE_STATUS_SUPERSEDED = "superseded"
EVIDENCE_STATUSES = {
    EVIDENCE_STATUS_CANDIDATE,
    EVIDENCE_STATUS_STABLE,
    EVIDENCE_STATUS_CONTESTED,
    EVIDENCE_STATUS_NEEDS_SOURCE,
    EVIDENCE_STATUS_STALE,
    EVIDENCE_STATUS_SUPERSEDED,
}
EVIDENCE_PRIORITIES = {"low", "normal", "high", "critical"}
EVIDENCE_REVIEW_ACTIONS = {
    "support",
    "challenge",
    "refine",
    "supersede",
    "mark_stale",
    "request_source",
    "confirm",
}
DEFAULT_STRUCTURED_DATAPOINT_CHUNKS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_DEFAULT_CHUNKS", "80"))
MAX_STRUCTURED_DATAPOINT_CHUNKS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_MAX_CHUNKS", "240"))
DEFAULT_STRUCTURED_DATAPOINTS_PER_RUN = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_DEFAULT_RECORDS", "400"))
MAX_STRUCTURED_DATAPOINTS_PER_RUN = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_MAX_RECORDS", "1200"))
MAX_AGENT_CONTRIBUTIONS_PER_APPEND = int(os.getenv("CANOPY_DIGESTION_MAX_AGENT_CONTRIBUTIONS_PER_APPEND", "50"))
MAX_AGENT_DATAPOINTS_PER_APPEND = int(os.getenv("CANOPY_DIGESTION_MAX_AGENT_DATAPOINTS_PER_APPEND", "500"))
MAX_DIGESTION_EVIDENCE_RECORDS_PER_APPEND = int(os.getenv("CANOPY_DIGESTION_MAX_EVIDENCE_RECORDS_PER_APPEND", "100"))
MAX_STRUCTURED_DATAPOINT_LLM_BATCH_CHUNKS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_BATCH_CHUNKS", "6"))
MAX_STRUCTURED_DATAPOINT_LLM_BATCH_CHARS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_BATCH_CHARS", "18000"))
MAX_STRUCTURED_DATAPOINT_LLM_CHUNK_CHARS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_CHUNK_CHARS", "2800"))
MAX_STRUCTURED_DATAPOINTS_PER_LLM_BATCH = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_BATCH_RECORDS", "40"))
MAX_STRUCTURED_DATAPOINT_LLM_OUTPUT_TOKENS = int(os.getenv("CANOPY_DIGESTION_DATAPOINT_LLM_OUTPUT_TOKENS", "7000"))
DATAPOINT_MIN_TERM_OVERLAP = float(os.getenv("CANOPY_DIGESTION_DATAPOINT_MIN_TERM_OVERLAP", "0.75"))
MAX_STRUCTURED_RECORDS_PER_APPEND = int(os.getenv("CANOPY_DIGESTION_MAX_STRUCTURED_RECORDS_PER_APPEND", "500"))
PDF_FIGURE_OUTPUT_KIND = "pdf_figures"
PDF_FIGURE_SCHEMA_VERSION = "canopy_pdf_figures_v1"
PDF_FIGURE_VISION_SCHEMA_VERSION = "canopy_pdf_figure_vision_v1"
VISUAL_EVIDENCE_OUTPUT_KIND = "visual_evidence"
VISUAL_EVIDENCE_SCHEMA_VERSION = "canopy_visual_evidence_v1"
MAX_PDF_FIGURES_PER_SOURCE = int(os.getenv("CANOPY_DIGESTION_MAX_PDF_FIGURES_PER_SOURCE", "80"))
MAX_PDF_VISUAL_EVIDENCE_PER_SOURCE = int(os.getenv("CANOPY_DIGESTION_MAX_PDF_VISUAL_EVIDENCE_PER_SOURCE", "120"))
MAX_PDF_FIGURE_BYTES = int(os.getenv("CANOPY_DIGESTION_MAX_PDF_FIGURE_BYTES", str(8 * 1024 * 1024)))
MIN_PDF_FIGURE_DIMENSION = int(os.getenv("CANOPY_DIGESTION_MIN_PDF_FIGURE_DIMENSION", "64"))
DEFAULT_FIGURE_VISION_LIMIT = int(os.getenv("CANOPY_DIGESTION_FIGURE_VISION_DEFAULT_LIMIT", "5"))
MAX_FIGURE_VISION_LIMIT = int(os.getenv("CANOPY_DIGESTION_FIGURE_VISION_MAX_LIMIT", "25"))
DEFAULT_FIGURE_VISION_IMAGE_BYTES = int(os.getenv("CANOPY_DIGESTION_FIGURE_VISION_IMAGE_BYTES", str(1536 * 1024)))
MAX_FIGURE_VISION_IMAGE_BYTES = int(os.getenv("CANOPY_DIGESTION_FIGURE_VISION_MAX_IMAGE_BYTES", str(6 * 1024 * 1024)))
DEFAULT_FIGURE_VISION_OUTPUT_TOKENS = int(os.getenv("CANOPY_DIGESTION_FIGURE_VISION_OUTPUT_TOKENS", "1200"))
MAX_FIGURE_VISION_OUTPUT_TOKENS = int(os.getenv("CANOPY_DIGESTION_FIGURE_VISION_MAX_OUTPUT_TOKENS", "4000"))
_SOURCE_REVEALING_OUTPUT_KINDS = {
    "manifest",
    "human_brief",
    STRUCTURED_DATAPOINT_OUTPUT_KIND,
    STRUCTURED_RECORD_OUTPUT_KIND,
    PDF_FIGURE_OUTPUT_KIND,
    VISUAL_EVIDENCE_OUTPUT_KIND,
}
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
        self._operation_cancel_requests: set[tuple[str, str]] = set()
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

                CREATE TABLE IF NOT EXISTS digestion_operations (
                    digestion_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle',
                    phase TEXT,
                    percent INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    current_label TEXT,
                    message TEXT,
                    details_json TEXT,
                    actor_user_id TEXT,
                    started_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP,
                    PRIMARY KEY (digestion_id, operation),
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL
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

                CREATE TABLE IF NOT EXISTS digestion_visual_evidence (
                    id TEXT PRIMARY KEY,
                    digestion_id TEXT NOT NULL,
                    source_file_id TEXT NOT NULL,
                    source_checksum TEXT,
                    evidence_kind TEXT NOT NULL DEFAULT 'visual',
                    evidence_index INTEGER NOT NULL,
                    page_number INTEGER NOT NULL DEFAULT 0,
                    page_label TEXT,
                    title TEXT,
                    caption TEXT,
                    context_text TEXT,
                    image_file_id TEXT,
                    table_text TEXT,
                    confidence REAL,
                    extraction_method TEXT,
                    metadata_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(digestion_id, source_file_id, evidence_kind, evidence_index),
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

                CREATE TABLE IF NOT EXISTS digestion_evidence_records (
                    id TEXT PRIMARY KEY,
                    digestion_id TEXT NOT NULL,
                    created_by_user_id TEXT,
                    record_kind TEXT NOT NULL DEFAULT 'finding',
                    statement TEXT NOT NULL,
                    summary TEXT,
                    scope TEXT,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    confidence REAL,
                    tags_json TEXT,
                    evidence_refs_json TEXT,
                    source_refs_json TEXT,
                    related_ids_json TEXT,
                    metadata_json TEXT,
                    superseded_by_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (superseded_by_id) REFERENCES digestion_evidence_records(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS digestion_evidence_reviews (
                    id TEXT PRIMARY KEY,
                    digestion_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    reviewer_user_id TEXT,
                    action TEXT NOT NULL,
                    note TEXT,
                    confidence REAL,
                    evidence_refs_json TEXT,
                    metadata_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                    FOREIGN KEY (evidence_id) REFERENCES digestion_evidence_records(id) ON DELETE CASCADE,
                    FOREIGN KEY (reviewer_user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_digestions_owner ON digestions(owner_user_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_acl_grantee ON digestion_acl(grantee_user_id, can_query, can_manage);
                CREATE INDEX IF NOT EXISTS idx_digestion_chunks_digestion ON digestion_chunks(digestion_id, file_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_digestion_sources_status ON digestion_sources(digestion_id, status);
                CREATE INDEX IF NOT EXISTS idx_digestion_outputs ON digestion_outputs(digestion_id, output_kind);
                CREATE INDEX IF NOT EXISTS idx_digestion_operations ON digestion_operations(digestion_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_pdf_figures ON digestion_pdf_figures(digestion_id, source_file_id, page_number);
                CREATE INDEX IF NOT EXISTS idx_digestion_visual_evidence ON digestion_visual_evidence(digestion_id, source_file_id, page_number, evidence_kind);
                CREATE INDEX IF NOT EXISTS idx_digestion_contributions_digestion ON digestion_contributions(digestion_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_contributions_contributor ON digestion_contributions(contributor_user_id, created_at);
                """
            )

            def _add_missing_columns(table_name: str, additions: dict[str, str]) -> None:
                columns = {
                    str(row["name"] if hasattr(row, "keys") else row[1])
                    for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                }
                for column, ddl in additions.items():
                    if column not in columns:
                        conn.execute(ddl)
                        columns.add(column)

            def _ensure_unique_text_index(table_name: str, column_name: str, index_name: str) -> None:
                duplicate = conn.execute(
                    f"""
                    SELECT {column_name}, COUNT(*) AS count
                    FROM {table_name}
                    WHERE {column_name} IS NOT NULL AND TRIM({column_name}) != ''
                    GROUP BY {column_name}
                    HAVING COUNT(*) > 1
                    LIMIT 1
                    """
                ).fetchone()
                if duplicate:
                    logger.warning(
                        "Skipping unique index %s on %s.%s because duplicate legacy values exist.",
                        index_name,
                        table_name,
                        column_name,
                    )
                    return
                conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table_name}({column_name})")

            def _table_info(table_name: str) -> list[Any]:
                return list(conn.execute(f"PRAGMA table_info({table_name})").fetchall())

            def _table_needs_canonical_rebuild(table_name: str, canonical_columns: set[str]) -> bool:
                info = _table_info(table_name)
                by_name = {str(row["name"] if hasattr(row, "keys") else row[1]): row for row in info}
                id_row = by_name.get("id")
                if not id_row:
                    return True
                try:
                    id_pk = int(id_row["pk"] if hasattr(id_row, "keys") else id_row[5])
                except Exception:
                    id_pk = 0
                if id_pk <= 0:
                    return True
                for row in info:
                    name = str(row["name"] if hasattr(row, "keys") else row[1])
                    if name in canonical_columns:
                        continue
                    try:
                        not_null = int(row["notnull"] if hasattr(row, "keys") else row[3])
                        default_value = row["dflt_value"] if hasattr(row, "keys") else row[4]
                    except Exception:
                        not_null = 0
                        default_value = None
                    if not_null and default_value is None:
                        return True
                return False

            def _rebuild_evidence_records_table() -> None:
                logger.warning("Rebuilding legacy digestion_evidence_records table into canonical schema.")
                conn.executescript(
                    """
                    DROP TABLE IF EXISTS digestion_evidence_records__repair;
                    CREATE TABLE digestion_evidence_records__repair (
                        id TEXT PRIMARY KEY,
                        digestion_id TEXT NOT NULL,
                        created_by_user_id TEXT,
                        record_kind TEXT NOT NULL DEFAULT 'finding',
                        statement TEXT NOT NULL,
                        summary TEXT,
                        scope TEXT,
                        status TEXT NOT NULL DEFAULT 'candidate',
                        priority TEXT NOT NULL DEFAULT 'normal',
                        confidence REAL,
                        tags_json TEXT,
                        evidence_refs_json TEXT,
                        source_refs_json TEXT,
                        related_ids_json TEXT,
                        metadata_json TEXT,
                        superseded_by_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                        FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                        FOREIGN KEY (superseded_by_id) REFERENCES digestion_evidence_records(id) ON DELETE SET NULL
                    );
                    INSERT OR IGNORE INTO digestion_evidence_records__repair (
                        id, digestion_id, created_by_user_id, record_kind, statement,
                        summary, scope, status, priority, confidence, tags_json,
                        evidence_refs_json, source_refs_json, related_ids_json,
                        metadata_json, superseded_by_id, created_at, updated_at
                    )
                    SELECT
                        CASE
                            WHEN id IS NOT NULL AND TRIM(id) != '' THEN id
                            ELSE 'ErLegacy' || lower(hex(randomblob(12)))
                        END,
                        digestion_id,
                        created_by_user_id,
                        COALESCE(NULLIF(record_kind, ''), 'finding'),
                        COALESCE(NULLIF(statement, ''), NULLIF(summary, ''), 'Legacy evidence record'),
                        summary,
                        scope,
                        COALESCE(NULLIF(status, ''), 'candidate'),
                        COALESCE(NULLIF(priority, ''), 'normal'),
                        confidence,
                        COALESCE(tags_json, '[]'),
                        COALESCE(evidence_refs_json, '[]'),
                        COALESCE(source_refs_json, '[]'),
                        COALESCE(related_ids_json, '[]'),
                        COALESCE(metadata_json, '{}'),
                        superseded_by_id,
                        COALESCE(created_at, CURRENT_TIMESTAMP),
                        COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
                    FROM digestion_evidence_records
                    WHERE digestion_id IS NOT NULL AND TRIM(digestion_id) != '';
                    DROP TABLE digestion_evidence_records;
                    ALTER TABLE digestion_evidence_records__repair RENAME TO digestion_evidence_records;
                    """
                )

            def _rebuild_evidence_reviews_table() -> None:
                logger.warning("Rebuilding legacy digestion_evidence_reviews table into canonical schema.")
                conn.executescript(
                    """
                    DROP TABLE IF EXISTS digestion_evidence_reviews__repair;
                    CREATE TABLE digestion_evidence_reviews__repair (
                        id TEXT PRIMARY KEY,
                        digestion_id TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        reviewer_user_id TEXT,
                        action TEXT NOT NULL,
                        note TEXT,
                        confidence REAL,
                        evidence_refs_json TEXT,
                        metadata_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (digestion_id) REFERENCES digestions(id) ON DELETE CASCADE,
                        FOREIGN KEY (evidence_id) REFERENCES digestion_evidence_records(id) ON DELETE CASCADE,
                        FOREIGN KEY (reviewer_user_id) REFERENCES users(id) ON DELETE SET NULL
                    );
                    INSERT OR IGNORE INTO digestion_evidence_reviews__repair (
                        id, digestion_id, evidence_id, reviewer_user_id, action,
                        note, confidence, evidence_refs_json, metadata_json, created_at
                    )
                    SELECT
                        CASE
                            WHEN id IS NOT NULL AND TRIM(id) != '' THEN id
                            ELSE 'ErvLegacy' || lower(hex(randomblob(12)))
                        END,
                        digestion_id,
                        evidence_id,
                        reviewer_user_id,
                        COALESCE(NULLIF(action, ''), 'support'),
                        note,
                        confidence,
                        COALESCE(evidence_refs_json, '[]'),
                        COALESCE(metadata_json, '{}'),
                        COALESCE(created_at, CURRENT_TIMESTAMP)
                    FROM digestion_evidence_reviews
                    WHERE digestion_id IS NOT NULL AND TRIM(digestion_id) != ''
                      AND evidence_id IS NOT NULL AND TRIM(evidence_id) != '';
                    DROP TABLE digestion_evidence_reviews;
                    ALTER TABLE digestion_evidence_reviews__repair RENAME TO digestion_evidence_reviews;
                    """
                )

            _add_missing_columns("digestion_sources", {
                "source_kind": "ALTER TABLE digestion_sources ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'vault_file'",
                "source_label": "ALTER TABLE digestion_sources ADD COLUMN source_label TEXT",
                "source_uri": "ALTER TABLE digestion_sources ADD COLUMN source_uri TEXT",
                "source_metadata_json": "ALTER TABLE digestion_sources ADD COLUMN source_metadata_json TEXT",
            })
            _add_missing_columns("digestion_evidence_records", {
                "id": "ALTER TABLE digestion_evidence_records ADD COLUMN id TEXT",
                "digestion_id": "ALTER TABLE digestion_evidence_records ADD COLUMN digestion_id TEXT",
                "created_by_user_id": "ALTER TABLE digestion_evidence_records ADD COLUMN created_by_user_id TEXT",
                "record_kind": "ALTER TABLE digestion_evidence_records ADD COLUMN record_kind TEXT DEFAULT 'finding'",
                "statement": "ALTER TABLE digestion_evidence_records ADD COLUMN statement TEXT DEFAULT ''",
                "summary": "ALTER TABLE digestion_evidence_records ADD COLUMN summary TEXT",
                "scope": "ALTER TABLE digestion_evidence_records ADD COLUMN scope TEXT",
                "status": "ALTER TABLE digestion_evidence_records ADD COLUMN status TEXT DEFAULT 'candidate'",
                "priority": "ALTER TABLE digestion_evidence_records ADD COLUMN priority TEXT DEFAULT 'normal'",
                "confidence": "ALTER TABLE digestion_evidence_records ADD COLUMN confidence REAL",
                "tags_json": "ALTER TABLE digestion_evidence_records ADD COLUMN tags_json TEXT",
                "evidence_refs_json": "ALTER TABLE digestion_evidence_records ADD COLUMN evidence_refs_json TEXT",
                "source_refs_json": "ALTER TABLE digestion_evidence_records ADD COLUMN source_refs_json TEXT",
                "related_ids_json": "ALTER TABLE digestion_evidence_records ADD COLUMN related_ids_json TEXT",
                "metadata_json": "ALTER TABLE digestion_evidence_records ADD COLUMN metadata_json TEXT",
                "superseded_by_id": "ALTER TABLE digestion_evidence_records ADD COLUMN superseded_by_id TEXT",
                "created_at": "ALTER TABLE digestion_evidence_records ADD COLUMN created_at TIMESTAMP",
                "updated_at": "ALTER TABLE digestion_evidence_records ADD COLUMN updated_at TIMESTAMP",
            })
            _add_missing_columns("digestion_evidence_reviews", {
                "id": "ALTER TABLE digestion_evidence_reviews ADD COLUMN id TEXT",
                "digestion_id": "ALTER TABLE digestion_evidence_reviews ADD COLUMN digestion_id TEXT",
                "evidence_id": "ALTER TABLE digestion_evidence_reviews ADD COLUMN evidence_id TEXT",
                "reviewer_user_id": "ALTER TABLE digestion_evidence_reviews ADD COLUMN reviewer_user_id TEXT",
                "action": "ALTER TABLE digestion_evidence_reviews ADD COLUMN action TEXT",
                "note": "ALTER TABLE digestion_evidence_reviews ADD COLUMN note TEXT",
                "confidence": "ALTER TABLE digestion_evidence_reviews ADD COLUMN confidence REAL",
                "evidence_refs_json": "ALTER TABLE digestion_evidence_reviews ADD COLUMN evidence_refs_json TEXT",
                "metadata_json": "ALTER TABLE digestion_evidence_reviews ADD COLUMN metadata_json TEXT",
                "created_at": "ALTER TABLE digestion_evidence_reviews ADD COLUMN created_at TIMESTAMP",
            })
            canonical_evidence_record_columns = {
                "id",
                "digestion_id",
                "created_by_user_id",
                "record_kind",
                "statement",
                "summary",
                "scope",
                "status",
                "priority",
                "confidence",
                "tags_json",
                "evidence_refs_json",
                "source_refs_json",
                "related_ids_json",
                "metadata_json",
                "superseded_by_id",
                "created_at",
                "updated_at",
            }
            canonical_evidence_review_columns = {
                "id",
                "digestion_id",
                "evidence_id",
                "reviewer_user_id",
                "action",
                "note",
                "confidence",
                "evidence_refs_json",
                "metadata_json",
                "created_at",
            }
            foreign_keys_enabled = 0
            try:
                fk_row = conn.execute("PRAGMA foreign_keys").fetchone()
                foreign_keys_enabled = int((fk_row[0] if fk_row else 0) or 0)
            except Exception:
                foreign_keys_enabled = 0
            if (
                _table_needs_canonical_rebuild("digestion_evidence_records", canonical_evidence_record_columns)
                or _table_needs_canonical_rebuild("digestion_evidence_reviews", canonical_evidence_review_columns)
            ):
                conn.execute("PRAGMA foreign_keys=OFF")
                if _table_needs_canonical_rebuild("digestion_evidence_records", canonical_evidence_record_columns):
                    _rebuild_evidence_records_table()
                if _table_needs_canonical_rebuild("digestion_evidence_reviews", canonical_evidence_review_columns):
                    _rebuild_evidence_reviews_table()
                conn.execute(f"PRAGMA foreign_keys={1 if foreign_keys_enabled else 0}")
            _ensure_unique_text_index(
                "digestion_evidence_records",
                "id",
                "idx_digestion_evidence_records_id_unique",
            )
            _ensure_unique_text_index(
                "digestion_evidence_reviews",
                "id",
                "idx_digestion_evidence_reviews_id_unique",
            )
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_digestion_evidence_records ON digestion_evidence_records(digestion_id, status, priority, updated_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_evidence_author ON digestion_evidence_records(created_by_user_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_digestion_evidence_reviews ON digestion_evidence_reviews(digestion_id, evidence_id, created_at);
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
                    "content_type": source_info.content_type,
                    "copied_to_owner_vault": copied_to_owner,
                    "submitted_by": actor_user_id,
                    "source_owner_user_id": str(source_info.uploaded_by or ""),
                    "digestion_owner_user_id": digestion_owner_id,
                    "metadata": metadata,
                })
            conn.execute("UPDATE digestions SET status = ?, updated_at = ? WHERE id = ?", ("draft", now, digestion.id))
            conn.commit()
        return {
            "success": True,
            "added": added,
            "skipped": skipped,
            "digestion_id": digestion.id,
            "access": self._access_for(digestion, actor_user_id),
            "sources": source_results,
            "source_count_after": len(self._source_rows(digestion.id)),
        }

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

    def _digestion_user_artifact_folder_id(self, digestion: Digestion, user_id: str) -> Optional[str]:
        """Return a caller-owned Vault folder for exported Digestion handoff artifacts."""
        actor_id = str(user_id or "").strip()
        if not actor_id:
            return None
        root_id = self._find_or_create_vault_folder(actor_id, "Digestion Intake")
        if not root_id:
            return None
        return self._find_or_create_vault_folder(actor_id, self._digestion_intake_folder_name(digestion), root_id)

    def _digestion_generated_figures_folder_id(self, digestion: Digestion) -> Optional[str]:
        intake_folder_id = self._digestion_intake_folder_id(digestion)
        if not intake_folder_id:
            return None
        return self._find_or_create_vault_folder(
            str(digestion.owner_user_id or "").strip(),
            "Generated figures",
            intake_folder_id,
        )

    def _organize_generated_figure_assets(self, digestion: Digestion) -> Optional[str]:
        """Move legacy generated PDF figure images out of Vault Home without changing file IDs."""
        owner_id = str(digestion.owner_user_id or "").strip()
        if not owner_id:
            return None
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT image_file_id
                FROM (
                    SELECT image_file_id FROM digestion_pdf_figures
                    WHERE digestion_id = ? AND image_file_id IS NOT NULL AND image_file_id != ''
                    UNION
                    SELECT image_file_id FROM digestion_visual_evidence
                    WHERE digestion_id = ? AND image_file_id IS NOT NULL AND image_file_id != ''
                )
                """,
                (digestion.id, digestion.id),
            ).fetchall()
        to_move: list[str] = []
        for row in rows:
            file_id = self._clean_id(row["image_file_id"] if row else "")
            if not file_id:
                continue
            info = self.file_manager.get_file(file_id)
            if not info or str(info.uploaded_by or "") != owner_id:
                continue
            if str(info.vault_folder_id or "").strip():
                continue
            to_move.append(file_id)
        if not to_move:
            return None
        folder_id = self._digestion_generated_figures_folder_id(digestion)
        if not folder_id:
            return None
        for file_id in to_move:
            try:
                self.file_manager.move_user_file_to_folder(owner_id, file_id, folder_id)
            except Exception:
                logger.debug("Could not organize generated Digestion figure asset %s", file_id, exc_info=True)
        return folder_id

    def _local_user_id_or_none(self, user_id: Any) -> Optional[str]:
        clean = self._clean_id(user_id)
        if not clean:
            return None
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT id FROM users WHERE id = ? LIMIT 1", (clean,)).fetchone()
        return clean if row else None

    @staticmethod
    def _merge_snapshot_output_kind(source_digestion_id: str, output_kind: str) -> str:
        source_tag = re.sub(r"[^A-Za-z0-9]+", "", str(source_digestion_id or ""))[-10:] or "source"
        kind_tag = re.sub(r"[^A-Za-z0-9_]+", "_", str(output_kind or "output").strip().lower())
        kind_tag = re.sub(r"_+", "_", kind_tag).strip("_")[:80] or "output"
        return f"merged_snapshot_{source_tag}_{kind_tag}"[:120]

    def _merge_remap_value(
        self,
        value: Any,
        file_id_map: dict[str, str],
        evidence_id_map: Optional[dict[str, str]] = None,
    ) -> Any:
        evidence_id_map = evidence_id_map or {}
        if isinstance(value, list):
            return [self._merge_remap_value(item, file_id_map, evidence_id_map) for item in value]
        if isinstance(value, dict):
            remapped: dict[str, Any] = {}
            file_keys = {
                "file_id",
                "source_file_id",
                "vault_file_id",
                "attachment_file_id",
                "image_file_id",
                "preview_file_id",
            }
            evidence_keys = {"evidence_id", "superseded_by_id"}
            for key, item in value.items():
                if key in file_keys and isinstance(item, str) and item in file_id_map:
                    remapped[key] = file_id_map[item]
                elif key in evidence_keys and isinstance(item, str) and item in evidence_id_map:
                    remapped[key] = evidence_id_map[item]
                else:
                    remapped[key] = self._merge_remap_value(item, file_id_map, evidence_id_map)
            return remapped
        if isinstance(value, str):
            return file_id_map.get(value, evidence_id_map.get(value, value))
        return value

    def merge_sources_from_digestion(
        self,
        target_digestion_id: str,
        source_digestion_id: str,
        actor_user_id: str,
        *,
        include_sources: bool = True,
        include_contributions: bool = True,
        include_evidence: bool = True,
        include_outputs: bool = True,
        build_after: bool = False,
    ) -> dict[str, Any]:
        """Merge one accessible Digestion into another without deleting either.

        The merge is conservative: it only de-duplicates direct file/original/checksum
        matches, copies foreign-owned source bytes into the target owner's intake
        folder when available, and preserves contribution/evidence/output snapshots
        as provenance-bearing target records.
        """
        source_digestion_id = self._clean_id(source_digestion_id)
        if not source_digestion_id:
            raise DigestionError("source_digestion_id is required.", status_code=400, reason="missing_source_digestion")
        target = self._require_digestion(target_digestion_id, actor_user_id, manage=True)
        source = self._require_digestion(source_digestion_id, actor_user_id, query=True)
        if target.id == source.id:
            raise DigestionError("Choose a different Digestion to merge.", status_code=400, reason="same_digestion")
        source_access = self._access_for(source, actor_user_id)
        if not source_access.get("can_read_sources"):
            raise DigestionError(
                "Merging Digestions requires source metadata access to the source Digestion.",
                status_code=403,
                reason="source_metadata_denied",
            )

        now = self._now()
        target_owner_id = str(target.owner_user_id or "")
        added = 0
        existing = 0
        skipped: list[dict[str, Any]] = []
        source_results: list[dict[str, Any]] = []
        file_id_map: dict[str, str] = {}
        intake_folder_id: Optional[str] = None

        with self.db.get_connection() as conn:
            source_rows = conn.execute(
                """
                SELECT *
                FROM digestion_sources
                WHERE digestion_id = ?
                ORDER BY file_name COLLATE NOCASE, file_id
                """,
                (source.id,),
            ).fetchall()

        if include_sources:
            for row in source_rows:
                file_id = self._clean_id(row["file_id"] if "file_id" in row.keys() else "")
                if not file_id:
                    skipped.append({"file_id": "", "reason": "missing_file_id"})
                    continue
                info = self.file_manager.get_file(file_id)
                if not info:
                    skipped.append({"file_id": file_id, "reason": "file_not_found"})
                    continue

                row_checksum = str(row["file_checksum"] or info.checksum or "")
                existing_source_id = self._existing_source_file_for_original(
                    target.id,
                    info.id,
                    checksum=row_checksum,
                )
                if existing_source_id:
                    existing += 1
                    file_id_map[file_id] = existing_source_id
                    source_results.append({
                        "input_file_id": file_id,
                        "file_id": existing_source_id,
                        "file_name": str(row["file_name"] or info.original_name or ""),
                        "status": "existing",
                        "reason": "direct_duplicate",
                    })
                    continue

                source_info = info
                copied_to_owner = False
                if str(info.uploaded_by or "") != target_owner_id:
                    source_path = self._resolved_source_file_path(info)
                    if not source_path or not source_path.exists():
                        skipped.append({
                            "file_id": file_id,
                            "file_name": str(row["file_name"] or info.original_name or ""),
                            "reason": "source_file_unavailable_for_owner_copy",
                            "source_owner_user_id": str(info.uploaded_by or ""),
                            "target_owner_user_id": target_owner_id,
                            "hint": "The source record is readable, but this node does not have the bytes needed to copy it into the target owner's Digestion Intake folder.",
                        })
                        continue
                    if intake_folder_id is None:
                        intake_folder_id = self._digestion_intake_folder_id(target)
                    if not intake_folder_id:
                        skipped.append({
                            "file_id": file_id,
                            "file_name": str(row["file_name"] or info.original_name or ""),
                            "reason": "intake_folder_unavailable",
                            "target_owner_user_id": target_owner_id,
                        })
                        continue
                    copied = self.file_manager.copy_file_to_user_vault(
                        info.id,
                        target_owner_id,
                        vault_folder_id=intake_folder_id,
                        duplicate_if_owned=True,
                    )
                    if not copied:
                        skipped.append({
                            "file_id": file_id,
                            "file_name": str(row["file_name"] or info.original_name or ""),
                            "reason": "copy_to_target_owner_failed",
                            "source_owner_user_id": str(info.uploaded_by or ""),
                            "target_owner_user_id": target_owner_id,
                        })
                        continue
                    source_info = copied
                    copied_to_owner = True

                metadata = self._json_loads(row["source_metadata_json"], {})
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.update({
                    "ingest_path": "digestion_merge",
                    "submitted_by": actor_user_id,
                    "source_owner_user_id": str(source_info.uploaded_by or ""),
                    "original_file_id": info.id,
                    "original_file_name": info.original_name,
                    "original_uploaded_by": str(info.uploaded_by or ""),
                    "original_checksum": info.checksum,
                    "merged_from_digestion_id": source.id,
                    "merged_from_digestion_name": source.name,
                    "merged_source_file_id": file_id,
                    "merged_source_checksum": row_checksum,
                })
                if copied_to_owner:
                    metadata.update({
                        "copied_to_owner_vault": True,
                        "owner_intake_folder_id": intake_folder_id or source_info.vault_folder_id,
                        "owner_intake_folder": self._digestion_intake_folder_name(target),
                    })

                with self.db.get_connection() as conn:
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
                            source_kind = COALESCE(digestion_sources.source_kind, excluded.source_kind),
                            source_label = COALESCE(digestion_sources.source_label, excluded.source_label),
                            source_uri = COALESCE(digestion_sources.source_uri, excluded.source_uri),
                            source_metadata_json = excluded.source_metadata_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            target.id,
                            source_info.id,
                            source_info.checksum,
                            source_info.original_name,
                            source_info.content_type,
                            now,
                            str(row["source_kind"] or "vault_file"),
                            str(row["source_label"] or source_info.original_name),
                            str(row["source_uri"] or ""),
                            json.dumps(metadata, sort_keys=True),
                        ),
                    )
                    conn.commit()
                added += 1
                file_id_map[file_id] = source_info.id
                source_results.append({
                    "input_file_id": file_id,
                    "file_id": source_info.id,
                    "file_name": source_info.original_name,
                    "content_type": source_info.content_type,
                    "status": "added",
                    "copied_to_target_owner_vault": copied_to_owner,
                    "source_owner_user_id": str(info.uploaded_by or ""),
                    "target_owner_user_id": target_owner_id,
                    "metadata": metadata,
                })

        contribution_rows: list[Any] = []
        contributions_copied = 0
        if include_contributions:
            with self.db.get_connection() as conn:
                contribution_rows = conn.execute(
                    """
                    SELECT *
                    FROM digestion_contributions
                    WHERE digestion_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (source.id,),
                ).fetchall()
                existing_keys: set[str] = set()
                for existing_row in conn.execute(
                    "SELECT id, metadata_json FROM digestion_contributions WHERE digestion_id = ?",
                    (target.id,),
                ).fetchall():
                    meta = self._json_loads(existing_row["metadata_json"], {})
                    if isinstance(meta, dict):
                        key = f"{meta.get('merged_from_digestion_id')}::{meta.get('merged_from_contribution_id')}"
                        if key != "::":
                            existing_keys.add(key)
                for row in contribution_rows:
                    source_contribution_id = str(row["id"] or "")
                    merge_key = f"{source.id}::{source_contribution_id}"
                    if merge_key in existing_keys:
                        continue
                    contributor_id = self._local_user_id_or_none(row["contributor_user_id"])
                    metadata = self._json_loads(row["metadata_json"], {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata.update({
                        "ingest_path": "digestion_merge",
                        "merged_from_digestion_id": source.id,
                        "merged_from_digestion_name": source.name,
                        "merged_from_contribution_id": source_contribution_id,
                        "original_contributor_user_id": str(row["contributor_user_id"] or ""),
                    })
                    conn.execute(
                        """
                        INSERT INTO digestion_contributions (
                            id, digestion_id, contributor_user_id, contribution_kind, title,
                            status, payload_json, summary, tags_json, confidence,
                            source_file_ids_json, material_file_ids_json, added_source_file_ids_json,
                            datapoint_count, skipped_json, result_json, metadata_json,
                            created_at, updated_at, reviewed_by, reviewed_at, review_note, accepted_at, rejected_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"Dc{secrets.token_hex(12)}",
                            target.id,
                            contributor_id,
                            str(row["contribution_kind"] or "agent_output"),
                            str(row["title"] or "Merged contribution")[:240],
                            str(row["status"] or CONTRIBUTION_STATUS_ACCEPTED),
                            self._json_dumps(self._merge_remap_value(self._json_loads(row["payload_json"], {}), file_id_map)),
                            str(row["summary"] or ""),
                            row["tags_json"] or "[]",
                            row["confidence"],
                            self._json_dumps(self._merge_remap_value(self._json_loads(row["source_file_ids_json"], []), file_id_map)),
                            self._json_dumps(self._merge_remap_value(self._json_loads(row["material_file_ids_json"], []), file_id_map)),
                            self._json_dumps(self._merge_remap_value(self._json_loads(row["added_source_file_ids_json"], []), file_id_map)),
                            int(row["datapoint_count"] or 0),
                            row["skipped_json"] or "[]",
                            self._json_dumps(self._merge_remap_value(self._json_loads(row["result_json"], {}), file_id_map)),
                            self._json_dumps(metadata),
                            row["created_at"] or now,
                            now,
                            self._local_user_id_or_none(row["reviewed_by"]),
                            row["reviewed_at"],
                            row["review_note"],
                            row["accepted_at"],
                            row["rejected_at"],
                        ),
                    )
                    existing_keys.add(merge_key)
                    contributions_copied += 1
                conn.commit()

        evidence_copied = 0
        evidence_reviews_copied = 0
        if include_evidence:
            with self.db.get_connection() as conn:
                evidence_rows = conn.execute(
                    """
                    SELECT *
                    FROM digestion_evidence_records
                    WHERE digestion_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (source.id,),
                ).fetchall()
                evidence_id_map: dict[str, str] = {}
                existing_by_key: dict[str, str] = {}
                for existing_row in conn.execute(
                    "SELECT id, metadata_json FROM digestion_evidence_records WHERE digestion_id = ?",
                    (target.id,),
                ).fetchall():
                    meta = self._json_loads(existing_row["metadata_json"], {})
                    if isinstance(meta, dict):
                        key = f"{meta.get('merged_from_digestion_id')}::{meta.get('merged_from_evidence_id')}"
                        if key != "::":
                            existing_by_key[key] = str(existing_row["id"] or "")
                for row in evidence_rows:
                    source_evidence_id = str(row["id"] or "")
                    merge_key = f"{source.id}::{source_evidence_id}"
                    evidence_id_map[source_evidence_id] = existing_by_key.get(merge_key) or f"Er{secrets.token_hex(12)}"
                inserted_source_evidence_ids: list[str] = []
                for row in evidence_rows:
                    source_evidence_id = str(row["id"] or "")
                    merge_key = f"{source.id}::{source_evidence_id}"
                    if merge_key in existing_by_key:
                        continue
                    metadata = self._json_loads(row["metadata_json"], {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata.update({
                        "ingest_path": "digestion_merge",
                        "merged_from_digestion_id": source.id,
                        "merged_from_digestion_name": source.name,
                        "merged_from_evidence_id": source_evidence_id,
                        "original_created_by_user_id": str(row["created_by_user_id"] or ""),
                    })
                    conn.execute(
                        """
                        INSERT INTO digestion_evidence_records (
                            id, digestion_id, created_by_user_id, record_kind, statement,
                            summary, scope, status, priority, confidence, tags_json,
                            evidence_refs_json, source_refs_json, related_ids_json,
                            metadata_json, superseded_by_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id_map[source_evidence_id],
                            target.id,
                            self._local_user_id_or_none(row["created_by_user_id"]),
                            str(row["record_kind"] or "finding"),
                            str(row["statement"] or ""),
                            row["summary"],
                            row["scope"],
                            str(row["status"] or EVIDENCE_STATUS_CANDIDATE),
                            str(row["priority"] or "normal"),
                            row["confidence"],
                            row["tags_json"] or "[]",
                            self._json_dumps(self._merge_remap_value(self._json_loads(row["evidence_refs_json"], []), file_id_map, evidence_id_map)),
                            self._json_dumps(self._merge_remap_value(self._json_loads(row["source_refs_json"], []), file_id_map, evidence_id_map)),
                            self._json_dumps(self._merge_remap_value(self._json_loads(row["related_ids_json"], []), file_id_map, evidence_id_map)),
                            self._json_dumps(metadata),
                            evidence_id_map.get(str(row["superseded_by_id"] or "")) or None,
                            row["created_at"] or now,
                            now,
                        ),
                    )
                    evidence_copied += 1
                    inserted_source_evidence_ids.append(source_evidence_id)
                if inserted_source_evidence_ids:
                    placeholders = ",".join("?" for _ in inserted_source_evidence_ids)
                    review_rows = conn.execute(
                        f"""
                        SELECT *
                        FROM digestion_evidence_reviews
                        WHERE digestion_id = ? AND evidence_id IN ({placeholders})
                        ORDER BY created_at ASC, id ASC
                        """,
                        (source.id, *inserted_source_evidence_ids),
                    ).fetchall()
                    for review in review_rows:
                        metadata = self._json_loads(review["metadata_json"], {})
                        if not isinstance(metadata, dict):
                            metadata = {}
                        metadata.update({
                            "ingest_path": "digestion_merge",
                            "merged_from_digestion_id": source.id,
                            "merged_from_review_id": str(review["id"] or ""),
                        })
                        conn.execute(
                            """
                            INSERT INTO digestion_evidence_reviews (
                                id, digestion_id, evidence_id, reviewer_user_id, action,
                                note, confidence, evidence_refs_json, metadata_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"Evr{secrets.token_hex(12)}",
                                target.id,
                                evidence_id_map.get(str(review["evidence_id"] or "")),
                                self._local_user_id_or_none(review["reviewer_user_id"]),
                                str(review["action"] or "support"),
                                review["note"],
                                review["confidence"],
                                self._json_dumps(self._merge_remap_value(self._json_loads(review["evidence_refs_json"], []), file_id_map, evidence_id_map)),
                                self._json_dumps(metadata),
                                review["created_at"] or now,
                            ),
                        )
                        evidence_reviews_copied += 1
                conn.commit()

        outputs_copied = 0
        if include_outputs:
            with self.db.get_connection() as conn:
                output_rows = conn.execute(
                    """
                    SELECT *
                    FROM digestion_outputs
                    WHERE digestion_id = ?
                    ORDER BY updated_at ASC, id ASC
                    """,
                    (source.id,),
                ).fetchall()
                for row in output_rows:
                    original_kind = str(row["output_kind"] or "output")
                    output_kind = self._merge_snapshot_output_kind(source.id, original_kind)
                    metadata = self._json_loads(row["metadata_json"], {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata.update({
                        "ingest_path": "digestion_merge",
                        "merged_from_digestion_id": source.id,
                        "merged_from_digestion_name": source.name,
                        "merged_from_output_id": str(row["id"] or ""),
                        "merged_from_output_kind": original_kind,
                        "source_revealing": bool(metadata.get("source_revealing")),
                    })
                    content = str(row["content"] or "")
                    if str(row["content_type"] or "").startswith("text/"):
                        content = (
                            f"<!-- Merged snapshot from Digestion {source.name} ({source.id}), "
                            f"output kind {original_kind}. -->\n\n{content}"
                        )
                    existing_output = conn.execute(
                        "SELECT id FROM digestion_outputs WHERE digestion_id = ? AND output_kind = ?",
                        (target.id, output_kind),
                    ).fetchone()
                    output_id = str(existing_output["id"]) if existing_output else f"Dgo{secrets.token_hex(12)}"
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
                            target.id,
                            output_kind,
                            f"Merged from {source.name}: {row['title'] or original_kind}"[:240],
                            str(row["content_type"] or "text/markdown"),
                            content,
                            self._json_dumps(metadata),
                            actor_user_id,
                            row["created_at"] or now,
                            now,
                        ),
                    )
                    outputs_copied += 1
                conn.commit()

        changed = added + contributions_copied + evidence_copied + outputs_copied
        if changed:
            with self.db.get_connection() as conn:
                if added:
                    conn.execute("UPDATE digestions SET status = ?, updated_at = ? WHERE id = ?", ("draft", now, target.id))
                else:
                    conn.execute("UPDATE digestions SET updated_at = ? WHERE id = ?", (now, target.id))
                conn.commit()
        build_result = None
        if build_after and added:
            build_result = self.build_digestion(target.id, actor_user_id, rebuild=False)
        return {
            "success": True,
            "digestion_id": target.id,
            "target_digestion_id": target.id,
            "source_digestion_id": source.id,
            "added": added,
            "sources_added": added,
            "sources_existing": existing,
            "skipped": skipped,
            "source_results": source_results,
            "file_id_map": file_id_map,
            "contributions_copied": contributions_copied,
            "evidence_copied": evidence_copied,
            "evidence_reviews_copied": evidence_reviews_copied,
            "outputs_copied": outputs_copied,
            "changed_records": changed,
            "build_result": build_result,
            "stats": self.stats(target.id),
        }

    def remove_sources(
        self,
        digestion_id: str,
        actor_user_id: str,
        source_file_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Detach sources from a Digestion while preserving Vault files.

        Removing a source invalidates derived index artifacts and reusable
        outputs because those artifacts may contain snippets, figure metadata, or
        structured datapoints from the removed file. The contribution ledger is
        intentionally preserved as audit history.
        """
        actor = self._clean_id(actor_user_id)
        digestion = self._require_digestion(digestion_id, actor, manage=True)
        file_ids = self._clean_id_list(source_file_ids)
        if not file_ids:
            raise DigestionError("source_file_ids is required", status_code=400, reason="missing_source_file_ids")

        placeholders = ",".join("?" for _ in file_ids)
        now = self._now()
        removed_sources: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT file_id, file_name, content_type, status, source_kind,
                       source_label, source_uri, source_metadata_json
                FROM digestion_sources
                WHERE digestion_id = ? AND file_id IN ({placeholders})
                """,
                (digestion.id, *file_ids),
            ).fetchall()
            found_ids = {str(row["file_id"] or "") for row in rows}
            for file_id in file_ids:
                if file_id not in found_ids:
                    skipped.append({"file_id": file_id, "reason": "source_not_found"})
            removed_sources = [self._source_row_to_dict(row) for row in rows]
            if rows:
                found_list = sorted(found_ids)
                found_placeholders = ",".join("?" for _ in found_list)
                self._delete_source_artifacts(conn, digestion.id, found_list)
                conn.execute(
                    f"DELETE FROM digestion_sources WHERE digestion_id = ? AND file_id IN ({found_placeholders})",
                    (digestion.id, *found_list),
                )
                conn.execute(
                    "UPDATE digestions SET status = ?, error = NULL, updated_at = ? WHERE id = ?",
                    ("draft", now, digestion.id),
                )
            conn.commit()

        return {
            "success": True,
            "digestion_id": digestion.id,
            "removed": len(removed_sources),
            "removed_sources": removed_sources,
            "skipped": skipped,
            "source_count_after": len(self._source_rows(digestion.id)),
            "stats": self.stats(digestion.id),
            "preserved": {
                "vault_files_are_preserved": True,
                "contribution_ledger_is_preserved": True,
                "derived_outputs_were_invalidated": bool(removed_sources),
            },
        }

    def replace_sources(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        remove_file_ids: Iterable[str],
        add_file_ids: Optional[Iterable[str]] = None,
        materials: Optional[Iterable[dict[str, Any]]] = None,
        build_after: bool = False,
    ) -> dict[str, Any]:
        """Replace one or more Digestion sources with Vault files/materials."""
        actor = self._clean_id(actor_user_id)
        digestion = self._require_digestion(digestion_id, actor, manage=True)
        to_remove = self._clean_id_list(remove_file_ids)
        if not to_remove:
            raise DigestionError("remove_file_ids is required", status_code=400, reason="missing_remove_file_ids")

        remove_result = self.remove_sources(digestion.id, actor, to_remove)
        add_result: dict[str, Any] = {}
        material_result: dict[str, Any] = {}
        add_ids = self._clean_id_list(add_file_ids)
        if add_ids:
            add_result = self.add_sources(digestion.id, actor, add_ids)
        material_items = list(materials or [])
        if material_items:
            material_result = self.add_materials(digestion.id, actor, material_items)

        result: dict[str, Any] = {
            "success": True,
            "digestion_id": digestion.id,
            "removed": remove_result.get("removed", 0),
            "removed_sources": remove_result.get("removed_sources", []),
            "remove_skipped": remove_result.get("skipped", []),
            "added": int(add_result.get("added") or 0) + int(material_result.get("added") or 0),
            "added_sources": [
                *(add_result.get("sources") or []),
                *(material_result.get("sources") or []),
            ],
            "add_skipped": [
                *(add_result.get("skipped") or []),
                *(material_result.get("skipped") or []),
            ],
            "source_count_after": len(self._source_rows(digestion.id)),
            "stats": self.stats(digestion.id),
        }
        if build_after:
            result["build_result"] = self.build_digestion(digestion.id, actor, rebuild=False)
            result["stats"] = self.stats(digestion.id)
        return result

    def update_source_metadata(
        self,
        digestion_id: str,
        actor_user_id: str,
        file_id: str,
        *,
        source_label: Optional[str] = None,
        source_uri: Optional[str] = None,
        source_metadata: Optional[dict[str, Any]] = None,
        merge_metadata: bool = True,
    ) -> dict[str, Any]:
        """Edit source-facing metadata without touching Vault bytes or chunks."""
        actor = self._clean_id(actor_user_id)
        digestion = self._require_digestion(digestion_id, actor, manage=True)
        source_id = self._clean_id(file_id)
        if not source_id:
            raise DigestionError("file_id is required", status_code=400, reason="missing_file_id")
        if source_metadata is not None and not isinstance(source_metadata, dict):
            raise DigestionError("source_metadata must be an object", status_code=400, reason="invalid_source_metadata")

        now = self._now()
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM digestion_sources WHERE digestion_id = ? AND file_id = ?",
                (digestion.id, source_id),
            ).fetchone()
            if not row:
                raise DigestionError("Source not found in this Digestion.", status_code=404, reason="source_not_found")
            current_metadata = self._json_loads(row["source_metadata_json"] if "source_metadata_json" in row.keys() else "{}", {})
            if not isinstance(current_metadata, dict):
                current_metadata = {}
            if source_metadata is not None:
                new_metadata = dict(current_metadata) if merge_metadata else {}
                new_metadata.update(source_metadata)
                current_metadata = new_metadata
            label_value = row["source_label"] if source_label is None else str(source_label or "").strip()[:260]
            uri_value = row["source_uri"] if source_uri is None else str(source_uri or "").strip()[:1200]
            conn.execute(
                """
                UPDATE digestion_sources
                SET source_label = ?, source_uri = ?, source_metadata_json = ?, updated_at = ?
                WHERE digestion_id = ? AND file_id = ?
                """,
                (
                    label_value,
                    uri_value,
                    json.dumps(current_metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    digestion.id,
                    source_id,
                ),
            )
            conn.execute("UPDATE digestions SET updated_at = ? WHERE id = ?", (now, digestion.id))
            updated = conn.execute(
                "SELECT * FROM digestion_sources WHERE digestion_id = ? AND file_id = ?",
                (digestion.id, source_id),
            ).fetchone()
            conn.commit()
        return {
            "success": True,
            "digestion_id": digestion.id,
            "source": self._source_row_to_dict(updated),
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
            "access": self._access_for(digestion, actor_user_id),
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
        if access.get("can_read_sources"):
            self._attach_contribution_preview_sources(digestion.id, contributions)
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

    def append_evidence_records(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        records: Optional[Iterable[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Append durable, reviewable evidence records to a managed Digestion.

        Evidence records are a generic truth-maintenance layer. Agents and
        humans can turn query results, figures, datapoints, uploaded files, or
        discussion outcomes into citable findings without granting raw Vault
        access to every later consumer.
        """
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        if isinstance(records, dict):
            raw_all = [records]
        else:
            raw_all = list(records or [])
        if not raw_all:
            raise DigestionError("Provide at least one evidence record to append.", status_code=400, reason="missing_evidence_records")
        raw_items = raw_all[:MAX_DIGESTION_EVIDENCE_RECORDS_PER_APPEND]
        skipped: list[dict[str, str]] = []
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(raw_items, start=1):
            if not isinstance(item, dict):
                skipped.append({"index": str(index), "reason": "record_not_object"})
                continue
            record = self._normalize_evidence_record(digestion, actor_user_id, item, index=index)
            if not record:
                skipped.append({"index": str(index), "reason": "empty_record"})
                continue
            normalized.append(record)
        extra_count = max(0, len(raw_all) - len(raw_items))
        if extra_count:
            skipped.append({"reason": "evidence_record_limit_reached", "count": str(extra_count)})
        if not normalized:
            raise DigestionError("No valid evidence records were provided.", status_code=400, reason="invalid_evidence_records")

        now = self._now()
        with self.db.get_connection() as conn:
            for record in normalized:
                record_id = record["id"]
                superseded_by_id = self._clean_id(record.get("superseded_by_id")) or None
                insert_params = (
                    record_id,
                    digestion.id,
                    actor_user_id,
                    record["record_kind"],
                    record["statement"],
                    record["summary"],
                    record["scope"],
                    record["status"],
                    record["priority"],
                    record["confidence"],
                    self._json_dumps(record["tags"]),
                    self._json_dumps(record["evidence_refs"]),
                    self._json_dumps(record["source_refs"]),
                    self._json_dumps(record["related_ids"]),
                    self._json_dumps(record["metadata"]),
                    superseded_by_id,
                    now,
                    now,
                )
                existing = conn.execute(
                    "SELECT id FROM digestion_evidence_records WHERE id = ? LIMIT 1",
                    (record_id,),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE digestion_evidence_records
                        SET record_kind = ?,
                            statement = ?,
                            summary = ?,
                            scope = ?,
                            status = ?,
                            priority = ?,
                            confidence = ?,
                            tags_json = ?,
                            evidence_refs_json = ?,
                            source_refs_json = ?,
                            related_ids_json = ?,
                            metadata_json = ?,
                            superseded_by_id = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            record["record_kind"],
                            record["statement"],
                            record["summary"],
                            record["scope"],
                            record["status"],
                            record["priority"],
                            record["confidence"],
                            self._json_dumps(record["tags"]),
                            self._json_dumps(record["evidence_refs"]),
                            self._json_dumps(record["source_refs"]),
                            self._json_dumps(record["related_ids"]),
                            self._json_dumps(record["metadata"]),
                            superseded_by_id,
                            now,
                            record_id,
                        ),
                    )
                    continue
                conn.execute(
                    """
                    INSERT INTO digestion_evidence_records (
                        id, digestion_id, created_by_user_id, record_kind, statement,
                        summary, scope, status, priority, confidence, tags_json,
                        evidence_refs_json, source_refs_json, related_ids_json,
                        metadata_json, superseded_by_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    insert_params,
                )
            conn.execute("UPDATE digestions SET updated_at = ? WHERE id = ?", (now, digestion.id))
            conn.commit()
        records_out = self._evidence_records_by_ids(
            digestion.id,
            [record["id"] for record in normalized],
            include_reviews=True,
        )
        return {
            "success": True,
            "digestion_id": digestion.id,
            "schema_version": DIGESTION_EVIDENCE_SCHEMA_VERSION,
            "added": len(normalized),
            "records": records_out,
            "skipped": skipped,
            "access": self._access_for(digestion, actor_user_id),
            "stats": self.stats(digestion.id),
        }

    def list_evidence_records(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        status: str = "",
        query: str = "",
        tag: str = "",
        limit: int = 100,
        include_reviews: bool = True,
    ) -> dict[str, Any]:
        """List reviewable evidence records for an accessible Digestion."""
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        max_rows = max(1, min(int(limit or 100), 250))
        requested_status = self._normalize_evidence_status(status, allow_empty=True)
        query_text = str(query or "").strip()
        tag_filter = str(tag or "").strip().lower()
        params: list[Any] = [digestion.id]
        status_clause = ""
        if requested_status:
            status_clause = "AND e.status = ?"
            params.append(requested_status)
        params.append(max_rows * 4 if (query_text or tag_filter) else max_rows)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    e.*,
                    u.username AS created_by_username,
                    u.avatar_file_id AS created_by_avatar_file_id
                FROM digestion_evidence_records e
                LEFT JOIN users u ON u.id = e.created_by_user_id
                WHERE e.digestion_id = ?
                {status_clause}
                ORDER BY
                    CASE e.priority
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'normal' THEN 2
                        ELSE 3
                    END,
                    CASE e.status
                        WHEN 'contested' THEN 0
                        WHEN 'needs_source' THEN 1
                        WHEN 'candidate' THEN 2
                        WHEN 'stable' THEN 3
                        WHEN 'stale' THEN 4
                        ELSE 5
                    END,
                    e.updated_at DESC,
                    e.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        records = [self._evidence_row_to_dict(row, include_reviews=False) for row in rows]
        if query_text:
            records = [
                item for item in records
                if self._evidence_record_matches_query(item, query_text)
            ]
        if tag_filter:
            records = [
                item for item in records
                if tag_filter in {str(value or "").strip().lower() for value in item.get("tags") or []}
            ]
        records = records[:max_rows]
        if include_reviews and records:
            self._attach_evidence_reviews(digestion.id, records)
        return {
            "success": True,
            "digestion_id": digestion.id,
            "schema_version": DIGESTION_EVIDENCE_SCHEMA_VERSION,
            "evidence": records,
            "records": records,
            "count": len(records),
            "status_counts": self._evidence_status_counts(digestion.id),
            "query": query_text,
            "tag": tag_filter,
            "access": self._access_for(digestion, actor_user_id),
            "stats": self.stats(digestion.id),
        }

    def search_evidence_records(
        self,
        digestion_id: str,
        actor_user_id: str,
        query: str,
        *,
        status: str = "",
        tag: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Search evidence records by statement, summary, tags, refs, or metadata."""
        query_text = str(query or "").strip()
        if not query_text and not str(tag or "").strip() and not str(status or "").strip():
            raise DigestionError("query, status, or tag is required", status_code=400, reason="missing_evidence_search")
        result = self.list_evidence_records(
            digestion_id,
            actor_user_id,
            status=status,
            query=query_text,
            tag=tag,
            limit=limit,
            include_reviews=True,
        )
        result["mode"] = "evidence_records"
        return result

    def review_evidence_record(
        self,
        digestion_id: str,
        evidence_id: str,
        actor_user_id: str,
        *,
        action: str,
        note: str = "",
        confidence: Optional[float] = None,
        evidence_refs: Optional[Iterable[Any]] = None,
        status: str = "",
        superseded_by_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Append a review event and update the evidence record state."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        evidence_id = self._clean_id(evidence_id)
        if not evidence_id:
            raise DigestionError("evidence_id is required", status_code=400, reason="missing_evidence_id")
        action_clean = self._normalize_evidence_review_action(action)
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, status FROM digestion_evidence_records WHERE digestion_id = ? AND id = ?",
                (digestion.id, evidence_id),
            ).fetchone()
        if not row:
            raise DigestionError("Evidence record not found.", status_code=404, reason="evidence_not_found")
        explicit_status = self._normalize_evidence_status(status, allow_empty=True)
        next_status = explicit_status or str(row["status"] or EVIDENCE_STATUS_CANDIDATE)
        if not explicit_status:
            if action_clean == "challenge":
                next_status = EVIDENCE_STATUS_CONTESTED
            elif action_clean == "request_source":
                next_status = EVIDENCE_STATUS_NEEDS_SOURCE
            elif action_clean == "mark_stale":
                next_status = EVIDENCE_STATUS_STALE
            elif action_clean == "supersede":
                next_status = EVIDENCE_STATUS_SUPERSEDED
            elif action_clean == "confirm":
                next_status = EVIDENCE_STATUS_STABLE
        evidence_refs_norm = self._normalize_evidence_refs(evidence_refs or [])
        review_id = f"Evr{secrets.token_hex(12)}"
        now = self._now()
        superseded_value = self._clean_id(superseded_by_id) or None
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO digestion_evidence_reviews (
                    id, digestion_id, evidence_id, reviewer_user_id, action,
                    note, confidence, evidence_refs_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    digestion.id,
                    evidence_id,
                    actor_user_id,
                    action_clean,
                    str(note or "").strip()[:4000],
                    self._normalize_confidence(confidence),
                    self._json_dumps(evidence_refs_norm),
                    self._json_dumps(metadata if isinstance(metadata, dict) else {}),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE digestion_evidence_records
                SET status = ?, superseded_by_id = COALESCE(?, superseded_by_id), updated_at = ?
                WHERE digestion_id = ? AND id = ?
                """,
                (
                    next_status,
                    superseded_value,
                    now,
                    digestion.id,
                    evidence_id,
                ),
            )
            conn.execute("UPDATE digestions SET updated_at = ? WHERE id = ?", (now, digestion.id))
            conn.commit()
        record = self._evidence_records_by_ids(digestion.id, [evidence_id], include_reviews=True)
        review = record[0]["reviews"][-1] if record and record[0].get("reviews") else {}
        return {
            "success": True,
            "digestion_id": digestion.id,
            "schema_version": DIGESTION_EVIDENCE_SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "action": action_clean,
            "status": next_status,
            "review": review,
            "record": record[0] if record else {},
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

    def rename_digestion(self, digestion_id: str, actor_user_id: str, name: str) -> dict[str, Any]:
        """Rename a managed Digestion without changing its corpus, outputs, or ACL."""
        actor = self._clean_id(actor_user_id)
        digestion = self._require_digestion(digestion_id, actor, manage=True)
        name_clean = " ".join(str(name or "").strip().split())
        if not name_clean:
            raise DigestionError("Digestion name is required", status_code=400, reason="missing_name")
        if len(name_clean) > 180:
            raise DigestionError("Digestion name must be 180 characters or fewer.", status_code=400, reason="name_too_long")
        old_name = str(digestion.name or "")
        now = self._now()
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE digestions SET name = ?, updated_at = ? WHERE id = ?",
                (name_clean, now, digestion.id),
            )
            conn.commit()
        item = self.get_digestion(digestion.id, user_id=actor) or {}
        return {
            "success": True,
            "digestion_id": digestion.id,
            "old_name": old_name,
            "name": name_clean,
            "digestion": item,
        }

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

    def cancel_operation(self, digestion_id: str, actor_user_id: str, operation: str) -> dict[str, Any]:
        """Request cancellation/reset for a long-running Digestion operation."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        operation_clean = self._normalize_operation_name(operation)
        with self._progress_lock:
            self._operation_cancel_requests.add((digestion.id, operation_clean))
        current = self._progress_snapshot(digestion.id).get(operation_clean, self._idle_progress(operation_clean))
        details = dict(current.get("details") or {})
        details.update({
            "cancel_requested": True,
            "cancelled_by": actor_user_id,
            "cancelled_at": self._now(),
        })
        payload = self._set_operation_progress(
            digestion.id,
            operation_clean,
            status="cancelled",
            phase="cancel_requested",
            percent=int(current.get("percent") or 0),
            processed=int(current.get("processed") or 0),
            total=int(current.get("total") or 0),
            current_label=str(current.get("current_label") or ""),
            message=(
                "Operation cancellation/reset was requested. "
                "If a provider call is already in flight, it will stop before the next batch."
            ),
            details=details,
            actor_user_id=actor_user_id,
        )
        return {
            "success": True,
            "digestion_id": digestion.id,
            "operation": operation_clean,
            "progress": payload,
            "operations": self._progress_snapshot(digestion.id),
            "stats": self.stats(digestion.id),
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
        return [self._source_row_to_dict(row) for row in rows]

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
        self._organize_generated_figure_assets(digestion)
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

    def enrich_figures_with_vision(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        max_figures: Optional[int] = None,
        overwrite: bool = False,
        lens: str = "",
    ) -> dict[str, Any]:
        """Run an opt-in, bounded vision model pass over extracted PDF figures."""
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        access = self._access_for(digestion, actor_user_id)
        self._set_operation_progress(
            digestion.id,
            "figure_vision",
            status="running",
            phase="preflight",
            percent=2,
            processed=0,
            total=0,
            message="Checking source access and Digestion AI figure-vision settings.",
            details={},
            actor_user_id=actor_user_id,
        )
        if not access.get("can_read_sources"):
            self._set_operation_progress(
                digestion.id,
                "figure_vision",
                status="failed",
                phase="source_access_denied",
                percent=0,
                message="Source-read access is required for figure vision enrichment.",
                details={"reason": "source_metadata_denied"},
                actor_user_id=actor_user_id,
            )
            raise DigestionError(
                "Figure vision enrichment sends extracted source images and captions to the configured LLM provider. Source metadata access is required.",
                status_code=403,
                reason="figure_vision_source_metadata_denied",
            )
        try:
            llm_context = self._resolve_figure_vision_llm_context(actor_user_id)
        except DigestionError as exc:
            self._set_operation_progress(
                digestion.id,
                "figure_vision",
                status="failed",
                phase="llm_unavailable",
                percent=0,
                message=str(exc),
                details={"reason": getattr(exc, "reason", "figure_vision_llm_unavailable")},
                actor_user_id=actor_user_id,
            )
            raise

        parameters = llm_context.get("parameters") if isinstance(llm_context.get("parameters"), dict) else {}
        figure_limit = self._bounded_int(
            max_figures,
            int(parameters.get("vision_max_figures") or DEFAULT_FIGURE_VISION_LIMIT),
            1,
            MAX_FIGURE_VISION_LIMIT,
        )
        max_image_bytes = self._bounded_int(
            parameters.get("vision_max_image_bytes"),
            DEFAULT_FIGURE_VISION_IMAGE_BYTES,
            100_000,
            MAX_FIGURE_VISION_IMAGE_BYTES,
        )
        max_output_tokens = self._bounded_int(
            parameters.get("vision_max_output_tokens"),
            DEFAULT_FIGURE_VISION_OUTPUT_TOKENS,
            300,
            MAX_FIGURE_VISION_OUTPUT_TOKENS,
        )
        effective_lens = str(lens or "").strip() or str(llm_context.get("default_lens") or "").strip()
        if len(effective_lens) > 800:
            effective_lens = effective_lens[:800]
        rows = self._figure_vision_candidate_rows(
            digestion.id,
            limit=figure_limit,
            overwrite=overwrite,
        )
        if not rows:
            stats = self.stats(digestion.id)
            figure_count = int(stats.get("figures") or 0)
            eligible_count = int(stats.get("figure_vision_eligible_count") or 0)
            analyzed_count = int(stats.get("figure_vision_analyzed_count") or 0)
            pending_count = int(stats.get("figure_vision_pending_count") or 0)
            message = (
                "No extracted figure images need vision enrichment."
                if figure_count
                else "No extracted figure images are available for vision enrichment."
            )
            self._set_operation_progress(
                digestion.id,
                "figure_vision",
                status="completed",
                phase="no_candidates",
                percent=100,
                processed=0,
                total=0,
                message=message,
                details={
                    "figure_count": figure_count,
                    "eligible_count": eligible_count,
                    "pending_count": pending_count,
                    "analyzed_count": 0,
                    "previously_analyzed_count": analyzed_count,
                    "skipped_count": 0,
                    "max_figures": figure_limit,
                    "max_image_bytes": max_image_bytes,
                    "provider": llm_context.get("provider") or "",
                    "model": llm_context.get("model") or "",
                },
                actor_user_id=actor_user_id,
            )
            return {
                "success": True,
                "digestion_id": digestion.id,
                "analyzed_count": 0,
                "skipped_count": 0,
                "eligible_count": eligible_count,
                "pending_count": pending_count,
                "previously_analyzed_count": analyzed_count,
                "errors": [],
                "figures": [],
                "progress": self._progress_snapshot(digestion.id).get("figure_vision", {}),
                "stats": stats,
                "reason": "no_candidates",
            }

        total = len(rows)
        self._set_operation_progress(
            digestion.id,
            "figure_vision",
            status="running",
            phase="starting",
            percent=5,
            processed=0,
            total=total,
            message=f"Preparing to analyze {total} extracted figure{'' if total == 1 else 's'} with an image-capable model.",
            details={
                "figure_count": total,
                "max_figures": figure_limit,
                "max_image_bytes": max_image_bytes,
                "max_output_tokens": max_output_tokens,
                "provider": llm_context.get("provider") or "",
                "model": llm_context.get("model") or "",
                "credential_source": llm_context.get("credential_source") or "",
                "overwrite": bool(overwrite),
            },
            actor_user_id=actor_user_id,
        )

        analyzed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        system_prompt = self._figure_vision_system_prompt()
        try:
            for index, row in enumerate(rows, start=1):
                self._raise_if_operation_cancelled(digestion.id, "figure_vision")
                figure = self._figure_row_to_dict(row)
                label = (
                    f"{figure.get('source_file_name') or 'PDF'} "
                    f"{figure.get('page_label') or ''} fig {figure.get('figure_index') or index}"
                ).strip()
                self._set_operation_progress(
                    digestion.id,
                    "figure_vision",
                    status="running",
                    phase="analyzing",
                    percent=min(96, 8 + int(((index - 1) / max(1, total)) * 86)),
                    processed=index - 1,
                    total=total,
                    current_label=label,
                    message=f"Analyzing figure {index} of {total}.",
                    details={
                        "figure_count": total,
                        "analyzed_count": len(analyzed),
                        "skipped_count": len(skipped),
                        "error_count": len(errors),
                        "max_image_bytes": max_image_bytes,
                        "provider": llm_context.get("provider") or "",
                        "model": llm_context.get("model") or "",
                    },
                    actor_user_id=actor_user_id,
                )
                image_file_id = str(figure.get("image_file_id") or "")
                if not image_file_id:
                    skipped.append({
                        "figure_id": figure.get("id") or "",
                        "source_file_id": figure.get("source_file_id") or "",
                        "source_file_name": figure.get("source_file_name") or "",
                        "page_label": figure.get("page_label") or "",
                        "figure_index": figure.get("figure_index") or index,
                        "reason": "missing_image_file_id",
                        "message": "This extracted figure does not have a stored image file ID.",
                    })
                    continue
                file_data = self.file_manager.get_file_data(image_file_id)
                if not file_data:
                    skipped.append({
                        "figure_id": figure.get("id") or "",
                        "source_file_id": figure.get("source_file_id") or "",
                        "source_file_name": figure.get("source_file_name") or "",
                        "page_label": figure.get("page_label") or "",
                        "figure_index": figure.get("figure_index") or index,
                        "image_file_id": image_file_id,
                        "reason": "image_file_unavailable",
                        "message": "The extracted figure image could not be read from the local Vault.",
                    })
                    continue
                image_bytes, image_info = file_data
                image_size = len(image_bytes)
                if image_size > max_image_bytes:
                    skipped.append({
                        "figure_id": figure.get("id") or "",
                        "source_file_id": figure.get("source_file_id") or "",
                        "source_file_name": figure.get("source_file_name") or "",
                        "page_label": figure.get("page_label") or "",
                        "figure_index": figure.get("figure_index") or index,
                        "image_file_id": image_file_id,
                        "image_file_name": figure.get("vault_image_name") or figure.get("image_name") or "",
                        "reason": "image_too_large",
                        "message": "The extracted figure image exceeds the configured vision byte cap.",
                        "size": image_size,
                        "max_image_bytes": max_image_bytes,
                    })
                    continue
                image_content_type = str(getattr(image_info, "content_type", "") or figure.get("content_type") or "image/png")
                prompt = self._figure_vision_user_prompt(digestion, figure, lens=effective_lens)
                try:
                    raw = self._call_figure_vision_llm(
                        llm_context,
                        system_prompt=system_prompt,
                        prompt=prompt,
                        image_bytes=image_bytes,
                        image_content_type=image_content_type,
                        max_output_tokens=max_output_tokens,
                    )
                    parsed = self._parse_figure_vision_json(raw)
                    updated = self._update_figure_vision_row(
                        row,
                        parsed,
                        llm_context=llm_context,
                        lens=effective_lens,
                        max_image_bytes=max_image_bytes,
                        image_byte_size=image_size,
                    )
                    analyzed.append(updated)
                except DigestionError as exc:
                    errors.append({
                        "figure_id": figure.get("id") or "",
                        "source_file_id": figure.get("source_file_id") or "",
                        "source_file_name": figure.get("source_file_name") or "",
                        "page_label": figure.get("page_label") or "",
                        "figure_index": figure.get("figure_index") or index,
                        "image_file_id": image_file_id,
                        "image_file_name": figure.get("vault_image_name") or figure.get("image_name") or "",
                        "reason": getattr(exc, "reason", "figure_vision_failed"),
                        "error": str(exc)[:500],
                        "message": str(exc)[:500],
                    })
                self._set_operation_progress(
                    digestion.id,
                    "figure_vision",
                    status="running",
                    phase="analyzed",
                    percent=min(98, 8 + int((index / max(1, total)) * 86)),
                    processed=index,
                    total=total,
                    current_label=label,
                    message=f"Analyzed {len(analyzed)} figure{'' if len(analyzed) == 1 else 's'} so far.",
                    details={
                        "figure_count": total,
                        "analyzed_count": len(analyzed),
                        "skipped_count": len(skipped),
                        "error_count": len(errors),
                        "errors": errors[-3:],
                        "skipped": skipped[-3:],
                        "recent_errors": errors[-3:],
                        "recent_skipped": skipped[-3:],
                        "max_image_bytes": max_image_bytes,
                        "provider": llm_context.get("provider") or "",
                        "model": llm_context.get("model") or "",
                    },
                    actor_user_id=actor_user_id,
                )
        except DigestionError as exc:
            if getattr(exc, "reason", "") == "operation_cancelled":
                self._set_operation_progress(
                    digestion.id,
                    "figure_vision",
                    status="cancelled",
                    phase="cancelled",
                    percent=self._progress_snapshot(digestion.id).get("figure_vision", {}).get("percent", 0),
                    processed=len(analyzed),
                    total=total,
                    message="Figure vision enrichment was cancelled before completion.",
                    details={"reason": "operation_cancelled", "cancel_requested": True},
                    actor_user_id=actor_user_id,
                )
                raise
            self._set_operation_progress(
                digestion.id,
                "figure_vision",
                status="failed",
                phase="failed",
                percent=0,
                processed=len(analyzed),
                total=total,
                message=str(exc)[:1000],
                details={"reason": getattr(exc, "reason", "figure_vision_failed")},
                actor_user_id=actor_user_id,
            )
            raise

        try:
            self._upsert_output(digestion, actor_user_id, *self._build_pdf_figures_output(digestion))
        except Exception as exc:
            logger.debug("Could not refresh PDF figure output after figure vision enrichment: %s", exc)
        all_failed = total > 0 and not analyzed and bool(errors or skipped)
        issue_bits: list[str] = []
        if skipped:
            issue_bits.append(f"{len(skipped)} skipped")
        if errors:
            issue_bits.append(f"{len(errors)} errors")
        first_issue = (errors or skipped or [{}])[0] if (errors or skipped) else {}
        first_reason = str(first_issue.get("reason") or "").replace("_", " ")
        detail_suffix = f" First issue: {first_reason}." if first_reason and all_failed else ""
        issue_suffix = f"; {', '.join(issue_bits)}" if issue_bits else ""
        self._set_operation_progress(
            digestion.id,
            "figure_vision",
            status="completed",
            phase="completed_with_issues" if issue_bits else "completed",
            percent=100,
            processed=total,
            total=total,
            message=(
                f"Analyzed {len(analyzed)} figure{'' if len(analyzed) == 1 else 's'}"
                f"{issue_suffix}."
                f"{detail_suffix}"
            ),
            details={
                "figure_count": total,
                "eligible_count": total,
                "pending_count": total,
                "analyzed_count": len(analyzed),
                "skipped_count": len(skipped),
                "error_count": len(errors),
                "all_failed": all_failed,
                "errors": errors[:8],
                "skipped": skipped[:8],
                "first_error": errors[0] if errors else None,
                "first_skipped": skipped[0] if skipped else None,
                "max_figures": figure_limit,
                "max_image_bytes": max_image_bytes,
                "provider": llm_context.get("provider") or "",
                "model": llm_context.get("model") or "",
                "credential_source": llm_context.get("credential_source") or "",
            },
            actor_user_id=actor_user_id,
        )
        return {
            "success": True,
            "digestion_id": digestion.id,
            "schema_version": PDF_FIGURE_VISION_SCHEMA_VERSION,
            "analyzed_count": len(analyzed),
            "skipped_count": len(skipped),
            "error_count": len(errors),
            "eligible_count": total,
            "pending_count": total,
            "all_failed": all_failed,
            "skipped": skipped,
            "errors": errors,
            "figures": analyzed,
            "progress": self._progress_snapshot(digestion.id).get("figure_vision", {}),
            "stats": self.stats(digestion.id),
        }

    def list_visual_evidence(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        limit: int = 160,
        evidence_kind: str = "",
    ) -> dict[str, Any]:
        """List caption/table/chart/diagram evidence captured from source-readable PDFs."""
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        access = self._access_for(digestion, actor_user_id)
        if not access.get("can_read_sources"):
            raise DigestionError(
                "Visual evidence includes source-derived captions, page labels, and optional figure images. Source metadata access is required.",
                status_code=403,
                reason="source_metadata_denied",
            )
        self._organize_generated_figure_assets(digestion)
        evidence_limit = max(1, min(int(limit or 160), 320))
        kind = str(evidence_kind or "").strip().lower()
        params: list[Any] = [digestion.id]
        kind_clause = ""
        if kind:
            kind_clause = "AND v.evidence_kind = ?"
            params.append(kind)
        params.append(evidence_limit)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    v.*,
                    s.file_name AS source_file_name,
                    s.content_type AS source_content_type,
                    img.original_name AS vault_image_name,
                    img.size AS vault_image_size
                FROM digestion_visual_evidence v
                LEFT JOIN digestion_sources s
                  ON s.digestion_id = v.digestion_id
                 AND s.file_id = v.source_file_id
                LEFT JOIN files img ON img.id = v.image_file_id
                WHERE v.digestion_id = ?
                {kind_clause}
                ORDER BY COALESCE(s.file_name, v.source_file_id) COLLATE NOCASE,
                         v.source_file_id, v.page_number, v.evidence_index
                LIMIT ?
                """,
                params,
            ).fetchall()
        evidence = [self._visual_evidence_row_to_dict(row) for row in rows]
        return {
            "success": True,
            "digestion_id": digestion.id,
            "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
            "count": len(evidence),
            "visual_evidence": evidence,
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
        grantee, grantee_row, row_keys = self._require_local_digestion_user(grantee_user_id)
        if grantee == digestion.owner_user_id:
            raise DigestionError(
                "The Digestion owner already has full access and cannot be added as a separate grantee.",
                status_code=400,
                reason="owner_not_grantable",
            )
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

    def transfer_ownership(
        self,
        digestion_id: str,
        actor_user_id: str,
        new_owner_user_id: str,
        *,
        keep_previous_owner_access: bool = True,
        copy_sources: bool = True,
        strict_source_copy: bool = True,
    ) -> dict[str, Any]:
        """Transfer a Digestion to another local user or agent without orphaning the corpus.

        The actor must be the current owner. When source copying is enabled, all
        current source files are copied into the new owner's Digestion Intake
        folder and the Digestion source/chunk/figure references are remapped so
        the new owner can rebuild later. The previous owner is retained as a
        manager by default so an agent can continue iterating after a handoff.
        """
        actor = self._clean_id(actor_user_id)
        digestion = self._require_digestion(digestion_id, actor, manage=True)
        if actor != str(digestion.owner_user_id):
            raise DigestionError(
                "Only the current Digestion owner can transfer ownership.",
                status_code=403,
                reason="owner_required",
            )
        new_owner, new_owner_row, row_keys = self._require_local_digestion_user(new_owner_user_id)
        if new_owner == str(digestion.owner_user_id):
            raise DigestionError(
                "Choose a different user or agent to receive this Digestion.",
                status_code=400,
                reason="same_owner",
            )

        now = self._now()
        source_rows = self._source_rows(digestion.id)
        remapped_sources: list[dict[str, Any]] = []
        retained_sources: list[dict[str, Any]] = []
        skipped_sources: list[dict[str, Any]] = []
        source_map: dict[str, FileInfo] = {}
        target_folder_id: Optional[str] = None
        if copy_sources and source_rows:
            root_id = self._find_or_create_vault_folder(new_owner, "Digestion Intake")
            if root_id:
                target_folder_id = self._find_or_create_vault_folder(
                    new_owner,
                    self._digestion_intake_folder_name(digestion),
                    root_id,
                )
            if not target_folder_id:
                raise DigestionError(
                    "Could not create the recipient's Digestion Intake folder.",
                    status_code=500,
                    reason="recipient_intake_unavailable",
                )

            for row in source_rows:
                old_file_id = str(row["file_id"] or "").strip()
                info = self.file_manager.get_file(old_file_id)
                if not info:
                    skipped_sources.append({
                        "file_id": old_file_id,
                        "file_name": str(self._row_get(row, "file_name", "") or old_file_id),
                        "source_kind": str(self._row_get(row, "source_kind", "vault_file") or "vault_file"),
                        "reason": "source_file_not_found",
                    })
                    continue
                source_owner = str(info.uploaded_by or "")
                if source_owner == new_owner:
                    source_map[old_file_id] = info
                    retained_sources.append({
                        "from_file_id": old_file_id,
                        "to_file_id": info.id,
                        "file_name": info.original_name,
                        "content_type": info.content_type,
                        "source_kind": str(self._row_get(row, "source_kind", "vault_file") or "vault_file"),
                        "source_label": str(self._row_get(row, "source_label", "") or info.original_name),
                        "reason": "already_owned_by_new_owner",
                    })
                    continue
                if source_owner != actor:
                    skipped_sources.append({
                        "file_id": old_file_id,
                        "file_name": info.original_name,
                        "content_type": info.content_type,
                        "source_kind": str(self._row_get(row, "source_kind", "vault_file") or "vault_file"),
                        "reason": "source_not_owned_by_current_owner",
                        "source_owner_user_id": source_owner,
                    })
                    continue
                copied = self.file_manager.copy_file_to_user_vault(
                    info.id,
                    new_owner,
                    vault_folder_id=target_folder_id,
                    duplicate_if_owned=True,
                )
                if not copied:
                    skipped_sources.append({
                        "file_id": old_file_id,
                        "file_name": info.original_name,
                        "content_type": info.content_type,
                        "source_kind": str(self._row_get(row, "source_kind", "vault_file") or "vault_file"),
                        "reason": "copy_to_new_owner_failed",
                    })
                    continue
                source_map[old_file_id] = copied
                remapped_sources.append({
                    "from_file_id": old_file_id,
                    "from_file_name": info.original_name,
                    "to_file_id": copied.id,
                    "file_name": copied.original_name,
                    "content_type": copied.content_type,
                    "source_kind": str(self._row_get(row, "source_kind", "vault_file") or "vault_file"),
                    "source_label": str(self._row_get(row, "source_label", "") or info.original_name),
                    "reason": "copied_to_new_owner_vault",
                })

            if skipped_sources and strict_source_copy:
                raise DigestionError(
                    "Could not transfer all Digestion source files to the new owner.",
                    status_code=409,
                    reason="source_transfer_failed",
                )

        with self.db.get_connection() as conn:
            for row in source_rows:
                old_file_id = str(row["file_id"] or "").strip()
                new_info = source_map.get(old_file_id)
                if not new_info or old_file_id == new_info.id:
                    continue
                try:
                    metadata = json.loads(row["source_metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                transfer_meta = dict(metadata)
                transfer_meta.update({
                    "ownership_transfer": {
                        "from_owner_user_id": str(digestion.owner_user_id),
                        "to_owner_user_id": new_owner,
                        "transferred_by": actor,
                        "transferred_at": now,
                        "previous_source_file_id": old_file_id,
                    },
                    "original_file_id": metadata.get("original_file_id") or old_file_id,
                    "copied_to_owner_vault": True,
                    "owner_intake_folder_id": target_folder_id,
                    "owner_intake_folder": self._digestion_intake_folder_name(digestion),
                })
                conn.execute(
                    """
                    UPDATE digestion_sources
                    SET file_id = ?, file_checksum = ?, file_name = ?, content_type = ?,
                        source_metadata_json = ?, updated_at = ?
                    WHERE digestion_id = ? AND file_id = ?
                    """,
                    (
                        new_info.id,
                        new_info.checksum,
                        new_info.original_name,
                        new_info.content_type,
                        json.dumps(transfer_meta, sort_keys=True),
                        now,
                        digestion.id,
                        old_file_id,
                    ),
                )
                conn.execute(
                    "UPDATE digestion_chunks SET file_id = ? WHERE digestion_id = ? AND file_id = ?",
                    (new_info.id, digestion.id, old_file_id),
                )
                conn.execute(
                    "UPDATE digestion_pdf_figures SET source_file_id = ? WHERE digestion_id = ? AND source_file_id = ?",
                    (new_info.id, digestion.id, old_file_id),
                )
            if source_map:
                self._rewrite_output_source_ids(conn, digestion.id, {old: info.id for old, info in source_map.items() if old != info.id}, now)
            conn.execute(
                "UPDATE digestions SET owner_user_id = ?, updated_at = ? WHERE id = ?",
                (new_owner, now, digestion.id),
            )
            conn.execute(
                "DELETE FROM digestion_acl WHERE digestion_id = ? AND grantee_user_id = ?",
                (digestion.id, new_owner),
            )
            if keep_previous_owner_access:
                conn.execute(
                    """
                    INSERT INTO digestion_acl (
                        digestion_id, grantee_user_id, grantee_kind, can_query,
                        can_manage, can_read_sources, created_at
                    ) VALUES (?, ?, 'user', 1, 1, 1, ?)
                    ON CONFLICT(digestion_id, grantee_user_id) DO UPDATE SET
                        can_query = 1,
                        can_manage = 1,
                        can_read_sources = 1
                    """,
                    (digestion.id, actor, now),
                )
            else:
                conn.execute(
                    "DELETE FROM digestion_acl WHERE digestion_id = ? AND grantee_user_id = ?",
                    (digestion.id, actor),
                )
            conn.commit()

        username = new_owner_row["username"] if "username" in row_keys else new_owner
        display_name = new_owner_row["display_name"] if "display_name" in row_keys else username
        updated_digestion = self._get_digestion_obj(digestion.id)
        caller_access = self._access_for(updated_digestion, actor) if updated_digestion else {
            "role": "none",
            "can_query": False,
            "can_manage": False,
            "can_read_sources": False,
        }
        new_owner_access = self._access_for(updated_digestion, new_owner) if updated_digestion else {
            "role": "owner",
            "can_query": True,
            "can_manage": True,
            "can_read_sources": True,
        }
        transferred = self.get_digestion(digestion.id, user_id=new_owner) or {}
        caller_view = self.get_digestion(digestion.id, user_id=actor) or {}
        source_state_after_transfer = self._source_summary_rows(digestion.id)
        return {
            "success": True,
            "digestion_id": digestion.id,
            "previous_owner_user_id": str(digestion.owner_user_id),
            "new_owner_user_id": new_owner,
            "new_owner": {
                "user_id": new_owner,
                "username": username or new_owner,
                "display_name": display_name or username or new_owner,
                "account_type": (new_owner_row["account_type"] if "account_type" in row_keys else "") or "",
            },
            "keep_previous_owner_access": bool(keep_previous_owner_access),
            "caller_access_after_transfer": caller_access,
            "new_owner_access": new_owner_access,
            "sources_remapped": remapped_sources,
            "sources_retained": retained_sources,
            "skipped_sources": skipped_sources,
            "source_counts": {
                "before": len(source_rows),
                "after": len(source_state_after_transfer),
                "remapped": len(remapped_sources),
                "retained": len(retained_sources),
                "skipped": len(skipped_sources),
            },
            "source_state_after_transfer": source_state_after_transfer,
            "caller_digestion_after_transfer": caller_view,
            "digestion": transferred,
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

    def delete_digestion(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        confirm_name: str = "",
        confirm_digestion_id: str = "",
    ) -> dict[str, Any]:
        """Delete an owned Digestion index and derived records while preserving Vault files.

        Deletion is intentionally owner-only. A manager can mutate a corpus, but
        removing the whole Digestion is destructive enough that the owner must
        explicitly confirm it. The Digestion rows, chunks, ACLs, outputs, visual
        evidence, contribution ledger, and query log are removed; source Vault
        files, exported package files, and generated image files remain in the
        Vault so attachments and reused materials are not broken.
        """
        actor = self._clean_id(actor_user_id)
        digestion = self._require_digestion(digestion_id, actor, manage=True)
        if actor != str(digestion.owner_user_id):
            raise DigestionError(
                "Only the Digestion owner can delete this Digestion.",
                status_code=403,
                reason="owner_required",
            )
        expected_name = str(digestion.name or "").strip()
        supplied_name = str(confirm_name or "").strip()
        supplied_id = self._clean_id(confirm_digestion_id)
        if supplied_id != digestion.id and supplied_name != expected_name:
            raise DigestionError(
                "Confirm the Digestion name or ID before deleting it.",
                status_code=400,
                reason="delete_confirmation_required",
            )

        counts: dict[str, int] = {}
        preserved_source_file_ids: set[str] = set()
        preserved_generated_file_ids: set[str] = set()
        with self.db.get_connection() as conn:
            for table, key in (
                ("digestion_sources", "sources"),
                ("digestion_chunks", "chunks"),
                ("digestion_acl", "acl_entries"),
                ("digestion_outputs", "outputs"),
                ("digestion_pdf_figures", "pdf_figures"),
                ("digestion_visual_evidence", "visual_evidence"),
                ("digestion_contributions", "contributions"),
                ("digestion_query_log", "query_log_entries"),
            ):
                row = conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE digestion_id = ?",
                    (digestion.id,),
                ).fetchone()
                counts[key] = int((row["count"] if row else 0) or 0)
            for row in conn.execute(
                "SELECT file_id FROM digestion_sources WHERE digestion_id = ?",
                (digestion.id,),
            ).fetchall():
                file_id = self._clean_id(row["file_id"] if row else "")
                if file_id:
                    preserved_source_file_ids.add(file_id)
            for table in ("digestion_pdf_figures", "digestion_visual_evidence"):
                for row in conn.execute(
                    f"SELECT image_file_id FROM {table} WHERE digestion_id = ? AND image_file_id IS NOT NULL",
                    (digestion.id,),
                ).fetchall():
                    file_id = self._clean_id(row["image_file_id"] if row else "")
                    if file_id:
                        preserved_generated_file_ids.add(file_id)

            # Explicit child-table deletes keep behavior stable even if SQLite
            # foreign-key enforcement is disabled for a test or local database.
            for table in (
                "digestion_query_log",
                "digestion_contributions",
                "digestion_operations",
                "digestion_visual_evidence",
                "digestion_pdf_figures",
                "digestion_outputs",
                "digestion_chunks",
                "digestion_acl",
                "digestion_sources",
            ):
                conn.execute(f"DELETE FROM {table} WHERE digestion_id = ?", (digestion.id,))
            cursor = conn.execute(
                "DELETE FROM digestions WHERE id = ? AND owner_user_id = ?",
                (digestion.id, actor),
            )
            conn.commit()

        with self._progress_lock:
            self._operation_progress.pop(digestion.id, None)

        deleted = bool(getattr(cursor, "rowcount", 0))
        return {
            "success": deleted,
            "deleted": deleted,
            "digestion_id": digestion.id,
            "name": digestion.name,
            "removed": counts,
            "preserved": {
                "vault_source_files": len(preserved_source_file_ids),
                "generated_image_files": len(preserved_generated_file_ids),
                "vault_files_are_preserved": True,
                "note": "Source Vault files, exported packages, and generated image files were not deleted.",
            },
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
                actor_user_id=actor_user_id,
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
            actor_user_id=actor_user_id,
        )
        total_chunks = 0
        embedded_count = 0
        total_figures = 0
        total_visual_evidence = 0
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
                    total_visual_evidence += int(file_chunks.get("visual_evidence_count") or 0)
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
                            "visual_evidence_count": total_visual_evidence,
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
                            "visual_evidence_count": total_visual_evidence,
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
                            "visual_evidence_count": total_visual_evidence,
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
                    "visual_evidence_count": total_visual_evidence,
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
                "visual_evidence_count": total_visual_evidence,
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
                actor_user_id=actor_user_id,
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
        warning = self._retrieval_preflight_warning(stats, has_indexed_rows=bool(rows))
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
                "retrieval_complete": False,
                "build_state": str(stats.get("build_state") or "empty"),
                "needs_build": bool(stats.get("needs_build")),
                "pending_source_count": int(stats.get("pending_source_count") or 0),
                "error_source_count": int(stats.get("error_source_count") or 0),
                "stats": stats,
                "warning": warning or "This Digestion has no indexed chunks yet. Build or rebuild it before expecting RAG results.",
                "warnings": [warning] if warning else ["This Digestion has no indexed chunks yet. Build or rebuild it before expecting RAG results."],
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
        response = {
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
            "retrieval_complete": not bool(warning) and str(stats.get("build_state") or "") == "ready",
            "build_state": str(stats.get("build_state") or ""),
            "needs_build": bool(stats.get("needs_build")),
            "pending_source_count": int(stats.get("pending_source_count") or 0),
            "error_source_count": int(stats.get("error_source_count") or 0),
            "stats": stats,
        }
        if warning:
            response["warning"] = warning
            response["warnings"] = [warning]
        return response

    @staticmethod
    def _retrieval_preflight_warning(stats: dict[str, Any], *, has_indexed_rows: bool) -> str:
        """Return a concise operator warning when retrieval can only be partial."""
        build_state = str(stats.get("build_state") or "").strip().lower()
        pending = int(stats.get("pending_source_count") or 0)
        errors = int(stats.get("error_source_count") or 0)
        chunks = int(stats.get("chunks") or 0)
        sources = int(stats.get("source_count") or 0)
        if not has_indexed_rows or chunks <= 0:
            return "This Digestion has no indexed chunks yet. Build or rebuild it before expecting RAG results."
        if build_state == "built_with_pending_sources" or pending > 0:
            suffix = f" ({pending} pending source{'s' if pending != 1 else ''})" if pending else ""
            return (
                "This Digestion has indexed chunks but also pending or unindexed sources"
                f"{suffix}. Build or rebuild it before treating query/context results as complete."
            )
        if build_state == "needs_build" or bool(stats.get("needs_build")):
            return "This Digestion has sources that still need a build. Build or rebuild it before expecting complete RAG results."
        if build_state == "error" or errors > 0:
            suffix = f" ({errors} source error{'s' if errors != 1 else ''})" if errors else ""
            return (
                "This Digestion has source build errors"
                f"{suffix}. Results may be incomplete; inspect sources/progress before relying on them."
            )
        if sources > 0 and build_state not in {"ready", ""}:
            return f"This Digestion is in build_state={build_state}. Verify build/progress before treating retrieval as complete."
        return ""

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

    def append_structured_records(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        profile: str = "generic",
        records: Optional[Iterable[dict[str, Any]]] = None,
        replace: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        """Append profile-specific source-of-truth records to a managed Digestion.

        This is intentionally schema-first. It lets agents convert graphical or
        semi-structured material, such as aviation approach plates, into durable,
        cited records without pretending ordinary text RAG can recover every
        operational field from chart geometry.
        """
        digestion = self._require_digestion(digestion_id, actor_user_id, manage=True)
        access = self._access_for(digestion, actor_user_id)
        self._set_operation_progress(
            digestion.id,
            "structured_records",
            status="running",
            phase="preflight",
            percent=5,
            processed=0,
            total=0,
            message="Checking source access before appending structured records.",
            details={},
            actor_user_id=actor_user_id,
        )
        if not access.get("can_read_sources"):
            self._set_operation_progress(
                digestion.id,
                "structured_records",
                status="failed",
                phase="source_access_denied",
                percent=0,
                message="Source-read access is required for source-grounded structured records.",
                actor_user_id=actor_user_id,
            )
            raise DigestionError(
                "Structured records are source-revealing. Grant source metadata access before appending or reading them.",
                status_code=403,
                reason="structured_record_source_metadata_denied",
            )
        profile_name = self._normalize_structured_record_profile(profile)
        raw_all = list(records or [])
        if not raw_all:
            self._set_operation_progress(
                digestion.id,
                "structured_records",
                status="failed",
                phase="missing_records",
                percent=0,
                message="Provide at least one structured record to append.",
                actor_user_id=actor_user_id,
            )
            raise DigestionError(
                "Provide at least one structured record to append.",
                status_code=400,
                reason="missing_structured_records",
            )
        raw_items = raw_all[:MAX_STRUCTURED_RECORDS_PER_APPEND]
        skipped: list[dict[str, str]] = []
        normalized: list[dict[str, Any]] = []
        total = len(raw_items)
        for index, item in enumerate(raw_items, start=1):
            self._set_operation_progress(
                digestion.id,
                "structured_records",
                status="running",
                phase="normalizing",
                percent=5 + int((index - 1) / max(1, total) * 70),
                processed=index - 1,
                total=total,
                current_label=str(item.get("title") or item.get("procedure_name") or item.get("record_type") or f"record {index}") if isinstance(item, dict) else f"record {index}",
                message=f"Normalizing structured record {index} of {total}.",
                details={"profile": profile_name},
                actor_user_id=actor_user_id,
            )
            if not isinstance(item, dict):
                skipped.append({"index": str(index), "reason": "record_not_object"})
                continue
            record = self._normalize_structured_record(digestion, actor_user_id, item, profile_name, index=index)
            if not record:
                skipped.append({"index": str(index), "reason": "empty_record"})
                continue
            normalized.append(record)
        extra_count = max(0, len(raw_all) - len(raw_items))
        if extra_count:
            skipped.append({"reason": "record_limit_reached", "count": str(extra_count)})

        existing_payload = self._structured_record_payload(digestion.id)
        existing_records = (
            existing_payload.get("records")
            if isinstance(existing_payload, dict) and isinstance(existing_payload.get("records"), list)
            else []
        )
        retained_records = [] if replace else [
            item for item in existing_records
            if isinstance(item, dict) and str(item.get("profile") or "generic") != profile_name
        ]
        if not replace:
            retained_records.extend(
                item for item in existing_records
                if isinstance(item, dict) and str(item.get("profile") or "generic") == profile_name
            )

        merged_by_id: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        updated_ids: set[str] = set()
        for item in [*retained_records, *normalized]:
            if not isinstance(item, dict):
                continue
            record_id = str(item.get("id") or "").strip()
            if not record_id:
                record_id = self._structured_record_identity(digestion.id, actor_user_id, item)
                item["id"] = record_id
            if record_id not in merged_by_id:
                ordered_ids.append(record_id)
            elif item in normalized:
                updated_ids.add(record_id)
            merged_by_id[record_id] = item
        records_merged = [merged_by_id[record_id] for record_id in ordered_ids if record_id in merged_by_id]
        profile_counts = self._count_by_key(records_merged, "profile")
        sources = self._source_summary_rows(digestion.id)
        now = self._now()
        payload = {
            "kind": STRUCTURED_RECORD_SCHEMA_VERSION,
            "schema_version": STRUCTURED_RECORD_SCHEMA_VERSION,
            "digestion": {
                "id": digestion.id,
                "name": digestion.name,
                "purpose": digestion.purpose or digestion.description,
                "status": digestion.status,
                "built_at": digestion.built_at,
            },
            "profiles": {
                name: self._structured_record_profile_definition(name)
                for name in sorted(profile_counts.keys() or {profile_name})
            },
            "stats": {
                "record_count": len(records_merged),
                "new_or_updated_record_count": len(normalized),
                "updated_record_count": len(updated_ids),
                "profile_counts": profile_counts,
                "source_count": len(sources),
                "skipped_count": len(skipped),
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
            "records": records_merged,
            "reuse_guidance": [
                "Treat structured records as cited operational facts derived from source material, not as a substitute for reviewing the source when safety-critical use demands it.",
                "For aviation chart records, prefer field-level provenance and verification_status=verified when a human or trusted agent has checked the exact chart field.",
                "Use structured-record search for normalized plate facts and Digestion query/context for cited chunk retrieval.",
            ],
            "updated_by": actor_user_id,
            "updated_note": str(note or "")[:1000],
            "updated_at": now,
        }
        output = self._upsert_output(
            digestion,
            actor_user_id,
            STRUCTURED_RECORD_OUTPUT_KIND,
            f"{digestion.name or 'Digestion'} Structured Records",
            "application/json",
            json.dumps(payload, indent=2, sort_keys=True),
            {
                "schema_version": STRUCTURED_RECORD_SCHEMA_VERSION,
                "record_count": len(records_merged),
                "new_or_updated_record_count": len(normalized),
                "updated_record_count": len(updated_ids),
                "profile_counts": profile_counts,
                "active_profile": profile_name,
                "source_count": len(sources),
                "source_revealing": True,
            },
        )
        self._set_operation_progress(
            digestion.id,
            "structured_records",
            status="completed",
            phase="completed",
            percent=100,
            processed=total,
            total=total,
            message=(
                f"Stored {len(normalized)} {profile_name.replace('_', ' ')} structured record"
                f"{'' if len(normalized) == 1 else 's'}; {len(records_merged)} total records retained."
            ),
            details={
                "profile": profile_name,
                "record_count": len(records_merged),
                "new_or_updated_record_count": len(normalized),
                "updated_record_count": len(updated_ids),
                "skipped_count": len(skipped),
                "profile_counts": profile_counts,
            },
            actor_user_id=actor_user_id,
        )
        return {
            "success": True,
            "digestion_id": digestion.id,
            "profile": profile_name,
            "added_or_updated": len(normalized),
            "updated": len(updated_ids),
            "record_count": len(records_merged),
            "skipped": skipped,
            "output": output,
            "preview": normalized[:5],
            "progress": self._progress_snapshot(digestion.id).get("structured_records", {}),
            "stats": payload["stats"],
        }

    def list_structured_records(
        self,
        digestion_id: str,
        actor_user_id: str,
        *,
        profile: str = "",
        limit: int = 120,
    ) -> dict[str, Any]:
        """List profile-specific source-of-truth records with source-gated access."""
        try:
            result_limit = int(limit or 120)
        except (TypeError, ValueError):
            result_limit = 120
        limit = max(1, min(result_limit, 500))
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        access = self._access_for(digestion, actor_user_id)
        if not access.get("can_read_sources"):
            raise DigestionError(
                "Structured records are source-revealing. Grant source metadata access before appending or reading them.",
                status_code=403,
                reason="structured_record_source_metadata_denied",
            )
        stats = self.stats(digestion.id)
        try:
            output = self.get_output(digestion.id, actor_user_id, STRUCTURED_RECORD_OUTPUT_KIND)
        except DigestionError as exc:
            if getattr(exc, "reason", "") == "output_not_found":
                return {
                    "success": True,
                    "digestion_id": digestion.id,
                    "profile": self._normalize_structured_record_profile(profile) if str(profile or "").strip() else "",
                    "mode": "structured_records",
                    "result_count": 0,
                    "record_count": 0,
                    "records": [],
                    "profiles": {},
                    "stats": stats,
                    "records_ready": False,
                    "warning": "No structured records output exists yet. Ask a manager or agent to append profile records first.",
                }
            raise
        try:
            payload = json.loads(str(output.get("content") or "{}"))
        except Exception:
            payload = {}
        records = payload.get("records") if isinstance(payload, dict) else []
        if not isinstance(records, list):
            records = []
        requested_profile = self._normalize_structured_record_profile(profile) if str(profile or "").strip() else ""
        listed: list[dict[str, Any]] = []
        for index, item in enumerate(records, start=1):
            if not isinstance(item, dict):
                continue
            item_profile = str(item.get("profile") or "generic").strip().lower()
            if requested_profile and item_profile != requested_profile:
                continue
            record = dict(item)
            record["record_index"] = index
            listed.append(record)
            if len(listed) >= limit:
                break
        return {
            "success": True,
            "digestion_id": digestion.id,
            "profile": requested_profile,
            "mode": "structured_records",
            "result_count": len(listed),
            "record_count": len(records),
            "records": listed,
            "profiles": (payload.get("profiles") if isinstance(payload, dict) else {}) or {},
            "stats": stats,
            "records_ready": True,
            "output": {
                "id": output.get("id") or "",
                "title": output.get("title") or "",
                "updated_at": output.get("updated_at") or "",
                "metadata": output.get("metadata") or {},
            },
        }

    def search_structured_records(
        self,
        digestion_id: str,
        actor_user_id: str,
        query: str,
        *,
        profile: str = "",
        limit: int = 25,
    ) -> dict[str, Any]:
        """Search profile-specific structured records with source-gated access."""
        query_text = str(query or "").strip()
        if not query_text:
            raise DigestionError("query is required", status_code=400, reason="missing_query")
        try:
            result_limit = int(limit or 25)
        except (TypeError, ValueError):
            result_limit = 25
        limit = max(1, min(result_limit, 120))
        digestion = self._require_digestion(digestion_id, actor_user_id, query=True)
        stats = self.stats(digestion.id)
        try:
            output = self.get_output(digestion.id, actor_user_id, STRUCTURED_RECORD_OUTPUT_KIND)
        except DigestionError as exc:
            if getattr(exc, "reason", "") == "output_not_found":
                return {
                    "success": True,
                    "digestion_id": digestion.id,
                    "query": query_text,
                    "mode": "structured_records",
                    "result_count": 0,
                    "results": [],
                    "stats": stats,
                    "records_ready": False,
                    "warning": "No structured records output exists yet. Ask a manager or agent to append profile records first.",
                }
            raise
        try:
            payload = json.loads(str(output.get("content") or "{}"))
        except Exception:
            payload = {}
        records = payload.get("records") if isinstance(payload, dict) else []
        if not isinstance(records, list):
            records = []
        requested_profile = self._normalize_structured_record_profile(profile) if str(profile or "").strip() else ""
        query_terms = self._query_terms(query_text)
        query_lower = query_text.lower()
        results: list[dict[str, Any]] = []
        for index, item in enumerate(records, start=1):
            if not isinstance(item, dict):
                continue
            item_profile = str(item.get("profile") or "generic").strip().lower()
            if requested_profile and item_profile != requested_profile:
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
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            title = str(
                item.get("title")
                or item.get("procedure_name")
                or item.get("record_type")
                or item.get("id")
                or "Structured record"
            ).strip()
            summary_parts = [
                title,
                str(item.get("summary") or ""),
                str(item.get("airport_icao") or ""),
                str(item.get("runway") or ""),
                " ".join(f"{key}: {value}" for key, value in list(fields.items())[:12]),
            ]
            score = 1.0 if phrase_match else (overlap / max(1, len(query_terms)))
            results.append({
                "record_index": index,
                "id": str(item.get("id") or ""),
                "profile": item_profile,
                "record_type": str(item.get("record_type") or ""),
                "title": title,
                "score": round(float(score), 6),
                "term_overlap": overlap,
                "summary": self._snippet(" ".join(part for part in summary_parts if part))[:900],
                "fields": fields,
                "airport_icao": str(item.get("airport_icao") or ""),
                "runway": str(item.get("runway") or ""),
                "verification": item.get("verification") if isinstance(item.get("verification"), dict) else {},
                "source": {
                    "file_id": str(source.get("file_id") or ""),
                    "file_name": str(source.get("file_name") or ""),
                    "page_label": str(source.get("page_label") or item.get("page_label") or ""),
                    "chart_id": str(source.get("chart_id") or item.get("chart_id") or ""),
                    "source_uri": str(source.get("source_uri") or item.get("source_uri") or ""),
                },
                "provenance": item.get("provenance")[:6] if isinstance(item.get("provenance"), list) else [],
                "updated_at": str(item.get("updated_at") or ""),
            })
        results.sort(key=lambda item: (item["score"], item["term_overlap"]), reverse=True)
        results = results[:limit]
        return {
            "success": True,
            "digestion_id": digestion.id,
            "query": query_text,
            "profile": requested_profile,
            "mode": "structured_records",
            "result_count": len(results),
            "results": results,
            "stats": stats,
            "records_ready": True,
            "record_count": len(records),
            "profiles": (payload.get("profiles") if isinstance(payload, dict) else {}) or {},
            "output": {
                "id": output.get("id") or "",
                "title": output.get("title") or "",
                "updated_at": output.get("updated_at") or "",
                "metadata": output.get("metadata") or {},
            },
        }

    def _structured_record_payload(self, digestion_id: str) -> dict[str, Any]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT content
                FROM digestion_outputs
                WHERE digestion_id = ? AND output_kind = ?
                """,
                (self._clean_id(digestion_id), STRUCTURED_RECORD_OUTPUT_KIND),
            ).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(row["content"] or "{}")
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _normalize_structured_record_profile(self, profile: str) -> str:
        value = str(profile or "").strip().lower().replace("-", "_")
        value = re.sub(r"[^a-z0-9_]+", "_", value).strip("_")
        if value in {"aviation", "aviation_plate", "approach_chart", "approach_plate", "chart"}:
            return "aviation_chart"
        return value or "generic"

    @staticmethod
    def _structured_record_profile_definition(profile: str) -> dict[str, Any]:
        if profile == "aviation_chart":
            return {
                "profile": "aviation_chart",
                "description": "Structured source-of-truth records derived from aviation charts, approach plates, airport data, and verified agent/human review.",
                "recommended_fields": [
                    "airport_icao",
                    "procedure_name",
                    "procedure_type",
                    "runway",
                    "chart_cycle",
                    "effective_date",
                    "nav_frequency",
                    "final_approach_fix",
                    "final_approach_course",
                    "glideslope_angle",
                    "minimums",
                    "missed_approach",
                    "notes",
                    "warnings",
                ],
                "verification_statuses": ["draft", "agent_extracted", "needs_human_review", "verified", "superseded"],
                "safety_note": "Aviation records should carry source file/page/chart identifiers and field-level provenance; do not rely on unverified extraction for operational flight safety.",
            }
        return {
            "profile": profile or "generic",
            "description": "Generic structured records derived from source materials and agent/human review.",
            "recommended_fields": ["subject", "claim", "value", "evidence", "source"],
            "verification_statuses": ["draft", "agent_extracted", "needs_human_review", "verified", "superseded"],
        }

    def _normalize_structured_record(
        self,
        digestion: Digestion,
        actor_user_id: str,
        item: dict[str, Any],
        profile: str,
        *,
        index: int,
    ) -> Optional[dict[str, Any]]:
        fields_raw = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        fields = {
            str(key or "").strip(): self._llm_scalar(value, limit=1200)
            for key, value in fields_raw.items()
            if str(key or "").strip() and self._llm_scalar(value, limit=1200)
        }
        source_raw = item.get("source") if isinstance(item.get("source"), dict) else {}
        source_file_id = self._clean_id(
            source_raw.get("file_id")
            or item.get("source_file_id")
            or item.get("file_id")
            or item.get("vault_file_id")
            or item.get("image_file_id")
        )
        record_type = self._llm_scalar(
            item.get("record_type") or item.get("type") or item.get("kind") or ("approach" if profile == "aviation_chart" else "record"),
            limit=120,
        )
        title = self._llm_scalar(
            item.get("title")
            or item.get("procedure_name")
            or fields.get("procedure_name")
            or item.get("subject")
            or item.get("label"),
            limit=300,
        )
        summary = self._llm_scalar(
            item.get("summary") or item.get("claim") or item.get("description") or item.get("text"),
            limit=1600,
        )
        if not any([title, summary, fields, source_file_id]):
            return None
        now = self._now()
        airport_icao = self._llm_scalar(item.get("airport_icao") or fields.get("airport_icao") or item.get("icao"), limit=24).upper()
        runway = self._llm_scalar(item.get("runway") or fields.get("runway"), limit=80).upper()
        procedure_name = self._llm_scalar(item.get("procedure_name") or fields.get("procedure_name") or title, limit=300)
        procedure_type = self._llm_scalar(item.get("procedure_type") or fields.get("procedure_type") or record_type, limit=120).upper()
        source = {
            "digestion_id": digestion.id,
            "file_id": source_file_id,
            "file_name": self._llm_scalar(source_raw.get("file_name") or item.get("file_name"), limit=240),
            "content_type": self._llm_scalar(source_raw.get("content_type") or item.get("content_type"), limit=120),
            "page_label": self._llm_scalar(source_raw.get("page_label") or item.get("page_label") or item.get("page"), limit=80),
            "chart_id": self._llm_scalar(source_raw.get("chart_id") or item.get("chart_id") or fields.get("chart_id"), limit=160),
            "source_uri": self._llm_scalar(source_raw.get("source_uri") or item.get("source_uri") or item.get("url"), limit=500),
            "source_ref": self._llm_scalar(source_raw.get("source_ref") or item.get("source_ref") or f"record_{index:04d}", limit=160),
        }
        provenance_raw = item.get("provenance") or item.get("evidence") or []
        if isinstance(provenance_raw, dict):
            provenance_raw = [provenance_raw]
        provenance: list[dict[str, Any]] = []
        for entry in provenance_raw[:24] if isinstance(provenance_raw, list) else []:
            if isinstance(entry, dict):
                text = self._llm_scalar(entry.get("text") or entry.get("quote") or entry.get("evidence"), limit=1000)
                field = self._llm_scalar(entry.get("field") or "record", limit=120)
                ref = self._llm_scalar(entry.get("source_ref") or source.get("source_ref") or "", limit=160)
                page = self._llm_scalar(entry.get("page_label") or entry.get("page") or source.get("page_label") or "", limit=80)
            else:
                text = self._llm_scalar(entry, limit=1000)
                field = "record"
                ref = source.get("source_ref") or ""
                page = source.get("page_label") or ""
            if text or ref or page:
                provenance.append({"field": field, "text": text, "source_ref": ref, "page_label": page})
        verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
        verification_status = self._llm_scalar(
            verification.get("status") or item.get("verification_status") or item.get("status") or "agent_extracted",
            limit=80,
        ).lower()
        record = {
            "id": self._llm_scalar(item.get("id") or item.get("record_id"), limit=160),
            "profile": profile,
            "record_type": record_type,
            "title": title or procedure_name or "Structured record",
            "summary": summary,
            "airport_icao": airport_icao,
            "procedure_name": procedure_name,
            "procedure_type": procedure_type,
            "runway": runway,
            "chart_cycle": self._llm_scalar(item.get("chart_cycle") or fields.get("chart_cycle") or item.get("cycle"), limit=80),
            "effective_date": self._llm_scalar(item.get("effective_date") or fields.get("effective_date"), limit=80),
            "fields": fields,
            "source": source,
            "source_refs": [source.get("source_ref") or source_file_id or f"record_{index:04d}"],
            "provenance": provenance,
            "verification": {
                "status": verification_status or "agent_extracted",
                "confidence": self._normalize_confidence(verification.get("confidence") if verification else item.get("confidence")),
                "reviewed_by": self._llm_scalar(verification.get("reviewed_by") if verification else item.get("reviewed_by"), limit=160),
                "notes": self._llm_scalar(verification.get("notes") if verification else item.get("verification_notes"), limit=800),
            },
            "tags": self._llm_string_list(item.get("tags"), limit=32, item_limit=80),
            "updated_by": actor_user_id,
            "updated_at": now,
        }
        if profile == "aviation_chart":
            for key in (
                "nav_frequency",
                "final_approach_fix",
                "final_approach_course",
                "glideslope_angle",
                "minimums",
                "missed_approach",
                "notes",
                "warnings",
            ):
                value = self._llm_scalar(item.get(key) or fields.get(key), limit=1200)
                if value:
                    record[key] = value
        if not record["id"]:
            record["id"] = self._structured_record_identity(digestion.id, actor_user_id, record)
        return record

    @staticmethod
    def _structured_record_identity(digestion_id: str, actor_user_id: str, item: dict[str, Any]) -> str:
        seed = json.dumps(
            {
                "digestion_id": digestion_id,
                "profile": item.get("profile") or "",
                "record_type": item.get("record_type") or "",
                "airport_icao": item.get("airport_icao") or "",
                "procedure_name": item.get("procedure_name") or item.get("title") or "",
                "runway": item.get("runway") or "",
                "source": item.get("source") or {},
                "actor_user_id": actor_user_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return "sr_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

    def stats(self, digestion_id: str) -> dict[str, Any]:
        return self.stats_many([digestion_id]).get(str(digestion_id or ""), self._empty_stats())

    @staticmethod
    def _empty_stats() -> dict[str, Any]:
        return {
            "chunks": 0,
            "token_estimate": 0,
            "figures": 0,
            "figure_vision_eligible_count": 0,
            "figure_vision_pending_count": 0,
            "figure_vision_analyzed_count": 0,
            "visual_evidence": 0,
            "outputs": 0,
            "source_count": 0,
            "indexed_source_count": 0,
            "pending_source_count": 0,
            "error_source_count": 0,
            "datapoint_count": 0,
            "quantitative_result_count": 0,
            "structured_record_count": 0,
            "contribution_count": 0,
            "pending_contribution_count": 0,
            "evidence_record_count": 0,
            "contested_evidence_count": 0,
            "needs_source_evidence_count": 0,
            "stable_evidence_count": 0,
            "retrieval_ready": False,
            "needs_build": False,
            "outputs_stale": False,
            "build_state": "empty",
            "sources_by_status": {},
            "evidence_by_status": {},
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
                SELECT
                    digestion_id,
                    COUNT(*) AS count,
                    COALESCE(SUM(CASE WHEN COALESCE(image_file_id, '') != '' THEN 1 ELSE 0 END), 0) AS image_count,
                    COALESCE(SUM(CASE WHEN COALESCE(image_file_id, '') != '' AND COALESCE(vision_description, '') = '' THEN 1 ELSE 0 END), 0) AS pending_vision_count,
                    COALESCE(SUM(CASE WHEN COALESCE(vision_description, '') != '' THEN 1 ELSE 0 END), 0) AS analyzed_vision_count
                FROM digestion_pdf_figures
                WHERE digestion_id IN ({placeholders})
                GROUP BY digestion_id
                """,
                ids,
            ).fetchall()
            visual_evidence_rows = conn.execute(
                f"""
                SELECT digestion_id, COUNT(*) AS count
                FROM digestion_visual_evidence
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
            evidence_rows = conn.execute(
                f"""
                SELECT digestion_id, status, COUNT(*) AS count
                FROM digestion_evidence_records
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
            stats_by_id[digestion_id]["figure_vision_eligible_count"] = int(row["image_count"] or 0)
            stats_by_id[digestion_id]["figure_vision_pending_count"] = int(row["pending_vision_count"] or 0)
            stats_by_id[digestion_id]["figure_vision_analyzed_count"] = int(row["analyzed_vision_count"] or 0)
        for row in visual_evidence_rows:
            digestion_id = str(row["digestion_id"] or "")
            if digestion_id not in stats_by_id:
                continue
            stats_by_id[digestion_id]["visual_evidence"] = int(row["count"] or 0)
        for row in output_rows:
            digestion_id = str(row["digestion_id"] or "")
            if digestion_id not in stats_by_id:
                continue
            stats_by_id[digestion_id]["outputs"] = int(stats_by_id[digestion_id].get("outputs") or 0) + 1
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            if str(row["output_kind"] or "") == STRUCTURED_DATAPOINT_OUTPUT_KIND and isinstance(metadata, dict):
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
            elif str(row["output_kind"] or "") == STRUCTURED_RECORD_OUTPUT_KIND and isinstance(metadata, dict):
                stats_by_id[digestion_id]["structured_record_count"] = self._bounded_int(
                    metadata.get("record_count"),
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
        for row in evidence_rows:
            digestion_id = str(row["digestion_id"] or "")
            if digestion_id not in stats_by_id:
                continue
            status = str(row["status"] or "").strip().lower() or "unknown"
            count = int(row["count"] or 0)
            stats_by_id[digestion_id]["evidence_by_status"][status] = count
            stats_by_id[digestion_id]["evidence_record_count"] = int(
                stats_by_id[digestion_id].get("evidence_record_count") or 0
            ) + count
            if status == EVIDENCE_STATUS_CONTESTED:
                stats_by_id[digestion_id]["contested_evidence_count"] = count
            elif status == EVIDENCE_STATUS_NEEDS_SOURCE:
                stats_by_id[digestion_id]["needs_source_evidence_count"] = count
            elif status == EVIDENCE_STATUS_STABLE:
                stats_by_id[digestion_id]["stable_evidence_count"] = count
        for digestion_id, stats in stats_by_id.items():
            statuses = stats.get("sources_by_status") if isinstance(stats.get("sources_by_status"), dict) else {}
            indexed = int(statuses.get("indexed") or 0)
            pending = int(statuses.get("pending") or 0)
            errors = int(statuses.get("error") or 0)
            chunks = int(stats.get("chunks") or 0)
            source_count = int(stats.get("source_count") or 0)
            pending_contributions = int(stats.get("pending_contribution_count") or 0)
            stats["indexed_source_count"] = indexed
            stats["pending_source_count"] = pending
            stats["error_source_count"] = errors
            stats["retrieval_ready"] = chunks > 0
            stats["needs_build"] = source_count > 0 and (chunks <= 0 or pending > 0)
            stats["outputs_stale"] = pending > 0 or pending_contributions > 0
            if source_count <= 0:
                stats["build_state"] = "empty"
            elif pending > 0 and chunks > 0:
                stats["build_state"] = "built_with_pending_sources"
            elif pending > 0:
                stats["build_state"] = "needs_build"
            elif chunks > 0:
                stats["build_state"] = "ready"
            elif errors > 0:
                stats["build_state"] = "error"
            else:
                stats["build_state"] = "needs_build"
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
            stats = self.stats(digestion.id)
            if int(stats.get("figures") or 0) > 0:
                requested.add(PDF_FIGURE_OUTPUT_KIND)
            if int(stats.get("visual_evidence") or 0) > 0:
                requested.add(VISUAL_EVIDENCE_OUTPUT_KIND)
        allowed = {
            "manifest",
            "human_brief",
            "agent_context",
            STRUCTURED_DATAPOINT_OUTPUT_KIND,
            PDF_FIGURE_OUTPUT_KIND,
            VISUAL_EVIDENCE_OUTPUT_KIND,
        }
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
            elif kind == VISUAL_EVIDENCE_OUTPUT_KIND:
                outputs.append(self._upsert_output(digestion, actor_user_id, *self._build_visual_evidence_output(digestion)))
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
            actor_user_id=actor_user_id,
        )
        if not access.get("can_read_sources"):
            self._set_operation_progress(
                digestion.id,
                "datapoints",
                status="failed",
                phase="source_access_denied",
                percent=0,
                message="Source-read access is required for structured datapoint extraction.",
                actor_user_id=actor_user_id,
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
        existing_chunk_ids = self._datapoint_source_chunk_ids(existing_datapoints)
        rows = self._datapoint_chunk_rows(
            digestion.id,
            limit=chunk_limit,
            exclude_file_ids=existing_file_ids if scope == "new" and existing_file_ids else None,
            exclude_chunk_ids=existing_chunk_ids if scope == "resume" and existing_chunk_ids else None,
        )
        if scope in {"new", "resume"} and existing_datapoints and not rows:
            output = self._structured_datapoint_output_row(digestion.id)
            quantitative_result_count = sum(
                len(item.get("quantitative_results") or [])
                for item in existing_datapoints
                if isinstance(item, dict)
            )
            no_work_message = (
                "No remaining indexed chunks need datapoint extraction; checkpointed structured datapoints were kept."
                if scope == "resume"
                else "No newly indexed sources need datapoint extraction; existing structured datapoints were kept."
            )
            self._set_operation_progress(
                digestion.id,
                "datapoints",
                status="completed",
                phase="no_remaining_chunks" if scope == "resume" else "no_new_chunks",
                percent=100,
                processed=0,
                total=0,
                message=no_work_message,
                details={
                    "datapoint_count": len(existing_datapoints),
                    "quantitative_result_count": quantitative_result_count,
                    "existing_datapoints_preserved": len(existing_datapoints),
                    "existing_chunk_count": len(existing_chunk_ids),
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
                "reason": "no_remaining_chunks" if scope == "resume" else "no_new_chunks",
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

        scoped_file_ids = {str(row["file_id"] or "") for row in rows if str(row["file_id"] or "")}
        scoped_chunk_ids = {str(row["chunk_id"] or "") for row in rows if str(row["chunk_id"] or "")}
        preserved_datapoints: list[dict[str, Any]] = []
        if scope == "resume":
            preserved_datapoints = [item for item in existing_datapoints if isinstance(item, dict)]
        elif scope == "new" and existing_datapoints:
            preserved_datapoints = [
                item
                for item in existing_datapoints
                if isinstance(item, dict)
                and not (self._datapoint_source_file_ids([item]) & scoped_file_ids)
            ]

        static_progress_details = {
            "max_chunks": chunk_limit,
            "max_datapoints": datapoint_limit,
            "provider": provider,
            "model": llm_context.get("model") or "",
            "credential_source": llm_context.get("credential_source") or "",
            "lens": effective_lens[:800],
            "extraction_scope": scope,
            "estimated_batches": estimated_batches,
            "checkpointed_datapoint_count": len(preserved_datapoints),
            "existing_chunk_count": len(existing_chunk_ids),
            "remaining_chunk_count": len(rows),
        }

        def checkpoint_structured_datapoints(payload: dict[str, Any]) -> None:
            partial = payload.get("datapoints") if isinstance(payload, dict) else []
            if not isinstance(partial, list):
                partial = []
            current_datapoints = [*preserved_datapoints, *[item for item in partial if isinstance(item, dict)]]
            if not current_datapoints:
                return
            extraction_stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
            errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
            self._upsert_structured_datapoint_output(
                digestion,
                actor_user_id,
                llm_context=llm_context,
                effective_lens=effective_lens,
                chunk_limit=chunk_limit,
                datapoint_limit=datapoint_limit,
                scope=scope,
                rows_considered=len(rows),
                datapoints=current_datapoints,
                new_datapoint_count=len(partial),
                preserved_datapoint_count=len(preserved_datapoints),
                extraction_stats=extraction_stats,
                errors=errors,
                scoped_file_ids=scoped_file_ids,
                scoped_chunk_ids=scoped_chunk_ids,
                checkpoint=True,
            )

        try:
            self._raise_if_operation_cancelled(digestion.id, "datapoints")
            extraction = self._extract_structured_datapoints_with_llm(
                digestion,
                rows,
                llm_context=llm_context,
                lens=effective_lens,
                datapoint_limit=datapoint_limit,
                cancel_check=lambda: self._operation_cancel_requested(digestion.id, "datapoints"),
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
                    details={
                        **static_progress_details,
                        **(payload.get("details") if isinstance(payload.get("details"), dict) else {}),
                    },
                ),
                checkpoint_callback=checkpoint_structured_datapoints,
            )
        except DigestionError as exc:
            if getattr(exc, "reason", "") == "operation_cancelled":
                self._set_operation_progress(
                    digestion.id,
                    "datapoints",
                    status="cancelled",
                    phase="cancelled",
                    percent=self._progress_snapshot(digestion.id).get("datapoints", {}).get("percent", 0),
                    message="Structured datapoint extraction was cancelled before completion.",
                    details={"reason": "operation_cancelled", "cancel_requested": True},
                )
                raise
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
        extracted_datapoints = extraction["datapoints"]
        datapoints = [*preserved_datapoints, *extracted_datapoints]
        quantitative_result_count = sum(len(item.get("quantitative_results") or []) for item in datapoints)
        output, payload = self._upsert_structured_datapoint_output(
            digestion,
            actor_user_id,
            llm_context=llm_context,
            effective_lens=effective_lens,
            chunk_limit=chunk_limit,
            datapoint_limit=datapoint_limit,
            scope=scope,
            rows_considered=len(rows),
            datapoints=datapoints,
            new_datapoint_count=len(extracted_datapoints),
            preserved_datapoint_count=len(preserved_datapoints),
            extraction_stats=extraction["stats"],
            errors=extraction["errors"],
            scoped_file_ids=scoped_file_ids,
            scoped_chunk_ids=scoped_chunk_ids,
            checkpoint=False,
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

    def _upsert_structured_datapoint_output(
        self,
        digestion: Digestion,
        actor_user_id: str,
        *,
        llm_context: dict[str, Any],
        effective_lens: str,
        chunk_limit: int,
        datapoint_limit: int,
        scope: str,
        rows_considered: int,
        datapoints: list[dict[str, Any]],
        new_datapoint_count: int,
        preserved_datapoint_count: int,
        extraction_stats: dict[str, Any],
        errors: list[dict[str, Any]],
        scoped_file_ids: set[str],
        scoped_chunk_ids: set[str],
        checkpoint: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Write the structured datapoint snapshot, including resumable checkpoints."""
        extraction_stats = extraction_stats if isinstance(extraction_stats, dict) else {}
        errors = errors if isinstance(errors, list) else []
        datapoints = [item for item in (datapoints or []) if isinstance(item, dict)]
        quantitative_result_count = sum(len(item.get("quantitative_results") or []) for item in datapoints)
        field_counts = self._datapoint_field_counts(datapoints)
        sources = self._source_summary_rows(digestion.id)
        digestion_stats = self.stats(digestion.id)
        parameters = llm_context.get("parameters") if isinstance(llm_context.get("parameters"), dict) else {}
        provider = str(llm_context.get("provider") or "openai").strip().lower()
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
                "lens": str(effective_lens or "")[:800],
                "source_boundary": "only indexed Digestion chunks are sent to the configured LLM provider; raw Vault files are not exported",
            },
            "limits": {
                "max_chunks": int(chunk_limit or 0),
                "max_datapoints": int(datapoint_limit or 0),
                "batch_chunks": extraction_stats.get("batch_chunk_limit"),
                "batch_chars": extraction_stats.get("batch_char_limit"),
                "chunk_chars": extraction_stats.get("chunk_char_limit"),
                "batch_records": extraction_stats.get("batch_record_limit"),
                "max_output_tokens": self._datapoint_max_output_tokens(provider=provider, parameters=parameters),
                "extraction_scope": scope,
            },
            "stats": {
                "datapoint_count": len(datapoints),
                "new_datapoint_count": int(new_datapoint_count or 0),
                "preserved_datapoint_count": int(preserved_datapoint_count or 0),
                "quantitative_result_count": quantitative_result_count,
                "source_count": len(sources),
                "chunks_considered": int(rows_considered or 0),
                "total_indexed_chunks": int(digestion_stats.get("chunks") or 0),
                "batches_considered": extraction_stats.get("batches_considered", 0),
                "failed_batches": extraction_stats.get("failed_batches", 0),
                "chunks_without_datapoints": extraction_stats.get("chunks_without_datapoints", 0),
                "field_counts": field_counts,
                "errors": errors[:8],
                "extraction_scope": scope,
                "scoped_source_file_ids": sorted(str(item) for item in (scoped_file_ids or set()) if str(item)),
                "scoped_chunk_ids": sorted(str(item) for item in (scoped_chunk_ids or set()) if str(item)),
                "checkpoint": bool(checkpoint),
                "checkpointed_at": self._now() if checkpoint else "",
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
        metadata = {
            "schema_version": STRUCTURED_DATAPOINT_SCHEMA_VERSION,
            "extractor": payload["extractor"],
            "datapoint_count": len(datapoints),
            "quantitative_result_count": quantitative_result_count,
            "chunks_considered": int(rows_considered or 0),
            "batches_considered": extraction_stats.get("batches_considered", 0),
            "failed_batches": extraction_stats.get("failed_batches", 0),
            "chunks_without_datapoints": extraction_stats.get("chunks_without_datapoints", 0),
            "field_counts": field_counts,
            "source_count": len(sources),
            "extraction_scope": scope,
            "new_datapoint_count": int(new_datapoint_count or 0),
            "preserved_datapoint_count": int(preserved_datapoint_count or 0),
            "checkpoint": bool(checkpoint),
            "checkpointed_at": payload["stats"].get("checkpointed_at") or "",
            "scoped_chunk_count": len(scoped_chunk_ids or set()),
        }
        output = self._upsert_output(
            digestion,
            actor_user_id,
            STRUCTURED_DATAPOINT_OUTPUT_KIND,
            f"{digestion.name or 'Digestion'} Structured Datapoints",
            "application/json",
            json.dumps(payload, indent=2, sort_keys=True),
            metadata,
        )
        return output, payload

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
                        WHEN 'visual_evidence' THEN 4
                        WHEN 'structured_datapoints' THEN 5
                        WHEN 'structured_records' THEN 6
                        WHEN 'manifest' THEN 7
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
            "evidence_list": f"GET {api_base}/evidence",
            "evidence_append": f"POST {api_base}/evidence",
            "evidence_search": f"POST {api_base}/evidence/search",
            "evidence_review": f"POST {api_base}/evidence/<evidence_id>/reviews",
            "build": f"POST {api_base}/build",
            "progress": f"GET {api_base}/progress",
            "query": f"POST {api_base}/query",
            "context": f"POST {api_base}/context",
            "datapoints_extract": f"POST {api_base}/datapoints/extract",
            "datapoints_search": f"POST {api_base}/datapoints/search",
            "structured_records_list": f"GET {api_base}/structured-records",
            "structured_records_append": f"POST {api_base}/structured-records",
            "structured_records_search": f"POST {api_base}/structured-records/search",
            "figures": f"GET {api_base}/figures",
            "visual_evidence": f"GET {api_base}/visual-evidence",
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
            "merge": "canopy_digest_merge",
            "add_materials": "canopy_digest_add_materials",
            "append_contributions": "canopy_digest_append_contributions",
            "contributions": "canopy_digest_contributions",
            "evidence": "canopy_digest_evidence",
            "datapoints_extract": "canopy_digest_datapoints_extract",
            "datapoints_search": "canopy_digest_datapoints_search",
            "structured_records": "canopy_digest_structured_records",
            "figures": "canopy_digest_figures",
            "visual_evidence": "canopy_digest_visual_evidence",
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
                "evidence_append": {
                    "records": [
                        {
                            "record_kind": "finding|claim|decision|risk|requirement",
                            "statement": "One durable source-grounded assertion or decision.",
                            "summary": "Why it matters and what evidence supports it.",
                            "status": "candidate",
                            "priority": "normal",
                            "confidence": 0.75,
                            "tags": ["topic", "review-needed"],
                            "evidence_refs": [
                                {
                                    "file_id": "<vault_or_source_file_id>",
                                    "file_name": "<source name>",
                                    "page_label": "p. 3",
                                    "chunk_id": "<chunk_id>",
                                    "quote": "short supporting quote",
                                }
                            ],
                        }
                    ]
                },
                "evidence_search": {"query": "claim, topic, tag, source, or decision term", "status": "", "tag": "", "limit": 25},
                "evidence_review": {
                    "action": "support|challenge|refine|supersede|mark_stale|request_source|confirm",
                    "note": "short critical-review note",
                    "confidence": 0.8,
                    "evidence_refs": [{"quote": "additional support or challenge evidence"}],
                    "status": "optional explicit status",
                    "superseded_by_id": "optional replacement evidence id",
                },
                "datapoints_extract": {
                    "lens": "optional extraction focus",
                    "max_chunks": 80,
                    "max_datapoints": 400,
                    "scope": "new",
                },
                "datapoints_search": {"query": "metric, material, method, claim, tag, or evidence term", "limit": 25},
                "structured_records_append": {
                    "profile": "aviation_chart",
                    "records": [
                        {
                            "record_type": "approach",
                            "airport_icao": "KSAN",
                            "procedure_name": "LOC RWY 27",
                            "procedure_type": "LOC",
                            "runway": "27",
                            "fields": {
                                "final_approach_fix": "REEBO",
                                "final_approach_altitude": "2000 ft",
                                "missed_approach": "cite exact chart text here",
                            },
                            "source": {"file_id": "<chart_pdf_file_id>", "file_name": "00373COMIX.pdf", "page_label": "p. 1"},
                            "provenance": [{"field": "final_approach_fix", "text": "quoted or visually verified chart evidence"}],
                            "verification": {"status": "needs_human_review", "confidence": 0.7},
                        }
                    ],
                    "replace": False,
                    "note": "What was extracted or corrected.",
                },
                "structured_records_search": {"query": "KSAN LOC RWY 27 FAF", "profile": "aviation_chart", "limit": 25},
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
                "Use evidence_append/search/review or canopy_digest_evidence to maintain a critical, reviewable truth layer: durable claims, decisions, risks, requirements, source-backed findings, challenges, and supersession history.",
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
                "evidence_read": "read_files plus Digestion query access",
                "evidence_write_review": "write_files plus Digestion manage access",
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
        visual_evidence: list[dict[str, Any]] = []
        if access.get("can_read_sources"):
            sources = self.list_sources(digestion.id, user_id=actor_user_id)
            figures = self.list_figures(digestion.id, actor_user_id, limit=80).get("figures") or []
            visual_evidence = self.list_visual_evidence(digestion.id, actor_user_id, limit=120).get("visual_evidence") or []
        digestion_payload = digestion.to_dict(access=access)
        digestion_payload["access_subject_user_id"] = actor_user_id
        digestion_payload["access_scope"] = "exporting_user"
        stats = self.stats(digestion.id)
        return {
            "kind": "canopy_digestion_package_v1",
            "generated_at": generated_at,
            "digestion": digestion_payload,
            "stats": stats,
            "output_policy_schema": "canopy_digestion_output_policy_v1",
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
            "visual_evidence_included": bool(visual_evidence),
            "visual_evidence": visual_evidence,
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
        folder_id = self._digestion_user_artifact_folder_id(digestion, actor_user_id)
        file_info = self.file_manager.save_file(
            content,
            filename,
            "application/json",
            actor_user_id,
            vault_folder_id=folder_id,
        )
        if not file_info:
            raise DigestionError("Could not export Digestion package to Vault.", status_code=500, reason="package_export_failed")
        return {
            "success": True,
            "digestion_id": digestion.id,
            "package": package,
            "file": file_info.to_dict(),
            "vault_folder_id": folder_id,
            "vault_folder_name": self._digestion_intake_folder_name(digestion),
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
            "retrieval_complete": bool(result.get("retrieval_complete")),
            "indexed_chunks": int(result.get("indexed_chunks") or 0),
            "build_state": str(result.get("build_state") or ""),
            "needs_build": bool(result.get("needs_build")),
            "pending_source_count": int(result.get("pending_source_count") or 0),
            "error_source_count": int(result.get("error_source_count") or 0),
            "warning": warning,
            "warnings": result.get("warnings") or ([warning] if warning else []),
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
        visual_evidence_segments: list[ExtractedSegment] = []
        visual_evidence_count = 0
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
                visual_result = self._extract_pdf_visual_evidence_for_source(
                    digestion,
                    info,
                    text_segments=segments,
                    figures=figure_result.get("figures") if isinstance(figure_result.get("figures"), list) else [],
                )
                visual_evidence_count = int(visual_result.get("visual_evidence_count") or 0)
                visual_evidence_segments = visual_result.get("segments") if isinstance(visual_result.get("segments"), list) else []
                if callable(progress_callback):
                    progress_callback(
                        "figures_extracted",
                        (
                            f"Captured {figure_count} PDF figure preview{'' if figure_count == 1 else 's'} and "
                            f"{visual_evidence_count} visual evidence record{'' if visual_evidence_count == 1 else 's'} from {info.original_name}."
                            if (figure_count or visual_evidence_count)
                            else f"No reusable embedded figures or visual evidence records were detected in {info.original_name}."
                        ),
                        0.32,
                        {"figure_count": figure_count, "visual_evidence_count": visual_evidence_count},
                    )
            except Exception as exc:
                logger.warning("PDF visual extraction failed for %s in %s: %s", info.id, digestion.id, exc, exc_info=True)
                if callable(progress_callback):
                    progress_callback(
                        "figures_unavailable",
                        "PDF text indexing will continue; figure/visual evidence extraction was unavailable for this source.",
                        0.32,
                        {"figure_error": str(exc)[:500]},
                    )
        else:
            with self.db.get_connection() as conn:
                conn.execute(
                    "DELETE FROM digestion_pdf_figures WHERE digestion_id = ? AND source_file_id = ?",
                    (digestion.id, info.id),
                )
                conn.execute(
                    "DELETE FROM digestion_visual_evidence WHERE digestion_id = ? AND source_file_id = ?",
                    (digestion.id, info.id),
                )
                conn.commit()
        if visual_evidence_segments:
            segments = [*segments, *visual_evidence_segments]
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
                {
                    "extracted_chars": extracted_chars,
                    "file_size": len(file_data),
                    "figure_count": figure_count,
                    "visual_evidence_count": visual_evidence_count,
                },
            )
        chunks = self._chunk_segments(segments, digestion.chunk_size, digestion.chunk_overlap, remaining_chunks=remaining_chunks)
        if not chunks:
            raise DigestionError("No indexable chunks were produced from source file.", status_code=415, reason="no_chunks")
        if callable(progress_callback):
            progress_callback(
                "chunking",
                f"Prepared {len(chunks)} semantic chunk{'' if len(chunks) == 1 else 's'} from {info.original_name}.",
                0.58,
                {
                    "source_chunk_count": len(chunks),
                    "extracted_chars": extracted_chars,
                    "figure_count": figure_count,
                    "visual_evidence_count": visual_evidence_count,
                },
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
        return {
            "chunk_count": len(chunks),
            "embedded_count": len(vectors),
            "figure_count": figure_count,
            "visual_evidence_count": visual_evidence_count,
        }

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
            self._organize_generated_figure_assets(digestion)
            cached = self._cached_pdf_figure_rows(digestion.id, info.id, info.checksum)
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
        folder_id = self._digestion_generated_figures_folder_id(digestion)
        if not folder_id:
            logger.warning(
                "Skipping generated PDF figure image for %s because the Digestion generated-figures folder is unavailable.",
                digestion.id,
            )
            return None
        saved = self.file_manager.save_file(
            image_bytes,
            filename,
            content_type,
            digestion.owner_user_id,
            vault_folder_id=folder_id,
        )
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
                "vault_folder_id": saved.vault_folder_id,
                "vault_folder": "Generated figures",
                "owner_intake_folder": self._digestion_intake_folder_name(digestion),
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

    def _extract_pdf_visual_evidence_for_source(
        self,
        digestion: Digestion,
        info: FileInfo,
        *,
        text_segments: list[ExtractedSegment],
        figures: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        cached = self._cached_visual_evidence_rows(digestion.id, info.id, info.checksum)
        if cached:
            return {
                "visual_evidence_count": len(cached),
                "visual_evidence": [self._visual_evidence_row_to_dict(row) for row in cached],
                "segments": self._visual_evidence_rows_to_segments(cached, source_name=info.original_name),
                "cached": True,
            }

        captions_by_page = self._pdf_caption_candidates_by_page(text_segments)
        with self.db.get_connection() as conn:
            conn.execute(
                "DELETE FROM digestion_visual_evidence WHERE digestion_id = ? AND source_file_id = ?",
                (digestion.id, info.id),
            )
            conn.commit()

        rows: list[dict[str, Any]] = []
        kind_counts: dict[str, int] = {}
        for page_label, candidates in captions_by_page.items():
            page_number = self._page_number_from_label(page_label)
            for page_order, caption in enumerate(candidates or [], start=1):
                if len(rows) >= MAX_PDF_VISUAL_EVIDENCE_PER_SOURCE:
                    break
                evidence_kind = self._classify_visual_evidence_kind(caption)
                kind_counts[evidence_kind] = kind_counts.get(evidence_kind, 0) + 1
                image = self._matching_visual_evidence_image(figures or [], page_label=page_label, order=page_order)
                image_file_id = str(image.get("image_file_id") or "") if image else ""
                title = self._visual_evidence_title(caption, evidence_kind=evidence_kind, page_label=page_label)
                context_text = self._visual_evidence_context_text(
                    caption,
                    evidence_kind=evidence_kind,
                    title=title,
                    page_label=page_label,
                    source_name=info.original_name,
                    image_file_id=image_file_id,
                )
                payload = {
                    "id": f"Dve{secrets.token_hex(12)}",
                    "digestion_id": digestion.id,
                    "source_file_id": info.id,
                    "source_checksum": info.checksum,
                    "evidence_kind": evidence_kind,
                    "evidence_index": kind_counts[evidence_kind],
                    "page_number": page_number,
                    "page_label": page_label,
                    "title": title,
                    "caption": caption[:1200],
                    "context_text": context_text,
                    "image_file_id": image_file_id,
                    "table_text": caption[:2000] if evidence_kind == "table" else "",
                    "confidence": 0.72 if evidence_kind in {"table", "chart", "diagram"} else 0.64,
                    "extraction_method": "pdf.caption_candidate",
                    "metadata": {
                        "source_file_name": info.original_name,
                        "source_content_type": info.content_type,
                        "caption_order_on_page": page_order,
                        "image_status": "linked_extracted_image" if image_file_id else "caption_only",
                        "image_name": str(image.get("image_name") or "") if image else "",
                    },
                }
                self._insert_visual_evidence(payload)
                rows.append(payload)
            if len(rows) >= MAX_PDF_VISUAL_EVIDENCE_PER_SOURCE:
                break
        return {
            "visual_evidence_count": len(rows),
            "visual_evidence": rows,
            "segments": self._visual_evidence_rows_to_segments(rows, source_name=info.original_name),
            "cached": False,
        }

    def _cached_visual_evidence_rows(self, digestion_id: str, source_file_id: str, source_checksum: str) -> list[Any]:
        with self.db.get_connection() as conn:
            return conn.execute(
                """
                SELECT
                    v.*,
                    s.file_name AS source_file_name,
                    s.content_type AS source_content_type,
                    img.original_name AS vault_image_name,
                    img.size AS vault_image_size
                FROM digestion_visual_evidence v
                LEFT JOIN digestion_sources s
                  ON s.digestion_id = v.digestion_id
                 AND s.file_id = v.source_file_id
                LEFT JOIN files img ON img.id = v.image_file_id
                WHERE v.digestion_id = ?
                  AND v.source_file_id = ?
                  AND COALESCE(v.source_checksum, '') = COALESCE(?, '')
                ORDER BY v.page_number, v.evidence_kind, v.evidence_index
                """,
                (digestion_id, source_file_id, source_checksum or ""),
            ).fetchall()

    def _insert_visual_evidence(self, payload: dict[str, Any]) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO digestion_visual_evidence (
                    id, digestion_id, source_file_id, source_checksum, evidence_kind,
                    evidence_index, page_number, page_label, title, caption,
                    context_text, image_file_id, table_text, confidence,
                    extraction_method, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(digestion_id, source_file_id, evidence_kind, evidence_index) DO UPDATE SET
                    source_checksum = excluded.source_checksum,
                    page_number = excluded.page_number,
                    page_label = excluded.page_label,
                    title = excluded.title,
                    caption = excluded.caption,
                    context_text = excluded.context_text,
                    image_file_id = excluded.image_file_id,
                    table_text = excluded.table_text,
                    confidence = excluded.confidence,
                    extraction_method = excluded.extraction_method,
                    metadata_json = excluded.metadata_json
                """,
                (
                    payload.get("id") or f"Dve{secrets.token_hex(12)}",
                    payload.get("digestion_id") or "",
                    payload.get("source_file_id") or "",
                    payload.get("source_checksum") or "",
                    payload.get("evidence_kind") or "visual",
                    int(payload.get("evidence_index") or 0),
                    int(payload.get("page_number") or 0),
                    payload.get("page_label") or "",
                    payload.get("title") or "",
                    payload.get("caption") or "",
                    payload.get("context_text") or "",
                    payload.get("image_file_id") or "",
                    payload.get("table_text") or "",
                    payload.get("confidence"),
                    payload.get("extraction_method") or "",
                    json.dumps(payload.get("metadata") or {}, sort_keys=True),
                    self._now(),
                ),
            )
            conn.commit()

    def _visual_evidence_rows_to_segments(self, rows: list[Any], *, source_name: str) -> list[ExtractedSegment]:
        segments: list[ExtractedSegment] = []
        for row in rows:
            data = self._visual_evidence_row_to_dict(row)
            text = self._visual_evidence_context_text(
                str(data.get("caption") or data.get("context_text") or ""),
                evidence_kind=str(data.get("evidence_kind") or "visual"),
                title=str(data.get("title") or ""),
                page_label=str(data.get("page_label") or ""),
                source_name=source_name or str(data.get("source_file_name") or ""),
                image_file_id=str(data.get("image_file_id") or ""),
            )
            if text:
                segments.append(ExtractedSegment(text=text, page_label=str(data.get("page_label") or "")))
        return segments

    def _visual_evidence_row_to_dict(self, row: Any) -> dict[str, Any]:
        try:
            metadata = json.loads(self._row_get(row, "metadata_json", "{}") or "{}")
        except Exception:
            metadata = self._row_get(row, "metadata", {}) if isinstance(self._row_get(row, "metadata", {}), dict) else {}
        image_file_id = str(self._row_get(row, "image_file_id", "") or "")
        source_file_id = str(self._row_get(row, "source_file_id", "") or "")
        return {
            "id": str(self._row_get(row, "id", "") or ""),
            "digestion_id": str(self._row_get(row, "digestion_id", "") or ""),
            "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
            "source_file_id": source_file_id,
            "source_file_name": str(self._row_get(row, "source_file_name", "") or source_file_id),
            "source_content_type": str(self._row_get(row, "source_content_type", "") or ""),
            "evidence_kind": str(self._row_get(row, "evidence_kind", "visual") or "visual"),
            "evidence_index": int(self._row_get(row, "evidence_index", 0) or 0),
            "page_number": int(self._row_get(row, "page_number", 0) or 0),
            "page_label": str(self._row_get(row, "page_label", "") or ""),
            "title": str(self._row_get(row, "title", "") or ""),
            "caption": str(self._row_get(row, "caption", "") or ""),
            "context_text": str(self._row_get(row, "context_text", "") or ""),
            "image_file_id": image_file_id,
            "image_name": str(self._row_get(row, "vault_image_name", "") or image_file_id),
            "image_url": f"/files/{image_file_id}" if image_file_id else "",
            "thumb_url": f"/files/{image_file_id}/thumb" if image_file_id else "",
            "table_text": str(self._row_get(row, "table_text", "") or ""),
            "confidence": self._row_get(row, "confidence", None),
            "extraction_method": str(self._row_get(row, "extraction_method", "") or ""),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": str(self._row_get(row, "created_at", "") or ""),
        }

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

    @staticmethod
    def _page_number_from_label(page_label: Any) -> int:
        match = re.search(r"(\d+)", str(page_label or ""))
        if not match:
            return 0
        try:
            return max(0, int(match.group(1)))
        except Exception:
            return 0

    @staticmethod
    def _classify_visual_evidence_kind(text: Any) -> str:
        clean = str(text or "").lower()
        if re.search(r"\btable\b", clean):
            return "table"
        if re.search(r"\b(chart|graph|plot|histogram|bar chart|scatter)\b", clean):
            return "chart"
        if re.search(r"\b(diagram|schematic|flowchart|architecture|pipeline|layout)\b", clean):
            return "diagram"
        if re.search(r"\b(fig(?:ure)?\.?)\b", clean):
            return "figure"
        return "visual"

    @staticmethod
    def _visual_evidence_title(caption: Any, *, evidence_kind: str, page_label: str) -> str:
        text = DigestionManager._normalize_text(str(caption or ""))
        first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0] if text else ""
        if first_sentence:
            return first_sentence[:180].rstrip()
        prefix = str(evidence_kind or "visual").replace("_", " ").title()
        return f"{prefix} evidence{f' on {page_label}' if page_label else ''}"

    @staticmethod
    def _matching_visual_evidence_image(
        figures: list[dict[str, Any]],
        *,
        page_label: str,
        order: int,
    ) -> dict[str, Any]:
        if not figures:
            return {}
        same_page = [
            item for item in figures
            if isinstance(item, dict)
            and str(item.get("page_label") or "") == str(page_label or "")
            and str(item.get("image_file_id") or "").strip()
        ]
        if not same_page:
            return {}
        index = max(0, min(len(same_page) - 1, int(order or 1) - 1))
        return same_page[index] if isinstance(same_page[index], dict) else {}

    @staticmethod
    def _visual_evidence_context_text(
        caption: str,
        *,
        evidence_kind: str,
        title: str,
        page_label: str,
        source_name: str,
        image_file_id: str = "",
    ) -> str:
        kind = str(evidence_kind or "visual").replace("_", " ")
        parts = [
            "PDF visual evidence extracted for Canopy Digestion.",
            f"Evidence kind: {kind}.",
            f"Title: {title}." if title else "",
            f"Source: {source_name}." if source_name else "",
            f"Page: {page_label}." if page_label else "",
            f"Caption/context: {caption}." if caption else "Caption/context: no nearby caption text was detected.",
            f"Image file id: {image_file_id}." if image_file_id else "Image file id: none; this record is caption/table/text-derived evidence.",
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
                if str(row["file_checksum"] or "").strip() == checksum:
                    return str(row["file_id"] or "")
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

    def _require_local_digestion_user(self, user_id: str) -> tuple[str, Any, set[str]]:
        """Return a local, non-shadow user row eligible for live Digestion access."""
        grantee = self._clean_id(user_id)
        if not grantee:
            raise DigestionError("grantee_user_id is required", status_code=400, reason="missing_grantee")
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (grantee,)).fetchone()
        if not row:
            raise DigestionError(
                "Digestion live access can only be granted to local users or agents on this node.",
                status_code=400,
                reason="grantee_not_eligible",
            )
        row_keys = set(row.keys()) if hasattr(row, "keys") else set()
        origin_peer = str((row["origin_peer"] if "origin_peer" in row_keys else "") or "").strip()
        if origin_peer:
            raise DigestionError(
                "Digestion live access can only be granted to local users or agents on this node.",
                status_code=400,
                reason="grantee_not_eligible",
            )
        return grantee, row, row_keys

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

    def _delete_source_artifacts(self, conn: Any, digestion_id: str, file_ids: Iterable[str]) -> None:
        clean_ids = self._clean_id_list(file_ids)
        if not clean_ids:
            return
        placeholders = ",".join("?" for _ in clean_ids)
        params = (digestion_id, *clean_ids)
        for table, column in (
            ("digestion_visual_evidence", "source_file_id"),
            ("digestion_pdf_figures", "source_file_id"),
            ("digestion_chunks", "file_id"),
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE digestion_id = ? AND {column} IN ({placeholders})",
                params,
            )
        # Reusable outputs may contain removed source names, snippets, figures, or datapoints.
        conn.execute("DELETE FROM digestion_outputs WHERE digestion_id = ?", (digestion_id,))

    def _source_row_to_dict(self, row: Any) -> dict[str, Any]:
        """Return a source row with parsed owner/copy/contribution metadata.

        The raw JSON is kept for backwards compatibility, while the parsed fields
        give agents and UI code a stable place to inspect provenance without
        reimplementing the Digestion metadata schema.
        """
        metadata_raw = self._row_get(row, "source_metadata_json", "{}")
        metadata = self._json_loads(metadata_raw, {})
        if not isinstance(metadata, dict):
            metadata = {}
        source_kind = str(self._row_get(row, "source_kind", "vault_file") or "vault_file")
        file_name = str(self._row_get(row, "file_name", "") or self._row_get(row, "file_id", "") or "")
        source_label = str(self._row_get(row, "source_label", "") or file_name)
        item = {
            "file_id": str(self._row_get(row, "file_id", "") or ""),
            "file_checksum": str(self._row_get(row, "file_checksum", "") or ""),
            "file_name": file_name,
            "content_type": str(self._row_get(row, "content_type", "") or ""),
            "status": str(self._row_get(row, "status", "") or ""),
            "extracted_chars": int(self._row_get(row, "extracted_chars", 0) or 0),
            "chunk_count": int(self._row_get(row, "chunk_count", 0) or 0),
            "error": str(self._row_get(row, "error", "") or ""),
            "updated_at": str(self._row_get(row, "updated_at", "") or ""),
            "source_kind": source_kind,
            "source_label": source_label,
            "source_uri": str(self._row_get(row, "source_uri", "") or ""),
            "source_metadata_json": str(metadata_raw or "{}"),
            "metadata": metadata,
            "source_metadata": metadata,
            "ingest_path": str(metadata.get("ingest_path") or source_kind or ""),
            "submitted_by": str(metadata.get("submitted_by") or ""),
            "source_owner_user_id": str(metadata.get("source_owner_user_id") or ""),
            "original_file_id": str(metadata.get("original_file_id") or ""),
            "original_file_name": str(metadata.get("original_file_name") or ""),
            "original_uploaded_by": str(metadata.get("original_uploaded_by") or ""),
            "original_checksum": str(metadata.get("original_checksum") or ""),
            "copied_to_owner_vault": bool(metadata.get("copied_to_owner_vault")),
            "owner_intake_folder_id": str(metadata.get("owner_intake_folder_id") or ""),
            "owner_intake_folder": str(metadata.get("owner_intake_folder") or ""),
            "ownership_transfer": metadata.get("ownership_transfer") if isinstance(metadata.get("ownership_transfer"), dict) else {},
            "preview_file_id": str(self._row_get(row, "file_id", "") or ""),
        }
        return item

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
        exclude_chunk_ids: Optional[Iterable[str]] = None,
    ) -> list[Any]:
        clauses = ["c.digestion_id = ?"]
        params: list[Any] = [digestion_id]
        include_ids = [self._clean_id(item) for item in (file_ids or []) if self._clean_id(item)]
        exclude_ids = [self._clean_id(item) for item in (exclude_file_ids or []) if self._clean_id(item)]
        exclude_chunks = [self._clean_id(item) for item in (exclude_chunk_ids or []) if self._clean_id(item)]
        if include_ids:
            clauses.append(f"c.file_id IN ({','.join('?' for _ in include_ids)})")
            params.extend(include_ids)
        if exclude_ids:
            clauses.append(f"c.file_id NOT IN ({','.join('?' for _ in exclude_ids)})")
            params.extend(exclude_ids)
        if exclude_chunks:
            clauses.append(f"c.id NOT IN ({','.join('?' for _ in exclude_chunks)})")
            params.extend(exclude_chunks)
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
        if scope in {"resume", "continue", "recover", "stalled", "remaining"}:
            return "resume"
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
    def _datapoint_source_chunk_ids(datapoints: Iterable[Any]) -> set[str]:
        chunk_ids: set[str] = set()
        for item in datapoints or []:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if isinstance(source, dict) and str(source.get("chunk_id") or "").strip():
                chunk_ids.add(str(source.get("chunk_id") or "").strip())
            source_chunks = item.get("source_chunks")
            if isinstance(source_chunks, list):
                for chunk in source_chunks:
                    if isinstance(chunk, dict) and str(chunk.get("chunk_id") or "").strip():
                        chunk_ids.add(str(chunk.get("chunk_id") or "").strip())
            evidence = item.get("evidence")
            if isinstance(evidence, list):
                for ev in evidence:
                    if isinstance(ev, dict) and str(ev.get("source_chunk_id") or "").strip():
                        chunk_ids.add(str(ev.get("source_chunk_id") or "").strip())
        return chunk_ids

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

    def _resolve_figure_vision_llm_context(self, actor_user_id: str) -> dict[str, Any]:
        from .canopy_ai import CanopyLLMError, CanopyLLMManager

        try:
            secret_key = getattr(self.config, "secret_key", "") if self.config is not None else ""
            manager = CanopyLLMManager(self.db, secret_key or os.getenv("CANOPY_SECRET_KEY", ""))
            settings = manager._resolve_effective_digestion_settings(actor_user_id)
        except CanopyLLMError as exc:
            raise DigestionError(
                f"LLM-backed figure vision is not configured: {exc}",
                status_code=int(getattr(exc, "status_code", 400) or 400),
                reason=f"figure_vision_{getattr(exc, 'reason', 'llm_unavailable')}",
            ) from exc
        provider = str(settings.get("provider") or "openai").strip().lower()
        if provider != "openai":
            raise DigestionError(
                "Figure vision enrichment currently requires an OpenAI Responses vision-capable model. "
                "Bedrock can be added once a bounded image-payload path is validated.",
                status_code=400,
                reason="figure_vision_unsupported_llm_provider",
            )
        return {
            "manager": manager,
            "provider": provider,
            "api_key": str(settings.get("api_key") or ""),
            "model": str(settings.get("model") or "gpt-5-mini"),
            "credential_source": str(settings.get("credential_source") or "user"),
            "default_lens": str(settings.get("default_lens") or ""),
            "parameters": settings.get("parameters") if isinstance(settings.get("parameters"), dict) else {},
        }

    def _figure_vision_candidate_rows(self, digestion_id: str, *, limit: int, overwrite: bool = False) -> list[Any]:
        where = "WHERE f.digestion_id = ? AND COALESCE(f.image_file_id, '') != ''"
        if not overwrite:
            where += " AND COALESCE(f.vision_description, '') = ''"
        with self.db.get_connection() as conn:
            return conn.execute(
                f"""
                SELECT
                    f.*,
                    s.file_name AS source_file_name,
                    s.content_type AS source_content_type,
                    img.original_name AS vault_image_name,
                    img.content_type AS vault_image_content_type,
                    img.size AS vault_image_size
                FROM digestion_pdf_figures f
                LEFT JOIN digestion_sources s
                  ON s.digestion_id = f.digestion_id
                 AND s.file_id = f.source_file_id
                LEFT JOIN files img ON img.id = f.image_file_id
                {where}
                ORDER BY COALESCE(s.file_name, f.source_file_id) COLLATE NOCASE,
                         f.source_file_id, f.page_number, f.figure_index
                LIMIT ?
                """,
                (self._clean_id(digestion_id), max(1, min(int(limit or 1), MAX_FIGURE_VISION_LIMIT))),
            ).fetchall()

    @staticmethod
    def _figure_vision_system_prompt() -> str:
        return """You are Canopy's source-grounded PDF figure vision enrichment engine.

Interpret exactly one extracted source figure image using only the image plus the supplied source metadata, caption, and context. The goal is to make the figure reusable for humans and agents without pretending to read the whole paper.

Rules:
- Return one valid JSON object only. No markdown fences, prose, comments, or trailing text.
- Stay source-grounded. Do not infer values, labels, causal claims, or author intent that are not visible in the figure/caption/context.
- Extract quantitative datapoints only when values, units, axes, legends, labels, or annotations are legible. Mark approximate values as approximate.
- If the figure is a diagram or photograph rather than a chart, describe structure, labels, relationships, and qualitative observations instead of inventing datapoints.
- Include limitations when text is illegible, axes are missing, values are approximate, or the figure is only partially interpretable.

Required JSON shape:
{
  "description": "concise source-grounded description of what the figure shows",
  "figure_type": "chart|diagram|table|screenshot|photograph|schematic|other",
  "author_intent": "what the figure appears intended to demonstrate, if supported",
  "datapoints": [
    {"label":"visible metric or item", "value_text":"visible value or approximate value", "unit":"visible unit", "series":"optional series/legend", "evidence":"visible label/caption basis", "approximate": false}
  ],
  "observations": ["important qualitative observations"],
  "limitations": ["what could not be read or verified"],
  "warnings": ["optional safety/citation caveats"],
  "confidence": 0.0
}
""".strip()

    def _figure_vision_user_prompt(self, digestion: Digestion, figure: dict[str, Any], *, lens: str = "") -> str:
        payload = {
            "digestion_id": digestion.id,
            "digestion_name": digestion.name,
            "digestion_purpose": digestion.purpose or digestion.description or "",
            "source_file_id": figure.get("source_file_id") or "",
            "source_file_name": figure.get("source_file_name") or "",
            "figure_id": figure.get("id") or "",
            "figure_index": figure.get("figure_index") or 0,
            "page_label": figure.get("page_label") or "",
            "width": figure.get("width") or 0,
            "height": figure.get("height") or 0,
            "caption": str(figure.get("caption") or "")[:1200],
            "context_text": str(figure.get("context_text") or "")[:1800],
            "lens": str(lens or "")[:800],
        }
        return (
            "Analyze this extracted PDF figure for a Canopy Digestion.\n"
            "Use the image and this metadata/caption/context only:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "Return the required JSON object now."
        )

    def _call_figure_vision_llm(
        self,
        llm_context: dict[str, Any],
        *,
        system_prompt: str,
        prompt: str,
        image_bytes: bytes,
        image_content_type: str,
        max_output_tokens: int,
    ) -> str:
        from .canopy_ai import CanopyLLMError

        manager = llm_context.get("manager")
        provider = str(llm_context.get("provider") or "openai").strip().lower()
        try:
            if provider == "openai":
                return manager._call_openai_vision(
                    api_key=str(llm_context.get("api_key") or ""),
                    model=str(llm_context.get("model") or "gpt-5-mini"),
                    system_prompt=system_prompt,
                    prompt=prompt,
                    image_bytes=image_bytes,
                    image_content_type=image_content_type,
                    max_output_tokens=max_output_tokens,
                )
        except CanopyLLMError as exc:
            raise DigestionError(
                f"LLM figure vision failed: {exc}",
                status_code=int(getattr(exc, "status_code", 502) or 502),
                reason=f"figure_vision_{getattr(exc, 'reason', 'llm_error')}",
            ) from exc
        raise DigestionError("Unsupported figure vision LLM provider.", status_code=400, reason="figure_vision_unsupported_llm_provider")

    def _parse_figure_vision_json(self, raw_text: str) -> dict[str, Any]:
        try:
            parsed = self._extract_json_object(str(raw_text or ""))
            if isinstance(parsed, dict):
                return self._normalize_figure_vision_payload(parsed)
        except Exception:
            pass
        fallback = self._llm_scalar(raw_text, limit=2800)
        if not fallback:
            raise DigestionError("LLM returned no usable figure vision description.", status_code=502, reason="figure_vision_empty_output")
        return {
            "description": fallback,
            "figure_type": "other",
            "author_intent": "",
            "datapoints": [],
            "observations": [],
            "limitations": ["The provider response was not valid JSON, so only a plain-text description was retained."],
            "warnings": ["non_json_provider_response"],
            "confidence": None,
        }

    def _normalize_figure_vision_payload(self, parsed: dict[str, Any]) -> dict[str, Any]:
        roots = [parsed]
        for key in (
            "figure",
            "image",
            "analysis",
            "result",
            "data",
            "output",
            "vision",
            "visual_analysis",
            "visual_evidence",
            "figure_analysis",
            "figure_vision",
        ):
            nested = parsed.get(key)
            if isinstance(nested, dict):
                roots.append(nested)
            elif isinstance(nested, list) and nested and isinstance(nested[0], dict):
                roots.append(nested[0])

        def first_value(*keys: str) -> Any:
            for root in roots:
                for key in keys:
                    value = root.get(key)
                    if value not in (None, "", [], {}):
                        return value
            return None

        description = self._llm_scalar(
            first_value(
                "description",
                "figure_description",
                "visual_description",
                "image_description",
                "vision_description",
                "summary",
                "caption_summary",
                "what_it_shows",
                "content_summary",
                "analysis_summary",
            ),
            limit=2800,
        )
        figure_type = self._llm_scalar(
            first_value("figure_type", "type", "kind", "visual_type", "image_type"),
            limit=80,
        ).lower() or "other"
        author_intent = self._llm_scalar(
            first_value("author_intent", "intent", "purpose", "interpretation", "main_point"),
            limit=900,
        )
        observations = self._llm_string_list(
            first_value("observations", "qualitative_observations", "findings", "key_observations", "visible_features"),
            limit=16,
            item_limit=500,
        )
        limitations = self._llm_string_list(
            first_value("limitations", "uncertainty", "caveats", "not_readable", "quality_limitations"),
            limit=12,
            item_limit=500,
        )
        warnings = self._llm_string_list(
            first_value("warnings", "safety_caveats", "citation_caveats", "notes"),
            limit=8,
            item_limit=400,
        )
        datapoints: list[dict[str, Any]] = []
        raw_datapoints = first_value(
            "datapoints",
            "data_points",
            "quantitative_results",
            "visible_datapoints",
            "measurements",
            "values",
        ) or []
        if isinstance(raw_datapoints, dict):
            raw_datapoints = [raw_datapoints]
        if isinstance(raw_datapoints, list):
            for item in raw_datapoints[:80]:
                if isinstance(item, dict):
                    record = {
                        "label": self._llm_scalar(item.get("label") or item.get("metric") or item.get("measurement_label"), limit=240),
                        "value_text": self._llm_scalar(item.get("value_text") or item.get("value") or item.get("number"), limit=240),
                        "unit": self._llm_scalar(item.get("unit"), limit=80),
                        "series": self._llm_scalar(item.get("series") or item.get("group") or item.get("legend"), limit=160),
                        "evidence": self._llm_scalar(item.get("evidence") or item.get("basis") or item.get("source_label"), limit=500),
                        "approximate": bool(item.get("approximate") or item.get("estimated")),
                    }
                    if any(record.get(key) for key in ("label", "value_text", "evidence")):
                        datapoints.append(record)
                else:
                    text = self._llm_scalar(item, limit=400)
                    if text:
                        datapoints.append({"label": "visible datapoint", "value_text": text, "unit": "", "series": "", "evidence": text, "approximate": False})
        if not description:
            fallback_bits: list[str] = []
            if observations:
                fallback_bits.append(f"Observations: {'; '.join(observations[:3])}.")
            if author_intent:
                fallback_bits.append(f"Apparent purpose: {author_intent}.")
            if datapoints:
                point_labels = [
                    " ".join(
                        part
                        for part in (
                            item.get("label") or "datapoint",
                            item.get("value_text") or "",
                            item.get("unit") or "",
                        )
                        if part
                    )
                    for item in datapoints[:4]
                ]
                fallback_bits.append(f"Visible datapoints include: {'; '.join(point_labels)}.")
            if limitations:
                fallback_bits.append(f"Limitations: {'; '.join(limitations[:2])}.")
            if fallback_bits:
                description = self._llm_scalar(" ".join(fallback_bits), limit=2800)
                if "description_synthesized_from_structured_fields" not in warnings:
                    warnings.append("description_synthesized_from_structured_fields")
        confidence = self._normalize_confidence(first_value("confidence", "score"))
        return {
            "description": description,
            "figure_type": figure_type,
            "author_intent": author_intent,
            "datapoints": datapoints,
            "observations": observations,
            "limitations": limitations,
            "warnings": warnings,
            "confidence": confidence,
        }

    def _update_figure_vision_row(
        self,
        row: Any,
        parsed: dict[str, Any],
        *,
        llm_context: dict[str, Any],
        lens: str,
        max_image_bytes: int,
        image_byte_size: int,
    ) -> dict[str, Any]:
        figure_id = str(self._row_get(row, "id", "") or "")
        digestion_id = str(self._row_get(row, "digestion_id", "") or "")
        description = self._llm_scalar(parsed.get("description"), limit=2800)
        if not description:
            raise DigestionError("LLM returned no figure description.", status_code=502, reason="figure_vision_missing_description")
        try:
            metadata = json.loads(self._row_get(row, "metadata_json", "{}") or "{}")
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update({
            "vision_status": "completed",
            "vision_schema_version": PDF_FIGURE_VISION_SCHEMA_VERSION,
            "vision_provider": str(llm_context.get("provider") or ""),
            "vision_model": str(llm_context.get("model") or ""),
            "vision_credential_source": str(llm_context.get("credential_source") or ""),
            "vision_updated_at": self._now(),
            "vision_lens": str(lens or "")[:800],
            "vision_figure_type": parsed.get("figure_type") or "other",
            "vision_author_intent": parsed.get("author_intent") or "",
            "vision_datapoints": parsed.get("datapoints") if isinstance(parsed.get("datapoints"), list) else [],
            "vision_observations": parsed.get("observations") if isinstance(parsed.get("observations"), list) else [],
            "vision_limitations": parsed.get("limitations") if isinstance(parsed.get("limitations"), list) else [],
            "vision_warnings": parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else [],
            "vision_confidence": parsed.get("confidence"),
            "vision_image_bytes": int(image_byte_size or 0),
            "vision_max_image_bytes": int(max_image_bytes or 0),
            "vision_source_boundary": "source-derived figure image, caption, and context only; raw PDF was not exported",
        })
        with self.db.get_connection() as conn:
            conn.execute(
                """
                UPDATE digestion_pdf_figures
                SET vision_description = ?, metadata_json = ?
                WHERE digestion_id = ? AND id = ?
                """,
                (description, json.dumps(metadata, ensure_ascii=False, sort_keys=True), digestion_id, figure_id),
            )
            updated = conn.execute(
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
                WHERE f.digestion_id = ? AND f.id = ?
                """,
                (digestion_id, figure_id),
            ).fetchone()
            conn.commit()
        return self._figure_row_to_dict(updated or row)

    def _extract_structured_datapoints_with_llm(
        self,
        digestion: Digestion,
        rows: list[Any],
        *,
        llm_context: dict[str, Any],
        lens: str,
        datapoint_limit: int,
        cancel_check: Optional[Any] = None,
        progress_callback: Optional[Any] = None,
        checkpoint_callback: Optional[Any] = None,
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
            if callable(cancel_check) and cancel_check():
                raise DigestionError("Digestion operation cancelled by user.", status_code=409, reason="operation_cancelled")
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
                if callable(cancel_check) and cancel_check():
                    raise DigestionError("Digestion operation cancelled by user.", status_code=409, reason="operation_cancelled")
                parsed = self._parse_datapoint_llm_json(raw, llm_context=llm_context, system_prompt=system_prompt)
                normalized, record_refs = self._normalize_llm_datapoints(
                    parsed.get("datapoints") if isinstance(parsed, dict) else [],
                    source_map=source_map,
                    digestion=digestion,
                    remaining=max(0, int(datapoint_limit) - len(datapoints)),
                )
                datapoints.extend(normalized)
                touched_refs.update(record_refs)
                if callable(checkpoint_callback):
                    checkpoint_callback({
                        "datapoints": datapoints[:int(datapoint_limit)],
                        "errors": errors[:],
                        "stats": {
                            "batches_considered": batch_index,
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
                    })
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
            source = self._source_row_to_dict(row)
            summaries.append({
                "file_id": source["file_id"],
                "file_name": source["file_name"],
                "content_type": source["content_type"],
                "source_kind": source["source_kind"],
                "source_label": source["source_label"],
                "source_uri": source["source_uri"],
                "status": source["status"],
                "extracted_chars": source["extracted_chars"],
                "chunk_count": source["chunk_count"],
                "error": source["error"],
                "updated_at": source["updated_at"],
                "metadata": source["metadata"],
                "submitted_by": source["submitted_by"],
                "original_file_id": source["original_file_id"],
                "copied_to_owner_vault": source["copied_to_owner_vault"],
                "ownership_transfer": source["ownership_transfer"],
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
                    "pdf_visual_evidence_caption_table_extraction",
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
                "vision_description and metadata.vision_* are populated only after an explicit, opt-in figure vision enrichment pass; normal builds do not spend vision-model tokens.",
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
            },
        )

    def _build_visual_evidence_output(self, digestion: Digestion) -> tuple[str, str, str, str, dict[str, Any]]:
        evidence = self.list_visual_evidence(digestion.id, digestion.owner_user_id, limit=320).get("visual_evidence") or []
        payload = {
            "kind": VISUAL_EVIDENCE_SCHEMA_VERSION,
            "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
            "digestion": {
                "id": digestion.id,
                "name": digestion.name,
                "status": digestion.status,
                "built_at": digestion.built_at,
            },
            "stats": self.stats(digestion.id),
            "visual_evidence": evidence,
            "evidence_kinds": self._count_by_key(evidence, "evidence_kind"),
            "reuse_guidance": [
                "Use source_file_name, page_label, evidence_kind, caption, context_text, and optional image_file_id together.",
                "Table/chart/diagram records may be caption-derived even when no embedded image file was recoverable.",
                "Treat visual_evidence as source metadata; cite source and page before using it as evidence.",
                "Image-capable model interpretation can be layered later using image_file_id values where present.",
            ],
            "generated_at": self._now(),
        }
        return (
            VISUAL_EVIDENCE_OUTPUT_KIND,
            f"{digestion.name or 'Digestion'} Visual Evidence",
            "application/json",
            json.dumps(payload, indent=2, sort_keys=True),
            {
                "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
                "visual_evidence_count": len(evidence),
                "evidence_kinds": payload["evidence_kinds"],
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
        policy = self._output_access_policy(output_kind, metadata)
        metadata = dict(metadata or {})
        metadata["source_revealing"] = bool(policy.get("source_revealing"))
        metadata["access_policy"] = policy
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

    @staticmethod
    def _output_access_policy(output_kind: str, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Describe how a generated output may be reused without exposing raw sources."""
        kind = str(output_kind or "").strip().lower()
        details = metadata if isinstance(metadata, dict) else {}
        source_revealing = kind in _SOURCE_REVEALING_OUTPUT_KINDS or bool(details.get("source_revealing"))
        policy: dict[str, Any] = {
            "schema_version": "canopy_digestion_output_policy_v1",
            "output_kind": kind,
            "source_revealing": source_revealing,
            "requires_source_metadata": source_revealing,
            "allowed_without_source_metadata": not source_revealing,
            "raw_source_withhold": True,
            "citation_required": source_revealing,
            "source_reveal_tier": "none",
            "sensitivity_label": "derived_guidance",
            "policy_decision_basis": "digestion_acl",
        }
        if kind == "manifest":
            policy.update({
                "source_reveal_tier": "source_metadata",
                "sensitivity_label": "source_manifest",
                "citation_required": False,
            })
        elif kind == "human_brief":
            policy.update({
                "source_reveal_tier": "source_summary",
                "sensitivity_label": "source_summary",
            })
        elif kind == STRUCTURED_DATAPOINT_OUTPUT_KIND:
            policy.update({
                "source_reveal_tier": "structured_source_facts",
                "sensitivity_label": "source_grounded_structured_data",
            })
        elif kind == STRUCTURED_RECORD_OUTPUT_KIND:
            policy.update({
                "source_reveal_tier": "profiled_source_facts",
                "sensitivity_label": "source_grounded_structured_records",
                "citation_required": True,
            })
        elif kind == PDF_FIGURE_OUTPUT_KIND:
            policy.update({
                "source_reveal_tier": "visual_source_derivative",
                "sensitivity_label": "source_derived_visuals",
                "visual_source_revealing": True,
            })
        elif kind == VISUAL_EVIDENCE_OUTPUT_KIND:
            policy.update({
                "source_reveal_tier": "visual_source_metadata",
                "sensitivity_label": "source_derived_visual_evidence",
                "visual_source_revealing": True,
            })
        elif kind == "agent_context":
            policy.update({
                "source_reveal_tier": "derived_context_only",
                "sensitivity_label": "agent_operating_context",
                "citation_required": True,
            })
        return policy

    def _output_row_to_dict(self, row: Any, *, include_content: bool = False) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        policy = metadata.get("access_policy")
        if not isinstance(policy, dict):
            policy = self._output_access_policy(str(row["output_kind"] or ""), metadata)
            metadata["source_revealing"] = bool(policy.get("source_revealing"))
            metadata["access_policy"] = policy
        content = str(row["content"] or "")
        data = {
            "id": str(row["id"] or ""),
            "digestion_id": str(row["digestion_id"] or ""),
            "output_kind": str(row["output_kind"] or ""),
            "title": str(row["title"] or ""),
            "content_type": str(row["content_type"] or "text/markdown"),
            "metadata": metadata,
            "access_policy": policy,
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

    def _rewrite_output_source_ids(self, conn: Any, digestion_id: str, source_id_map: dict[str, str], now: str) -> None:
        """Rewrite generated output snapshots after source files are re-homed."""
        clean_map = {
            str(old or "").strip(): str(new or "").strip()
            for old, new in (source_id_map or {}).items()
            if str(old or "").strip()
            and str(new or "").strip()
            and str(old or "").strip() != str(new or "").strip()
        }
        if not clean_map:
            return
        rows = conn.execute(
            "SELECT id, content, metadata_json FROM digestion_outputs WHERE digestion_id = ?",
            (digestion_id,),
        ).fetchall()
        for row in rows:
            content = str(row["content"] or "")
            metadata = str(row["metadata_json"] or "")
            rewritten_content = content
            rewritten_metadata = metadata
            for old_id, new_id in clean_map.items():
                rewritten_content = rewritten_content.replace(old_id, new_id)
                rewritten_metadata = rewritten_metadata.replace(old_id, new_id)
            if rewritten_content != content or rewritten_metadata != metadata:
                conn.execute(
                    """
                    UPDATE digestion_outputs
                    SET content = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (rewritten_content, rewritten_metadata, now, row["id"]),
                )

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

    def _normalize_operation_name(self, operation: Any) -> str:
        operation_clean = str(operation or "").strip().lower()
        if operation_clean not in CANCELLABLE_OPERATIONS:
            raise DigestionError("Unsupported Digestion operation.", status_code=400, reason="unsupported_digestion_operation")
        return operation_clean

    @staticmethod
    def _operation_stale_seconds() -> int:
        try:
            raw = int(os.getenv("CANOPY_DIGESTION_OPERATION_STALE_SECONDS", str(DEFAULT_OPERATION_STALE_SECONDS)) or DEFAULT_OPERATION_STALE_SECONDS)
        except Exception:
            raw = DEFAULT_OPERATION_STALE_SECONDS
        return max(300, min(raw, 24 * 60 * 60))

    def _operation_cancel_requested(self, digestion_id: str, operation: str) -> bool:
        key = (self._clean_id(digestion_id), str(operation or "").strip().lower())
        with self._progress_lock:
            return key in self._operation_cancel_requests

    def _raise_if_operation_cancelled(self, digestion_id: str, operation: str) -> None:
        if not self._operation_cancel_requested(digestion_id, operation):
            return
        raise DigestionError("Digestion operation cancelled by user.", status_code=409, reason="operation_cancelled")

    def _stale_operation_payload(self, payload: dict[str, Any], now: str) -> dict[str, Any]:
        if str(payload.get("status") or "").lower() != "running":
            return payload
        updated_at = str(payload.get("updated_at") or "")
        stale_after = self._operation_stale_seconds()
        silent_seconds = self._elapsed_seconds(updated_at, now) if updated_at else 0
        if silent_seconds < stale_after:
            return payload
        stale = dict(payload)
        details = dict(stale.get("details") or {})
        details.update({
            "stale": True,
            "recoverable": True,
            "stale_seconds": silent_seconds,
            "stale_after_seconds": stale_after,
        })
        stale["status"] = "stalled"
        stale["phase"] = str(stale.get("phase") or "stalled")
        stale["message"] = (
            "This operation has not reported progress recently. "
            "It may have been interrupted by a restart or provider timeout; reset it and rerun when ready."
        )
        stale["details"] = details
        stale["elapsed_seconds"] = self._elapsed_seconds(str(stale.get("started_at") or ""), now) if stale.get("started_at") else 0
        return stale

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
        actor_user_id: str = "",
    ) -> dict[str, Any]:
        digestion_id = self._clean_id(digestion_id)
        operation = str(operation or "").strip().lower() or "operation"
        now = self._now()
        bounded_percent = max(0, min(int(percent or 0), 100))
        with self._progress_lock:
            by_operation = self._operation_progress.setdefault(digestion_id, {})
            existing = dict(by_operation.get(operation) or {})
            next_status = str(status or "running").lower()
            existing_status = str(existing.get("status") or "").lower()
            if next_status == "running" and existing_status in {"completed", "failed", "cancelled", "idle"}:
                self._operation_cancel_requests.discard((digestion_id, operation))
            if next_status == "running" and (digestion_id, operation) in self._operation_cancel_requests:
                next_status = "cancelled"
                status = "cancelled"
                phase = phase or "cancel_requested"
                message = message or "Operation cancellation/reset was requested."
                next_details = dict(details if isinstance(details, dict) else dict(existing.get("details") or {}))
                next_details["cancel_requested"] = True
                details = next_details
            if next_status == "running" and existing_status in {"completed", "failed", "cancelled", "idle"}:
                started_at = now
            else:
                started_at = str(existing.get("started_at") or now)
            finished_at = now if next_status in {"completed", "failed", "cancelled"} else ""
            actor = self._clean_id(actor_user_id) or self._clean_id(existing.get("actor_user_id") or "")
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
                "finished_at": finished_at,
                "elapsed_seconds": self._elapsed_seconds(started_at, now),
                "details": details if isinstance(details, dict) else dict(existing.get("details") or {}),
                "actor_user_id": actor,
            }
            by_operation[operation] = payload
        self._persist_operation_progress(digestion_id, operation, payload)
        return dict(payload)

    def _persist_operation_progress(self, digestion_id: str, operation: str, payload: dict[str, Any]) -> None:
        """Persist the last progress snapshot so owners can audit agent work after completion."""
        try:
            actor_value = self._clean_id(payload.get("actor_user_id") or "") or None
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO digestion_operations (
                        digestion_id, operation, status, phase, percent, processed, total,
                        current_label, message, details_json, actor_user_id,
                        started_at, updated_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(digestion_id, operation) DO UPDATE SET
                        status = excluded.status,
                        phase = excluded.phase,
                        percent = excluded.percent,
                        processed = excluded.processed,
                        total = excluded.total,
                        current_label = excluded.current_label,
                        message = excluded.message,
                        details_json = excluded.details_json,
                        actor_user_id = COALESCE(excluded.actor_user_id, digestion_operations.actor_user_id),
                        started_at = excluded.started_at,
                        updated_at = excluded.updated_at,
                        finished_at = excluded.finished_at
                    """,
                    (
                        digestion_id,
                        operation,
                        str(payload.get("status") or "idle"),
                        str(payload.get("phase") or ""),
                        int(payload.get("percent") or 0),
                        int(payload.get("processed") or 0),
                        int(payload.get("total") or 0),
                        str(payload.get("current_label") or ""),
                        str(payload.get("message") or ""),
                        json.dumps(payload.get("details") or {}, sort_keys=True),
                        actor_value,
                        str(payload.get("started_at") or ""),
                        str(payload.get("updated_at") or self._now()),
                        str(payload.get("finished_at") or ""),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("Could not persist Digestion operation progress for %s/%s: %s", digestion_id, operation, exc)

    def _persisted_operation_progress(self, digestion_id: str) -> dict[str, dict[str, Any]]:
        operations: dict[str, dict[str, Any]] = {}
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT digestion_id, operation, status, phase, percent, processed, total,
                           current_label, message, details_json, actor_user_id,
                           started_at, updated_at, finished_at
                    FROM digestion_operations
                    WHERE digestion_id = ?
                    """,
                    (digestion_id,),
                ).fetchall()
        except Exception:
            rows = []
        now = self._now()
        for row in rows:
            operation = str(row["operation"] or "").strip().lower()
            if not operation:
                continue
            try:
                details = json.loads(row["details_json"] or "{}")
            except Exception:
                details = {}
            if not isinstance(details, dict):
                details = {}
            started_at = str(row["started_at"] or "")
            updated_at = str(row["updated_at"] or "")
            payload = {
                "operation": operation,
                "status": str(row["status"] or "idle"),
                "phase": str(row["phase"] or ""),
                "percent": max(0, min(int(row["percent"] or 0), 100)),
                "processed": max(0, int(row["processed"] or 0)),
                "total": max(0, int(row["total"] or 0)),
                "current_label": str(row["current_label"] or ""),
                "message": str(row["message"] or ""),
                "started_at": started_at,
                "updated_at": updated_at,
                "finished_at": str(row["finished_at"] or ""),
                "elapsed_seconds": self._elapsed_seconds(started_at, updated_at or now) if started_at else 0,
                "details": details,
                "actor_user_id": self._clean_id(row["actor_user_id"] or ""),
            }
            operations[operation] = self._stale_operation_payload(payload, now)
        return operations

    def _progress_snapshot(self, digestion_id: str, *, include_source_details: bool = True) -> dict[str, Any]:
        digestion_id = self._clean_id(digestion_id)
        operations = self._persisted_operation_progress(digestion_id)
        with self._progress_lock:
            operations.update({
                str(operation): dict(payload or {})
                for operation, payload in (self._operation_progress.get(digestion_id) or {}).items()
            })
        now = self._now()
        operations = {
            operation: self._stale_operation_payload(payload, now)
            for operation, payload in operations.items()
        }
        for operation in ("build", "datapoints", "structured_records", "figure_vision"):
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
        public["actor_user_id"] = ""
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
            "profile",
            "record_count",
            "new_or_updated_record_count",
            "updated_record_count",
            "analyzed_count",
            "eligible_count",
            "error_count",
            "pending_count",
            "previously_analyzed_count",
            "max_figures",
            "max_image_bytes",
            "max_output_tokens",
            "overwrite",
            "skipped_count",
            "all_failed",
            "stale",
            "recoverable",
            "stale_seconds",
            "stale_after_seconds",
            "cancel_requested",
            "checkpoint",
            "checkpointed_at",
            "checkpointed_datapoint_count",
            "existing_chunk_count",
            "remaining_chunk_count",
            "scoped_chunk_count",
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
        if status == "cancelled":
            return "Operation was reset/cancelled."
        if status == "stalled":
            return "Operation appears stalled; resume if possible, or reset before rerunning."
        if operation == "build":
            return f"Building Digestion source {processed + 1} of {total}." if total else "Building Digestion."
        if operation == "datapoints":
            if phase in {"llm_batch", "batch_normalized", "batch_error"} and total:
                return f"Extracting datapoints batch {min(processed + 1, total)} of {total}."
            return "Extracting structured datapoints."
        if operation == "structured_records":
            if total:
                return f"Updating structured record {min(processed + 1, total)} of {total}."
            return "Updating structured records."
        if operation == "figure_vision":
            if total:
                return f"Analyzing extracted figure {min(processed + 1, total)} of {total}."
            return "Analyzing extracted figures."
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
    # Evidence record helpers
    # ------------------------------------------------------------------
    def _normalize_evidence_record(
        self,
        digestion: Digestion,
        actor_user_id: str,
        item: dict[str, Any],
        *,
        index: int,
    ) -> Optional[dict[str, Any]]:
        statement = self._llm_scalar(
            item.get("statement")
            or item.get("claim")
            or item.get("finding")
            or item.get("conclusion")
            or item.get("title"),
            limit=1200,
        )
        summary = self._llm_scalar(
            item.get("summary")
            or item.get("rationale")
            or item.get("description")
            or item.get("notes")
            or item.get("body"),
            limit=4000,
        )
        if not statement and summary:
            statement = self._llm_scalar(summary, limit=320)
        if not statement:
            return None
        record_id = self._clean_id(item.get("id") or item.get("evidence_id"))
        if not record_id:
            record_id = "Er" + secrets.token_hex(12)
        record_kind = self._normalize_evidence_kind(item.get("record_kind") or item.get("kind") or item.get("type") or "finding")
        tags = self._normalize_evidence_tags(item.get("tags") or item.get("labels") or [])
        evidence_refs = self._normalize_evidence_refs(
            item.get("evidence_refs")
            or item.get("evidence")
            or item.get("citations")
            or item.get("references")
            or []
        )
        source_refs = self._normalize_evidence_refs(
            item.get("source_refs")
            or item.get("sources")
            or item.get("source")
            or []
        )
        related_ids = self._clean_id_list(
            item.get("related_ids")
            or item.get("related_evidence_ids")
            or item.get("related")
            or []
        )
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        metadata_clean = self._normalize_evidence_metadata(metadata)
        metadata_clean.setdefault("created_via", "digestion_evidence_append")
        metadata_clean.setdefault("source", "human_or_agent")
        metadata_clean["digestion_id"] = digestion.id
        metadata_clean["created_by_user_id"] = actor_user_id
        metadata_clean["input_index"] = index
        return {
            "id": record_id,
            "record_kind": record_kind,
            "statement": statement,
            "summary": summary,
            "scope": self._llm_scalar(item.get("scope") or item.get("lane") or item.get("domain"), limit=500),
            "status": self._normalize_evidence_status(item.get("status") or "", allow_empty=False),
            "priority": self._normalize_evidence_priority(item.get("priority") or item.get("severity") or ""),
            "confidence": self._normalize_confidence(item.get("confidence")),
            "tags": tags,
            "evidence_refs": evidence_refs,
            "source_refs": source_refs,
            "related_ids": related_ids,
            "metadata": metadata_clean,
            "superseded_by_id": self._clean_id(item.get("superseded_by_id") or item.get("superseded_by")),
        }

    def _evidence_records_by_ids(
        self,
        digestion_id: str,
        evidence_ids: Iterable[str],
        *,
        include_reviews: bool = True,
    ) -> list[dict[str, Any]]:
        ids = self._clean_id_list(evidence_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    e.*,
                    u.username AS created_by_username,
                    u.avatar_file_id AS created_by_avatar_file_id
                FROM digestion_evidence_records e
                LEFT JOIN users u ON u.id = e.created_by_user_id
                WHERE e.digestion_id = ? AND e.id IN ({placeholders})
                """,
                (self._clean_id(digestion_id), *ids),
            ).fetchall()
        by_id = {str(row["id"] or ""): self._evidence_row_to_dict(row, include_reviews=False) for row in rows}
        records = [by_id[item_id] for item_id in ids if item_id in by_id]
        if include_reviews and records:
            self._attach_evidence_reviews(digestion_id, records)
        return records

    def _attach_evidence_reviews(self, digestion_id: str, records: list[dict[str, Any]]) -> None:
        ids = [str(item.get("id") or "") for item in records if str(item.get("id") or "")]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    r.*,
                    u.username AS reviewer_username,
                    u.avatar_file_id AS reviewer_avatar_file_id
                FROM digestion_evidence_reviews r
                LEFT JOIN users u ON u.id = r.reviewer_user_id
                WHERE r.digestion_id = ? AND r.evidence_id IN ({placeholders})
                ORDER BY r.created_at ASC, r.id ASC
                """,
                (self._clean_id(digestion_id), *ids),
            ).fetchall()
        by_evidence: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_evidence.setdefault(str(row["evidence_id"] or ""), []).append(self._evidence_review_row_to_dict(row))
        for item in records:
            reviews = by_evidence.get(str(item.get("id") or ""), [])
            item["reviews"] = reviews
            item["review_summary"] = self._evidence_review_summary(reviews)

    def _evidence_status_counts(self, digestion_id: str) -> dict[str, int]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM digestion_evidence_records
                WHERE digestion_id = ?
                GROUP BY status
                """,
                (self._clean_id(digestion_id),),
            ).fetchall()
        return {str(row["status"] or "unknown"): int(row["count"] or 0) for row in rows}

    def _evidence_row_to_dict(self, row: Any, *, include_reviews: bool = False) -> dict[str, Any]:
        item = {
            "id": str(self._row_get(row, "id", "") or ""),
            "digestion_id": str(self._row_get(row, "digestion_id", "") or ""),
            "schema_version": DIGESTION_EVIDENCE_SCHEMA_VERSION,
            "record_kind": str(self._row_get(row, "record_kind", "finding") or "finding"),
            "statement": str(self._row_get(row, "statement", "") or ""),
            "summary": str(self._row_get(row, "summary", "") or ""),
            "scope": str(self._row_get(row, "scope", "") or ""),
            "status": str(self._row_get(row, "status", EVIDENCE_STATUS_CANDIDATE) or EVIDENCE_STATUS_CANDIDATE),
            "priority": str(self._row_get(row, "priority", "normal") or "normal"),
            "confidence": self._normalize_confidence(self._row_get(row, "confidence", None)),
            "tags": self._json_loads(self._row_get(row, "tags_json", "[]"), []),
            "evidence_refs": self._json_loads(self._row_get(row, "evidence_refs_json", "[]"), []),
            "source_refs": self._json_loads(self._row_get(row, "source_refs_json", "[]"), []),
            "related_ids": self._json_loads(self._row_get(row, "related_ids_json", "[]"), []),
            "metadata": self._json_loads(self._row_get(row, "metadata_json", "{}"), {}),
            "superseded_by_id": str(self._row_get(row, "superseded_by_id", "") or ""),
            "created_at": str(self._row_get(row, "created_at", "") or ""),
            "updated_at": str(self._row_get(row, "updated_at", "") or ""),
            "created_by": {
                "user_id": str(self._row_get(row, "created_by_user_id", "") or ""),
                "username": str(self._row_get(row, "created_by_username", "") or ""),
                "avatar_file_id": str(self._row_get(row, "created_by_avatar_file_id", "") or ""),
            },
        }
        for key in ("tags", "evidence_refs", "source_refs", "related_ids"):
            if not isinstance(item.get(key), list):
                item[key] = []
        if not isinstance(item.get("metadata"), dict):
            item["metadata"] = {}
        item["review_summary"] = self._evidence_review_summary([])
        if include_reviews:
            self._attach_evidence_reviews(item["digestion_id"], [item])
        return item

    def _evidence_review_row_to_dict(self, row: Any) -> dict[str, Any]:
        metadata = self._json_loads(self._row_get(row, "metadata_json", "{}"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        refs = self._json_loads(self._row_get(row, "evidence_refs_json", "[]"), [])
        return {
            "id": str(self._row_get(row, "id", "") or ""),
            "digestion_id": str(self._row_get(row, "digestion_id", "") or ""),
            "evidence_id": str(self._row_get(row, "evidence_id", "") or ""),
            "action": str(self._row_get(row, "action", "") or ""),
            "note": str(self._row_get(row, "note", "") or ""),
            "confidence": self._normalize_confidence(self._row_get(row, "confidence", None)),
            "evidence_refs": refs if isinstance(refs, list) else [],
            "metadata": metadata,
            "created_at": str(self._row_get(row, "created_at", "") or ""),
            "reviewer": {
                "user_id": str(self._row_get(row, "reviewer_user_id", "") or ""),
                "username": str(self._row_get(row, "reviewer_username", "") or ""),
                "avatar_file_id": str(self._row_get(row, "reviewer_avatar_file_id", "") or ""),
            },
        }

    @staticmethod
    def _evidence_review_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for review in reviews or []:
            action = str(review.get("action") or "unknown")
            counts[action] = counts.get(action, 0) + 1
        last_review = reviews[-1] if reviews else {}
        return {
            "review_count": len(reviews or []),
            "action_counts": counts,
            "support_count": counts.get("support", 0),
            "challenge_count": counts.get("challenge", 0),
            "confirm_count": counts.get("confirm", 0),
            "last_action": str(last_review.get("action") or ""),
            "last_review_at": str(last_review.get("created_at") or ""),
        }

    def _evidence_record_matches_query(self, item: dict[str, Any], query: str) -> bool:
        query_text = str(query or "").strip()
        if not query_text:
            return True
        haystack = " ".join(
            [
                str(item.get("statement") or ""),
                str(item.get("summary") or ""),
                str(item.get("scope") or ""),
                " ".join(str(value or "") for value in item.get("tags") or []),
                json.dumps(item.get("evidence_refs") or [], ensure_ascii=False),
                json.dumps(item.get("source_refs") or [], ensure_ascii=False),
                json.dumps(item.get("metadata") or {}, ensure_ascii=False),
            ]
        ).lower()
        query_lower = query_text.lower()
        if query_lower in haystack:
            return True
        terms = self._query_terms(query_text)
        if not terms:
            return False
        hay_terms = self._query_terms(haystack)
        return bool(terms & hay_terms)

    @staticmethod
    def _normalize_evidence_kind(value: Any) -> str:
        text = str(value or "finding").strip().lower().replace("-", "_")
        text = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
        return (text or "finding")[:80]

    @staticmethod
    def _normalize_evidence_priority(value: Any) -> str:
        text = str(value or "normal").strip().lower()
        if text in {"urgent", "blocker"}:
            text = "critical"
        if text in {"medium", "med"}:
            text = "normal"
        return text if text in EVIDENCE_PRIORITIES else "normal"

    @staticmethod
    def _normalize_evidence_status(value: Any, *, allow_empty: bool = False) -> str:
        text = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "": "",
            "needs_review": EVIDENCE_STATUS_CANDIDATE,
            "draft": EVIDENCE_STATUS_CANDIDATE,
            "accepted": EVIDENCE_STATUS_STABLE,
            "verified": EVIDENCE_STATUS_STABLE,
            "challenged": EVIDENCE_STATUS_CONTESTED,
            "needs_evidence": EVIDENCE_STATUS_NEEDS_SOURCE,
            "needs_citation": EVIDENCE_STATUS_NEEDS_SOURCE,
            "old": EVIDENCE_STATUS_STALE,
            "replaced": EVIDENCE_STATUS_SUPERSEDED,
        }
        text = aliases.get(text, text)
        if allow_empty and not text:
            return ""
        return text if text in EVIDENCE_STATUSES else EVIDENCE_STATUS_CANDIDATE

    @staticmethod
    def _normalize_evidence_review_action(value: Any) -> str:
        text = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "verify": "confirm",
            "verified": "confirm",
            "accept": "confirm",
            "accepted": "confirm",
            "dispute": "challenge",
            "challenged": "challenge",
            "stale": "mark_stale",
            "needs_source": "request_source",
            "source_needed": "request_source",
            "replace": "supersede",
            "superseded": "supersede",
        }
        text = aliases.get(text, text)
        if text not in EVIDENCE_REVIEW_ACTIONS:
            raise DigestionError("Invalid evidence review action.", status_code=400, reason="invalid_evidence_review_action")
        return text

    def _normalize_evidence_tags(self, value: Any) -> list[str]:
        tags = self._llm_string_list(value, limit=40, item_limit=80)
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            text = str(tag or "").strip().lower()
            text = re.sub(r"\s+", "-", text)
            text = re.sub(r"[^a-z0-9_\-:.]+", "", text)[:80]
            if text and text not in seen:
                seen.add(text)
                normalized.append(text)
        return normalized

    def _normalize_evidence_refs(self, value: Iterable[Any]) -> list[dict[str, Any]]:
        if isinstance(value, dict) or isinstance(value, str):
            values: Iterable[Any] = [value]
        elif isinstance(value, list) or isinstance(value, tuple):
            values = value
        else:
            values = []
        refs: list[dict[str, Any]] = []
        for raw in list(values)[:80]:
            ref = self._normalize_evidence_ref(raw)
            if ref:
                refs.append(ref)
        return refs

    def _normalize_evidence_ref(self, value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            return {"kind": "text_ref", "label": self._llm_scalar(text, limit=300), "text": self._llm_scalar(text, limit=1200)}
        if not isinstance(value, dict):
            return {}
        allowed = {
            "kind",
            "type",
            "label",
            "title",
            "file_id",
            "file_name",
            "source_file_id",
            "source_uri",
            "page_label",
            "page_number",
            "chunk_id",
            "chunk_index",
            "figure_id",
            "image_file_id",
            "datapoint_id",
            "structured_record_id",
            "output_ref",
            "contribution_id",
            "post_id",
            "message_id",
            "quote",
            "snippet",
            "note",
            "confidence",
        }
        ref: dict[str, Any] = {}
        for key, raw in value.items():
            key_text = str(key or "").strip()
            if key_text not in allowed:
                continue
            if key_text == "confidence":
                ref[key_text] = self._normalize_confidence(raw)
            elif key_text in {"page_number", "chunk_index"}:
                ref[key_text] = self._bounded_int(raw, 0, 0, 1_000_000)
            else:
                ref[key_text] = self._llm_scalar(raw, limit=1200 if key_text in {"quote", "snippet", "note"} else 300)
        if "kind" not in ref and "type" in ref:
            ref["kind"] = ref.pop("type")
        return {key: value for key, value in ref.items() if value not in ("", None)}

    def _normalize_evidence_metadata(self, value: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        for key, raw in (value or {}).items():
            key_text = str(key or "").strip()
            if not key_text:
                continue
            metadata[key_text[:80]] = self._llm_scalar(raw, limit=1200)
        return metadata

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

    def _attach_contribution_preview_sources(self, digestion_id: str, contributions: list[dict[str, Any]]) -> None:
        """Attach source-readable preview targets for contribution ledger rows.

        Contribution rows may retain the contributor's original file ids as well as
        owner-side material/source copies. The UI should preview the owner-bound
        corpus copy when it exists, because that is the file actually present in
        the Digestion intake folder and covered by Digestion source permissions.
        """
        candidate_order: dict[str, list[tuple[str, str]]] = {}
        file_ids: list[str] = []
        seen: set[str] = set()
        for item in contributions or []:
            row_id = str(item.get("id") or "")
            candidates: list[tuple[str, str]] = []
            for relationship, key in (
                ("added_source", "added_source_file_ids"),
                ("material", "material_file_ids"),
                ("referenced_source", "source_file_ids"),
            ):
                for file_id in self._clean_id_list(item.get(key) if isinstance(item.get(key), list) else []):
                    candidates.append((file_id, relationship))
                    if file_id not in seen:
                        seen.add(file_id)
                        file_ids.append(file_id)
            candidate_order[row_id] = candidates
        if not file_ids:
            return
        source_id_by_candidate = {file_id: file_id for file_id in file_ids}
        with self.db.get_connection() as conn:
            source_rows = conn.execute(
                """
                SELECT file_id, source_metadata_json
                FROM digestion_sources
                WHERE digestion_id = ?
                """,
                (self._clean_id(digestion_id),),
            ).fetchall()
            candidate_set = set(file_ids)
            for source_row in source_rows:
                source_file_id = str(source_row["file_id"] or "")
                if not source_file_id:
                    continue
                if source_file_id in candidate_set:
                    source_id_by_candidate[source_file_id] = source_file_id
                metadata = self._json_loads(source_row["source_metadata_json"], {})
                if not isinstance(metadata, dict):
                    metadata = {}
                for key in ("original_file_id", "vault_file_id", "source_file_id", "file_id"):
                    candidate_id = self._clean_id(metadata.get(key))
                    if candidate_id and candidate_id in candidate_set:
                        source_id_by_candidate[candidate_id] = source_file_id
            lookup_ids = self._clean_id_list([*file_ids, *source_id_by_candidate.values()])
            if not lookup_ids:
                return
            placeholders = ",".join("?" for _ in lookup_ids)
            rows = conn.execute(
                f"""
                SELECT
                    f.id AS file_id,
                    f.original_name AS original_name,
                    f.content_type AS file_content_type,
                    f.size AS file_size,
                    f.uploaded_by AS uploaded_by,
                    ds.file_name AS source_file_name,
                    ds.content_type AS source_content_type,
                    ds.source_kind AS source_kind,
                    ds.source_label AS source_label,
                    ds.source_uri AS source_uri,
                    ds.status AS source_status
                FROM files f
                LEFT JOIN digestion_sources ds
                  ON ds.digestion_id = ?
                 AND ds.file_id = f.id
                WHERE f.id IN ({placeholders})
                """,
                [self._clean_id(digestion_id), *lookup_ids],
            ).fetchall()
        info_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            file_id = str(row["file_id"] or "")
            if not file_id:
                continue
            source_name = str(row["source_file_name"] or row["original_name"] or file_id)
            content_type = str(row["source_content_type"] or row["file_content_type"] or "")
            info_by_id[file_id] = {
                "id": file_id,
                "file_id": file_id,
                "vault_file_id": file_id,
                "source_file_id": file_id,
                "preview_file_id": file_id,
                "file_name": source_name,
                "name": source_name,
                "filename": source_name,
                "original_name": source_name,
                "content_type": content_type,
                "type": content_type,
                "mime_type": content_type,
                "url": f"/files/{file_id}",
                "source_kind": str(row["source_kind"] or ""),
                "source_label": str(row["source_label"] or source_name),
                "source_uri": str(row["source_uri"] or ""),
                "source_status": str(row["source_status"] or ""),
                "size": int(row["file_size"] or 0),
                "uploaded_by": str(row["uploaded_by"] or ""),
                "in_digestion_sources": bool(str(row["source_status"] or "")),
            }
        for item in contributions or []:
            previews: list[dict[str, Any]] = []
            preview_seen: set[str] = set()
            for file_id, relationship in candidate_order.get(str(item.get("id") or ""), []):
                resolved_file_id = source_id_by_candidate.get(file_id, file_id)
                info = info_by_id.get(resolved_file_id)
                if not info or resolved_file_id in preview_seen:
                    continue
                preview = dict(info)
                preview["relationship"] = relationship
                preview["requested_file_id"] = file_id
                preview["resolved_from_owner_copy"] = resolved_file_id != file_id
                previews.append(preview)
                preview_seen.add(resolved_file_id)
            item["preview_sources"] = previews

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
        # Keep these generated files out of Vault Home so large Digestions do not
        # bury the user's ordinary files.
        intake_folder_id = self._digestion_intake_folder_id(digestion)
        if not intake_folder_id:
            raise DigestionError(
                "Could not create the owner's Digestion Intake folder.",
                status_code=500,
                reason="intake_folder_unavailable",
            )
        file_info = self.file_manager.save_file(
            normalized_text.encode("utf-8"),
            filename,
            content_type,
            digestion.owner_user_id,
            vault_folder_id=intake_folder_id,
        )
        if not file_info:
            raise DigestionError("Could not persist normalized material to Vault.", status_code=500, reason="material_vault_save_failed")
        material_meta = dict(header)
        material_meta.update({
            "vault_file_id": file_info.id,
            "vault_file_name": file_info.original_name,
            "owner_intake_folder_id": intake_folder_id,
            "owner_intake_folder": self._digestion_intake_folder_name(digestion),
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
