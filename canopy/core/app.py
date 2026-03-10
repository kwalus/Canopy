"""
Main Flask application for Canopy.

Creates and configures the Flask app with all necessary components
and routes for the local mesh communication system.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from flask import Flask, jsonify
from pathlib import Path
from typing import Any, Optional, cast

from .config import Config
from .database import DatabaseManager
from .logging_config import setup_logging
from .files import FileManager
from .interactions import InteractionManager
from .profile import ProfileManager
from .feed import FeedManager
from .tasks import TaskManager
from .search import SearchManager
from ..security.api_keys import ApiKeyManager
from ..security.trust import TrustManager
from .messaging import (
    Message,
    MessageManager,
    MessageStatus,
    MessageType,
    build_dm_preview,
    build_dm_security_summary,
    filter_local_dm_targets,
    is_local_dm_user,
    unwrap_dm_transport_bundle,
)
from .channels import ChannelManager
from .identity_portability import IdentityPortabilityManager
from .mentions import (
    MentionManager,
    extract_mentions,
    resolve_mention_targets,
    split_mention_targets,
    build_preview,
    record_mention_activity,
    broadcast_mention_interaction,
    sync_edited_mention_activity,
)
from .events import (
    EVENT_ATTACHMENT_AVAILABLE,
    EVENT_DM_MESSAGE_DELETED,
    WorkspaceEventManager,
)
from .large_attachments import (
    LARGE_ATTACHMENT_CAPABILITY,
    LARGE_ATTACHMENT_CHUNK_SIZE,
    LARGE_ATTACHMENT_DOWNLOAD_AUTO,
    LARGE_ATTACHMENT_DOWNLOAD_MANUAL,
    LARGE_ATTACHMENT_DOWNLOAD_PAUSED,
    get_attachment_origin_file_id,
    get_attachment_source_peer_id,
    get_large_attachment_download_mode,
    is_large_attachment_reference,
)
from ..network.manager import P2PNetworkManager
from ..network.routing import (
    decrypt_with_channel_key,
    decrypt_key_from_peer,
    encode_channel_key_material,
    decode_channel_key_material,
    encrypt_key_for_peer,
)
from ..security.file_access import evaluate_file_access_for_peer
from ..security.encryption import DataEncryptor
from ..api.routes import create_api_blueprint
from ..ui.routes import create_ui_blueprint

logger = logging.getLogger('canopy.app')


def _coerce_app_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            raw = str(value).strip()
            if not raw:
                return None
            try:
                dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except Exception:
                try:
                    dt = datetime.strptime(raw, '%Y-%m-%d %H:%M:%S.%f')
                except Exception:
                    dt = datetime.strptime(raw, '%Y-%m-%d %H:%M:%S')
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _finalize_inbound_dm_message(
    db_manager: Any,
    message_manager: MessageManager,
    msg: Optional[Message],
    canonical_message_id: str,
) -> bool:
    """Finalize an inbound DM so journal writes use the canonical stored ID."""
    if not msg:
        return False
    canonical_id = str(canonical_message_id or '').strip()
    if canonical_id and msg.id != canonical_id:
        try:
            with db_manager.get_connection() as conn:
                cur = conn.execute(
                    "UPDATE messages SET id = ? WHERE id = ?",
                    (canonical_id, msg.id),
                )
                conn.commit()
            if (cur.rowcount or 0) <= 0:
                return False
            msg.id = canonical_id
        except Exception:
            return False
    return bool(message_manager.send_message(msg))


def _apply_inbound_dm_delete(
    db_manager: Any,
    message_manager: MessageManager,
    inbox_manager: Any,
    message_id: str,
) -> bool:
    """Delete a materialized inbound DM and emit one local delete journal event."""
    message_id = str(message_id or '').strip()
    if not message_id:
        return False
    deleted_row = None
    try:
        with db_manager.get_connection() as conn:
            deleted_row = conn.execute(
                """
                SELECT id, sender_id, recipient_id, content, message_type,
                       created_at, delivered_at, read_at, edited_at, metadata
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            if not deleted_row:
                return False
            cur = conn.execute(
                "DELETE FROM messages WHERE id = ?",
                (message_id,),
            )
            deleted = (cur.rowcount or 0) > 0
            conn.commit()
    except Exception:
        raise

    if not deleted or not deleted_row:
        return False

    try:
        raw_content = deleted_row['content']
        if message_manager.data_encryptor and message_manager.data_encryptor.is_enabled:
            raw_content = message_manager.data_encryptor.decrypt(raw_content)
        metadata = json.loads(deleted_row['metadata']) if deleted_row['metadata'] else None
        deleted_message = Message(
            id=deleted_row['id'],
            sender_id=deleted_row['sender_id'],
            recipient_id=deleted_row['recipient_id'],
            content=raw_content,
            message_type=MessageType(deleted_row['message_type']),
            status=MessageStatus.READ if deleted_row['read_at'] else (
                MessageStatus.DELIVERED if deleted_row['delivered_at'] else MessageStatus.SENT
            ),
            created_at=_coerce_app_datetime(deleted_row['created_at']) or datetime.now(timezone.utc),
            metadata=metadata,
            delivered_at=_coerce_app_datetime(deleted_row['delivered_at']),
            read_at=_coerce_app_datetime(deleted_row['read_at']),
            edited_at=_coerce_app_datetime(deleted_row['edited_at']),
        )
        message_manager._emit_dm_event(
            event_type=EVENT_DM_MESSAGE_DELETED,
            message=deleted_message,
            dedupe_key=f"{EVENT_DM_MESSAGE_DELETED}:{message_id}",
        )
    except Exception as emit_err:
        logger.warning("Failed to emit inbound DM delete event for %s: %s", message_id, emit_err)

    if inbox_manager:
        try:
            inbox_manager.remove_source_triggers(
                source_type='dm',
                source_id=message_id,
                trigger_type='dm',
            )
        except Exception as inbox_err:
            logger.warning(
                "Failed to remove DM inbox triggers for deleted message %s: %s",
                message_id,
                inbox_err,
            )
    return True


def create_app(config: Optional[Config] = None) -> Flask:
    """Create and configure the Flask application."""
    
    # Load configuration first
    if config is None:
        config = Config.from_env()
    
    # Initialize comprehensive logging
    log_system = setup_logging(debug=config.debug)
    logger.info("Starting Canopy application initialization")
    
    # Create Flask app — static files live under canopy/ui/static/ (absolute path, cwd-independent)
    import os as _os
    _pkg_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    _ui_static = _os.path.join(_pkg_root, 'ui', 'static')
    app = Flask(__name__, static_folder=_ui_static)
    
    app.config['SECRET_KEY'] = config.secret_key
    app.config['DEBUG'] = config.debug
    app.config['TESTING'] = config.testing
    
    # Store config in app for access in routes
    app.config['CANOPY_CONFIG'] = config
    logger.info(f"Configuration loaded: Debug={config.debug}, Port={config.network.port}")
    
    # Initialize core components
    try:
        logger.info("Initializing database manager...")
        db_manager = DatabaseManager(config)
        app.config['DB_MANAGER'] = db_manager
        logger.info("Database manager initialized successfully")
        
        logger.info("Initializing API key manager...")
        api_key_manager = ApiKeyManager(db_manager)
        app.config['API_KEY_MANAGER'] = api_key_manager
        logger.info("API key manager initialized successfully")

        logger.info("Initializing workspace event manager...")
        workspace_event_manager = WorkspaceEventManager(db_manager)
        app.config['WORKSPACE_EVENT_MANAGER'] = workspace_event_manager
        logger.info("Workspace event manager initialized successfully")
        
        logger.info("Initializing trust manager...")
        trust_manager = TrustManager(db_manager)
        app.config['TRUST_MANAGER'] = trust_manager
        logger.info("Trust manager initialized successfully")
        
        logger.info("Initializing message manager...")
        message_manager = MessageManager(db_manager, api_key_manager)
        message_manager.workspace_events = workspace_event_manager
        app.config['MESSAGE_MANAGER'] = message_manager
        logger.info("Message manager initialized successfully")

        logger.info("Initializing file manager...")
        files_dir = str(Path(config.storage.data_dir) / 'files') if config.storage.data_dir else './data/files'
        file_manager = FileManager(db_manager, files_dir)
        app.config['FILE_MANAGER'] = file_manager
        logger.info("File manager initialized successfully")

        logger.info("Initializing interaction manager...")
        interaction_manager = InteractionManager(db_manager)
        app.config['INTERACTION_MANAGER'] = interaction_manager
        logger.info("Interaction manager initialized successfully")

        logger.info("Initializing profile manager...")
        profile_manager = ProfileManager(db_manager, file_manager)
        app.config['PROFILE_MANAGER'] = profile_manager
        logger.info("Profile manager initialized successfully")
        
        logger.info("Initializing channel manager...")
        channel_manager = ChannelManager(db_manager, api_key_manager)
        app.config['CHANNEL_MANAGER'] = channel_manager
        logger.info("Channel manager initialized successfully")

        logger.info("Initializing feed manager...")
        feed_manager = FeedManager(db_manager, api_key_manager)
        app.config['FEED_MANAGER'] = feed_manager
        logger.info("Feed manager initialized successfully")

        logger.info("Initializing task manager...")
        task_manager = TaskManager(db_manager)
        app.config['TASK_MANAGER'] = task_manager
        logger.info("Task manager initialized successfully")

        logger.info("Initializing request manager...")
        from .requests import RequestManager
        request_manager = RequestManager(db_manager)
        app.config['REQUEST_MANAGER'] = request_manager
        logger.info("Request manager initialized successfully")

        logger.info("Initializing objective manager...")
        from .objectives import ObjectiveManager
        objective_manager = ObjectiveManager(db_manager, task_manager=task_manager)
        app.config['OBJECTIVE_MANAGER'] = objective_manager
        logger.info("Objective manager initialized successfully")

        logger.info("Initializing signal manager...")
        from .signals import SignalManager
        signal_manager = SignalManager(db_manager)
        app.config['SIGNAL_MANAGER'] = signal_manager
        logger.info("Signal manager initialized successfully")

        logger.info("Initializing contract manager...")
        from .contracts import ContractManager
        contract_manager = ContractManager(db_manager)
        app.config['CONTRACT_MANAGER'] = contract_manager
        logger.info("Contract manager initialized successfully")

        logger.info("Initializing handoff manager...")
        from .handoffs import HandoffManager
        handoff_manager = HandoffManager(db_manager)
        app.config['HANDOFF_MANAGER'] = handoff_manager
        logger.info("Handoff manager initialized successfully")

        logger.info("Initializing circle manager...")
        from .circles import CircleManager
        circle_manager = CircleManager(db_manager, trust_manager=trust_manager, task_manager=task_manager)
        app.config['CIRCLE_MANAGER'] = circle_manager
        logger.info("Circle manager initialized successfully")

        logger.info("Initializing search manager...")
        search_manager = SearchManager(db_manager)
        app.config['SEARCH_MANAGER'] = search_manager
        logger.info(f"Search manager initialized (enabled={search_manager.enabled})")

        logger.info("Initializing skill manager...")
        from .skills import SkillManager
        skill_manager = SkillManager(db_manager)
        app.config['SKILL_MANAGER'] = skill_manager
        logger.info(f"Skill manager initialized ({skill_manager.count()} skills registered)")

        logger.info("Initializing mention manager...")
        mention_manager = MentionManager(db_manager)
        mention_manager.workspace_events = workspace_event_manager
        app.config['MENTION_MANAGER'] = mention_manager
        logger.info("Mention manager initialized successfully")

        logger.info("Initializing inbox manager...")
        from .inbox import InboxManager
        inbox_manager = InboxManager(db_manager, trust_manager=trust_manager)
        inbox_manager.workspace_events = workspace_event_manager
        app.config['INBOX_MANAGER'] = inbox_manager
        logger.info("Inbox manager initialized successfully")
        
        logger.info("Initializing P2P network manager...")
        p2p_manager = P2PNetworkManager(config, db_manager)
        # Relay policy priority: persisted file > env var > default
        # Manager __init__ already loads persisted policy.
        # Only override with config if env var was explicitly set.
        if os.getenv('CANOPY_RELAY_POLICY'):
            p2p_manager.set_relay_policy(config.network.relay_policy)
        app.config['P2P_MANAGER'] = p2p_manager
        # Safely get relay policy for logging
        relay_policy = getattr(p2p_manager, 'relay_policy', 'broker_only')
        logger.info(f"P2P network manager initialized (relay_policy={relay_policy})")

        _large_attachment_state_lock = threading.Lock()
        _incoming_large_attachment_states: dict[str, dict[str, Any]] = {}
        _large_attachment_temp_root = Path(config.storage.data_dir or './data') / 'tmp' / 'large_attachment_transfers'
        _large_attachment_temp_root.mkdir(parents=True, exist_ok=True)

        def _replace_large_attachment_references(
            source_peer_id: str,
            origin_file_id: str,
            local_file_id: str,
        ) -> int:
            """Rewrite remote large-attachment placeholders to local file metadata."""
            if not source_peer_id or not origin_file_id or not local_file_id:
                return 0
            local_file = file_manager.get_file(local_file_id)
            if not local_file:
                return 0

            replacement = {
                'id': local_file.id,
                'name': local_file.original_name,
                'type': local_file.content_type,
                'size': local_file.size,
                'url': local_file.url,
                'origin_file_id': origin_file_id,
                'source_peer_id': source_peer_id,
                'large_attachment': False,
                'storage_mode': 'local_cached',
                'download_status': 'completed',
                'checksum': local_file.checksum,
            }

            updated = 0
            available_dm_message_ids: list[str] = []

            def _maybe_replace_attachment(att: Any) -> tuple[Any, bool]:
                if not isinstance(att, dict):
                    return att, False
                att_origin = get_attachment_origin_file_id(att)
                att_source = get_attachment_source_peer_id(att)
                if att_origin == origin_file_id and att_source == source_peer_id:
                    return dict(replacement), True
                return att, False

            try:
                with db_manager.get_connection() as conn:
                    channel_rows = conn.execute(
                        "SELECT id, attachments FROM channel_messages WHERE attachments LIKE ?",
                        (f'%{origin_file_id}%',),
                    ).fetchall()
                    for row in channel_rows:
                        try:
                            attachments = json.loads(row['attachments'] or '[]')
                        except Exception:
                            continue
                        changed = False
                        normalized = []
                        for att in attachments if isinstance(attachments, list) else []:
                            new_att, did_change = _maybe_replace_attachment(att)
                            changed = changed or did_change
                            normalized.append(new_att)
                        if changed:
                            conn.execute(
                                "UPDATE channel_messages SET attachments = ? WHERE id = ?",
                                (json.dumps(normalized), row['id']),
                            )
                            updated += 1

                    feed_rows = conn.execute(
                        "SELECT id, metadata FROM feed_posts WHERE metadata LIKE ?",
                        (f'%{origin_file_id}%',),
                    ).fetchall()
                    for row in feed_rows:
                        try:
                            metadata = json.loads(row['metadata'] or '{}')
                        except Exception:
                            continue
                        attachments = (metadata or {}).get('attachments') or []
                        changed = False
                        normalized = []
                        for att in attachments if isinstance(attachments, list) else []:
                            new_att, did_change = _maybe_replace_attachment(att)
                            changed = changed or did_change
                            normalized.append(new_att)
                        if changed:
                            metadata = dict(metadata or {})
                            metadata['attachments'] = normalized
                            conn.execute(
                                "UPDATE feed_posts SET metadata = ? WHERE id = ?",
                                (json.dumps(metadata), row['id']),
                            )
                            updated += 1

                    dm_rows = conn.execute(
                        "SELECT id, metadata FROM messages WHERE metadata LIKE ?",
                        (f'%{origin_file_id}%',),
                    ).fetchall()
                    for row in dm_rows:
                        try:
                            metadata = json.loads(row['metadata'] or '{}')
                        except Exception:
                            continue
                        attachments = (metadata or {}).get('attachments') or []
                        changed = False
                        normalized = []
                        for att in attachments if isinstance(attachments, list) else []:
                            new_att, did_change = _maybe_replace_attachment(att)
                            changed = changed or did_change
                            normalized.append(new_att)
                        if changed:
                            metadata = dict(metadata or {})
                            metadata['attachments'] = normalized
                            conn.execute(
                                "UPDATE messages SET metadata = ? WHERE id = ?",
                                (json.dumps(metadata), row['id']),
                            )
                            updated += 1
                            available_dm_message_ids.append(str(row['id']))

                    conn.commit()
            except Exception as repl_err:
                logger.warning(
                    "Failed to rewrite large attachment references for %s/%s: %s",
                    source_peer_id,
                    origin_file_id,
                    repl_err,
                )
            if available_dm_message_ids and workspace_event_manager and local_file:
                for message_id in available_dm_message_ids:
                    workspace_event_manager.emit_event(
                        event_type=EVENT_ATTACHMENT_AVAILABLE,
                        actor_user_id=None,
                        target_user_id=None,
                        message_id=message_id,
                        visibility_scope='dm',
                        dedupe_key=f"{EVENT_ATTACHMENT_AVAILABLE}:dm:{message_id}:{local_file.id}",
                        payload={
                            'origin_file_id': origin_file_id,
                            'source_peer_id': source_peer_id,
                            'local_file_id': local_file.id,
                            'file_name': local_file.original_name,
                            'content_type': local_file.content_type,
                            'size': local_file.size,
                            'checksum': local_file.checksum,
                        },
                    )
            return updated

        def _request_remote_large_attachment(
            attachment: dict[str, Any],
            *,
            force: bool = False,
            source_context: Optional[dict[str, Any]] = None,
        ) -> bool:
            """Request a remote large attachment from its source peer."""
            source_peer_id = get_attachment_source_peer_id(attachment)
            origin_file_id = get_attachment_origin_file_id(attachment)
            if not source_peer_id or not origin_file_id or not p2p_manager:
                return False
            if not p2p_manager.peer_supports_capability(source_peer_id, LARGE_ATTACHMENT_CAPABILITY):
                return False
            if not p2p_manager.connection_manager or not p2p_manager.connection_manager.is_connected(source_peer_id):
                return False

            download_mode = get_large_attachment_download_mode(db_manager)
            if not force and download_mode != LARGE_ATTACHMENT_DOWNLOAD_AUTO:
                return False
            if force and download_mode == LARGE_ATTACHMENT_DOWNLOAD_PAUSED:
                return False

            transfer = file_manager.get_remote_attachment_transfer(source_peer_id, origin_file_id)
            local_file_id = str((transfer or {}).get('local_file_id') or '').strip()
            if local_file_id and file_manager.get_file(local_file_id):
                return True

            request_id = f"LAR{secrets.token_hex(8)}"
            file_manager.upsert_remote_attachment_transfer(
                origin_peer_id=source_peer_id,
                origin_file_id=origin_file_id,
                file_name=attachment.get('name'),
                content_type=attachment.get('type'),
                size=attachment.get('size'),
                checksum=attachment.get('checksum'),
                status='requested',
                last_request_id=request_id,
                error=None,
            )
            sent = p2p_manager.send_large_attachment_request(
                to_peer=source_peer_id,
                request_id=request_id,
                origin_file_id=origin_file_id,
                source_context=source_context or {},
            )
            if not sent:
                file_manager.upsert_remote_attachment_transfer(
                    origin_peer_id=source_peer_id,
                    origin_file_id=origin_file_id,
                    status='error',
                    error='request_send_failed',
                )
            return bool(sent)

        def _normalize_incoming_attachment_entry(
            attachment: Any,
            *,
            uploaded_by: str,
            default_source_peer_id: str,
            source_context: Optional[dict[str, Any]] = None,
        ) -> Optional[dict[str, Any]]:
            if not isinstance(attachment, dict):
                return None

            if attachment.get('data'):
                try:
                    import base64 as _b64_att
                    file_bytes = _b64_att.b64decode(attachment['data'])
                    finfo = file_manager.save_file(
                        file_data=file_bytes,
                        original_name=attachment.get('name', 'file'),
                        content_type=attachment.get('type', 'application/octet-stream'),
                        uploaded_by=uploaded_by,
                    )
                    if finfo:
                        return {
                            'id': finfo.id,
                            'name': finfo.original_name,
                            'type': finfo.content_type,
                            'size': finfo.size,
                            'url': finfo.url,
                            'checksum': finfo.checksum,
                        }
                except Exception as save_err:
                    logger.debug("Failed to save inline attachment: %s", save_err)

            if is_large_attachment_reference(attachment):
                source_peer_id = get_attachment_source_peer_id(attachment) or str(default_source_peer_id or '').strip()
                origin_file_id = get_attachment_origin_file_id(attachment) or str(attachment.get('id') or '').strip()
                checksum = str(attachment.get('checksum') or '').strip()
                if source_peer_id and origin_file_id:
                    transfer = file_manager.get_remote_attachment_transfer(source_peer_id, origin_file_id)
                    local_file_id = str((transfer or {}).get('local_file_id') or '').strip()
                    if local_file_id:
                        finfo = file_manager.get_file(local_file_id)
                        if finfo:
                            return {
                                'id': finfo.id,
                                'name': finfo.original_name,
                                'type': finfo.content_type,
                                'size': finfo.size,
                                'url': finfo.url,
                                'checksum': finfo.checksum,
                                'origin_file_id': origin_file_id,
                                'source_peer_id': source_peer_id,
                                'storage_mode': 'local_cached',
                                'download_status': 'completed',
                            }

                    file_manager.upsert_remote_attachment_transfer(
                        origin_peer_id=source_peer_id,
                        origin_file_id=origin_file_id,
                        file_name=attachment.get('name'),
                        content_type=attachment.get('type'),
                        size=attachment.get('size'),
                        checksum=checksum or None,
                        status='pending',
                        error=None,
                    )
                    normalized = {
                        'name': attachment.get('name', 'file'),
                        'type': attachment.get('type', 'application/octet-stream'),
                        'size': attachment.get('size', 0),
                        'checksum': checksum,
                        'origin_file_id': origin_file_id,
                        'source_peer_id': source_peer_id,
                        'large_attachment': True,
                        'storage_mode': 'remote_large',
                        'download_status': str((transfer or {}).get('status') or 'pending').strip().lower() or 'pending',
                    }
                    if get_large_attachment_download_mode(db_manager) == LARGE_ATTACHMENT_DOWNLOAD_AUTO:
                        _request_remote_large_attachment(
                            normalized,
                            force=False,
                            source_context=source_context,
                        )
                    return normalized

            return {k: v for k, v in attachment.items() if k != 'data' and k != 'url'}

        def _on_large_attachment_request(
            request_id: Optional[str],
            origin_file_id: Optional[str],
            requester_peer: Optional[str],
            source_context: Optional[dict[str, Any]],
            from_peer: str,
        ) -> None:
            req_id = str(request_id or '').strip()
            file_id = str(origin_file_id or '').strip()
            requester = str(from_peer or requester_peer or '').strip()
            if not req_id or not file_id or not requester:
                return
            if requester_peer and str(requester_peer).strip() and str(requester_peer).strip() != requester:
                logger.warning(
                    "Ignoring mismatched large attachment requester for %s: claimed=%s actual=%s",
                    file_id,
                    requester_peer,
                    requester,
                )

            file_info = file_manager.get_file(file_id)
            if not file_info:
                p2p_manager.send_large_attachment_error(
                    to_peer=requester,
                    request_id=req_id,
                    origin_file_id=file_id,
                    error='file_not_found',
                )
                return

            access = evaluate_file_access_for_peer(
                db_manager=db_manager,
                file_id=file_id,
                requester_peer_id=requester,
                file_uploaded_by=file_info.uploaded_by,
            )
            if not access.allowed:
                p2p_manager.send_large_attachment_error(
                    to_peer=requester,
                    request_id=req_id,
                    origin_file_id=file_id,
                    error=f"access_denied:{access.reason}",
                )
                return

            result = file_manager.get_file_data(file_id)
            if not result:
                p2p_manager.send_large_attachment_error(
                    to_peer=requester,
                    request_id=req_id,
                    origin_file_id=file_id,
                    error='file_data_unavailable',
                )
                return
            file_data, resolved_info = result

            def _worker() -> None:
                import base64 as _b64_tx
                total_chunks = max(1, (len(file_data) + LARGE_ATTACHMENT_CHUNK_SIZE - 1) // LARGE_ATTACHMENT_CHUNK_SIZE)
                for chunk_index in range(total_chunks):
                    start = chunk_index * LARGE_ATTACHMENT_CHUNK_SIZE
                    end = min(len(file_data), start + LARGE_ATTACHMENT_CHUNK_SIZE)
                    data_b64 = _b64_tx.b64encode(file_data[start:end]).decode('ascii')
                    sent = p2p_manager.send_large_attachment_chunk(
                        to_peer=requester,
                        request_id=req_id,
                        origin_file_id=file_id,
                        file_name=resolved_info.original_name,
                        content_type=resolved_info.content_type,
                        checksum=resolved_info.checksum,
                        size=resolved_info.size,
                        uploaded_by=resolved_info.uploaded_by,
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                        data_b64=data_b64,
                    )
                    if not sent:
                        logger.warning(
                            "Failed to send large attachment chunk %s/%s for %s to %s",
                            chunk_index + 1,
                            total_chunks,
                            file_id,
                            requester,
                        )
                        break

            threading.Thread(
                target=_worker,
                name=f"canopy-large-attachment-send-{file_id[:8]}",
                daemon=True,
            ).start()

        def _finalize_incoming_large_attachment(state: dict[str, Any]) -> None:
            request_id = str(state.get('request_id') or '').strip()
            origin_file_id = str(state.get('origin_file_id') or '').strip()
            source_peer_id = str(state.get('source_peer_id') or '').strip()
            tmp_path = Path(state.get('tmp_path'))
            if not tmp_path.exists():
                file_manager.upsert_remote_attachment_transfer(
                    origin_peer_id=source_peer_id,
                    origin_file_id=origin_file_id,
                    status='error',
                    error='missing_temp_file',
                    last_request_id=request_id or None,
                )
                return

            try:
                file_bytes = tmp_path.read_bytes()
                checksum = file_manager._calculate_checksum(file_bytes)
                expected_checksum = str(state.get('checksum') or '').strip()
                if expected_checksum and checksum != expected_checksum:
                    raise ValueError('checksum_mismatch')
                uploaded_by = str(state.get('uploaded_by') or '').strip()
                if not uploaded_by:
                    uploaded_by = str(db_manager.get_instance_owner_user_id() or '').strip()
                if not uploaded_by:
                    with db_manager.get_connection() as conn:
                        fallback_row = conn.execute(
                            "SELECT id FROM users WHERE id != 'system' ORDER BY created_at ASC LIMIT 1"
                        ).fetchone()
                    uploaded_by = str((fallback_row['id'] if fallback_row and hasattr(fallback_row, 'keys') else fallback_row[0]) or '').strip() if fallback_row else ''
                if not uploaded_by:
                    raise ValueError('uploaded_by_unavailable')
                finfo = file_manager.save_file(
                    file_data=file_bytes,
                    original_name=str(state.get('file_name') or 'attachment'),
                    content_type=str(state.get('content_type') or 'application/octet-stream'),
                    uploaded_by=uploaded_by,
                )
                if not finfo:
                    raise ValueError('save_failed')
                file_manager.upsert_remote_attachment_transfer(
                    origin_peer_id=source_peer_id,
                    origin_file_id=origin_file_id,
                    local_file_id=finfo.id,
                    file_name=finfo.original_name,
                    content_type=finfo.content_type,
                    size=finfo.size,
                    checksum=finfo.checksum,
                    status='completed',
                    last_request_id=request_id or None,
                    error=None,
                )
                _replace_large_attachment_references(source_peer_id, origin_file_id, finfo.id)
            except Exception as finalize_err:
                file_manager.upsert_remote_attachment_transfer(
                    origin_peer_id=source_peer_id,
                    origin_file_id=origin_file_id,
                    status='error',
                    last_request_id=request_id or None,
                    error=str(finalize_err),
                )
                logger.warning(
                    "Failed to finalize large attachment %s from %s: %s",
                    origin_file_id,
                    source_peer_id,
                    finalize_err,
                )
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        def _on_large_attachment_chunk(
            request_id: Optional[str],
            origin_file_id: Optional[str],
            file_name: Optional[str],
            content_type: Optional[str],
            checksum: Optional[str],
            size: Optional[int],
            uploaded_by: Optional[str],
            chunk_index: Optional[int],
            total_chunks: Optional[int],
            data_b64: Optional[str],
            source_peer_id: Optional[str],
            from_peer: str,
        ) -> None:
            req_id = str(request_id or '').strip()
            origin_id = str(origin_file_id or '').strip()
            source_peer = str(source_peer_id or from_peer or '').strip()
            if not req_id or not origin_id or data_b64 is None or not source_peer:
                return

            try:
                idx = int(chunk_index or 0)
                total = max(1, int(total_chunks or 1))
            except Exception:
                return

            import base64 as _b64_rx
            try:
                chunk_bytes = _b64_rx.b64decode(data_b64)
            except Exception:
                file_manager.upsert_remote_attachment_transfer(
                    origin_peer_id=source_peer,
                    origin_file_id=origin_id,
                    status='error',
                    last_request_id=req_id,
                    error='chunk_decode_failed',
                )
                return

            finalize_state: Optional[dict[str, Any]] = None
            with _large_attachment_state_lock:
                state = _incoming_large_attachment_states.get(req_id)
                if state is None:
                    temp_path = _large_attachment_temp_root / f"{req_id}.part"
                    state = {
                        'request_id': req_id,
                        'origin_file_id': origin_id,
                        'source_peer_id': source_peer,
                        'file_name': file_name or 'attachment',
                        'content_type': content_type or 'application/octet-stream',
                        'checksum': checksum or '',
                        'size': int(size or 0),
                        'uploaded_by': str(uploaded_by or '').strip() or None,
                        'total_chunks': total,
                        'next_index': 0,
                        'pending_chunks': {},
                        'tmp_path': str(temp_path),
                    }
                    _incoming_large_attachment_states[req_id] = state

                if idx in state['pending_chunks'] or idx < int(state.get('next_index', 0) or 0):
                    return

                state['pending_chunks'][idx] = chunk_bytes
                temp_path = Path(state['tmp_path'])
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                with temp_path.open('ab') as handle:
                    while state['next_index'] in state['pending_chunks']:
                        next_chunk = state['pending_chunks'].pop(state['next_index'])
                        handle.write(next_chunk)
                        state['next_index'] += 1

                if state['next_index'] >= state['total_chunks']:
                    finalize_state = dict(state)
                    _incoming_large_attachment_states.pop(req_id, None)

            if finalize_state:
                _finalize_incoming_large_attachment(finalize_state)

        def _on_large_attachment_error(
            request_id: Optional[str],
            origin_file_id: Optional[str],
            error: Optional[str],
            source_peer_id: Optional[str],
            from_peer: str,
        ) -> None:
            source_peer = str(source_peer_id or from_peer or '').strip()
            origin_id = str(origin_file_id or '').strip()
            req_id = str(request_id or '').strip()
            if not source_peer or not origin_id:
                return
            file_manager.upsert_remote_attachment_transfer(
                origin_peer_id=source_peer,
                origin_file_id=origin_id,
                status='error',
                last_request_id=req_id or None,
                error=str(error or 'transfer_failed'),
            )

        def _retry_pending_large_attachments_for_peer(peer_id: str) -> None:
            if not peer_id:
                return
            if get_large_attachment_download_mode(db_manager) != LARGE_ATTACHMENT_DOWNLOAD_AUTO:
                return
            for transfer in file_manager.list_pending_remote_attachment_transfers(
                origin_peer_id=peer_id,
                statuses=['pending', 'error'],
                limit=500,
            ):
                attachment = {
                    'origin_file_id': transfer.get('origin_file_id'),
                    'source_peer_id': transfer.get('origin_peer_id'),
                    'name': transfer.get('file_name'),
                    'type': transfer.get('content_type'),
                    'size': transfer.get('size'),
                    'checksum': transfer.get('checksum'),
                    'large_attachment': True,
                    'storage_mode': 'remote_large',
                }
                _request_remote_large_attachment(attachment, force=True, source_context={'retry': True})

        p2p_manager.on_large_attachment_request = _on_large_attachment_request
        p2p_manager.on_large_attachment_chunk = _on_large_attachment_chunk
        p2p_manager.on_large_attachment_error = _on_large_attachment_error

        identity_portability_manager = IdentityPortabilityManager(
            db_manager=db_manager,
            config=config,
            p2p_manager=p2p_manager,
        )
        app.config['IDENTITY_PORTABILITY_MANAGER'] = identity_portability_manager
        if identity_portability_manager.enabled:
            logger.info("Identity portability manager initialized (enabled)")
        else:
            logger.info("Identity portability manager initialized (disabled)")

        # Allow P2P manager to fetch device profiles for peer announcements
        p2p_manager.get_peer_device_profile = channel_manager.get_peer_device_profile

        def _resolve_local_mentions(handles: list[str],
                                    channel_id: Optional[str] = None,
                                    visibility: Optional[str] = None,
                                    permissions: Optional[list[str]] = None,
                                    author_id: Optional[str] = None) -> list[dict]:
            if not handles:
                return []
            targets = resolve_mention_targets(
                db_manager,
                handles,
                channel_id=channel_id,
                visibility=visibility,
                permissions=permissions,
                author_id=author_id,
            )
            local_peer_id = None
            try:
                if p2p_manager:
                    local_peer_id = p2p_manager.get_peer_id()
            except Exception:
                local_peer_id = None
            local_targets, _ = split_mention_targets(targets, local_peer_id=local_peer_id)
            return local_targets

        def _record_local_mention_events(targets: list[dict],
                                         source_type: str,
                                         source_id: str,
                                         author_id: str,
                                         from_peer: str,
                                         channel_id: Optional[str] = None,
                                         preview: Optional[str] = None,
                                         source_content: Optional[str] = None) -> None:
            if not targets:
                return
            target_ids = cast(list[str], [t.get('user_id') for t in targets if t.get('user_id')])
            if not target_ids:
                return
            record_mention_activity(
                mention_manager,
                p2p_manager,
                target_ids=target_ids,
                source_type=source_type,
                source_id=source_id,
                author_id=author_id,
                origin_peer=from_peer,
                channel_id=channel_id,
                preview=preview or '',
                extra_ref=None,
                inbox_manager=inbox_manager,
                source_content=source_content,
            )

        def _notify_channel_added(user_id: str, channel_id: str,
                                   channel_name: str, added_by: Optional[str] = None,
                                   origin_peer: Optional[str] = None) -> None:
            """Fire inbox + mention_event notifications when a user is added to a channel."""
            if not user_id or not channel_id:
                return
            import secrets as _sec
            source_id = f"channel_add_{channel_id}_{user_id}_{_sec.token_hex(4)}"
            preview = f"You were added to #{channel_name or channel_id[:12]}"
            if added_by:
                try:
                    with db_manager.get_connection() as conn:
                        adder = conn.execute(
                            "SELECT display_name, username FROM users WHERE id = ?",
                            (added_by,),
                        ).fetchone()
                    if adder:
                        adder_name = (adder['display_name'] or adder['username'] or added_by[:12])
                        preview = f"{adder_name} added you to #{channel_name or channel_id[:12]}"
                except Exception:
                    pass

            if mention_manager:
                try:
                    mention_manager.record_mentions(
                        user_ids=[user_id],
                        source_type='channel_added',
                        source_id=source_id,
                        author_id=added_by,
                        origin_peer=origin_peer,
                        channel_id=channel_id,
                        preview=preview,
                    )
                except Exception as e:
                    logger.debug(f"Channel-added mention_event failed: {e}")

            if inbox_manager:
                try:
                    inbox_manager.record_mention_triggers(
                        target_ids=[user_id],
                        source_type='channel_added',
                        source_id=source_id,
                        author_id=added_by,
                        origin_peer=origin_peer,
                        channel_id=channel_id,
                        preview=preview,
                        trigger_type='channel_added',
                    )
                except Exception as e:
                    logger.debug(f"Channel-added inbox trigger failed: {e}")

            if p2p_manager:
                try:
                    p2p_manager.record_activity_event({
                        'id': f"ch_add:{source_id}",
                        'peer_id': origin_peer or '',
                        'kind': 'channel_added',
                        'timestamp': __import__('time').time(),
                        'preview': preview,
                        'ref': {
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                            'user_id': user_id,
                            'added_by': added_by,
                        },
                    })
                except Exception:
                    pass

        def _ensure_origin_peer(
            user_id: str,
            peer_id: str,
            *,
            allow_remote_reassign: bool = False,
        ) -> None:
            """Set or update origin_peer for remote-shadow users.

            Passive sync surfaces such as profile propagation should not
            rewrite remote ownership, because those packets can arrive via
            relays or stale peers. Only direct authored evidence (DMs,
            posts, channel messages, catchup rows carrying an explicit
            origin) is allowed to reassign an existing remote origin.
            """
            if not user_id or not peer_id:
                return
            try:
                local_peer = p2p_manager.get_peer_id() if p2p_manager else None
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT origin_peer FROM users WHERE id = ?",
                        (user_id,)
                    ).fetchone()
                    if not row:
                        return
                    current_peer = (row['origin_peer'] if 'origin_peer' in row.keys() else None) or ''
                    if current_peer == peer_id:
                        return
                    if current_peer == local_peer and (
                        not allow_remote_reassign
                        or is_local_dm_user(db_manager, p2p_manager, user_id)
                    ):
                        return
                    if not current_peer and is_local_dm_user(db_manager, p2p_manager, user_id):
                        return
                    if current_peer and not allow_remote_reassign:
                        return
                    conn.execute(
                        "UPDATE users SET origin_peer = ? WHERE id = ?",
                        (peer_id, user_id)
                    )
                    conn.commit()
                    logger.info(f"Updated origin_peer for {user_id}: {current_peer} -> {peer_id}")
            except Exception:
                pass

        def _sanitize_shadow_username(name: str) -> str:
            """Make a safe username token for shadow users."""
            import re
            base = (name or '').strip()
            if not base:
                return 'peer'
            base = re.sub(r'[^A-Za-z0-9_.-]+', '_', base)
            base = base.strip('._-')
            return base or 'peer'

        def _ensure_shadow_user(
            user_id: str,
            display_name: Optional[str],
            from_peer: str,
            *,
            allow_origin_reassign: bool = False,
        ) -> None:
            """Create/update a shadow user safely, avoiding username collisions."""
            if not user_id or not from_peer:
                return
            try:
                existing = db_manager.get_user(user_id)
                shadow_display = display_name or f"peer-{from_peer[:8]}"

                if existing:
                    try:
                        current_display = existing.get('display_name', '')
                        if display_name and (current_display.startswith('peer-') or current_display == user_id):
                            with db_manager.get_connection() as conn:
                                conn.execute(
                                    "UPDATE users SET display_name = ? WHERE id = ?",
                                    (display_name, user_id)
                                )
                                conn.commit()
                    except Exception:
                        pass
                    _ensure_origin_peer(
                        user_id,
                        from_peer,
                        allow_remote_reassign=allow_origin_reassign,
                    )
                    return

                base = _sanitize_shadow_username(display_name or f"peer-{from_peer[:8]}")
                candidates = [
                    f"{base}.{from_peer[:6]}",
                    f"{base}-{user_id[-6:]}",
                    f"peer-{user_id[-12:]}",
                    user_id,
                ]

                created = False
                for cand in candidates:
                    cand = cand.strip()
                    if not cand:
                        continue
                    try:
                        if db_manager.get_user_by_username(cand):
                            continue
                        if db_manager.create_user(
                            user_id=user_id,
                            username=cand,
                            public_key='',
                            password_hash=None,
                            display_name=shadow_display,
                            origin_peer=from_peer
                        ):
                            created = True
                            logger.info(
                                f"Created shadow user {user_id} (username={cand}, display_name={shadow_display})"
                            )
                            break
                    except Exception:
                        continue

                if not created:
                    try:
                        db_manager.create_user(
                            user_id=user_id,
                            username=user_id,
                            public_key='',
                            password_hash=None,
                            display_name=shadow_display,
                            origin_peer=from_peer
                        )
                    except Exception as e:
                        logger.warning(f"Could not create shadow user for {user_id}: {e}")

                _ensure_origin_peer(
                    user_id,
                    from_peer,
                    allow_remote_reassign=allow_origin_reassign,
                )
            except Exception as e:
                logger.warning(f"Shadow user ensure failed for {user_id}: {e}")

        def _resolve_incoming_channel_content(
            channel_id: str,
            from_peer: str,
            content: str,
            encrypted_content: Optional[str],
            crypto_state: Optional[str],
            key_id: Optional[str],
            nonce: Optional[str],
        ) -> tuple[str, str, Optional[str], Optional[str], Optional[str], bool]:
            """Resolve/decrypt incoming channel content for E2E private channels.

            Returns:
                (content_out, crypto_state_out, encrypted_content_out, key_id_out, nonce_out, key_missing)
            """
            state = str(crypto_state or '').strip().lower()
            if state != 'encrypted' and encrypted_content and key_id and nonce:
                state = 'encrypted'
            if state != 'encrypted' or not encrypted_content or not key_id or not nonce:
                return (content or '', 'plaintext', None, None, None, False)

            key_bytes = channel_manager.get_channel_key_bytes(channel_id, key_id)
            if not key_bytes:
                # Ask the channel origin peer for key re-send (best effort).
                try:
                    with db_manager.get_connection() as conn:
                        row = conn.execute(
                            "SELECT origin_peer FROM channels WHERE id = ?",
                            (channel_id,),
                        ).fetchone()
                    origin_peer = row['origin_peer'] if row and 'origin_peer' in row.keys() else None
                    if origin_peer and origin_peer != (p2p_manager.get_peer_id() if p2p_manager else None):
                        p2p_manager.send_channel_key_request(
                            to_peer=origin_peer,
                            channel_id=channel_id,
                            reason='missing_key_for_decrypt',
                            key_id=key_id,
                        )
                except Exception:
                    pass
                return ('', 'pending_decrypt', encrypted_content, key_id, nonce, True)

            try:
                plaintext = decrypt_with_channel_key(
                    encrypted_content_b64=encrypted_content,
                    key_material=key_bytes,
                    nonce_b64=nonce,
                )
                return (plaintext, 'decrypted', encrypted_content, key_id, nonce, False)
            except Exception as dec_err:
                logger.warning(
                    "Failed to decrypt channel message for %s key=%s from %s: %s",
                    channel_id,
                    key_id,
                    from_peer,
                    dec_err,
                )
                return ('', 'decrypt_failed', encrypted_content, key_id, nonce, False)
        
        # Wire up P2P <-> channel sync: incoming P2P channel messages
        # get stored in the local channel DB so they appear in the UI.
        def _on_p2p_channel_message(channel_id: str, user_id: str, content: str,
                                     message_id: str, timestamp: str, from_peer: str,
                                     attachments: Optional[list[Any]] = None, security: Optional[dict[str, Any]] = None,
                                     message_type: str = 'text',
                                     display_name: Optional[str] = None, expires_at: Optional[str] = None,
                                     ttl_seconds: Optional[int] = None, ttl_mode: Optional[str] = None,
                                     update_only: bool = False,
                                     origin_peer: Optional[str] = None,
                                     parent_message_id: Optional[str] = None,
                                     edited_at: Optional[str] = None,
                                     encrypted_content: Optional[str] = None,
                                     crypto_state: Optional[str] = None,
                                     key_id: Optional[str] = None,
                                     nonce: Optional[str] = None) -> None:
            """Store an incoming P2P channel message locally.
            
            If attachments with embedded 'data' (base64) are present,
            decode and save them via FileManager so the UI can render
            images and files natively.
            """
            try:
                existing_msg = None
                if message_id:
                    try:
                        with db_manager.get_connection() as conn:
                            existing_msg = conn.execute(
                                "SELECT user_id FROM channel_messages WHERE id = ?",
                                (message_id,)
                            ).fetchone()
                    except Exception:
                        existing_msg = None

                # --- Persistent dedup check ---
                if message_id and channel_manager.is_message_processed(message_id) and not update_only:
                    logger.debug(f"Skipping duplicate P2P message {message_id}")
                    # Even for already-stored messages, re-derive inbox items for
                    # local agent users that were mentioned but whose inbox item
                    # was lost (e.g. due to rate-limiting or the bot being offline
                    # when the message was first processed).
                    # Skip for private channels with no local members.
                    _dedup_skip = False
                    if inbox_manager and content:
                        try:
                            with db_manager.get_connection() as conn:
                                _ch = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)).fetchone()
                                if _ch and (_ch['privacy_mode'] or 'open').lower() in ('private', 'confidential'):
                                    _lp = p2p_manager.get_peer_id() if p2p_manager else None
                                    if not conn.execute(
                                        "SELECT 1 FROM channel_members cm "
                                        "JOIN users u ON cm.user_id = u.id "
                                        "WHERE cm.channel_id = ? "
                                        "AND (u.origin_peer IS NULL OR u.origin_peer = '' "
                                        "     OR u.origin_peer = ?) LIMIT 1",
                                        (channel_id, _lp or ''),
                                    ).fetchone():
                                        _dedup_skip = True
                        except Exception:
                            pass
                    if inbox_manager and content and not _dedup_skip:
                        try:
                            mentions = extract_mentions(content)
                            if mentions:
                                targets = _resolve_local_mentions(
                                    mentions,
                                    channel_id=channel_id,
                                    author_id=user_id,
                                )
                                for t in targets:
                                    tid = t.get('user_id')
                                    if not tid:
                                        continue
                                    # Only patch agent accounts — avoid spamming human inboxes
                                    try:
                                        with db_manager.get_connection() as conn:
                                            urow = conn.execute(
                                                "SELECT account_type FROM users WHERE id = ?",
                                                (tid,)
                                            ).fetchone()
                                        if not urow or (urow[0] or '').lower() != 'agent':
                                            continue
                                    except Exception:
                                        continue
                                    mid_check = message_id
                                    try:
                                        with db_manager.get_connection() as conn:
                                            has_item = conn.execute(
                                                "SELECT 1 FROM agent_inbox WHERE agent_user_id = ? "
                                                "AND source_id = ? AND trigger_type = 'mention'",
                                                (tid, mid_check)
                                            ).fetchone()
                                    except Exception:
                                        has_item = None
                                    if not has_item:
                                        inbox_manager.record_mention_triggers(
                                            target_ids=[tid],
                                            source_type='channel_message',
                                            source_id=mid_check,
                                            author_id=user_id,
                                            origin_peer=from_peer,
                                            channel_id=channel_id,
                                            preview=build_preview(content),
                                            source_content=content,
                                        )
                        except Exception as _patch_err:
                            logger.debug(f"Duplicate-message inbox patch failed: {_patch_err}")
                    return

                effective_origin_peer = origin_peer or from_peer

                # Ensure remote user exists as a shadow account so FK works.
                # IMPORTANT: shadow users are created per user_id (not per
                # peer) so that different users on the same peer device
                # appear with their own display names.
                _ensure_shadow_user(
                    user_id,
                    display_name,
                    effective_origin_peer,
                    allow_origin_reassign=True,
                )

                # Ensure the channel exists locally (auto-create if received
                # from a peer who has a channel we don't know about yet).
                # Fail closed: unknown channels start as private until an
                # explicit channel/member sync defines broader visibility.
                with db_manager.get_connection() as conn:
                    existing_ch = conn.execute(
                        "SELECT privacy_mode FROM channels WHERE id = ?", (channel_id,)
                    ).fetchone()
                    if not existing_ch:
                        conn.execute(
                            "INSERT OR IGNORE INTO channels "
                            "(id, name, channel_type, created_by, description, "
                            " origin_peer, privacy_mode, created_at) "
                            "VALUES (?, ?, 'private', ?, 'Auto-created from P2P sync', "
                            " ?, 'private', datetime('now'))",
                            (channel_id, f"peer-channel-{channel_id[:8]}",
                             user_id, effective_origin_peer)
                        )
                        conn.commit()
                        logger.info(f"Auto-created channel {channel_id} from P2P sync "
                                    f"(origin_peer={effective_origin_peer}, privacy_mode=private)")
                        channel_privacy_mode = 'private'
                    else:
                        channel_privacy_mode = str(existing_ch['privacy_mode'] or 'open').strip().lower()

                    # Ensure shadow user is a member
                    conn.execute(
                        "INSERT OR IGNORE INTO channel_members "
                        "(channel_id, user_id, role) VALUES (?, ?, 'member')",
                        (channel_id, user_id)
                    )

                    # Only open/public channels auto-include all local users.
                    # Restricted channels must keep explicit membership.
                    if channel_privacy_mode in ('open', 'public'):
                        human_users = conn.execute(
                            "SELECT id FROM users "
                            "WHERE id != 'system' AND id != 'local_user' "
                            "AND (password_hash IS NOT NULL AND password_hash != '' "
                            "     OR account_type = 'agent')",
                        ).fetchall()
                        for (uid,) in human_users:
                            conn.execute(
                                "INSERT OR IGNORE INTO channel_members "
                                "(channel_id, user_id, role) VALUES (?, ?, 'member')",
                                (channel_id, uid)
                            )

                    conn.commit()

                # --- Process file attachments from P2P ---
                import base64 as _b64
                processed_attachments = None
                (
                    content_rewritten,
                    crypto_state_db,
                    encrypted_content_db,
                    key_id_db,
                    nonce_db,
                    _key_missing,
                ) = _resolve_incoming_channel_content(
                    channel_id=channel_id,
                    from_peer=from_peer,
                    content=content,
                    encrypted_content=encrypted_content,
                    crypto_state=crypto_state,
                    key_id=key_id,
                    nonce=nonce,
                )
                if attachments:
                    processed_attachments = []
                    file_id_map = {}  # sender file_id -> local file_id (so we can fix /files/ in content)
                    for att in attachments:
                        original_id = att.get('id')
                        normalized = _normalize_incoming_attachment_entry(
                            att,
                            uploaded_by=user_id,
                            default_source_peer_id=from_peer,
                            source_context={
                                'source_type': 'channel_message',
                                'source_id': message_id,
                                'channel_id': channel_id,
                            },
                        )
                        if normalized:
                            if original_id and normalized.get('id'):
                                file_id_map[original_id] = normalized['id']
                            processed_attachments.append(normalized)

                    if processed_attachments:
                        message_type = 'file'
                    # Rewrite /files/SENDER_ID in content to /files/LOCAL_ID so inline images load
                    if content_rewritten and file_id_map and crypto_state_db in {'plaintext', 'decrypted'}:
                        for orig_id, local_id in file_id_map.items():
                            if orig_id and local_id and orig_id != local_id:
                                content_rewritten = content_rewritten.replace(
                                    f'/files/{orig_id}',
                                    f'/files/{local_id}',
                                )

                # Store the message — reuse the original message_id to avoid dupes
                import secrets as _sec2
                mid = message_id or f"P{_sec2.token_hex(12)}"
                attachments_json = (json.dumps(processed_attachments)
                                    if processed_attachments else None)
                security_clean = None
                if security:
                    security_clean, sec_error = channel_manager.validate_security_metadata(security, strict=False)
                    if sec_error:
                        logger.warning(
                            f"Dropping invalid security metadata for P2P channel message "
                            f"{message_id or mid}: {sec_error}"
                        )
                security_json = None
                if security_clean:
                    try:
                        security_json = json.dumps(security_clean)
                    except Exception:
                        security_json = None

                # Normalise timestamp to SQLite format (YYYY-MM-DD HH:MM:SS)
                # so P2P messages sort correctly alongside local messages.
                normalised_ts = None
                if timestamp:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        dt = _dt.fromisoformat(
                            timestamp.replace('Z', '+00:00'))
                        normalised_ts = dt.strftime('%Y-%m-%d %H:%M:%S')
                    except Exception:
                        normalised_ts = timestamp  # fallback

                created_dt = None
                if timestamp:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        created_dt = _dt.fromisoformat(timestamp.replace('Z', '+00:00'))
                    except Exception:
                        created_dt = None
                if created_dt is None:
                    from datetime import datetime as _dt, timezone as _tz
                    created_dt = _dt.now(_tz.utc)

                expiry_base = created_dt
                if update_only:
                    from datetime import datetime as _dt, timezone as _tz
                    expiry_base = _dt.now(_tz.utc)
                expires_dt = channel_manager._resolve_expiry(
                    expires_at=expires_at,
                    ttl_seconds=ttl_seconds,
                    ttl_mode=ttl_mode,
                    apply_default=not update_only,
                    base_time=expiry_base,
                )
                if expires_dt:
                    from datetime import datetime as _dt, timezone as _tz
                    now_dt = _dt.now(_tz.utc)
                    if expires_dt <= now_dt and not (update_only and existing_msg):
                        logger.debug(f"Skipping expired P2P channel message {message_id}")
                        return

                expires_db = (channel_manager._format_db_timestamp(expires_dt)
                              if expires_dt else None)

                # If this is an update for an existing message, only refresh expiry/TTL.
                if update_only and existing_msg:
                    if existing_msg['user_id'] != user_id:
                        logger.warning(f"Ignoring update for {message_id}: "
                                       f"author mismatch ({existing_msg['user_id']} != {user_id})")
                        return
                    ttl_sec_db = int(ttl_seconds) if ttl_seconds is not None else None
                    ttl_mode_db = (ttl_mode or '').strip() or None
                    edited_db = None
                    if edited_at:
                        try:
                            from datetime import datetime as _dt
                            ed = _dt.fromisoformat(str(edited_at).replace('Z', '+00:00'))
                            edited_db = ed.strftime('%Y-%m-%d %H:%M:%S')
                        except Exception:
                            edited_db = None
                    if edited_db is None:
                        from datetime import datetime as _dt, timezone as _tz
                        edited_db = _dt.now(_tz.utc).strftime('%Y-%m-%d %H:%M:%S')

                    # Preserve existing attachments if none provided
                    stored_attachments = attachments_json
                    stored_message_type = message_type or 'text'
                    stored_security = security_json
                    stored_encrypted = encrypted_content_db
                    stored_crypto_state = crypto_state_db
                    stored_key_id = key_id_db
                    stored_nonce = nonce_db
                    if attachments_json is None:
                        try:
                            with db_manager.get_connection() as conn:
                                row = conn.execute(
                                    "SELECT attachments, message_type, security, encrypted_content, crypto_state, key_id, nonce "
                                    "FROM channel_messages WHERE id = ?",
                                    (message_id,)
                                ).fetchone()
                                if row:
                                    stored_attachments = row['attachments']
                                    stored_message_type = row['message_type'] or stored_message_type
                                    stored_security = row['security']
                                    if stored_encrypted is None:
                                        stored_encrypted = row['encrypted_content']
                                    if (not stored_key_id) and row['key_id']:
                                        stored_key_id = row['key_id']
                                    if (not stored_nonce) and row['nonce']:
                                        stored_nonce = row['nonce']
                                    if (
                                        stored_crypto_state in {'plaintext', 'decrypted'}
                                        and row['crypto_state'] in {'encrypted', 'pending_decrypt', 'decrypt_failed'}
                                        and stored_encrypted
                                    ):
                                        stored_crypto_state = row['crypto_state']
                        except Exception:
                            pass

                    with db_manager.get_connection() as conn:
                        conn.execute(
                            "UPDATE channel_messages "
                            "SET content = ?, message_type = ?, attachments = ?, security = ?, edited_at = ?, "
                            "expires_at = ?, ttl_seconds = ?, ttl_mode = ?, "
                            "encrypted_content = ?, crypto_state = ?, key_id = ?, nonce = ? "
                            "WHERE id = ?",
                            (
                                content_rewritten,
                                stored_message_type,
                                stored_attachments,
                                stored_security,
                                edited_db,
                                expires_db,
                                ttl_sec_db,
                                ttl_mode_db,
                                stored_encrypted,
                                stored_crypto_state,
                                stored_key_id,
                                stored_nonce,
                                message_id,
                            )
                        )
                        conn.execute(
                            """
                            UPDATE channels
                               SET last_activity_at = COALESCE(?, CURRENT_TIMESTAMP),
                                   lifecycle_archived_at = NULL,
                                   lifecycle_archive_reason = NULL
                             WHERE id = ?
                            """,
                            (normalised_ts, channel_id),
                        )
                        conn.commit()
                    channel_manager.mark_message_processed(message_id)
                    try:
                        sync_edited_mention_activity(
                            db_manager=db_manager,
                            mention_manager=mention_manager,
                            inbox_manager=inbox_manager,
                            p2p_manager=p2p_manager,
                            content=content_rewritten,
                            source_type='channel_message',
                            source_id=message_id,
                            author_id=user_id,
                            origin_peer=from_peer,
                            channel_id=channel_id,
                            edited_at=edited_at,
                        )
                        if inbox_manager:
                            inbox_manager.sync_source_triggers(
                                source_type='channel_message',
                                source_id=message_id,
                                trigger_type='reply',
                                sender_user_id=user_id,
                                origin_peer=from_peer,
                                channel_id=channel_id,
                                preview=build_preview(content_rewritten or '') or None,
                                payload={
                                    'channel_id': channel_id,
                                    'message_id': message_id,
                                    'parent_message_id': parent_message_id,
                                    'edited_at': edited_at,
                                },
                                message_id=message_id,
                                source_content=content_rewritten,
                            )
                    except Exception as mention_sync_err:
                        logger.warning(
                            "Failed to refresh channel edit notices for %s: %s",
                            message_id,
                            mention_sync_err,
                        )
                    logger.info(f"Updated P2P channel message {message_id} in #{channel_id}")
                    return

                with db_manager.get_connection() as conn:
                    conn.execute("""
                        INSERT OR IGNORE INTO channel_messages
                        (id, channel_id, user_id, content, message_type,
                         attachments, security, created_at, origin_peer, expires_at, ttl_seconds, ttl_mode,
                         parent_message_id, encrypted_content, crypto_state, key_id, nonce)
                        VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (mid, channel_id, user_id, content_rewritten,
                          message_type, attachments_json, security_json, normalised_ts,
                          effective_origin_peer,
                          expires_db,
                          int(ttl_seconds) if ttl_seconds is not None else None,
                          (ttl_mode or '').strip() or None,
                          (parent_message_id or '').strip() or None,
                          encrypted_content_db,
                          crypto_state_db,
                          key_id_db,
                          nonce_db))
                    conn.execute(
                        """
                        UPDATE channels
                           SET last_activity_at = COALESCE(?, CURRENT_TIMESTAMP),
                               lifecycle_archived_at = NULL,
                               lifecycle_archive_reason = NULL
                         WHERE id = ?
                        """,
                        (normalised_ts, channel_id),
                    )
                    conn.commit()

                # Mark as processed so catch-up won't re-insert after restart
                channel_manager.mark_message_processed(mid)

                logger.info(f"Stored P2P channel message {mid} in #{channel_id}"
                            f"{' with ' + str(len(processed_attachments)) + ' attachment(s)' if processed_attachments else ''}")

                # Inline circles from [circle] blocks (allow update-only)
                try:
                    from .circles import parse_circle_blocks, derive_circle_id
                    if circle_manager:
                        circle_specs = parse_circle_blocks(content_rewritten or '')
                        if circle_specs:
                            # Messages received over P2P are inherently 'network' —
                            # they already traversed the mesh.  Only downgrade to
                            # 'local' when the channel is explicitly restricted.
                            channel_visibility = 'network'
                            try:
                                with db_manager.get_connection() as conn:
                                    row = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (channel_id,)
                                    ).fetchone()
                                    if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                        channel_visibility = 'local'
                            except Exception:
                                # Default to 'network' for P2P messages — the
                                # message was already broadcast over the network.
                                pass

                            for cidx, cspec in enumerate(circle_specs):
                                circle_id = derive_circle_id('channel', mid, cidx, len(circle_specs), override=cspec.circle_id)
                                facilitator_id = None
                                if cspec.facilitator:
                                    handle = cspec.facilitator.strip()
                                    if handle.startswith('@'):
                                        handle = handle[1:]
                                    try:
                                        row = db_manager.get_user(handle)
                                        if row:
                                            facilitator_id = row.get('id') or handle
                                    except Exception:
                                        facilitator_id = None
                                    if not facilitator_id:
                                        targets = resolve_mention_targets(
                                            db_manager,
                                            [handle],
                                            channel_id=channel_id,
                                            author_id=user_id,
                                        )
                                        if targets:
                                            facilitator_id = targets[0].get('user_id')
                                if not facilitator_id:
                                    facilitator_id = user_id

                                if cspec.participants is not None:
                                    resolved = []
                                    for part in cspec.participants or []:
                                        try:
                                            prow = db_manager.get_user(part)
                                            if prow:
                                                resolved.append(prow.get('id') or part)
                                                continue
                                        except Exception:
                                            pass
                                        try:
                                            targets = resolve_mention_targets(
                                                db_manager,
                                                [part],
                                                channel_id=channel_id,
                                                author_id=user_id,
                                            )
                                            if targets:
                                                resolved.append(targets[0].get('user_id'))
                                        except Exception:
                                            continue
                                    cspec.participants = resolved

                                circle_manager.upsert_circle(
                                    circle_id=circle_id,
                                    source_type='channel',
                                    source_id=mid,
                                    created_by=user_id,
                                    spec=cspec,
                                    channel_id=channel_id,
                                    facilitator_id=facilitator_id,
                                    visibility=channel_visibility,
                                    origin_peer=from_peer,
                                    created_at=normalised_ts,
                                )
                except Exception as circle_err:
                    logger.warning(f"Inline circle creation (P2P channel) failed: {circle_err}")

                if not update_only:
                    # For private/confidential channels, only record mentions
                    # for users who are actual members of the channel.
                    _skip_mentions = False
                    if channel_privacy_mode in ('private', 'confidential'):
                        try:
                            with db_manager.get_connection() as conn:
                                _local_peer = (p2p_manager.get_peer_id()
                                               if p2p_manager else None)
                                _has_local_member = conn.execute(
                                    "SELECT 1 FROM channel_members cm "
                                    "JOIN users u ON cm.user_id = u.id "
                                    "WHERE cm.channel_id = ? "
                                    "AND (u.origin_peer IS NULL OR u.origin_peer = '' "
                                    "     OR u.origin_peer = ?) LIMIT 1",
                                    (channel_id, _local_peer or ''),
                                ).fetchone()
                            if not _has_local_member:
                                _skip_mentions = True
                        except Exception:
                            pass

                    if not _skip_mentions:
                        mentions = extract_mentions(content_rewritten or '')
                        targets = _resolve_local_mentions(
                            mentions,
                            channel_id=channel_id,
                            author_id=user_id,
                        )
                        if targets:
                            # Further filter: only keep targets who are members
                            if channel_privacy_mode in ('private', 'confidential'):
                                member_ids = set()
                                try:
                                    with db_manager.get_connection() as conn:
                                        rows = conn.execute(
                                            "SELECT user_id FROM channel_members "
                                            "WHERE channel_id = ?", (channel_id,)
                                        ).fetchall()
                                        member_ids = {r['user_id'] for r in rows}
                                except Exception:
                                    pass
                                targets = [t for t in targets
                                           if t.get('user_id') in member_ids]

                            if targets:
                                preview = build_preview(content_rewritten or '')
                                _record_local_mention_events(
                                    targets=targets,
                                    source_type='channel_message',
                                    source_id=mid,
                                    author_id=user_id,
                                    from_peer=from_peer,
                                    channel_id=channel_id,
                                    preview=preview,
                                    source_content=content_rewritten,
                                )

                    # Notify original author when their message is replied to
                    # (parent_message_id set but author not already @mentioned).
                    if parent_message_id and inbox_manager and not _skip_mentions:
                        try:
                            with db_manager.get_connection() as conn:
                                parent_row = conn.execute(
                                    "SELECT user_id FROM channel_messages WHERE id = ?",
                                    (parent_message_id,)
                                ).fetchone()
                            if parent_row:
                                parent_author_id = parent_row['user_id'] if hasattr(parent_row, '__getitem__') else parent_row[0]
                                already_mentioned = any(
                                    t.get('user_id') == parent_author_id for t in (targets or [])
                                )
                                if not already_mentioned and parent_author_id != user_id:
                                    preview = build_preview(content_rewritten or '')
                                    inbox_manager.record_mention_triggers(
                                        target_ids=[parent_author_id],
                                        source_type='channel_message',
                                        source_id=mid,
                                        author_id=user_id,
                                        origin_peer=from_peer,
                                        channel_id=channel_id,
                                        preview=preview,
                                        source_content=content_rewritten,
                                        trigger_type='reply',
                                    )
                        except Exception as _reply_err:
                            logger.debug(f"Reply-to-author inbox trigger skipped: {_reply_err}")

                    # Inline tasks from [task] blocks
                    try:
                        from .tasks import parse_task_blocks, derive_task_id
                        if task_manager:
                            task_specs = parse_task_blocks(content or '')
                            if task_specs:
                                task_visibility = 'network'
                                try:
                                    with db_manager.get_connection() as conn:
                                        row = conn.execute(
                                            "SELECT privacy_mode FROM channels WHERE id = ?",
                                            (channel_id,)
                                        ).fetchone()
                                        if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                            task_visibility = 'local'
                                except Exception:
                                    task_visibility = 'local'
                                for idx, spec in enumerate(cast(Any, task_specs)):
                                    spec = cast(Any, spec)
                                    if not spec.confirmed:
                                        continue
                                    task_id = derive_task_id('channel', mid, idx, len(task_specs), override=spec.task_id)
                                    assignee_id = None
                                    if spec.assignee_clear:
                                        assignee_id = None
                                    elif spec.assignee:
                                        handle = spec.assignee.strip()
                                        if handle.startswith('@'):
                                            handle = handle[1:]
                                        try:
                                            row = db_manager.get_user(handle)
                                            if row:
                                                assignee_id = row.get('id') or handle
                                        except Exception:
                                            assignee_id = None
                                        if not assignee_id:
                                            targets = resolve_mention_targets(
                                                db_manager,
                                                [handle],
                                                channel_id=channel_id,
                                                author_id=user_id,
                                            )
                                            if targets:
                                                assignee_id = targets[0].get('user_id')
                                    editor_ids: list[str] = []
                                    if spec.editors_clear:
                                        editor_ids = []
                                    else:
                                        for editor in spec.editors or []:
                                            try:
                                                eid = None
                                                row = db_manager.get_user(editor)
                                                if row:
                                                    eid = row.get('id') or editor
                                            except Exception:
                                                eid = None
                                            if not eid:
                                                try:
                                                    targets = resolve_mention_targets(
                                                        db_manager,
                                                        [editor],
                                                        channel_id=channel_id,
                                                        author_id=user_id,
                                                    )
                                                    if targets:
                                                        eid = targets[0].get('user_id')
                                                except Exception:
                                                    eid = None
                                            if eid:
                                                editor_ids.append(eid)

                                    meta_payload = {
                                        'inline_task': True,
                                        'source_type': 'channel_message',
                                        'source_id': mid,
                                        'channel_id': channel_id,
                                    }
                                    if editor_ids is not None:
                                        meta_payload['editors'] = editor_ids

                                    task_manager.create_task(
                                        task_id=task_id,
                                        title=spec.title,
                                        description=spec.description,
                                        status=spec.status,
                                        priority=spec.priority,
                                        created_by=user_id,
                                        assigned_to=assignee_id,
                                        due_at=spec.due_at.isoformat() if spec.due_at else None,
                                        visibility=channel_visibility,
                                        metadata=meta_payload,
                                        origin_peer=from_peer,
                                        source_type='human',
                                        updated_by=user_id,
                                    )
                    except Exception as task_err:
                        logger.warning(f"Inline task creation (P2P channel) failed: {task_err}")

                    # Inline objectives from [objective] blocks
                    try:
                        from .objectives import parse_objective_blocks, derive_objective_id
                        objective_manager = app.config.get('OBJECTIVE_MANAGER')
                        if objective_manager:
                            obj_specs = parse_objective_blocks(content or '')
                            if obj_specs:
                                obj_visibility = 'network'
                                try:
                                    with db_manager.get_connection() as conn:
                                        row = conn.execute(
                                            "SELECT privacy_mode FROM channels WHERE id = ?",
                                            (channel_id,)
                                        ).fetchone()
                                        if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                            obj_visibility = 'local'
                                except Exception:
                                    obj_visibility = 'local'

                                def _resolve_handle(handle: str) -> Optional[str]:
                                    if not handle:
                                        return None
                                    token = handle.strip()
                                    if token.startswith('@'):
                                        token = token[1:]
                                    if not token:
                                        return None
                                    try:
                                        row = db_manager.get_user(token)
                                        if row:
                                            return row.get('id') or token
                                    except Exception:
                                        pass
                                    try:
                                        targets = resolve_mention_targets(
                                            db_manager,
                                            [token],
                                            channel_id=channel_id,
                                            author_id=user_id,
                                        )
                                        if targets:
                                            return targets[0].get('user_id')
                                    except Exception:
                                        return None
                                    return None

                                for oidx, obj_spec in enumerate(obj_specs):
                                    objective_id = derive_objective_id('channel', mid, oidx, len(obj_specs), override=obj_spec.objective_id)
                                    members_payload = []
                                    for obj_member in obj_spec.members or []:
                                        uid = _resolve_handle(obj_member.handle)
                                        if uid:
                                            members_payload.append({
                                                'user_id': uid,
                                                'role': obj_member.role,
                                            })
                                    tasks_payload = []
                                    for task_spec in obj_spec.tasks or []:
                                        assignee_id = _resolve_handle(task_spec.assignee) if task_spec.assignee else None
                                        tasks_payload.append({
                                            'title': task_spec.title,
                                            'status': task_spec.status,
                                            'assigned_to': assignee_id,
                                            'metadata': {
                                                'inline_objective_task': True,
                                                'source_type': 'channel_message',
                                                'source_id': mid,
                                                'channel_id': channel_id,
                                            },
                                        })
                                    objective_manager.upsert_objective(
                                        objective_id=objective_id,
                                        title=obj_spec.title,
                                        description=obj_spec.description,
                                        status=obj_spec.status,
                                        deadline=obj_spec.deadline.isoformat() if obj_spec.deadline else None,
                                        created_by=user_id,
                                        visibility=obj_visibility,
                                        origin_peer=from_peer,
                                        source_type='channel_message',
                                        source_id=mid,
                                        created_at=normalised_ts,
                                        members=members_payload,
                                        tasks=tasks_payload,
                                        updated_by=user_id,
                                    )
                    except Exception as obj_err:
                        logger.warning(f"Inline objective creation (P2P channel) failed: {obj_err}")

                    # Inline signals from [signal] blocks
                    try:
                        from .signals import parse_signal_blocks, derive_signal_id
                        signal_manager = app.config.get('SIGNAL_MANAGER')
                        if signal_manager:
                            sig_specs = parse_signal_blocks(content or '')
                            if sig_specs:
                                sig_visibility = 'network'
                                try:
                                    with db_manager.get_connection() as conn:
                                        row = conn.execute(
                                            "SELECT privacy_mode FROM channels WHERE id = ?",
                                            (channel_id,)
                                        ).fetchone()
                                        if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                            sig_visibility = 'local'
                                except Exception:
                                    sig_visibility = 'local'

                                def _resolve_handle(handle: str) -> Optional[str]:
                                    if not handle:
                                        return None
                                    token = handle.strip()
                                    if token.startswith('@'):
                                        token = token[1:]
                                    if not token:
                                        return None
                                    try:
                                        row = db_manager.get_user(token)
                                        if row:
                                            return row.get('id') or token
                                    except Exception:
                                        pass
                                    try:
                                        targets = resolve_mention_targets(
                                            db_manager,
                                            [token],
                                            channel_id=channel_id,
                                            author_id=user_id,
                                        )
                                        if targets:
                                            return targets[0].get('user_id')
                                    except Exception:
                                        return None
                                    return None

                                for sidx, sig_spec in enumerate(sig_specs):
                                    signal_id = derive_signal_id('channel', mid, sidx, len(sig_specs), override=sig_spec.signal_id)
                                    owner_id = _resolve_handle(sig_spec.owner) if sig_spec.owner else user_id
                                    signal_manager.upsert_signal(
                                        signal_id=signal_id,
                                        signal_type=sig_spec.signal_type,
                                        title=sig_spec.title,
                                        summary=sig_spec.summary,
                                        status=sig_spec.status,
                                        confidence=sig_spec.confidence,
                                        tags=sig_spec.tags,
                                        data=sig_spec.data,
                                        notes=sig_spec.notes,
                                        owner_id=owner_id or user_id,
                                        created_by=user_id,
                                        visibility=sig_visibility,
                                        origin_peer=from_peer,
                                        source_type='channel_message',
                                        source_id=mid,
                                        expires_at=sig_spec.expires_at.isoformat() if sig_spec.expires_at else None,
                                        ttl_seconds=sig_spec.ttl_seconds,
                                        ttl_mode=sig_spec.ttl_mode,
                                        created_at=normalised_ts,
                                        actor_id=user_id,
                                    )
                    except Exception as sig_err:
                        logger.warning(f"Inline signal creation (P2P channel) failed: {sig_err}")

                # Inline contracts from [contract] blocks
                try:
                    from .contracts import parse_contract_blocks, derive_contract_id
                    contract_manager = app.config.get('CONTRACT_MANAGER')
                    if contract_manager:
                        contract_specs = parse_contract_blocks(content_rewritten or '')
                        if contract_specs:
                            contract_visibility = 'network'
                            try:
                                with db_manager.get_connection() as conn:
                                    row = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (channel_id,)
                                    ).fetchone()
                                    if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                        contract_visibility = 'local'
                            except Exception:
                                contract_visibility = 'local'

                            def _resolve_contract_handle(handle: str) -> Optional[str]:
                                if not handle:
                                    return None
                                token = handle.strip()
                                if token.startswith('@'):
                                    token = token[1:]
                                if not token:
                                    return None
                                try:
                                    row = db_manager.get_user(token)
                                    if row:
                                        return row.get('id') or token
                                except Exception:
                                    pass
                                try:
                                    targets = resolve_mention_targets(
                                        db_manager,
                                        [token],
                                        channel_id=channel_id,
                                        author_id=user_id,
                                    )
                                    if targets:
                                        return targets[0].get('user_id')
                                except Exception:
                                    return None
                                return None

                            for cidx, spec in enumerate(contract_specs):
                                if not spec.confirmed:
                                    continue
                                contract_id = derive_contract_id(
                                    'channel', mid, cidx, len(contract_specs), override=spec.contract_id
                                )
                                owner_id = _resolve_contract_handle(spec.owner) if spec.owner else user_id
                                counterparty_ids = []
                                for cp in spec.counterparties or []:
                                    cp_id = _resolve_contract_handle(cp)
                                    if cp_id:
                                        counterparty_ids.append(cp_id)

                                contract_manager.upsert_contract(
                                    contract_id=contract_id,
                                    title=spec.title,
                                    summary=spec.summary,
                                    terms=spec.terms,
                                    status=spec.status,
                                    owner_id=owner_id or user_id,
                                    counterparties=counterparty_ids,
                                    created_by=user_id,
                                    visibility=contract_visibility,
                                    origin_peer=from_peer,
                                    source_type='channel_message',
                                    source_id=mid,
                                    expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                                    ttl_seconds=spec.ttl_seconds,
                                    ttl_mode=spec.ttl_mode,
                                    metadata=spec.metadata,
                                    created_at=normalised_ts,
                                    actor_id=user_id,
                                )
                except Exception as contract_err:
                    logger.warning(f"Inline contract creation (P2P channel) failed: {contract_err}")

                # Inline requests from [request] blocks
                try:
                    from .requests import parse_request_blocks, derive_request_id
                    request_manager = app.config.get('REQUEST_MANAGER')
                    if request_manager:
                        req_specs = parse_request_blocks(content_rewritten or '')
                        if req_specs:
                            req_visibility = 'network'
                            try:
                                with db_manager.get_connection() as conn:
                                    row = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (channel_id,)
                                    ).fetchone()
                                    if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                        req_visibility = 'local'
                            except Exception:
                                req_visibility = 'local'

                            for ridx, req_spec in enumerate(req_specs):
                                if not req_spec.confirmed:
                                    continue
                                request_id = derive_request_id('channel', mid, ridx, len(req_specs), override=req_spec.request_id)
                                members_payload = []
                                for req_member in req_spec.members or []:
                                    uid = _resolve_handle(req_member.handle) if hasattr(req_member, 'handle') else None
                                    if uid:
                                        members_payload.append({'user_id': uid, 'role': req_member.role})
                                request_manager.upsert_request(
                                    request_id=request_id,
                                    title=req_spec.title,
                                    created_by=user_id,
                                    request_text=req_spec.request,
                                    required_output=req_spec.required_output,
                                    status=req_spec.status,
                                    priority=req_spec.priority,
                                    tags=req_spec.tags,
                                    due_at=req_spec.due_at.isoformat() if req_spec.due_at else None,
                                    visibility=req_visibility,
                                    origin_peer=origin_peer,
                                    source_type='channel_message',
                                    source_id=mid,
                                    created_at=normalised_ts,
                                    actor_id=user_id,
                                    members=members_payload,
                                    members_defined=('members' in req_spec.fields),
                                    fields=req_spec.fields,
                                )
                except Exception as req_err:
                    logger.warning(f"Inline request creation (P2P channel) failed: {req_err}")

                # Inline handoffs from [handoff] blocks (allow update-only)
                try:
                    from .handoffs import parse_handoff_blocks, derive_handoff_id
                    handoff_manager = app.config.get('HANDOFF_MANAGER')
                    if handoff_manager:
                        handoff_specs = parse_handoff_blocks(content_rewritten or '')
                        if handoff_specs:
                            handoff_visibility = 'network'
                            try:
                                with db_manager.get_connection() as conn:
                                    row = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (channel_id,)
                                    ).fetchone()
                                    if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                        handoff_visibility = 'local'
                            except Exception:
                                # Fail closed: default to local for private channel safety
                                handoff_visibility = 'local'

                            for hidx, hspec in enumerate(handoff_specs):
                                if not hspec.confirmed:
                                    continue
                                handoff_id = derive_handoff_id(
                                    'channel', mid, hidx, len(handoff_specs), override=hspec.handoff_id
                                )
                                handoff_manager.upsert_handoff(
                                    handoff_id=handoff_id,
                                    source_type='channel',
                                    source_id=mid,
                                    author_id=user_id,
                                    title=hspec.title,
                                    summary=hspec.summary,
                                    next_steps=hspec.next_steps,
                                    owner=hspec.owner,
                                    tags=hspec.tags,
                                    raw=hspec.raw,
                                    channel_id=channel_id,
                                    visibility=handoff_visibility,
                                    origin_peer=from_peer,
                                    created_at=normalised_ts,
                                )
                except Exception as handoff_err:
                    logger.warning(f"Inline handoff creation (P2P channel) failed: {handoff_err}")

                # Inline skill registration from [skill] blocks
                try:
                    skill_manager = app.config.get('SKILL_MANAGER')
                    if skill_manager:
                        from .skills import parse_skill_blocks
                        skill_specs = parse_skill_blocks(content_rewritten or '')
                        for skill_spec in skill_specs:
                            skill_manager.register_skill(
                                skill_spec,
                                source_type='channel_message',
                                source_id=mid,
                                channel_id=channel_id,
                                author_id=user_id,
                            )
                except Exception as skill_err:
                    logger.warning(f"Inline skill registration (P2P channel) failed: {skill_err}")
            except Exception as e:
                logger.error(f"Failed to store P2P channel message: {e}",
                             exc_info=True)

        p2p_manager.on_channel_message = _on_p2p_channel_message
        p2p_manager.file_manager = file_manager

        # --- Channel announce callback ---
        def _on_channel_announce(channel_id, name, channel_type,
                                  description, created_by_peer, created_by_user_id, privacy_mode,
                                  last_activity_at=None,
                                  lifecycle_ttl_days=None, lifecycle_preserved=None,
                                  lifecycle_archived_at=None, lifecycle_archive_reason=None,
                                  from_peer=None, initial_members=None):
            """Handle a CHANNEL_ANNOUNCE from a connected peer.

            For private/confidential channels, initial_members specifies exactly which
            local users should be added (targeted propagation). For public
            channels, all local human users are added as before.
            """
            try:
                mode = str(privacy_mode or '').strip().lower()
                channel_type_norm = str(channel_type or '').strip().lower()
                if mode not in {'open', 'guarded', 'private', 'confidential'}:
                    # Backward compatibility: older peers may omit privacy_mode.
                    mode = 'private' if channel_type_norm == 'private' else 'open'
                is_targeted = mode in {'private', 'confidential'} or channel_type_norm == 'private'

                # SECURITY: Log channel announce for audit trail
                logger.info(f"Channel announce from {from_peer}: '{name}' ({channel_id}) "
                           f"type={channel_type}, privacy={privacy_mode}")

                # SECURITY: Strip initial_members from non-targeted channel announces
                if initial_members and not is_targeted:
                    logger.warning(
                        f"SECURITY: Non-targeted channel announce from {from_peer} "
                        f"includes initial_members (suspicious). Ignoring initial_members."
                    )
                    initial_members = None

                if is_targeted:
                    # Targeted channel announce — create with specific members.
                    # Filter initial_members to only users that are genuinely
                    # local to this peer (origin_peer matches or is empty/null).
                    # This prevents relay peers from adding shadow-user members
                    # for users that actually live on a different machine.
                    targeted_mode = mode if mode in {'private', 'confidential'} else 'private'
                    creator_hint = str(created_by_user_id or '').strip() or None

                    local_members: list[str] = []
                    if initial_members:
                        local_peer_id = ''
                        try:
                            local_peer_id = str(p2p_manager.get_peer_id() or '').strip() if p2p_manager else ''
                        except Exception:
                            pass
                        with db_manager.get_connection() as conn:
                            for uid in initial_members:
                                uid_s = str(uid).strip()
                                if not uid_s:
                                    continue
                                urow = conn.execute(
                                    "SELECT origin_peer FROM users WHERE id = ?",
                                    (uid_s,),
                                ).fetchone()
                                if not urow:
                                    continue
                                u_origin = str((urow['origin_peer'] if hasattr(urow, 'keys') and 'origin_peer' in urow.keys() else '') or '').strip()
                                if not u_origin or u_origin == local_peer_id:
                                    local_members.append(uid_s)

                    if not local_members and initial_members:
                        logger.debug(
                            f"Targeted channel announce {channel_id} from {from_peer}: "
                            f"none of {len(initial_members)} member(s) are local, skipping"
                        )
                        return

                    logger.info(f"Targeted channel announce {channel_id} ('{name}') from {from_peer}, "
                                f"initial_members={initial_members}, local_members={local_members}")
                    result = channel_manager.create_channel_from_sync(
                        channel_id=channel_id,
                        name=name,
                        channel_type=channel_type,
                        description=description,
                        local_user_id=creator_hint,
                        origin_peer=from_peer,
                        privacy_mode=targeted_mode,
                        last_activity_at=last_activity_at,
                        initial_members=local_members,
                        lifecycle_ttl_days=lifecycle_ttl_days,
                        lifecycle_preserved=bool(lifecycle_preserved),
                        lifecycle_archived_at=lifecycle_archived_at,
                        lifecycle_archive_reason=lifecycle_archive_reason,
                    )
                    if result:
                        logger.info(f"Created targeted channel {channel_id} from {from_peer} "
                                    f"with {len(local_members)} local member(s)")
                    else:
                        # Channel exists — add any local members not yet in it
                        if local_members:
                            with db_manager.get_connection() as conn:
                                for uid in local_members:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO channel_members "
                                        "(channel_id, user_id, role) VALUES (?, ?, 'member')",
                                        (channel_id, uid),
                                    )
                                conn.commit()
                            logger.info(
                                f"Targeted channel announce {channel_id}: added {len(local_members)} "
                                f"local member(s) to existing channel"
                            )
                    return

                # Public channel — existing merge/adopt logic
                local_user = 'local_user'
                try:
                    with db_manager.get_connection() as conn:
                        row = conn.execute(
                            "SELECT id FROM users WHERE id != 'system' "
                            "AND id != 'local_user' LIMIT 1"
                        ).fetchone()
                        if row:
                            local_user = row[0]
                except Exception:
                    pass

                creator_hint = str(created_by_user_id or '').strip() or None
                merge_result = channel_manager.merge_or_adopt_channel(
                    remote_id=channel_id,
                    remote_name=name,
                    remote_type=channel_type,
                    remote_desc=description,
                    local_user_id=creator_hint or local_user,
                    from_peer=from_peer,
                    privacy_mode=mode,
                    last_activity_at=last_activity_at,
                    lifecycle_ttl_days=lifecycle_ttl_days,
                    lifecycle_preserved=bool(lifecycle_preserved),
                    lifecycle_archived_at=lifecycle_archived_at,
                    lifecycle_archive_reason=lifecycle_archive_reason,
                )
                if merge_result:
                    logger.info(f"Channel announce from {from_peer}: "
                                f"synced '{name}' as {merge_result}")
                else:
                    logger.debug(f"Channel announce from {from_peer}: "
                                 f"'{name}' ({channel_id}) already exists, skipped")
            except Exception as e:
                logger.error(f"Failed to handle channel announce: {e}",
                             exc_info=True)

        p2p_manager.on_channel_announce = _on_channel_announce

        def _send_member_sync_ack(sync_id: Optional[str], to_peer: Optional[str],
                                  status: str = 'ok', error: Optional[str] = None,
                                  channel_id: Optional[str] = None,
                                  target_user_id: Optional[str] = None,
                                  action: Optional[str] = None) -> None:
            """Best-effort ack for member_sync delivery/application."""
            sid = str(sync_id or '').strip()
            peer = str(to_peer or '').strip()
            if not sid or not peer or not p2p_manager:
                return
            try:
                p2p_manager.send_member_sync_ack(
                    to_peer=peer,
                    sync_id=sid,
                    status=status,
                    error=error,
                    channel_id=channel_id,
                    target_user_id=target_user_id,
                    action=action,
                )
            except Exception:
                pass

        # --- Member sync callback (private channel membership propagation) ---
        def _on_member_sync(channel_id, target_user_id, action, role,
                            channel_name, channel_type, channel_description,
                            privacy_mode, sync_id, from_peer):
            """Handle a MEMBER_SYNC from a remote peer.

            When a member is added/removed from a private channel on a remote
            peer, this creates the channel locally if needed and adds/removes
            the specified user.
            """
            try:
                channel_id = str(channel_id or '').strip()
                target_user_id = str(target_user_id or '').strip()
                action = str(action or '').strip().lower()
                role = str(role or 'member').strip().lower() or 'member'
                logger.info(f"Member sync from {from_peer}: {action} user {target_user_id} "
                            f"in channel {channel_id}")

                if not channel_id or not target_user_id or action not in {'add', 'remove'}:
                    _send_member_sync_ack(
                        sync_id=sync_id,
                        to_peer=from_peer,
                        status='error',
                        error='invalid_payload',
                        channel_id=channel_id or None,
                        target_user_id=target_user_id or None,
                        action=action or None,
                    )
                    return

                # SECURITY: Validate that target_user_id exists locally
                with db_manager.get_connection() as conn:
                    user_check = conn.execute(
                        "SELECT id FROM users WHERE id = ?",
                        (target_user_id,)
                    ).fetchone()

                    if not user_check:
                        logger.warning(
                            f"SECURITY: Rejected member_sync from {from_peer}: "
                            f"user {target_user_id} does not exist locally"
                        )
                        _send_member_sync_ack(
                            sync_id=sync_id,
                            to_peer=from_peer,
                            status='error',
                            error='unknown_target_user',
                            channel_id=channel_id,
                            target_user_id=target_user_id,
                            action=action,
                        )
                        return

                    # Verify sender has authority over the channel.
                    # If the channel exists locally, its origin_peer must
                    # match the sending peer.  For new channels (not yet
                    # created locally) we accept — create_channel_from_sync
                    # will record from_peer as origin.
                    ch_row = conn.execute(
                        "SELECT origin_peer FROM channels WHERE id = ?",
                        (channel_id,)
                    ).fetchone()
                    if ch_row:
                        ch_origin = ch_row['origin_peer'] if 'origin_peer' in ch_row.keys() else None
                        if ch_origin and ch_origin != from_peer:
                            logger.warning(
                                f"SECURITY: Rejected member_sync from {from_peer}: "
                                f"channel {channel_id} belongs to peer {ch_origin}"
                            )
                            _send_member_sync_ack(
                                sync_id=sync_id,
                                to_peer=from_peer,
                                status='error',
                                error='unauthorized_sender',
                                channel_id=channel_id,
                                target_user_id=target_user_id,
                                action=action,
                            )
                            return

                if action == 'add':
                    # Ensure channel exists locally
                    with db_manager.get_connection() as conn:
                        existing = conn.execute(
                            "SELECT 1 FROM channels WHERE id = ?", (channel_id,)
                        ).fetchone()
                    if not existing:
                        # Create the channel locally with this user as initial member
                        channel_manager.create_channel_from_sync(
                            channel_id=channel_id,
                            name=channel_name or f'private-{channel_id[:8]}',
                            channel_type=channel_type or 'private',
                            description=channel_description or '',
                            local_user_id=cast(str, None),
                            origin_peer=from_peer,
                            privacy_mode=privacy_mode or 'private',
                            initial_members=[target_user_id],
                        )
                    else:
                        # Channel exists — just add the member directly
                        with db_manager.get_connection() as conn:
                            conn.execute(
                                "INSERT OR IGNORE INTO channel_members "
                                "(channel_id, user_id, role) VALUES (?, ?, ?)",
                                (channel_id, target_user_id, role or 'member'))
                            conn.commit()
                    logger.info(f"Member sync: added {target_user_id} to {channel_id}")

                    _notify_channel_added(
                        user_id=target_user_id,
                        channel_id=channel_id,
                        channel_name=channel_name or '',
                        added_by=None,
                        origin_peer=from_peer,
                    )

                    # Recover any mention inbox items that may have raced ahead
                    # of membership sync delivery for this user/channel.
                    if inbox_manager:
                        try:
                            with db_manager.get_connection() as conn:
                                user_row = conn.execute(
                                    "SELECT username, display_name FROM users WHERE id = ?",
                                    (target_user_id,),
                                ).fetchone()
                            username = ((user_row['username'] if user_row and 'username' in user_row.keys() else None) or '').strip()
                            display_name = (user_row['display_name'] if user_row and 'display_name' in user_row.keys() else None)
                            # Fall back to display_name so inbox rebuild runs even if
                            # username column is empty on a legacy shadow user row.
                            effective_username = username or (display_name or '').strip()
                            if effective_username:
                                rebuild = inbox_manager.rebuild_from_channel_messages(
                                    user_id=target_user_id,
                                    username=effective_username,
                                    display_name=display_name,
                                    window_hours=72,
                                    limit=500,
                                    channel_id=channel_id,
                                )
                                created = int((rebuild or {}).get('created') or 0)
                                if created > 0:
                                    logger.info(
                                        "Member sync mention backfill created %d inbox item(s) "
                                        "for user %s channel %s",
                                        created,
                                        target_user_id,
                                        channel_id,
                                    )
                        except Exception as backfill_err:
                            logger.debug(
                                "Member sync mention backfill skipped for user %s channel %s: %s",
                                target_user_id,
                                channel_id,
                                backfill_err,
                            )

                elif action == 'remove':
                    with db_manager.get_connection() as conn:
                        conn.execute(
                            "DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?",
                            (channel_id, target_user_id))
                        conn.commit()
                    logger.info(f"Member sync: removed {target_user_id} from {channel_id}")

                _send_member_sync_ack(
                    sync_id=sync_id,
                    to_peer=from_peer,
                    status='ok',
                    error=None,
                    channel_id=channel_id,
                    target_user_id=target_user_id,
                    action=action,
                )

            except Exception as e:
                logger.error(f"Failed to handle member sync: {e}", exc_info=True)
                _send_member_sync_ack(
                    sync_id=sync_id,
                    to_peer=from_peer,
                    status='error',
                    error='internal_error',
                    channel_id=str(channel_id or '').strip() or None,
                    target_user_id=str(target_user_id or '').strip() or None,
                    action=str(action or '').strip().lower() or None,
                )

        p2p_manager.on_member_sync = _on_member_sync

        def _on_member_sync_ack(sync_id, status, error, channel_id, target_user_id, action, from_peer):
            """Persist member_sync acknowledgement for retry/audit handling."""
            sid = str(sync_id or '').strip()
            if not sid:
                return
            ok = channel_manager.mark_member_sync_delivery_acked(
                sync_id=sid,
                status=status or 'ok',
                error=error,
            )
            if not ok:
                logger.debug(
                    "Member sync ack received for unknown sync_id=%s from %s",
                    sid,
                    from_peer,
                )

        p2p_manager.on_member_sync_ack = _on_member_sync_ack

        def _normalize_channel_crypto_mode(raw_mode: Any) -> str:
            mode = str(raw_mode or '').strip().lower()
            if mode in {'e2e_optional', 'e2e_enforced', 'legacy_plaintext'}:
                return mode
            return 'legacy_plaintext'

        def _e2e_private_enabled() -> bool:
            sec = getattr(config, 'security', None) if config else None
            return bool(getattr(sec, 'e2e_private_channels', False))

        def _channel_targets_e2e(privacy_mode: str, crypto_mode: str) -> bool:
            return (
                str(privacy_mode or '').strip().lower() in {'private', 'confidential'}
                and str(crypto_mode or '').strip().lower() in {'e2e_optional', 'e2e_enforced'}
            )

        def _on_channel_membership_query(query_id, local_user_ids, limit, from_peer):
            """Respond with private-channel metadata for querying peer users."""
            try:
                qid = str(query_id or '').strip() or None
                user_ids = []
                seen_users = set()
                for uid in (local_user_ids or []):
                    uid_s = str(uid or '').strip()
                    if not uid_s or uid_s in seen_users:
                        continue
                    seen_users.add(uid_s)
                    user_ids.append(uid_s)
                if not user_ids:
                    return
                try:
                    max_channels = max(1, min(int(limit or 200), 300))
                except Exception:
                    max_channels = 200

                payload = channel_manager.get_private_channel_recovery_payload(
                    query_user_ids=user_ids,
                    requester_peer_id=str(from_peer or '').strip(),
                    limit=max_channels,
                    max_members_per_channel=250,
                )
                channels_payload = list(payload.get('channels') or [])
                truncated = bool(payload.get('truncated'))
                if p2p_manager and p2p_manager.is_running():
                    p2p_manager.send_channel_membership_response(
                        to_peer=str(from_peer or '').strip(),
                        query_id=qid,
                        channels=channels_payload,
                        truncated=truncated,
                    )
            except Exception as e:
                logger.error(f"Failed to handle channel membership query: {e}", exc_info=True)

        p2p_manager.on_channel_membership_query = _on_channel_membership_query

        def _on_channel_membership_response(query_id, channels, truncated, from_peer):
            """Recover missing private-channel metadata/membership after reconnect."""
            try:
                local_peer = str((p2p_manager.get_peer_id() if p2p_manager else '') or '').strip()
                imported_channels = 0
                for item in (channels or []):
                    if not isinstance(item, dict):
                        continue
                    channel_id = str(item.get('channel_id') or '').strip()
                    if not channel_id:
                        continue
                    name = str(item.get('name') or f'private-{channel_id[:8]}').strip() or f'private-{channel_id[:8]}'
                    channel_type = str(item.get('channel_type') or 'private').strip().lower() or 'private'
                    description = str(item.get('description') or '')
                    origin_peer = str(item.get('origin_peer') or from_peer or '').strip() or str(from_peer or '').strip()
                    created_by_user_id = str(item.get('created_by_user_id') or '').strip() or None
                    privacy_mode = str(item.get('privacy_mode') or 'private').strip().lower() or 'private'
                    crypto_mode = _normalize_channel_crypto_mode(item.get('crypto_mode') or 'legacy_plaintext')
                    members = item.get('members') if isinstance(item.get('members'), list) else []
                    sender_is_member = False

                    local_member_ids: list[str] = []
                    seen_local = set()
                    for member in members:
                        if not isinstance(member, dict):
                            continue
                        uid = str(member.get('user_id') or '').strip()
                        if not uid:
                            continue
                        m_origin = str(member.get('origin_peer') or '').strip()
                        if m_origin and str(from_peer or '').strip() and m_origin == str(from_peer).strip():
                            sender_is_member = True
                        # Ensure user exists locally (shadow rows for remote peers).
                        _ensure_shadow_user(
                            uid,
                            member.get('display_name'),
                            m_origin or origin_peer or from_peer,
                        )
                        if (not m_origin or m_origin == local_peer) and uid not in seen_local:
                            seen_local.add(uid)
                            local_member_ids.append(uid)

                    # Ignore responses that do not include any local users.
                    if not local_member_ids:
                        continue
                    if str(from_peer or '').strip() and not sender_is_member and origin_peer != str(from_peer).strip():
                        logger.info(
                            "Membership recovery for %s from %s (sender not in member list — "
                            "accepted: we queried this peer)",
                            channel_id,
                            from_peer,
                        )

                    with db_manager.get_connection() as conn:
                        existing = conn.execute(
                            "SELECT origin_peer FROM channels WHERE id = ?",
                            (channel_id,),
                        ).fetchone()
                    if existing:
                        existing_origin = str((existing['origin_peer'] if 'origin_peer' in existing.keys() else '') or '').strip()
                        if existing_origin and existing_origin not in {origin_peer, str(from_peer or '').strip()}:
                            logger.warning(
                                "SECURITY: Ignoring membership recovery for %s from %s (origin mismatch existing=%s remote=%s hint=%s)",
                                channel_id,
                                from_peer,
                                existing_origin,
                                str(from_peer or '').strip(),
                                origin_peer,
                            )
                            continue
                    if not existing:
                        channel_manager.create_channel_from_sync(
                            channel_id=channel_id,
                            name=name,
                            channel_type=channel_type,
                            description=description,
                            local_user_id=created_by_user_id,
                            origin_peer=origin_peer,
                            privacy_mode=privacy_mode,
                            initial_members=local_member_ids,
                        )
                        imported_channels += 1

                    # Merge member list (best effort; unknown rows are shadow-created above).
                    with db_manager.get_connection() as conn:
                        for member in members:
                            if not isinstance(member, dict):
                                continue
                            uid = str(member.get('user_id') or '').strip()
                            if not uid:
                                continue
                            role = str(member.get('role') or 'member').strip().lower() or 'member'
                            conn.execute(
                                "INSERT OR IGNORE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
                                (channel_id, uid, role),
                            )
                        conn.commit()

                    # Trigger key request if this is an E2E channel and we still lack a key.
                    if _e2e_private_enabled() and _channel_targets_e2e(privacy_mode, crypto_mode):
                        active_key = channel_manager.get_active_channel_key(channel_id)
                        key_bytes = channel_manager.decode_channel_key_material(
                            active_key.get('key_material_enc')
                        ) if active_key else None
                        if not key_bytes:
                            request_peer = ''
                            if origin_peer and origin_peer != local_peer:
                                request_peer = origin_peer
                            elif from_peer and str(from_peer).strip() != local_peer:
                                request_peer = str(from_peer).strip()
                            if request_peer:
                                p2p_manager.send_channel_key_request(
                                    to_peer=request_peer,
                                    channel_id=channel_id,
                                    reason='membership_recovery_missing_key',
                                )

                if imported_channels:
                    logger.info(
                        "Membership recovery imported %d channel(s) from %s (query=%s, truncated=%s)",
                        imported_channels,
                        from_peer,
                        query_id,
                        bool(truncated),
                    )
            except Exception as e:
                logger.error(f"Failed to handle channel membership response: {e}", exc_info=True)

        p2p_manager.on_channel_membership_response = _on_channel_membership_response

        # --- Channel key callbacks (phase-2 E2E implementation) ---
        def _backfill_pending_decrypt_for_key(channel_id: str, key_id: str, key_bytes: bytes) -> None:
            """Decrypt and backfill messages waiting on a newly received key."""
            pending_rows = channel_manager.get_pending_decrypt_messages(channel_id, key_id)
            if not pending_rows:
                return
            for pending in pending_rows:
                message_id = pending.get('id')
                encrypted_blob = pending.get('encrypted_content')
                nonce_blob = pending.get('nonce')
                if not message_id or not encrypted_blob or not nonce_blob:
                    channel_manager.update_message_decrypt(
                        message_id=message_id or '',
                        content='',
                        new_state='decrypt_failed',
                    )
                    continue
                try:
                    plaintext = decrypt_with_channel_key(
                        encrypted_content_b64=encrypted_blob,
                        key_material=key_bytes,
                        nonce_b64=nonce_blob,
                    )
                    channel_manager.update_message_decrypt(
                        message_id=message_id,
                        content=plaintext,
                        new_state='decrypted',
                    )
                    author_id = str(pending.get('user_id') or '').strip()
                    origin_for_event = str(pending.get('origin_peer') or '').strip()
                    parent_message_id = str(pending.get('parent_message_id') or '').strip()

                    mentions = extract_mentions(plaintext or '')
                    local_targets = _resolve_local_mentions(
                        mentions,
                        channel_id=channel_id,
                        author_id=author_id or None,
                    )
                    if local_targets:
                        _record_local_mention_events(
                            targets=local_targets,
                            source_type='channel_message',
                            source_id=message_id,
                            author_id=author_id,
                            from_peer=origin_for_event,
                            channel_id=channel_id,
                            preview=build_preview(plaintext or ''),
                            source_content=plaintext,
                        )

                    if parent_message_id and inbox_manager:
                        try:
                            with db_manager.get_connection() as conn:
                                parent_row = conn.execute(
                                    "SELECT user_id FROM channel_messages WHERE id = ?",
                                    (parent_message_id,),
                                ).fetchone()
                            if parent_row:
                                parent_author_id = (
                                    parent_row['user_id']
                                    if hasattr(parent_row, 'keys') and 'user_id' in parent_row.keys()
                                    else parent_row[0]
                                )
                                already_mentioned = any(
                                    t.get('user_id') == parent_author_id for t in (local_targets or [])
                                )
                                if (
                                    parent_author_id
                                    and parent_author_id != author_id
                                    and not already_mentioned
                                ):
                                    inbox_manager.record_mention_triggers(
                                        target_ids=[parent_author_id],
                                        source_type='channel_message',
                                        source_id=message_id,
                                        author_id=author_id or None,
                                        origin_peer=origin_for_event or None,
                                        channel_id=channel_id,
                                        preview=build_preview(plaintext or ''),
                                        source_content=plaintext,
                                        trigger_type='reply',
                                    )
                        except Exception as reply_err:
                            logger.debug(
                                "Pending decrypt reply trigger skipped for %s in %s: %s",
                                message_id,
                                channel_id,
                                reply_err,
                            )
                except Exception as dec_err:
                    logger.debug(
                        "Pending decrypt failed for %s in %s key=%s: %s",
                        message_id,
                        channel_id,
                        key_id,
                        dec_err,
                    )
                    channel_manager.update_message_decrypt(
                        message_id=message_id,
                        content='',
                        new_state='decrypt_failed',
                    )

        def _on_channel_key_distribution(channel_id, key_id, encrypted_key,
                                         key_version, rotated_from, from_peer):
            """Unwrap, store, and apply a channel key received from a trusted origin."""
            try:
                if not channel_id or not key_id or not encrypted_key:
                    logger.warning(
                        "Ignoring invalid channel key distribution from %s (channel=%s key=%s)",
                        from_peer, channel_id, key_id,
                    )
                    return

                # Sender must be channel authority (origin peer) when channel exists.
                ch_origin = None
                with db_manager.get_connection() as conn:
                    ch_row = conn.execute(
                        "SELECT origin_peer FROM channels WHERE id = ?",
                        (channel_id,),
                    ).fetchone()
                if ch_row:
                    ch_origin = ch_row['origin_peer'] if 'origin_peer' in ch_row.keys() else None
                    if ch_origin and ch_origin != from_peer:
                        logger.warning(
                            "SECURITY: Rejected channel key distribution for %s from %s (origin=%s)",
                            channel_id,
                            from_peer,
                            ch_origin,
                        )
                        p2p_manager.send_channel_key_ack(
                            to_peer=from_peer,
                            channel_id=channel_id,
                            key_id=key_id,
                            status='error',
                            error='unauthorized_sender',
                        )
                        return
                else:
                    # Channel metadata may race with key delivery. Create a safe placeholder.
                    channel_manager.create_channel_from_sync(
                        channel_id=channel_id,
                        name=f'private-{channel_id[:8]}',
                        channel_type='private',
                        description='Auto-created from channel key distribution',
                        local_user_id=cast(str, None),
                        origin_peer=from_peer,
                        privacy_mode='private',
                        initial_members=None,
                    )

                sender_identity = p2p_manager.identity_manager.get_peer(from_peer)
                local_identity = p2p_manager.identity_manager.local_identity
                key_bytes = None
                if sender_identity and local_identity:
                    try:
                        key_bytes = decrypt_key_from_peer(
                            wrapped_key_hex=encrypted_key,
                            local_identity=local_identity,
                            sender_identity=sender_identity,
                        )
                    except Exception:
                        key_bytes = None
                if key_bytes is None:
                    # Backward compatibility with scaffold payloads.
                    key_bytes = decode_channel_key_material(encrypted_key)
                if key_bytes is None:
                    logger.warning(
                        "Failed to decrypt channel key distribution %s/%s from %s",
                        channel_id,
                        key_id,
                        from_peer,
                    )
                    p2p_manager.send_channel_key_ack(
                        to_peer=from_peer,
                        channel_id=channel_id,
                        key_id=key_id,
                        status='error',
                        error='decrypt_failed',
                    )
                    return

                metadata = {
                    'key_version': key_version,
                    'rotated_from': rotated_from,
                    'received_from_peer': from_peer,
                    'received_at': datetime.now(timezone.utc).isoformat(),
                    'algorithm': 'chacha20poly1305',
                }
                stored = channel_manager.upsert_channel_key(
                    channel_id=channel_id,
                    key_id=key_id,
                    key_material_enc=encode_channel_key_material(key_bytes),
                    created_by_peer=from_peer,
                    metadata=metadata,
                )
                if not stored:
                    logger.warning(
                        "Failed to store channel key distribution %s/%s from %s",
                        channel_id,
                        key_id,
                        from_peer,
                    )
                    p2p_manager.send_channel_key_ack(
                        to_peer=from_peer,
                        channel_id=channel_id,
                        key_id=key_id,
                        status='error',
                        error='store_failed',
                    )
                    return

                # Receiving a valid channel key implies E2E mode for this channel.
                try:
                    channel_manager.set_channel_crypto_mode(channel_id, ChannelManager.CRYPTO_MODE_E2E_OPTIONAL)
                except Exception:
                    pass

                channel_manager.upsert_channel_member_key_state(
                    channel_id=channel_id,
                    key_id=key_id,
                    peer_id=from_peer,
                    delivery_state='received',
                    delivered=True,
                )
                _backfill_pending_decrypt_for_key(channel_id, key_id, key_bytes)
                p2p_manager.send_channel_key_ack(
                    to_peer=from_peer,
                    channel_id=channel_id,
                    key_id=key_id,
                    status='ok',
                )
                logger.info(
                    "Imported channel key for %s key=%s from peer %s",
                    channel_id,
                    key_id,
                    from_peer,
                )
            except Exception as e:
                logger.error(f"Failed to handle channel key distribution: {e}", exc_info=True)

        p2p_manager.on_channel_key_distribution = _on_channel_key_distribution

        def _on_channel_key_request(channel_id, requesting_peer, reason, key_id, from_peer):
            """Respond to key requests when this peer is the channel authority."""
            try:
                if not channel_id:
                    return
                req_peer = requesting_peer or from_peer
                logger.info(
                    "Channel key request for %s from peer %s (requesting=%s reason=%s key_id=%s)",
                    channel_id, from_peer, req_peer, reason or 'missing_key', key_id,
                )

                requested_key_id = str(key_id or '').strip()

                def _send_request_error(error_code: str) -> None:
                    if not requested_key_id:
                        return
                    try:
                        p2p_manager.send_channel_key_ack(
                            to_peer=from_peer,
                            channel_id=channel_id,
                            key_id=requested_key_id,
                            status='error',
                            error=error_code,
                        )
                    except Exception:
                        pass

                if not req_peer:
                    _send_request_error('missing_requester_peer')
                    return
                local_peer = p2p_manager.get_peer_id() if p2p_manager else None
                if not local_peer:
                    _send_request_error('local_peer_unavailable')
                    return
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT origin_peer FROM channels WHERE id = ?",
                        (channel_id,),
                    ).fetchone()
                    member_peer = conn.execute(
                        "SELECT 1 FROM channel_members cm "
                        "JOIN users u ON cm.user_id = u.id "
                        "WHERE cm.channel_id = ? AND u.origin_peer = ? LIMIT 1",
                        (channel_id, req_peer),
                    ).fetchone()
                channel_origin = row['origin_peer'] if row and 'origin_peer' in row.keys() else None
                if channel_origin and channel_origin != local_peer:
                    _send_request_error('not_channel_origin')
                    return
                if not member_peer:
                    _send_request_error('requester_not_member')
                    return

                active_key = channel_manager.get_active_channel_key(channel_id)
                if not active_key:
                    _send_request_error('active_key_missing')
                    return
                key_bytes = channel_manager.decode_channel_key_material(
                    active_key.get('key_material_enc')
                )
                if not key_bytes:
                    _send_request_error('key_decode_failed')
                    return
                recipient_identity = p2p_manager.identity_manager.get_peer(req_peer)
                local_identity = p2p_manager.identity_manager.local_identity
                if not recipient_identity or not local_identity:
                    _send_request_error('unknown_requester_identity')
                    return
                wrapped_key = encrypt_key_for_peer(
                    key_material=key_bytes,
                    local_identity=local_identity,
                    recipient_identity=recipient_identity,
                )
                sent = p2p_manager.send_channel_key_distribution(
                    to_peer=req_peer,
                    channel_id=channel_id,
                    key_id=active_key['key_id'],
                    encrypted_key=wrapped_key,
                    key_version=int((active_key.get('metadata') or {}).get('key_version') or 1),
                    rotated_from=(active_key.get('metadata') or {}).get('rotated_from'),
                )
                state = 'delivered' if sent else 'failed'
                channel_manager.upsert_channel_member_key_state(
                    channel_id=channel_id,
                    key_id=active_key['key_id'],
                    peer_id=req_peer,
                    delivery_state=state,
                    delivered=sent,
                    last_error=None if sent else 'send_failed',
                )
                if not sent:
                    _send_request_error('send_failed')
            except Exception as e:
                logger.error(f"Failed to handle channel key request: {e}", exc_info=True)

        p2p_manager.on_channel_key_request = _on_channel_key_request

        def _on_channel_key_ack(channel_id, key_id, status, error, from_peer):
            """Persist key-delivery acknowledgement state."""
            try:
                if not channel_id or not key_id or not from_peer:
                    return
                if not channel_manager.get_channel_key(channel_id, key_id):
                    logger.debug(
                        "Ignoring channel key ack for unknown key %s/%s from %s (status=%s)",
                        channel_id,
                        key_id,
                        from_peer,
                        status,
                    )
                    return
                state = 'acked' if str(status or '').lower() == 'ok' else 'failed'
                channel_manager.upsert_channel_member_key_state(
                    channel_id=channel_id,
                    key_id=key_id,
                    peer_id=from_peer,
                    delivery_state=state,
                    last_error=error,
                    delivered=True,
                    acked=(state == 'acked'),
                )
            except Exception as e:
                logger.error(f"Failed to handle channel key ack: {e}", exc_info=True)

        p2p_manager.on_channel_key_ack = _on_channel_key_ack

        if identity_portability_manager and identity_portability_manager.enabled:
            def _on_principal_announce(principal, keys, from_peer):
                try:
                    identity_portability_manager.handle_principal_announce(
                        principal=principal or {},
                        keys=keys or [],
                        from_peer=from_peer,
                    )
                except Exception as e:
                    logger.error(f"Failed to handle principal announce: {e}", exc_info=True)

            p2p_manager.on_principal_announce = _on_principal_announce

            def _on_principal_key_update(principal_id, key, from_peer):
                try:
                    identity_portability_manager.handle_principal_key_update(
                        principal_id=principal_id,
                        key=key or {},
                        from_peer=from_peer,
                    )
                except Exception as e:
                    logger.error(f"Failed to handle principal key update: {e}", exc_info=True)

            p2p_manager.on_principal_key_update = _on_principal_key_update

            def _on_bootstrap_grant_sync(grant, from_peer):
                try:
                    identity_portability_manager.handle_bootstrap_grant_sync(
                        grant=grant or {},
                        from_peer=from_peer,
                    )
                except Exception as e:
                    logger.error(f"Failed to handle bootstrap grant sync: {e}", exc_info=True)

            p2p_manager.on_bootstrap_grant_sync = _on_bootstrap_grant_sync

            def _on_bootstrap_grant_revoke(grant_id, revoked_at, reason, issuer_peer_id, from_peer):
                try:
                    identity_portability_manager.handle_bootstrap_grant_revoke(
                        grant_id=grant_id,
                        revoked_at=revoked_at,
                        reason=reason,
                        issuer_peer_id=issuer_peer_id,
                        from_peer=from_peer,
                    )
                except Exception as e:
                    logger.error(f"Failed to handle bootstrap grant revoke: {e}", exc_info=True)

            p2p_manager.on_bootstrap_grant_revoke = _on_bootstrap_grant_revoke

        def _mark_stale_pending_decrypt() -> None:
            """Bound pending_decrypt backlog so old ciphertext does not linger forever."""
            try:
                max_age_hours = int(os.getenv('CANOPY_PENDING_DECRYPT_MAX_AGE_HOURS', '24'))
            except Exception:
                max_age_hours = 24
            try:
                sweep_limit = int(os.getenv('CANOPY_PENDING_DECRYPT_SWEEP_LIMIT', '1000'))
            except Exception:
                sweep_limit = 1000

            marked = channel_manager.mark_stale_pending_decrypt(
                max_age_hours=max_age_hours,
                limit=sweep_limit,
            )
            if marked:
                logger.info(
                    "Marked %d stale pending_decrypt message(s) as decrypt_failed (max_age=%sh)",
                    marked,
                    max_age_hours,
                )

        def _retry_member_sync_delivery_for_peer(peer_id: str) -> None:
            """Retry pending member-sync deliveries when a peer reconnects."""
            if not peer_id or not p2p_manager:
                return

            try:
                min_retry_seconds = int(os.getenv('CANOPY_MEMBER_SYNC_RETRY_MIN_SECONDS', '10'))
            except Exception:
                min_retry_seconds = 10
            try:
                max_attempts = int(os.getenv('CANOPY_MEMBER_SYNC_RETRY_MAX_ATTEMPTS', '8'))
            except Exception:
                max_attempts = 8

            retries = channel_manager.get_retryable_member_sync_deliveries(
                peer_id=peer_id,
                limit=256,
                min_retry_seconds=max(1, min_retry_seconds),
                max_attempts=max(1, max_attempts),
            )
            if not retries:
                return

            sent_count = 0
            for item in retries:
                sync_id = str(item.get('sync_id') or '').strip()
                channel_id_retry = str(item.get('channel_id') or '').strip()
                target_user_id_retry = str(item.get('target_user_id') or '').strip()
                action_retry = str(item.get('action') or '').strip().lower()
                role_retry = str(item.get('role') or 'member').strip().lower() or 'member'
                if not sync_id or not channel_id_retry or not target_user_id_retry or action_retry not in {'add', 'remove'}:
                    if sync_id:
                        channel_manager.mark_member_sync_delivery_attempt(
                            sync_id=sync_id,
                            sent=False,
                            error='invalid_retry_payload',
                        )
                    continue

                payload = item.get('payload') or {}
                sent = p2p_manager.broadcast_member_sync(
                    channel_id=channel_id_retry,
                    target_user_id=target_user_id_retry,
                    action=action_retry,
                    target_peer_id=peer_id,
                    role=role_retry,
                    channel_name=str(payload.get('channel_name') or ''),
                    channel_type=str(payload.get('channel_type') or 'private'),
                    channel_description=str(payload.get('channel_description') or ''),
                    privacy_mode=str(payload.get('privacy_mode') or 'private'),
                    sync_id=sync_id,
                )
                channel_manager.mark_member_sync_delivery_attempt(
                    sync_id=sync_id,
                    sent=sent,
                    error=None if sent else 'send_failed',
                )
                if sent:
                    sent_count += 1

            if sent_count:
                logger.info(
                    "Retried %d member-sync delivery item(s) to peer %s",
                    sent_count,
                    peer_id,
                )

        def _retry_channel_key_delivery_for_peer(peer_id: str) -> None:
            """Retry pending/failed channel-key deliveries when a peer reconnects."""
            if not peer_id:
                return
            retries = channel_manager.get_retryable_channel_member_key_states(peer_id, limit=256)
            if not retries:
                return

            local_identity = p2p_manager.identity_manager.local_identity if p2p_manager else None
            recipient_identity = (
                p2p_manager.identity_manager.get_peer(peer_id)
                if p2p_manager and p2p_manager.identity_manager
                else None
            )
            if not local_identity or not recipient_identity:
                for item in retries:
                    channel_manager.upsert_channel_member_key_state(
                        channel_id=item['channel_id'],
                        key_id=item['key_id'],
                        peer_id=peer_id,
                        delivery_state='failed',
                        last_error='unknown_peer_identity',
                    )
                return

            sent_count = 0
            for item in retries:
                channel_id_retry = str(item.get('channel_id') or '').strip()
                key_id_retry = str(item.get('key_id') or '').strip()
                key_material_enc = item.get('key_material_enc')
                metadata_retry = item.get('metadata') or {}
                if not channel_id_retry or not key_id_retry or not key_material_enc:
                    continue
                key_bytes_retry = channel_manager.decode_channel_key_material(key_material_enc)
                if not key_bytes_retry:
                    channel_manager.upsert_channel_member_key_state(
                        channel_id=channel_id_retry,
                        key_id=key_id_retry,
                        peer_id=peer_id,
                        delivery_state='failed',
                        last_error='decode_failed',
                    )
                    continue
                try:
                    wrapped_retry = encrypt_key_for_peer(
                        key_material=key_bytes_retry,
                        local_identity=local_identity,
                        recipient_identity=recipient_identity,
                    )
                except Exception as wrap_err:
                    channel_manager.upsert_channel_member_key_state(
                        channel_id=channel_id_retry,
                        key_id=key_id_retry,
                        peer_id=peer_id,
                        delivery_state='failed',
                        last_error=f'wrap_failed:{wrap_err}',
                    )
                    continue

                channel_manager.upsert_channel_member_key_state(
                    channel_id=channel_id_retry,
                    key_id=key_id_retry,
                    peer_id=peer_id,
                    delivery_state='pending',
                    last_error=None,
                )
                sent_retry = p2p_manager.send_channel_key_distribution(
                    to_peer=peer_id,
                    channel_id=channel_id_retry,
                    key_id=key_id_retry,
                    encrypted_key=wrapped_retry,
                    key_version=int(metadata_retry.get('key_version') or 1),
                    rotated_from=metadata_retry.get('rotated_from'),
                )
                channel_manager.upsert_channel_member_key_state(
                    channel_id=channel_id_retry,
                    key_id=key_id_retry,
                    peer_id=peer_id,
                    delivery_state='delivered' if sent_retry else 'failed',
                    delivered=sent_retry,
                    last_error=None if sent_retry else 'send_failed',
                )
                if sent_retry:
                    sent_count += 1

            if sent_count:
                logger.info(
                    "Retried %d channel-key delivery item(s) to peer %s",
                    sent_count,
                    peer_id,
                )

        def _on_peer_connected(peer_id: str) -> None:
            """Schedule non-blocking key-delivery retry when a peer connects."""
            if not peer_id:
                return

            def _worker() -> None:
                try:
                    _mark_stale_pending_decrypt()
                    _retry_member_sync_delivery_for_peer(peer_id)
                    _retry_channel_key_delivery_for_peer(peer_id)
                    _retry_pending_large_attachments_for_peer(peer_id)
                except Exception as retry_err:
                    logger.debug(
                        "Peer-connect retry worker failed for %s: %s",
                        peer_id,
                        retry_err,
                    )

            threading.Thread(
                target=_worker,
                name=f"canopy-key-retry-{peer_id[:8]}",
                daemon=True,
            ).start()

        p2p_manager.on_peer_connected = _on_peer_connected
        _mark_stale_pending_decrypt()

        # --- Channel sync callback ---
        def _on_channel_sync(channels, from_peer):
            """Handle a CHANNEL_SYNC (bulk list) from a connected peer."""
            try:
                local_user = 'local_user'
                try:
                    with db_manager.get_connection() as conn:
                        row = conn.execute(
                            "SELECT id FROM users WHERE id != 'system' "
                            "AND id != 'local_user' LIMIT 1"
                        ).fetchone()
                        if row:
                            local_user = row[0]
                except Exception:
                    pass

                synced = 0
                for ch in channels:
                    ch_id = ch.get('id')
                    ch_name = ch.get('name', '')
                    ch_type = ch.get('type', 'public')
                    ch_desc = ch.get('desc', '')
                    ch_privacy = ch.get('privacy_mode') or 'open'
                    ch_last_activity_at = ch.get('last_activity_at')
                    ch_ttl_days = ch.get('lifecycle_ttl_days')
                    ch_preserved = bool(ch.get('lifecycle_preserved'))
                    ch_archived_at = ch.get('lifecycle_archived_at')
                    ch_archive_reason = ch.get('lifecycle_archive_reason')
                    # Use the channel's original origin_peer if available;
                    # fall back to the peer that sent the sync.
                    ch_origin = ch.get('origin_peer') or from_peer
                    if not ch_id:
                        continue
                    result = channel_manager.merge_or_adopt_channel(
                        remote_id=ch_id,
                        remote_name=ch_name,
                        remote_type=ch_type,
                        remote_desc=ch_desc,
                        local_user_id=local_user,
                        from_peer=ch_origin,
                        privacy_mode=ch_privacy,
                        last_activity_at=ch_last_activity_at,
                        lifecycle_ttl_days=ch_ttl_days,
                        lifecycle_preserved=ch_preserved,
                        lifecycle_archived_at=ch_archived_at,
                        lifecycle_archive_reason=ch_archive_reason,
                    )
                    if result:
                        synced += 1

                if synced:
                    logger.info(f"Channel sync from {from_peer}: "
                                f"synced {synced}/{len(channels)} channels")
                else:
                    logger.debug(f"Channel sync from {from_peer}: "
                                 f"all {len(channels)} channels already present")
            except Exception as e:
                logger.error(f"Failed to handle channel sync: {e}",
                             exc_info=True)

        p2p_manager.on_channel_sync = _on_channel_sync

        # --- Provide public channels for sync on new connections ---
        def _get_public_channels_for_sync():
            """Return public channels for P2P CHANNEL_SYNC messages."""
            return channel_manager.get_all_public_channels()

        p2p_manager.get_public_channels_for_sync = _get_public_channels_for_sync

        # --- Catch-up: provide latest timestamps ---
        def _get_channel_latest_timestamps():
            """Return {channel_id: latest_created_at} for catch-up requests."""
            return channel_manager.get_channel_latest_timestamps()

        p2p_manager.get_channel_latest_timestamps = _get_channel_latest_timestamps

        def _get_channel_sync_digests(
            channel_ids: Optional[list[str]] = None,
            max_channels: int = 200,
        ):
            """Return channel digest map for optional Merkle-assisted catch-up."""
            return channel_manager.get_channel_sync_digests(
                channel_ids=channel_ids,
                max_channels=max_channels,
            )

        p2p_manager.get_channel_sync_digests = _get_channel_sync_digests

        # Extra timestamp callbacks for extended catch-up (circles, tasks, feed)
        def _get_feed_latest_timestamp():
            try:
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT MAX(created_at) AS latest FROM feed_posts"
                    ).fetchone()
                return row['latest'] if row and row['latest'] else None
            except Exception:
                return None

        def _get_circle_entries_latest():
            return circle_manager.get_entries_latest_timestamp() if circle_manager else None

        def _get_circle_votes_latest():
            return circle_manager.get_votes_latest_timestamp() if circle_manager else None

        def _get_circles_latest():
            return circle_manager.get_circles_latest_timestamp() if circle_manager else None

        def _get_tasks_latest():
            return task_manager.get_tasks_latest_timestamp() if task_manager else None

        p2p_manager.get_feed_latest_timestamp = _get_feed_latest_timestamp
        p2p_manager.get_circle_entries_latest_timestamp = _get_circle_entries_latest
        p2p_manager.get_circle_votes_latest_timestamp = _get_circle_votes_latest
        p2p_manager.get_circles_latest_timestamp = _get_circles_latest
        p2p_manager.get_tasks_latest_timestamp = _get_tasks_latest

        denied_catchup_audit_ts: dict[tuple[str, str], float] = {}
        denied_catchup_audit_interval_s = 120.0

        # --- Catch-up request handler ---
        def _on_catchup_request(channel_timestamps, from_peer,
                                feed_latest=None, circle_entries_latest=None,
                                circle_votes_latest=None, circles_latest=None,
                                tasks_latest=None,
                                digest=None):
            """A peer is asking us for messages it missed.

            For each channel, query messages newer than the timestamp
            the peer reports, then send them back.  Also gathers missed
            feed posts, circle entries, circle votes, and tasks.

            IMPORTANT: This callback runs on the asyncio event loop
            (called from the routing layer's message handler).  We
            must NOT block here — schedule the actual sending as an
            async task so the event loop stays responsive.
            """
            try:
                all_messages = []
                # Get all local channels (peer may not know about some)
                local_ts = channel_manager.get_channel_latest_timestamps()
                digest_checked = 0
                digest_matched = 0
                digest_mismatched = 0
                digest_fallbacks = 0
                digest_meta: Dict[str, Any] = {}
                remote_digest_channels: Dict[str, Any] = {}
                local_digest_channels: Dict[str, Any] = {}

                try:
                    if (
                        getattr(p2p_manager, 'sync_digest_enabled', False)
                        and isinstance(digest, dict)
                        and int(digest.get('version') or 0) == 1
                        and isinstance(digest.get('channels'), dict)
                    ):
                        remote_digest_channels = cast(Dict[str, Any], digest.get('channels') or {})
                        if remote_digest_channels:
                            local_digest_channels = channel_manager.get_channel_sync_digests(
                                channel_ids=list(remote_digest_channels.keys()),
                                max_channels=getattr(
                                    p2p_manager,
                                    'sync_digest_max_channels_per_request',
                                    200,
                                ),
                            ) or {}
                except Exception as dig_err:
                    digest_fallbacks += 1
                    logger.debug(f"Catchup digest comparison setup failed: {dig_err}")

                # Relay all channels (including restricted) so messages
                # can propagate through intermediary peers.  Content
                # confidentiality will be enforced via E2E encryption;
                # access-control filtering was removed to fix relay gaps.

                for ch_id, local_latest in local_ts.items():
                    remote_digest = remote_digest_channels.get(ch_id)
                    local_digest = local_digest_channels.get(ch_id)
                    if remote_digest is not None:
                        digest_checked += 1
                        try:
                            local_root = str((local_digest or {}).get('root') or '')
                            remote_root = str((remote_digest or {}).get('root') or '')
                            local_count = int((local_digest or {}).get('live_count') or 0)
                            remote_count_raw = (remote_digest or {}).get('live_count')
                            remote_count = (
                                int(remote_count_raw)
                                if remote_count_raw is not None else None
                            )
                            count_matches = (
                                True if remote_count is None else (local_count == remote_count)
                            )
                            if local_root and remote_root and local_root == remote_root and count_matches:
                                digest_matched += 1
                                digest_meta[ch_id] = {
                                    'remote_root': local_root,
                                    'remote_live_count': local_count,
                                    'status': 'match',
                                }
                                continue
                            digest_mismatched += 1
                            digest_meta[ch_id] = {
                                'remote_root': local_root,
                                'remote_live_count': local_count,
                                'status': 'mismatch',
                            }
                        except Exception:
                            digest_fallbacks += 1

                    peer_latest = channel_timestamps.get(ch_id)
                    if peer_latest is None:
                        # Peer has no messages in this channel — send
                        # everything (up to the limit).
                        since = '1970-01-01 00:00:00'
                    elif local_latest > peer_latest:
                        since = peer_latest
                    else:
                        continue  # peer is up-to-date

                    msgs = channel_manager.get_messages_since(ch_id, since)
                    all_messages.extend(msgs)

                if remote_digest_channels:
                    for ch_id in remote_digest_channels.keys():
                        if ch_id in digest_meta:
                            continue
                        local_digest = local_digest_channels.get(ch_id) or {}
                        digest_meta[ch_id] = {
                            'remote_root': str(local_digest.get('root') or ''),
                            'remote_live_count': int(local_digest.get('live_count') or 0),
                            'status': 'missing',
                        }

                if all_messages:
                    # Enrich each message with the author's display_name
                    # so the receiving peer can create shadow users with
                    # correct names instead of generic "peer-XXXX".
                    for msg in all_messages:
                        uid = msg.get('user_id')
                        if uid and 'display_name' not in msg:
                            try:
                                u = db_manager.get_user(uid)
                                if u:
                                    dn = u.get('display_name') or u.get('username')
                                    if dn and not dn.startswith('peer-'):
                                        msg['display_name'] = dn
                            except Exception:
                                pass

                    # Prevent plaintext leakage of encrypted private-channel
                    # content during catch-up relay. Members can decrypt via
                    # encrypted_content/key_id/nonce metadata.
                    privacy_cache: dict[str, str] = {}
                    for msg in all_messages:
                        ch_id = str(msg.get('channel_id') or '')
                        if not ch_id:
                            continue
                        mode = privacy_cache.get(ch_id)
                        if mode is None:
                            try:
                                with db_manager.get_connection() as conn:
                                    row = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (ch_id,),
                                    ).fetchone()
                                mode = str((row['privacy_mode'] if row else 'open') or 'open').strip().lower()
                            except Exception:
                                mode = 'open'
                            privacy_cache[ch_id] = mode
                        if mode in {'private', 'confidential'} and msg.get('encrypted_content'):
                            msg['content'] = ''
                            msg['crypto_state'] = 'encrypted'

                    # Normalize attachments for catch-up:
                    # small files are still embedded inline (up to a bounded
                    # total), larger files become remote-large placeholders
                    # that upgraded peers can auto-fetch.
                    _catchup_embed_limit = 20 * 1024 * 1024  # 20MB total
                    _catchup_embedded = 0
                    if file_manager and p2p_manager and _catchup_embedded < _catchup_embed_limit:
                        import base64 as _b64_catchup
                        for msg in all_messages:
                            atts = msg.get('attachments') or []
                            if not atts:
                                continue
                            normalized_atts = []
                            for att in atts:
                                if not isinstance(att, dict):
                                    continue
                                entry = p2p_manager._build_p2p_attachment_entry(att)
                                if not entry:
                                    continue
                                if entry.get('data'):
                                    try:
                                        embedded_size = len(_b64_catchup.b64decode(entry['data']))
                                    except Exception:
                                        embedded_size = int(entry.get('size') or 0)
                                    if _catchup_embedded + embedded_size > _catchup_embed_limit:
                                        entry.pop('data', None)
                                        entry.pop('url', None)
                                        entry.setdefault('origin_file_id', entry.get('id'))
                                        entry.setdefault('source_peer_id', p2p_manager.get_peer_id())
                                        entry['large_attachment'] = True
                                        entry['storage_mode'] = 'remote_large'
                                        entry['download_status'] = 'pending'
                                    else:
                                        _catchup_embedded += embedded_size
                                        logger.debug(
                                            "Catchup: embedding attachment %s (%d bytes) for peer %s",
                                            entry.get('id'),
                                            embedded_size,
                                            from_peer,
                                        )
                                elif entry.get('id'):
                                    entry.pop('url', None)
                                    entry.setdefault('origin_file_id', entry.get('id'))
                                    entry.setdefault('source_peer_id', p2p_manager.get_peer_id())
                                    entry.setdefault('large_attachment', True)
                                    entry.setdefault('storage_mode', 'remote_large')
                                    entry.setdefault('download_status', 'pending')
                                normalized_atts.append(entry)
                            msg['attachments'] = normalized_atts
                            if _catchup_embedded >= _catchup_embed_limit:
                                break

                # ---- Gather extra catch-up data (circles, tasks, feed) ----
                extra_data = {}
                if digest_meta:
                    extra_data['digest'] = {
                        'version': 1,
                        'channels': digest_meta,
                    }
                if (
                    p2p_manager
                    and (
                        digest_checked > 0
                        or digest_matched > 0
                        or digest_mismatched > 0
                        or digest_fallbacks > 0
                    )
                    and hasattr(p2p_manager, 'record_sync_digest_stats')
                ):
                    try:
                        p2p_manager.record_sync_digest_stats(
                            checked=digest_checked,
                            matched=digest_matched,
                            mismatched=digest_mismatched,
                            fallbacks=digest_fallbacks,
                        )
                    except Exception:
                        pass

                # Feed posts newer than what the peer has
                try:
                    since_feed = feed_latest or '1970-01-01 00:00:00'
                    with db_manager.get_connection() as conn:
                        rows = conn.execute(
                            "SELECT id, author_id, content, content_type, "
                            "visibility, metadata, created_at, expires_at "
                            "FROM feed_posts WHERE created_at > ? AND "
                            "(visibility = 'network' OR visibility = 'public') "
                            "ORDER BY created_at ASC LIMIT 200",
                            (since_feed,)
                        ).fetchall()
                    if rows:
                        feed_posts = []
                        for r in rows:
                            fp = dict(r)
                            # Enrich with display name
                            uid = fp.get('author_id')
                            if uid:
                                try:
                                    u = db_manager.get_user(uid)
                                    if u:
                                        fp['display_name'] = u.get('display_name') or u.get('username')
                                except Exception:
                                    pass
                            try:
                                metadata_blob = json.loads(fp.get('metadata') or '{}')
                            except Exception:
                                metadata_blob = {}
                            if isinstance(metadata_blob, dict) and metadata_blob.get('attachments') and p2p_manager:
                                normalized_feed_atts = []
                                for att in metadata_blob.get('attachments') or []:
                                    entry = p2p_manager._build_p2p_attachment_entry(att) if isinstance(att, dict) else None
                                    if not entry:
                                        continue
                                    if not entry.get('data') and entry.get('id'):
                                        entry.pop('url', None)
                                        entry.setdefault('origin_file_id', entry.get('id'))
                                        entry.setdefault('source_peer_id', p2p_manager.get_peer_id())
                                        entry.setdefault('large_attachment', True)
                                        entry.setdefault('storage_mode', 'remote_large')
                                        entry.setdefault('download_status', 'pending')
                                    normalized_feed_atts.append(entry)
                                metadata_blob = dict(metadata_blob)
                                metadata_blob['attachments'] = normalized_feed_atts
                                fp['metadata'] = metadata_blob
                            feed_posts.append(fp)
                        extra_data['feed_posts'] = feed_posts
                except Exception as fp_err:
                    logger.debug(f"Catchup feed posts gathering failed: {fp_err}")

                # Circle objects newer than what the peer has (v0.3.55+)
                if circle_manager:
                    try:
                        since_co = circles_latest or '1970-01-01 00:00:00'
                        circles_data = circle_manager.get_circles_since(since_co)
                        if circles_data:
                            extra_data['circles'] = circles_data
                    except Exception as co_err:
                        logger.debug(f"Catchup circles gathering failed: {co_err}")

                # Circle entries newer than what the peer has
                if circle_manager:
                    try:
                        since_ce = circle_entries_latest or '1970-01-01 00:00:00'
                        entries = circle_manager.get_entries_since(since_ce)
                        if entries:
                            extra_data['circle_entries'] = entries
                    except Exception as ce_err:
                        logger.debug(f"Catchup circle entries gathering failed: {ce_err}")

                    # Circle votes
                    try:
                        since_cv = circle_votes_latest or '1970-01-01 00:00:00'
                        votes = circle_manager.get_votes_since(since_cv)
                        if votes:
                            extra_data['circle_votes'] = votes
                    except Exception as cv_err:
                        logger.debug(f"Catchup circle votes gathering failed: {cv_err}")

                # Tasks newer than what the peer has
                if task_manager:
                    try:
                        since_t = tasks_latest or '1970-01-01 00:00:00'
                        tasks = task_manager.get_tasks_since(since_t)
                        if tasks:
                            extra_data['tasks'] = tasks
                    except Exception as t_err:
                        logger.debug(f"Catchup tasks gathering failed: {t_err}")

                total_extra = sum(len(v) for v in extra_data.values() if isinstance(v, list))
                has_data = bool(all_messages) or total_extra > 0

                if has_data:
                    logger.info(f"Catchup response to {from_peer}: "
                                f"{len(all_messages)} channel msgs"
                                f"{f', +{total_extra} extra items' if total_extra else ''}")

                    # Schedule the sends as an async task so we don't
                    # block the event loop (which would deadlock
                    # run_coroutine_threadsafe calls).
                    import asyncio as _aio_catchup
                    _aio_catchup.ensure_future(
                        p2p_manager.send_catchup_response_async(
                            from_peer, all_messages,
                            extra_data=extra_data if extra_data else None))
                else:
                    logger.debug(f"Catchup request from {from_peer}: "
                                 f"peer is up-to-date")
            except Exception as e:
                logger.error(f"Failed to handle catchup request: {e}",
                             exc_info=True)

        p2p_manager.on_catchup_request = _on_catchup_request

        # --- Catch-up response handler ---
        def _on_catchup_response(messages, from_peer,
                                 feed_posts=None, circle_entries=None,
                                 circle_votes=None, circles=None,
                                 tasks=None):
            """Store missed messages received in a catch-up response.

            Uses the same INSERT OR IGNORE pattern as the live P2P
            message handler to avoid duplicates.  Also processes
            missed feed posts, circle objects, circle entries, circle
            votes, and tasks sent by peers running v0.3.36+.

            NOTE: The send side dispatches channel messages individually
            and extra data (feed posts, circles, tasks) as a separate
            batch with messages=[].  We must NOT return early when
            messages is empty — the extra data still needs processing.
            """
            has_messages = bool(messages)
            has_extra = bool(feed_posts) or bool(circle_entries) or bool(circle_votes) or bool(circles) or bool(tasks)
            if not has_messages and not has_extra:
                return
            try:
                stored = 0
                skipped_dup = 0
                for msg in (messages or []):
                    mid = msg.get('id')
                    if not mid:
                        continue

                    # --- Persistent dedup check ---
                    if channel_manager.is_message_processed(mid):
                        skipped_dup += 1
                        continue

                    channel_id = msg.get('channel_id', 'general')
                    user_id = msg.get('user_id', f'peer_{from_peer}')
                    content = msg.get('content', '')
                    incoming_encrypted = msg.get('encrypted_content')
                    incoming_crypto_state = msg.get('crypto_state')
                    incoming_key_id = msg.get('key_id')
                    incoming_nonce = msg.get('nonce')
                    (
                        content,
                        crypto_state_db,
                        encrypted_content_db,
                        key_id_db,
                        nonce_db,
                        _key_missing,
                    ) = _resolve_incoming_channel_content(
                        channel_id=channel_id,
                        from_peer=from_peer,
                        content=content,
                        encrypted_content=incoming_encrypted,
                        crypto_state=incoming_crypto_state,
                        key_id=incoming_key_id,
                        nonce=incoming_nonce,
                    )
                    message_type = msg.get('message_type', 'text')
                    timestamp = msg.get('created_at')

                    # Normalise timestamp
                    normalised_ts = None
                    if timestamp:
                        try:
                            from datetime import datetime as _dt
                            dt = _dt.fromisoformat(
                                timestamp.replace('Z', '+00:00'))
                            normalised_ts = dt.strftime('%Y-%m-%d %H:%M:%S')
                        except Exception:
                            normalised_ts = timestamp

                    created_dt = None
                    if timestamp:
                        try:
                            from datetime import datetime as _dt
                            created_dt = _dt.fromisoformat(
                                timestamp.replace('Z', '+00:00'))
                        except Exception:
                            created_dt = None
                    if created_dt is None:
                        from datetime import datetime as _dt, timezone as _tz
                        created_dt = _dt.now(_tz.utc)

                    expires_raw = msg.get('expires_at')
                    ttl_sec = msg.get('ttl_seconds')
                    ttl_md = msg.get('ttl_mode')
                    if ttl_sec is not None and isinstance(ttl_sec, str):
                        try:
                            ttl_sec = int(ttl_sec)
                        except (TypeError, ValueError):
                            ttl_sec = None
                    expires_dt = channel_manager._resolve_expiry(
                        expires_at=expires_raw,
                        ttl_seconds=ttl_sec,
                        ttl_mode=ttl_md,
                        apply_default=True,
                        base_time=created_dt,
                    )
                    if expires_dt:
                        from datetime import datetime as _dt, timezone as _tz
                        if expires_dt <= _dt.now(_tz.utc):
                            continue

                    expires_db = (channel_manager._format_db_timestamp(expires_dt)
                                  if expires_dt else None)

                    # Extract display_name from catchup message metadata
                    # (the sending peer may include it in the stored msg data)
                    catchup_display = msg.get('display_name') or msg.get('author_display_name')

                    # Ensure shadow user exists (per-user-id, not per-peer)
                    catchup_origin_peer = str(msg.get('origin_peer') or from_peer or '').strip() or str(from_peer or '').strip()
                    _ensure_shadow_user(
                        user_id,
                        catchup_display,
                        catchup_origin_peer,
                        allow_origin_reassign=True,
                    )

                    # Ensure channel exists
                    with db_manager.get_connection() as conn:
                        existing_ch = conn.execute(
                            "SELECT privacy_mode FROM channels WHERE id = ?",
                            (channel_id,)
                        ).fetchone()
                        if not existing_ch:
                            conn.execute(
                                "INSERT OR IGNORE INTO channels "
                                "(id, name, channel_type, created_by, "
                                "description, origin_peer, privacy_mode, created_at) "
                                "VALUES (?, ?, 'private', ?, "
                                "'Auto-created from P2P catchup', ?, 'private', "
                                "datetime('now'))",
                                (channel_id,
                                 f"peer-channel-{channel_id[:8]}",
                                 user_id, from_peer)
                            )
                            conn.commit()

                        # Ensure memberships
                        conn.execute(
                            "INSERT OR IGNORE INTO channel_members "
                            "(channel_id, user_id, role) "
                            "VALUES (?, ?, 'member')",
                            (channel_id, user_id)
                        )
                        conn.commit()

                    # Process file attachments — decode base64 data and
                    # save to local FileManager, just like live messages.
                    attachments_json = None
                    atts = msg.get('attachments')
                    if atts:
                        processed_atts = []
                        for att in atts:
                            normalized = _normalize_incoming_attachment_entry(
                                att,
                                uploaded_by=user_id,
                                default_source_peer_id=from_peer,
                                source_context={
                                    'source_type': 'channel_message',
                                    'source_id': mid,
                                    'channel_id': channel_id,
                                    'catchup': True,
                                },
                            )
                            if normalized:
                                processed_atts.append(normalized)
                        attachments_json = json.dumps(processed_atts)
                        message_type = 'file'

                    origin_peer = msg.get('origin_peer') or from_peer
                    parent_message_id = (msg.get('parent_message_id') or '').strip() or None

                    with db_manager.get_connection() as conn:
                        conn.execute("""
                            INSERT OR IGNORE INTO channel_messages
                            (id, channel_id, user_id, content,
                             message_type, attachments, created_at, origin_peer, expires_at,
                             parent_message_id, encrypted_content, crypto_state, key_id, nonce)
                            VALUES (?, ?, ?, ?, ?, ?,
                                    COALESCE(?, datetime('now')), ?, ?, ?, ?, ?, ?, ?)
                        """, (mid, channel_id, user_id, content,
                              message_type, attachments_json,
                              normalised_ts, origin_peer, expires_db, parent_message_id,
                              encrypted_content_db, crypto_state_db, key_id_db, nonce_db))
                        conn.execute(
                            """
                            UPDATE channels
                               SET last_activity_at = COALESCE(?, CURRENT_TIMESTAMP),
                                   lifecycle_archived_at = NULL,
                                   lifecycle_archive_reason = NULL
                             WHERE id = ?
                            """,
                            (normalised_ts, channel_id),
                        )
                        conn.commit()

                    channel_manager.mark_message_processed(mid)
                    stored += 1

                if stored or skipped_dup:
                    logger.info(f"Catchup from {from_peer}: stored "
                                f"{stored}/{len(messages or [])} missed messages"
                                f"{f', skipped {skipped_dup} duplicates' if skipped_dup else ''}")
            except Exception as e:
                logger.error(f"Failed to handle catchup response: {e}",
                             exc_info=True)

            # ---- Process extra catch-up data (v0.3.36+) ----

            # Feed posts
            if feed_posts:
                fp_stored = 0
                for fp in feed_posts:
                    try:
                        pid = fp.get('id')
                        if not pid:
                            continue
                        author_id = fp.get('author_id', f'peer_{from_peer}')
                        content = fp.get('content', '')
                        display_name = fp.get('display_name')
                        feed_origin_peer = ''
                        try:
                            if isinstance(fp.get('metadata'), dict):
                                feed_origin_peer = str(fp.get('metadata', {}).get('origin_peer') or '').strip()
                        except Exception:
                            feed_origin_peer = ''
                        feed_origin_peer = str(fp.get('origin_peer') or feed_origin_peer or from_peer or '').strip() or str(from_peer or '').strip()
                        _ensure_shadow_user(
                            author_id,
                            display_name,
                            feed_origin_peer,
                            allow_origin_reassign=True,
                        )
                        with db_manager.get_connection() as conn:
                            existing = conn.execute(
                                "SELECT 1 FROM feed_posts WHERE id = ?", (pid,)
                            ).fetchone()
                            if not existing:
                                conn.execute("""
                                    INSERT OR IGNORE INTO feed_posts
                                    (id, author_id, content, content_type,
                                     visibility, metadata, created_at, expires_at)
                                    VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?)
                                """, (pid, author_id, content,
                                      fp.get('content_type', 'text'),
                                      fp.get('visibility', 'network'),
                                      fp.get('metadata'),
                                      fp.get('created_at'),
                                      fp.get('expires_at')))
                                conn.commit()
                                fp_stored += 1
                    except Exception as fp_err:
                        logger.debug(f"Catchup feed post {fp.get('id','?')} failed: {fp_err}")
                if fp_stored:
                    logger.info(f"Catchup from {from_peer}: stored "
                                f"{fp_stored} missed feed posts")

            # Circle objects (v0.3.55+) — must be ingested BEFORE entries
            # so that entries have a parent circle to reference.
            if circles and circle_manager:
                co_stored = 0
                for circle_data in circles:
                    try:
                        if circle_manager.ingest_circle_snapshot(circle_data):
                            co_stored += 1
                    except Exception as co_err:
                        logger.debug(f"Catchup circle object failed: {co_err}")
                if co_stored:
                    logger.info(f"Catchup from {from_peer}: stored "
                                f"{co_stored} missed circle objects")

            # Circle entries
            if circle_entries and circle_manager:
                ce_stored = 0
                for entry in circle_entries:
                    try:
                        if circle_manager.ingest_entry_snapshot(entry):
                            ce_stored += 1
                    except Exception as ce_err:
                        logger.debug(f"Catchup circle entry failed: {ce_err}")
                if ce_stored:
                    logger.info(f"Catchup from {from_peer}: stored "
                                f"{ce_stored} missed circle entries")

            # Circle votes
            if circle_votes and circle_manager:
                cv_stored = 0
                for vote in circle_votes:
                    try:
                        circle_id = vote.get('circle_id')
                        user_id_v = vote.get('user_id')
                        option_index = vote.get('option_index')
                        created_at = vote.get('created_at')
                        if circle_id and user_id_v is not None and option_index is not None:
                            circle_manager.ingest_vote_snapshot(
                                circle_id, user_id_v, option_index,
                                created_at=created_at)
                            cv_stored += 1
                    except Exception as cv_err:
                        logger.debug(f"Catchup circle vote failed: {cv_err}")
                if cv_stored:
                    logger.info(f"Catchup from {from_peer}: stored "
                                f"{cv_stored} missed circle votes")

            # Tasks
            if tasks and task_manager:
                t_stored = 0
                for task_data in tasks:
                    try:
                        if task_manager.apply_task_snapshot(task_data):
                            t_stored += 1
                    except Exception as t_err:
                        logger.debug(f"Catchup task failed: {t_err}")
                if t_stored:
                    logger.info(f"Catchup from {from_peer}: stored "
                                f"{t_stored} missed tasks")

        p2p_manager.on_catchup_response = _on_catchup_response

        # --- Profile sync callback ---
        # Deduplication: track (peer_id, user_id) we've already relayed
        # to prevent profile sync storms when peers run different code versions.
        import time as _time
        _relayed_profiles: dict[tuple[str, str], float] = {}  # (peer_id, user_id) -> timestamp of last relay
        _PROFILE_RELAY_COOLDOWN = 30  # seconds — don't re-relay same peer's profile within this window

        # Profile hash cache — skip re-processing unchanged profiles
        _seen_profile_hashes: dict[tuple[str, str], str] = {}  # (peer_id, user_id) -> profile_hash

        def _on_profile_sync(profile_data, from_peer):
            """Handle incoming PROFILE_SYNC / PROFILE_UPDATE from a peer.

            Updates the shadow user's display_name, bio, and avatar so
            that remote users appear with their real names in the UI.

            Device profiles are stored unconditionally (keyed by peer_id),
            even if no shadow user exists yet — this is critical for
            profiles arriving via relay before any messages.
            """
            try:
                remote_peer_id = profile_data.get('peer_id', from_peer)

                # ---- Skip unchanged profiles (version hash dedup) ----
                # Exception: if our copy of this user's avatar file is missing (e.g. after
                # migration or DB copy), do not skip so we re-save the avatar from the payload.
                def _avatar_file_missing_for_user(uid: str, peer_id: str) -> bool:
                    if not uid or not getattr(profile_manager, 'file_manager', None):
                        return False
                    try:
                        with db_manager.get_connection() as conn:
                            r = conn.execute(
                                "SELECT id, avatar_file_id FROM users WHERE id = ?", (uid,)
                            ).fetchone()
                            if not r:
                                for pattern in (
                                    f"peer-{peer_id[:8]}",
                                    f"peer-{peer_id[:8]}-%",
                                    f"peer-%-{uid[-8:] if len(uid) >= 8 else ''}",
                                ):
                                    r = conn.execute(
                                        "SELECT id, avatar_file_id FROM users WHERE username LIKE ?",
                                        (pattern,),
                                    ).fetchone()
                                    if r:
                                        break
                            if not r:
                                return False
                            fid_raw = r['avatar_file_id'] if 'avatar_file_id' in r.keys() else ''
                            fid = (fid_raw or '').strip() if isinstance(fid_raw, str) else str(fid_raw or '').strip()
                            if not fid:
                                return False
                        profile_manager.file_manager.get_file_data(fid)
                        return False  # file exists
                    except Exception:
                        return True  # no user, no file_id, or get_file_data failed

                incoming_hash = profile_data.get('profile_hash')
                if incoming_hash:
                    hash_key = (remote_peer_id, profile_data.get('user_id', ''))
                    if _seen_profile_hashes.get(hash_key) == incoming_hash:
                        need_avatar = (
                            profile_data.get('avatar_thumbnail')
                            and _avatar_file_missing_for_user(
                                profile_data.get('user_id', ''), remote_peer_id
                            )
                        )
                        if not need_avatar:
                            logger.debug(
                                f"Profile from {from_peer} unchanged (hash={incoming_hash[:8]}), "
                                f"skipping")
                            return
                        logger.info(
                            "Profile hash unchanged but our avatar file is missing; "
                            "re-applying to recover avatar from peer"
                        )
                    _seen_profile_hashes[hash_key] = incoming_hash

                # ---- Store device profile FIRST (independent of user) ----
                # Device profiles are keyed by peer_id in a separate table,
                # so they don't require a shadow user to exist.  This must
                # happen before the early-return below, otherwise profiles
                # arriving via relay (before any messages) would be lost.
                device_info = profile_data.get('device')
                if device_info and remote_peer_id:
                    channel_manager.store_peer_device_profile(
                        peer_id=remote_peer_id,
                        display_name=device_info.get('display_name'),
                        description=device_info.get('description'),
                        avatar_b64=device_info.get('avatar_b64'),
                        avatar_mime=device_info.get('avatar_mime'),
                    )
                    logger.debug(f"Stored device profile for {remote_peer_id} "
                                 f"(device_name={device_info.get('display_name')})")

                # ---- Re-broadcast to other peers (relay) ----
                # Do this before the shadow-user check so profiles propagate
                # through the mesh even if this node doesn't have a shadow
                # user yet (e.g. A relays B's profile to C before C has
                # ever received a message from B).
                profile_origin = profile_data.get('peer_id', '')
                relay_key = (profile_origin, profile_data.get('user_id', '') or '')
                now = _time.time()
                last_relay = _relayed_profiles.get(relay_key, 0)
                if profile_origin == from_peer and (now - last_relay) > _PROFILE_RELAY_COOLDOWN:
                    _relayed_profiles[relay_key] = now
                    try:
                        import asyncio
                        connected = p2p_manager.get_connected_peers()
                        relayed = 0
                        for pid in connected:
                            if pid == from_peer:
                                continue  # don't echo back to sender
                            if pid == (p2p_manager.get_peer_id() or ''):
                                continue  # skip ourselves
                            if p2p_manager.message_router and p2p_manager._event_loop:
                                asyncio.run_coroutine_threadsafe(
                                    p2p_manager.message_router.send_profile_sync(
                                        pid, profile_data),
                                    p2p_manager._event_loop
                                )
                                relayed += 1
                        if relayed:
                            logger.debug(f"Relayed profile from {from_peer} to "
                                         f"{relayed} other peer(s)")
                    except Exception as relay_err:
                        logger.warning(f"Failed to relay profile: {relay_err}")

                # ---- Update user profile ----
                # If no shadow user exists yet (e.g. profile arrived via
                # relay before any messages), create one now so the
                # display name / avatar are ready when messages arrive.
                remote_user_id = profile_data.get('user_id')
                if not remote_user_id:
                    logger.debug(f"Profile sync from {from_peer}: no user_id, "
                                 f"skipping user profile update (device profile stored)")
                    return

                target_user_id = remote_user_id

                with db_manager.get_connection() as conn:
                    # Try direct user_id first (most reliable)
                    row = conn.execute(
                        "SELECT id FROM users WHERE id = ?",
                        (remote_user_id,)
                    ).fetchone()
                    if not row:
                        # Broader fallback: look for any shadow user whose
                        # username contains the peer prefix.  Shadow users
                        # may have been created with various naming patterns
                        # (peer-XXXX, peer-XXXX-YYYY, etc.)
                        for pattern in [
                            f"peer-{remote_peer_id[:8]}",
                            f"peer-{remote_peer_id[:8]}-%",
                            f"peer-%-{remote_user_id[-8:]}",
                        ]:
                            row = conn.execute(
                                "SELECT id FROM users WHERE username LIKE ?",
                                (pattern,)
                            ).fetchone()
                            if row:
                                break
                    if row:
                        target_user_id = row[0]
                    else:
                        # No shadow user yet — create one now so the
                        # profile (display name, avatar) is applied
                        # immediately instead of being lost.
                        _ensure_shadow_user(
                            remote_user_id,
                            profile_data.get('display_name'),
                            remote_peer_id,
                            allow_origin_reassign=False,
                        )
                        if db_manager.get_user(remote_user_id):
                            target_user_id = remote_user_id
                        else:
                            logger.warning(
                                f"Could not create shadow user for {remote_peer_id} "
                                f"(user_id={remote_user_id}); profile update skipped"
                            )
                            return

                # Profile sync from the actual peer always wins — force
                # update display_name even if the current name is not a
                # generic "peer-" prefix (e.g. it was set from a catchup
                # message with the wrong display name).
                profile_manager.update_from_remote(target_user_id, profile_data,
                                                   force_display_name=True)
                _ensure_origin_peer(
                    target_user_id,
                    remote_peer_id,
                    allow_remote_reassign=False,
                )
                logger.debug(f"Profile sync from {from_peer}: updated {target_user_id} "
                             f"(display_name={profile_data.get('display_name')})")

            except Exception as e:
                logger.error(f"Failed to handle profile sync: {e}", exc_info=True)

        p2p_manager.on_profile_sync = _on_profile_sync

        def _resync_user_avatar(user_id: str, hint_peer: str = "") -> dict:
            """Invalidate profile hash cache for a user and trigger re-sync from their origin peer."""
            if not user_id:
                return {"ok": False, "reason": "no user_id"}
            origin_peer = None
            try:
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT origin_peer FROM users WHERE id = ?", (user_id,)
                    ).fetchone()
                    if row:
                        origin_peer = row[0] if isinstance(row, (tuple, list)) else row.get('origin_peer', row[0])
                    if not origin_peer:
                        msg_row = conn.execute(
                            "SELECT origin_peer FROM channel_messages WHERE user_id = ? AND origin_peer IS NOT NULL AND origin_peer != '' LIMIT 1",
                            (user_id,)
                        ).fetchone()
                        if msg_row:
                            origin_peer = msg_row[0] if isinstance(msg_row, (tuple, list)) else msg_row.get('origin_peer', msg_row[0])
            except Exception as e:
                logger.warning(f"resync_user_avatar: DB lookup failed for {user_id}: {e}")

            if not origin_peer and hint_peer:
                origin_peer = hint_peer

            if not origin_peer:
                if p2p_manager:
                    connected = p2p_manager.get_connected_peers() or []
                    if connected:
                        origin_peer = connected[0]
                        logger.info(f"resync_user_avatar: no origin_peer for {user_id}, broadcasting to first connected peer {origin_peer}")

            if not origin_peer:
                return {"ok": False, "reason": "no origin_peer for user and no connected peers"}

            cleared = 0
            keys_to_clear = [k for k in _seen_profile_hashes if k[1] == user_id]
            for k in keys_to_clear:
                del _seen_profile_hashes[k]
                cleared += 1

            synced = p2p_manager.trigger_peer_sync(origin_peer) if p2p_manager else False
            logger.info(
                f"resync_user_avatar: user={user_id} origin_peer={origin_peer} "
                f"hashes_cleared={cleared} sync_triggered={synced}"
            )
            return {"ok": True, "origin_peer": origin_peer, "hashes_cleared": cleared, "sync_triggered": synced}

        setattr(p2p_manager, 'resync_user_avatar', _resync_user_avatar)  # dynamic; route checks hasattr()

        # --- Provide local profile card for sync ---
        def _get_local_profile_sync_user_ids():
            """Return local user IDs that should be included in profile sync.

            Older instances may contain local users without public keys,
            and older shadow users may have missing origin_peer metadata.
            We therefore classify sync candidates by local origin when
            available, with a conservative fallback for legacy rows.
            """
            user_ids = []
            seen = set()
            try:
                local_peer_id = (p2p_manager.get_peer_id() or '').strip() if p2p_manager else ''
                with db_manager.get_connection() as conn:
                    api_user_ids = set()
                    try:
                        api_rows = conn.execute(
                            "SELECT DISTINCT user_id FROM api_keys WHERE COALESCE(revoked, 0) = 0"
                        ).fetchall()
                        for api_row in api_rows:
                            try:
                                api_user_ids.add(api_row['user_id'])
                            except Exception:
                                api_user_ids.add(api_row[0])
                    except Exception:
                        api_user_ids = set()

                    rows = conn.execute(
                        "SELECT id, username, origin_peer, public_key, password_hash FROM users "
                        "WHERE id != 'system' AND id != 'local_user' "
                        "AND COALESCE(status, 'active') = 'active' "
                        "ORDER BY created_at ASC"
                    ).fetchall()

                    for row in rows:
                        try:
                            user_id = row['id']
                            username = (row['username'] or '').strip().lower()
                            origin_peer = (row['origin_peer'] or '').strip()
                            has_public_key = bool((row['public_key'] or '').strip())
                            has_password = bool((row['password_hash'] or '').strip())
                        except Exception:
                            user_id = row[0]
                            username = ''
                            origin_peer = ''
                            has_public_key = False
                            has_password = False

                        has_api_key = user_id in api_user_ids

                        is_local = False
                        if origin_peer:
                            is_local = bool(local_peer_id and origin_peer == local_peer_id)
                        else:
                            # Legacy fallback: local users may have no origin_peer.
                            # Avoid relaying synthetic shadow rows (peer-* without
                            # local credentials) as if they were local profiles.
                            has_local_auth_evidence = bool(has_password or has_public_key or has_api_key)
                            if username.startswith('peer-') and not has_local_auth_evidence:
                                is_local = False
                            else:
                                is_local = has_local_auth_evidence

                        if is_local and user_id and user_id not in seen:
                            seen.add(user_id)
                            user_ids.append(user_id)
            except Exception as e:
                logger.error(f"Failed to collect local profile sync users: {e}", exc_info=True)
            return user_ids

        def _get_local_profile_card():
            """Return profile card of the primary local user."""
            try:
                user_ids = _get_local_profile_sync_user_ids()
                if user_ids:
                    return profile_manager.get_profile_card(user_ids[0])
            except Exception as e:
                logger.error(f"Failed to get local profile card: {e}", exc_info=True)
            return None

        p2p_manager.get_local_profile_card = _get_local_profile_card

        def _get_all_local_profile_cards():
            """Return profile cards for ALL registered local users.
            
            This ensures that remote peers receive display names for
            every user on this device (not just the primary one), so
            messages from different users show the correct names.
            """
            cards = []
            try:
                for user_id in _get_local_profile_sync_user_ids():
                    card = profile_manager.get_profile_card(user_id)
                    if card:
                        cards.append(card)
            except Exception as e:
                logger.error(f"Failed to get all local profile cards: {e}", exc_info=True)
            return cards

        p2p_manager.get_all_local_profile_cards = _get_all_local_profile_cards

        # --- Peer announcement callback ---
        def _on_peer_announcement(introduced_peers, from_peer):
            """Handle peer introductions from a connected peer.

            Stores introduced peer identities so users can connect to
            them from the Connect page.
            """
            try:
                import base58
                for p in introduced_peers:
                    pid = p.get('peer_id')
                    epk = p.get('ed25519_public_key')
                    xpk = p.get('x25519_public_key')
                    device_profile = p.get('device_profile') if isinstance(p, dict) else None
                    if pid and epk and xpk:
                        try:
                            ed_bytes = base58.b58decode(epk)
                            x_bytes = base58.b58decode(xpk)
                            p2p_manager.identity_manager.create_remote_peer(
                                pid, ed_bytes, x_bytes
                            )
                        except Exception as e:
                            logger.warning(f"Could not register introduced peer {pid}: {e}")
                    # Store device profile if provided (helps show friendly names for contacts list)
                    if pid and device_profile and channel_manager:
                        try:
                            channel_manager.store_peer_device_profile(
                                pid,
                                display_name=device_profile.get('display_name'),
                                description=device_profile.get('description'),
                                avatar_b64=device_profile.get('avatar_b64'),
                                avatar_mime=device_profile.get('avatar_mime'),
                            )
                        except Exception:
                            pass

                p2p_manager.store_introduced_peers(introduced_peers, from_peer)
                logger.info(f"Peer announcement from {from_peer}: "
                            f"{len(introduced_peers)} peer(s) introduced")
            except Exception as e:
                logger.error(f"Failed to handle peer announcement: {e}", exc_info=True)

        p2p_manager.on_peer_announcement = _on_peer_announcement

        # --- Feed post P2P handler ---
        def _on_p2p_feed_post(post_id, author_id, content, post_type,
                               visibility, timestamp, metadata,
                               expires_at, ttl_seconds, ttl_mode,
                               display_name, from_peer):
            """Store an incoming P2P feed post locally. Updates content/metadata when post already exists (edit broadcast)."""
            try:
                # Ensure shadow user exists (reuse channel message logic)
                feed_origin_peer = ''
                try:
                    if isinstance(metadata, dict):
                        feed_origin_peer = str(metadata.get('origin_peer') or '').strip()
                except Exception:
                    feed_origin_peer = ''
                feed_origin_peer = feed_origin_peer or str(from_peer or '').strip()
                _ensure_shadow_user(
                    author_id,
                    display_name,
                    feed_origin_peer,
                    allow_origin_reassign=True,
                )

                # Normalise timestamp
                normalised_ts = None
                created_dt = None
                if timestamp:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        dt = _dt.fromisoformat(timestamp.replace('Z', '+00:00'))
                        normalised_ts = dt.strftime('%Y-%m-%d %H:%M:%S')
                        created_dt = dt
                    except Exception:
                        normalised_ts = timestamp
                if created_dt is None:
                    from datetime import datetime as _dt, timezone as _tz
                    created_dt = _dt.now(_tz.utc)

                # Process feed attachments with embedded data (if any)
                processed_attachments = None
                if metadata and metadata.get('attachments'):
                    try:
                        processed_attachments = []
                        for att in metadata.get('attachments') or []:
                            normalized = _normalize_incoming_attachment_entry(
                                att,
                                uploaded_by=author_id,
                                default_source_peer_id=from_peer,
                                source_context={
                                    'source_type': 'feed_post',
                                    'source_id': post_id,
                                },
                            )
                            if normalized:
                                processed_attachments.append(normalized)
                        metadata = dict(metadata)
                        metadata['attachments'] = processed_attachments
                    except Exception:
                        pass
                if metadata is None:
                    metadata = {}
                metadata = dict(metadata)
                metadata.setdefault('origin_peer', from_peer)

                # Resolve expiry through FeedManager so retention policy is
                # consistent across local API/UI writes and P2P sync writes.
                expires_raw = expires_at or (metadata or {}).get('expires_at')
                ttl_raw = ttl_seconds if ttl_seconds is not None else (metadata or {}).get('ttl_seconds')
                ttl_mode_val = ttl_mode or (metadata or {}).get('ttl_mode')

                try:
                    expires_dt = feed_manager._resolve_expiry(
                        expires_at=expires_raw,
                        ttl_seconds=ttl_raw,
                        ttl_mode=ttl_mode_val,
                        apply_default=True,
                        base_time=created_dt,
                    )
                except Exception:
                    from datetime import timedelta as _td
                    expires_dt = created_dt + _td(days=90)

                if expires_dt:
                    from datetime import datetime as _dt
                    from datetime import timezone as _tz
                    now_dt = _dt.now(_tz.utc)
                    if expires_dt <= now_dt:
                        logger.debug(f"Skipping expired P2P feed post {post_id}")
                        return

                expires_db = None
                if expires_dt:
                    from datetime import timezone as _tz
                    expires_db = expires_dt.astimezone(_tz.utc).strftime('%Y-%m-%d %H:%M:%S')

                # Store or update the feed post
                import secrets as _sec2
                pid = post_id or f"FP{_sec2.token_hex(12)}"
                metadata_json = json.dumps(metadata) if metadata else None

                is_new_post = True
                with db_manager.get_connection() as conn:
                    existing = conn.execute(
                        "SELECT author_id, content_type, visibility, metadata FROM feed_posts WHERE id = ?",
                        (pid,)
                    ).fetchone()
                    if existing:
                        is_new_post = False
                        if existing['author_id'] == author_id:
                            final_post_type = post_type or existing['content_type'] or 'text'
                            final_visibility = visibility or existing['visibility'] or 'network'
                            final_metadata = metadata_json if metadata_json is not None else existing['metadata']
                            conn.execute(
                                "UPDATE feed_posts SET content = ?, content_type = ?, visibility = ?, metadata = ?, expires_at = ? WHERE id = ?",
                                (content, final_post_type, final_visibility, final_metadata, expires_db, pid)
                            )
                            conn.commit()
                            # Sync inline circles on updates as well
                            try:
                                from .circles import parse_circle_blocks, derive_circle_id
                                if circle_manager:
                                    circle_specs = parse_circle_blocks(content or '')
                                    if circle_specs:
                                        for idx, spec in enumerate(cast(Any, circle_specs)):
                                            spec = cast(Any, spec)
                                            circle_id = derive_circle_id('feed', pid, idx, len(circle_specs), override=spec.circle_id)
                                            facilitator_id = None
                                            if spec.facilitator:
                                                handle = spec.facilitator.strip()
                                                if handle.startswith('@'):
                                                    handle = handle[1:]
                                                try:
                                                    row = db_manager.get_user(handle)
                                                    if row:
                                                        facilitator_id = row.get('id') or handle
                                                except Exception:
                                                    facilitator_id = None
                                                if not facilitator_id:
                                                    targets = resolve_mention_targets(
                                                        db_manager,
                                                        [handle],
                                                        visibility=final_visibility,
                                                        permissions=None,
                                                        author_id=author_id,
                                                    )
                                                    if targets:
                                                        facilitator_id = targets[0].get('user_id')
                                            if not facilitator_id:
                                                facilitator_id = author_id

                                            if spec.participants is not None:
                                                resolved = []
                                                for part in spec.participants or []:
                                                    try:
                                                        prow = db_manager.get_user(part)
                                                        if prow:
                                                            resolved.append(prow.get('id') or part)
                                                            continue
                                                    except Exception:
                                                        pass
                                                    try:
                                                        targets = resolve_mention_targets(
                                                            db_manager,
                                                            [part],
                                                            visibility=final_visibility,
                                                            permissions=None,
                                                            author_id=author_id,
                                                        )
                                                        if targets:
                                                            resolved.append(targets[0].get('user_id'))
                                                    except Exception:
                                                        continue
                                                spec.participants = resolved

                                            circle_manager.upsert_circle(
                                                circle_id=circle_id,
                                                source_type='feed',
                                                source_id=pid,
                                                created_by=author_id,
                                                spec=spec,
                                                facilitator_id=facilitator_id,
                                                visibility=final_visibility,
                                                origin_peer=from_peer,
                                                created_at=normalised_ts,
                                            )
                            except Exception as circle_err:
                                logger.warning(f"Inline circle update (P2P feed) failed: {circle_err}")

                            # Sync inline handoffs on updates as well
                            try:
                                from .handoffs import parse_handoff_blocks, derive_handoff_id
                                handoff_manager = app.config.get('HANDOFF_MANAGER')
                                if handoff_manager:
                                    handoff_specs = parse_handoff_blocks(content or '')
                                    if handoff_specs:
                                        vis_val = visibility or 'network'
                                        for hidx, hspec in enumerate(handoff_specs):
                                            if not hspec.confirmed:
                                                continue
                                            handoff_id = derive_handoff_id(
                                                'feed', pid, hidx, len(handoff_specs), override=hspec.handoff_id
                                            )
                                            handoff_manager.upsert_handoff(
                                                handoff_id=handoff_id,
                                                source_type='feed',
                                                source_id=pid,
                                                author_id=author_id,
                                                title=hspec.title,
                                                summary=hspec.summary,
                                                next_steps=hspec.next_steps,
                                                owner=hspec.owner,
                                                tags=hspec.tags,
                                                raw=hspec.raw,
                                                channel_id=None,
                                                visibility=vis_val,
                                                origin_peer=from_peer,
                                                permissions=None,
                                                created_at=normalised_ts,
                                            )
                            except Exception as handoff_err:
                                logger.warning(f"Inline handoff update (P2P feed) failed: {handoff_err}")
                            try:
                                edited_at = None
                                try:
                                    meta_for_edit = json.loads(final_metadata) if isinstance(final_metadata, str) and final_metadata else None
                                except Exception:
                                    meta_for_edit = None
                                if isinstance(meta_for_edit, dict):
                                    edited_at = meta_for_edit.get('edited_at')
                                sync_edited_mention_activity(
                                    db_manager=db_manager,
                                    mention_manager=mention_manager,
                                    inbox_manager=inbox_manager,
                                    p2p_manager=p2p_manager,
                                    content=content,
                                    source_type='feed_post',
                                    source_id=pid,
                                    author_id=author_id,
                                    origin_peer=from_peer,
                                    channel_id=None,
                                    visibility=final_visibility,
                                    permissions=None,
                                    edited_at=edited_at,
                                )
                            except Exception as mention_sync_err:
                                logger.warning(
                                    "Failed to refresh feed edit notices for %s: %s",
                                    pid,
                                    mention_sync_err,
                                )
                        return
                    conn.execute("""
                        INSERT OR IGNORE INTO feed_posts
                        (id, author_id, content, content_type, visibility, metadata, created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?)
                    """, (pid, author_id, content, post_type or 'text',
                          visibility or 'network', metadata_json, normalised_ts, expires_db))
                    conn.commit()

                logger.info(f"Stored P2P feed post {pid} by {author_id} from peer {from_peer}")

                if is_new_post:
                    mentions = extract_mentions(content or '')
                    targets = _resolve_local_mentions(
                        mentions,
                        visibility=visibility,
                        permissions=None,
                        author_id=author_id,
                    )
                    if targets:
                        preview = build_preview(content or '')
                        _record_local_mention_events(
                            targets=targets,
                            source_type='feed_post',
                            source_id=pid,
                            author_id=author_id,
                            from_peer=from_peer,
                            channel_id=None,
                            preview=preview,
                            source_content=content,
                        )

                    # Inline circles from [circle] blocks
                    try:
                        from .circles import parse_circle_blocks, derive_circle_id
                        if circle_manager:
                            circle_specs = parse_circle_blocks(content or '')
                            if circle_specs:
                                vis_val = visibility or 'network'
                                for idx, spec in enumerate(cast(Any, circle_specs)):
                                    spec = cast(Any, spec)
                                    circle_id = derive_circle_id('feed', pid, idx, len(circle_specs), override=spec.circle_id)
                                    facilitator_id = None
                                    if spec.facilitator:
                                        handle = spec.facilitator.strip()
                                        if handle.startswith('@'):
                                            handle = handle[1:]
                                        try:
                                            row = db_manager.get_user(handle)
                                            if row:
                                                facilitator_id = row.get('id') or handle
                                        except Exception:
                                            facilitator_id = None
                                        if not facilitator_id:
                                            targets = resolve_mention_targets(
                                                db_manager,
                                                [handle],
                                                visibility=vis_val,
                                                permissions=None,
                                                author_id=author_id,
                                            )
                                            if targets:
                                                facilitator_id = targets[0].get('user_id')
                                    if not facilitator_id:
                                        facilitator_id = author_id

                                    if spec.participants is not None:
                                        resolved = []
                                        for part in spec.participants or []:
                                            try:
                                                pid_row = db_manager.get_user(part)
                                                if pid_row:
                                                    resolved.append(pid_row.get('id') or part)
                                                    continue
                                            except Exception:
                                                pass
                                            try:
                                                targets = resolve_mention_targets(
                                                    db_manager,
                                                    [part],
                                                    visibility=vis_val,
                                                    permissions=None,
                                                    author_id=author_id,
                                                )
                                                if targets:
                                                    resolved.append(targets[0].get('user_id'))
                                            except Exception:
                                                continue
                                        spec.participants = resolved

                                    circle_manager.upsert_circle(
                                        circle_id=circle_id,
                                        source_type='feed',
                                        source_id=pid,
                                        created_by=author_id,
                                        spec=spec,
                                        facilitator_id=facilitator_id,
                                        visibility=vis_val,
                                        origin_peer=from_peer,
                                        created_at=normalised_ts,
                                    )
                    except Exception as circle_err:
                        logger.warning(f"Inline circle creation (P2P feed) failed: {circle_err}")

                    # Inline tasks from [task] blocks
                    try:
                        from .tasks import parse_task_blocks, derive_task_id
                        if task_manager:
                            task_specs = parse_task_blocks(content or '')
                            if task_specs:
                                vis_val = visibility or 'network'
                                task_visibility = 'network' if vis_val in ('public', 'network') else 'local'
                                for idx, spec in enumerate(cast(Any, task_specs)):
                                    spec = cast(Any, spec)
                                    if not spec.confirmed:
                                        continue
                                    task_id = derive_task_id('feed', pid, idx, len(task_specs), override=spec.task_id)
                                    assignee_id = None
                                    if spec.assignee_clear:
                                        assignee_id = None
                                    elif spec.assignee:
                                        handle = spec.assignee.strip()
                                        if handle.startswith('@'):
                                            handle = handle[1:]
                                        try:
                                            row = db_manager.get_user(handle)
                                            if row:
                                                assignee_id = row.get('id') or handle
                                        except Exception:
                                            assignee_id = None
                                        if not assignee_id:
                                            targets = resolve_mention_targets(
                                                db_manager,
                                                [handle],
                                                visibility=vis_val,
                                                permissions=None,
                                                author_id=author_id,
                                            )
                                            if targets:
                                                assignee_id = targets[0].get('user_id')
                                    editor_ids: list[str] = []
                                    if spec.editors_clear:
                                        editor_ids = []
                                    else:
                                        for editor in spec.editors or []:
                                            try:
                                                eid = None
                                                row = db_manager.get_user(editor)
                                                if row:
                                                    eid = row.get('id') or editor
                                            except Exception:
                                                eid = None
                                            if not eid:
                                                try:
                                                    targets = resolve_mention_targets(
                                                        db_manager,
                                                        [editor],
                                                        visibility=vis_val,
                                                        permissions=None,
                                                        author_id=author_id,
                                                    )
                                                    if targets:
                                                        eid = targets[0].get('user_id')
                                                except Exception:
                                                    eid = None
                                            if eid:
                                                editor_ids.append(eid)

                                    meta_payload = {
                                        'inline_task': True,
                                        'source_type': 'feed_post',
                                        'source_id': pid,
                                        'post_visibility': vis_val,
                                    }
                                    if editor_ids is not None:
                                        meta_payload['editors'] = editor_ids

                                    task_manager.create_task(
                                        task_id=task_id,
                                        title=spec.title,
                                        description=spec.description,
                                        status=spec.status,
                                        priority=spec.priority,
                                        created_by=author_id,
                                        assigned_to=assignee_id,
                                        due_at=spec.due_at.isoformat() if spec.due_at else None,
                                        visibility=task_visibility,
                                        metadata=meta_payload,
                                        origin_peer=from_peer,
                                        source_type='human',
                                        updated_by=author_id,
                                    )
                    except Exception as task_err:
                        logger.warning(f"Inline task creation (P2P feed) failed: {task_err}")

                    # Inline objectives from [objective] blocks
                    try:
                        from .objectives import parse_objective_blocks, derive_objective_id
                        objective_manager = app.config.get('OBJECTIVE_MANAGER')
                        if objective_manager:
                            obj_specs = parse_objective_blocks(content or '')
                            if obj_specs:
                                vis_val = visibility or 'network'
                                obj_visibility = 'network' if vis_val in ('public', 'network') else 'local'

                                def _resolve_handle(handle: str) -> Optional[str]:
                                    if not handle:
                                        return None
                                    token = handle.strip()
                                    if token.startswith('@'):
                                        token = token[1:]
                                    if not token:
                                        return None
                                    try:
                                        row = db_manager.get_user(token)
                                        if row:
                                            return row.get('id') or token
                                    except Exception:
                                        pass
                                    try:
                                        targets = resolve_mention_targets(
                                            db_manager,
                                            [token],
                                            visibility=vis_val,
                                            permissions=None,
                                            author_id=author_id,
                                        )
                                        if targets:
                                            return targets[0].get('user_id')
                                    except Exception:
                                        return None
                                    return None

                                for oidx, spec in enumerate(obj_specs):
                                    objective_id = derive_objective_id('feed', pid, oidx, len(obj_specs), override=spec.objective_id)
                                    members_payload = []
                                    for member in spec.members or []:
                                        uid = _resolve_handle(member.handle)
                                        if uid:
                                            members_payload.append({
                                                'user_id': uid,
                                                'role': member.role,
                                            })
                                    tasks_payload = []
                                    for t in spec.tasks or []:
                                        assignee_id = _resolve_handle(t.assignee) if t.assignee else None
                                        tasks_payload.append({
                                            'title': t.title,
                                            'status': t.status,
                                            'assigned_to': assignee_id,
                                            'metadata': {
                                                'inline_objective_task': True,
                                                'source_type': 'feed_post',
                                                'source_id': pid,
                                                'post_visibility': vis_val,
                                            },
                                        })
                                    objective_manager.upsert_objective(
                                        objective_id=objective_id,
                                        title=spec.title,
                                        description=spec.description,
                                        status=spec.status,
                                        deadline=spec.deadline.isoformat() if spec.deadline else None,
                                        created_by=author_id,
                                        visibility=obj_visibility,
                                        origin_peer=from_peer,
                                        source_type='feed_post',
                                        source_id=pid,
                                        created_at=normalised_ts,
                                        members=members_payload,
                                        tasks=tasks_payload,
                                        updated_by=author_id,
                                    )
                    except Exception as obj_err:
                        logger.warning(f"Inline objective creation (P2P feed) failed: {obj_err}")

                    # Inline signals from [signal] blocks
                    try:
                        from .signals import parse_signal_blocks, derive_signal_id
                        signal_manager = app.config.get('SIGNAL_MANAGER')
                        if signal_manager:
                            sig_specs = parse_signal_blocks(content or '')
                            if sig_specs:
                                vis_val = visibility or 'network'
                                sig_visibility = 'network' if vis_val in ('public', 'network') else 'local'

                                def _resolve_handle(handle: str) -> Optional[str]:
                                    if not handle:
                                        return None
                                    token = handle.strip()
                                    if token.startswith('@'):
                                        token = token[1:]
                                    if not token:
                                        return None
                                    try:
                                        row = db_manager.get_user(token)
                                        if row:
                                            return row.get('id') or token
                                    except Exception:
                                        pass
                                    try:
                                        targets = resolve_mention_targets(
                                            db_manager,
                                            [token],
                                            visibility=vis_val,
                                            permissions=None,
                                            author_id=author_id,
                                        )
                                        if targets:
                                            return targets[0].get('user_id')
                                    except Exception:
                                        return None
                                    return None

                                for sidx, spec in enumerate(sig_specs):
                                    signal_id = derive_signal_id('feed', pid, sidx, len(sig_specs), override=spec.signal_id)
                                    owner_id = _resolve_handle(spec.owner) if spec.owner else author_id
                                    signal_manager.upsert_signal(
                                        signal_id=signal_id,
                                        signal_type=spec.signal_type,
                                        title=spec.title,
                                        summary=spec.summary,
                                        status=spec.status,
                                        confidence=spec.confidence,
                                        tags=spec.tags,
                                        data=spec.data,
                                        notes=spec.notes,
                                        owner_id=owner_id or author_id,
                                        created_by=author_id,
                                        visibility=sig_visibility,
                                        origin_peer=from_peer,
                                        source_type='feed_post',
                                        source_id=pid,
                                        expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                                        ttl_seconds=spec.ttl_seconds,
                                        ttl_mode=spec.ttl_mode,
                                        created_at=normalised_ts,
                                        actor_id=author_id,
                                    )
                    except Exception as sig_err:
                        logger.warning(f"Inline signal creation (P2P feed) failed: {sig_err}")

                    # Inline contracts from [contract] blocks
                    try:
                        from .contracts import parse_contract_blocks, derive_contract_id
                        contract_manager = app.config.get('CONTRACT_MANAGER')
                        if contract_manager:
                            contract_specs = parse_contract_blocks(content or '')
                            if contract_specs:
                                vis_val = visibility or 'network'
                                contract_visibility = 'network' if vis_val in ('public', 'network') else 'local'

                                def _resolve_contract_handle(handle: str) -> Optional[str]:
                                    if not handle:
                                        return None
                                    token = handle.strip()
                                    if token.startswith('@'):
                                        token = token[1:]
                                    if not token:
                                        return None
                                    try:
                                        row = db_manager.get_user(token)
                                        if row:
                                            return row.get('id') or token
                                    except Exception:
                                        pass
                                    try:
                                        targets = resolve_mention_targets(
                                            db_manager,
                                            [token],
                                            visibility=vis_val,
                                            permissions=None,
                                            author_id=author_id,
                                        )
                                        if targets:
                                            return targets[0].get('user_id')
                                    except Exception:
                                        return None
                                    return None

                                for cidx, spec in enumerate(contract_specs):
                                    if not spec.confirmed:
                                        continue
                                    contract_id = derive_contract_id(
                                        'feed', pid, cidx, len(contract_specs), override=spec.contract_id
                                    )
                                    owner_id = _resolve_contract_handle(spec.owner) if spec.owner else author_id
                                    counterparty_ids = []
                                    for cp in spec.counterparties or []:
                                        cp_id = _resolve_contract_handle(cp)
                                        if cp_id:
                                            counterparty_ids.append(cp_id)
                                    contract_manager.upsert_contract(
                                        contract_id=contract_id,
                                        title=spec.title,
                                        summary=spec.summary,
                                        terms=spec.terms,
                                        status=spec.status,
                                        owner_id=owner_id or author_id,
                                        counterparties=counterparty_ids,
                                        created_by=author_id,
                                        visibility=contract_visibility,
                                        origin_peer=from_peer,
                                        source_type='feed_post',
                                        source_id=pid,
                                        expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                                        ttl_seconds=spec.ttl_seconds,
                                        ttl_mode=spec.ttl_mode,
                                        metadata=spec.metadata,
                                        created_at=normalised_ts,
                                        actor_id=author_id,
                                    )
                    except Exception as contract_err:
                        logger.warning(f"Inline contract creation (P2P feed) failed: {contract_err}")

                    # Inline requests from [request] blocks
                    try:
                        from .requests import parse_request_blocks, derive_request_id
                        request_manager = app.config.get('REQUEST_MANAGER')
                        if request_manager:
                            req_specs = parse_request_blocks(content or '')
                            if req_specs:
                                vis_val = visibility or 'network'
                                req_visibility = 'network' if vis_val in ('public', 'network') else 'local'
                                for ridx, spec in enumerate(req_specs):
                                    if not spec.confirmed:
                                        continue
                                    request_id = derive_request_id('feed', pid, ridx, len(req_specs), override=spec.request_id)
                                    members_payload = []
                                    for member in cast(Any, spec.members or []):
                                        uid = _resolve_handle(member.handle) if hasattr(member, 'handle') else None
                                        if uid:
                                            members_payload.append({'user_id': uid, 'role': member.role})
                                    request_manager.upsert_request(
                                        request_id=request_id,
                                        title=spec.title,
                                        created_by=author_id,
                                        request_text=spec.request,
                                        required_output=spec.required_output,
                                        status=spec.status,
                                        priority=spec.priority,
                                        tags=spec.tags,
                                        due_at=spec.due_at.isoformat() if spec.due_at else None,
                                        visibility=req_visibility,
                                        origin_peer=from_peer,
                                        source_type='feed_post',
                                        source_id=pid,
                                        created_at=normalised_ts,
                                        actor_id=author_id,
                                        members=members_payload,
                                        members_defined=('members' in spec.fields),
                                        fields=spec.fields,
                                    )
                    except Exception as req_err:
                        logger.warning(f"Inline request creation (P2P feed) failed: {req_err}")

                    # Inline handoffs from [handoff] blocks
                    try:
                        from .handoffs import parse_handoff_blocks, derive_handoff_id
                        handoff_manager = app.config.get('HANDOFF_MANAGER')
                        if handoff_manager:
                            handoff_specs = parse_handoff_blocks(content or '')
                            if handoff_specs:
                                vis_val = visibility or 'network'
                                for hidx, hspec in enumerate(handoff_specs):
                                    if not hspec.confirmed:
                                        continue
                                    handoff_id = derive_handoff_id(
                                        'feed', pid, hidx, len(handoff_specs), override=hspec.handoff_id
                                    )
                                    handoff_manager.upsert_handoff(
                                        handoff_id=handoff_id,
                                        source_type='feed',
                                        source_id=pid,
                                        author_id=author_id,
                                        title=hspec.title,
                                        summary=hspec.summary,
                                        next_steps=hspec.next_steps,
                                        owner=hspec.owner,
                                        tags=hspec.tags,
                                        raw=hspec.raw,
                                        channel_id=None,
                                        visibility=vis_val,
                                        origin_peer=from_peer,
                                        permissions=None,
                                        created_at=normalised_ts,
                                    )
                    except Exception as handoff_err:
                        logger.warning(f"Inline handoff creation (P2P feed) failed: {handoff_err}")

            except Exception as e:
                logger.error(f"Failed to store P2P feed post: {e}", exc_info=True)

        p2p_manager.on_feed_post = _on_p2p_feed_post

        # --- Interaction P2P handler ---
        def _on_p2p_interaction(item_id, user_id, action, item_type,
                                 display_name, metadata, from_peer):
            """Apply an incoming P2P interaction locally (idempotent)."""
            try:
                # Ensure shadow user exists
                interaction_origin_peer = str((metadata or {}).get('origin_peer') or from_peer or '').strip() or str(from_peer or '').strip()
                _ensure_shadow_user(
                    user_id,
                    display_name,
                    interaction_origin_peer,
                    allow_origin_reassign=True,
                )
                meta = metadata or {}

                if action == 'mention':
                    target_ids = meta.get('target_user_ids') or []
                    if isinstance(target_ids, str):
                        target_ids = [target_ids]
                    single_target = meta.get('target_user_id')
                    if single_target:
                        target_ids = list(target_ids) + [single_target]

                    source_id = meta.get('source_id') or item_id
                    source_type = (meta.get('source_type') or item_type or 'feed_post').strip()
                    author_id = meta.get('author_id') or user_id
                    channel_id = meta.get('channel_id')
                    preview = build_preview(meta.get('preview') or '')

                    local_targets = []
                    for tid in target_ids:
                        if not tid:
                            continue
                        try:
                            row = db_manager.get_user(tid)
                        except Exception:
                            row = None
                        if not row:
                            continue
                        # Include user if they have a public_key (local/registered) or are an
                        # agent — agents without public_key (e.g. API-key-only) should still
                        # receive inbox items when mentioned via P2P broadcast.
                        has_key = bool((row.get('public_key') or '').strip())
                        is_agent = (row.get('account_type') or '').strip().lower() == 'agent'
                        if has_key or is_agent:
                            local_targets.append({'user_id': tid})

                    if local_targets:
                        # Extract source_content from P2P metadata so inbox
                        # items include the full message, not just preview.
                        p2p_source_content = meta.get('content') or meta.get('source_content')
                        _record_local_mention_events(
                            targets=local_targets,
                            source_type=source_type,
                            source_id=source_id,
                            author_id=author_id,
                            from_peer=from_peer,
                            channel_id=channel_id,
                            preview=preview,
                            source_content=p2p_source_content,
                        )
                    return

                if action in ('task_create', 'task_update', 'task_status', 'task_assign') or item_type == 'task':
                    if task_manager:
                        task_payload = meta.get('task') or {}
                        if not isinstance(task_payload, dict):
                            task_payload = {}
                        if item_id and not task_payload.get('id'):
                            task_payload['id'] = item_id
                        if not task_payload.get('created_by'):
                            task_payload['created_by'] = user_id
                        if from_peer and not task_payload.get('origin_peer'):
                            task_payload['origin_peer'] = from_peer
                        task_manager.apply_task_snapshot(task_payload)
                    return

                if action == 'circle_entry' or item_type == 'circle_entry':
                    if circle_manager:
                        entry_payload = meta.get('entry') or {}
                        if not isinstance(entry_payload, dict):
                            entry_payload = {}
                        if item_id and not entry_payload.get('id'):
                            entry_payload['id'] = item_id
                        if not entry_payload.get('circle_id'):
                            entry_payload['circle_id'] = meta.get('circle_id') or item_id
                        if not entry_payload.get('user_id'):
                            entry_payload['user_id'] = user_id
                        circle_manager.ingest_entry_snapshot(entry_payload)
                    return

                if action == 'circle_phase':
                    if circle_manager:
                        circle_id = meta.get('circle_id') or item_id
                        phase = cast(str, meta.get('phase'))
                        updated_at = meta.get('updated_at')
                        circle_manager.ingest_phase_snapshot(
                            circle_id,
                            phase,
                            updated_at=updated_at,
                            round_number=meta.get('round_number'),
                        )
                    return

                if action == 'circle_vote':
                    if circle_manager:
                        circle_id = meta.get('circle_id') or item_id
                        circle_option_index = cast(int, meta.get('option_index'))
                        circle_manager.ingest_vote_snapshot(circle_id, user_id, circle_option_index, created_at=meta.get('created_at'))
                    return

                with db_manager.get_connection() as conn:
                    if action == 'poll_vote':
                        poll_id = meta.get('poll_id') or item_id
                        poll_kind = (meta.get('poll_kind') or 'feed').strip().lower()
                        poll_option_index = meta.get('option_index')
                        if poll_id is not None and poll_option_index is not None:
                            try:
                                interaction_manager.record_poll_vote(
                                    poll_id=poll_id,
                                    item_type=poll_kind,
                                    user_id=user_id,
                                    option_index=poll_option_index,
                                )
                            except Exception:
                                pass
                        logger.info(f"Applied P2P poll vote for {poll_id} ({poll_kind}) by {user_id}")
                        return

                    if action == 'poll_closed':
                        poll_id = meta.get('poll_id') or item_id
                        poll_kind = (meta.get('poll_kind') or 'feed').strip().lower()
                        summary = meta.get('summary') or meta.get('preview')
                        if poll_id:
                            interaction_manager.mark_poll_closed(poll_id, poll_kind, summary=summary)
                        logger.info(f"Applied P2P poll closure for {poll_id} ({poll_kind}) by {user_id}")
                        return

                    if action in ('like', 'unlike') and item_type != 'post':
                        _ch_row = conn.execute(
                            "SELECT channel_id FROM channel_messages WHERE id = ?",
                            (item_id,),
                        ).fetchone()
                        if _ch_row:
                            _priv = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (_ch_row['channel_id'],),
                            ).fetchone()
                            if _priv and (_priv['privacy_mode'] or 'open').lower() in ('private', 'confidential'):
                                _lp = p2p_manager.get_peer_id() if p2p_manager else None
                                if not conn.execute(
                                    "SELECT 1 FROM channel_members cm "
                                    "JOIN users u ON cm.user_id = u.id "
                                    "WHERE cm.channel_id = ? "
                                    "AND (u.origin_peer IS NULL OR u.origin_peer = '' "
                                    "     OR u.origin_peer = ?) LIMIT 1",
                                    (_ch_row['channel_id'], _lp or ''),
                                ).fetchone():
                                    return

                    if action == 'like':
                        import secrets as _sec2
                        like_id = f"L{_sec2.token_hex(8)}"
                        conn.execute("""
                            INSERT OR IGNORE INTO likes (id, message_id, user_id, reaction_type)
                            VALUES (?, ?, ?, 'like')
                        """, (like_id, item_id, user_id))

                        if item_type == 'post':
                            conn.execute(
                                "UPDATE feed_posts SET likes = likes + 1 WHERE id = ? AND "
                                "NOT EXISTS (SELECT 1 FROM likes WHERE message_id = ? AND user_id = ? AND id != ?)",
                                (item_id, item_id, user_id, like_id))

                    elif action == 'unlike':
                        conn.execute("""
                            DELETE FROM likes WHERE message_id = ? AND user_id = ?
                        """, (item_id, user_id))
                        if item_type == 'post':
                            conn.execute(
                                "UPDATE feed_posts SET likes = MAX(0, likes - 1) WHERE id = ?",
                                (item_id,))

                    conn.commit()

                logger.info(f"Applied P2P interaction: {action} on {item_type} {item_id} "
                            f"by {user_id} from peer {from_peer}")

            except Exception as e:
                logger.error(f"Failed to apply P2P interaction: {e}", exc_info=True)

        p2p_manager.on_interaction = _on_p2p_interaction

        # --- Direct message handler (P2P) ---
        def _on_p2p_direct_message(sender_id, recipient_id, content,
                                    message_id, timestamp, display_name,
                                    metadata, update_only, edited_at, from_peer):
            """Handle an incoming direct message from P2P.

            Only store the message if the recipient is a local user on
            this node.  Otherwise ignore it (the message was broadcast
            to all peers, but only the recipient's node should store it).
            """
            try:
                if not recipient_id:
                    return

                # Ignore local echo: we already store local sends before broadcast.
                try:
                    local_peer_id = p2p_manager.get_peer_id() if p2p_manager else None
                except Exception:
                    local_peer_id = None
                if local_peer_id and from_peer == local_peer_id:
                    if message_id:
                        try:
                            channel_manager.mark_message_processed(message_id)
                        except Exception:
                            pass
                    return

                # Check if recipient is a local user on this node
                recipient = db_manager.get_user(recipient_id)
                if not recipient:
                    logger.debug(f"DM for {recipient_id} — not a local user, ignoring")
                    return

                # Skip if recipient is a shadow/peer user (not a real local account)
                r_username = recipient.get('username', '')
                if r_username.startswith('peer-'):
                    logger.debug(f"DM for {recipient_id} — shadow user, ignoring")
                    return

                update_only = bool(update_only)

                if update_only and not message_id:
                    logger.debug("DM update missing message_id — ignoring")
                    return

                # If we already stored this message (e.g., local send echoed back via relay),
                # skip storing a duplicate and mark it processed to stop replays.
                # BUT: allow update_only edits through — the message MUST exist for edits.
                if message_id and not update_only:
                    try:
                        with db_manager.get_connection() as conn:
                            existing_row = conn.execute(
                                "SELECT id FROM messages WHERE id = ?",
                                (message_id,)
                            ).fetchone()
                        if existing_row:
                            try:
                                channel_manager.mark_message_processed(message_id)
                            except Exception:
                                pass
                            return
                    except Exception:
                        pass

                # Dedup
                mid = message_id or f"dm_{sender_id}_{timestamp}"
                if mid and channel_manager.is_message_processed(mid) and not update_only:
                    return

                # Track existing message for update-only edits
                existing_msg = None
                if message_id:
                    try:
                        with db_manager.get_connection() as conn:
                            existing_msg = conn.execute(
                                "SELECT sender_id, message_type, metadata FROM messages WHERE id = ?",
                                (message_id,)
                            ).fetchone()
                    except Exception:
                        existing_msg = None

                # Ensure shadow user exists for sender
                _ensure_shadow_user(
                    sender_id,
                    display_name,
                    str(from_peer or '').strip(),
                    allow_origin_reassign=True,
                )

                meta_payload = metadata if isinstance(metadata, dict) else {}
                local_peer_id_for_dm = str((p2p_manager.get_peer_id() if p2p_manager else '') or '').strip()
                local_private_key = None
                try:
                    local_private_key = (
                        p2p_manager.local_identity.x25519_private_key
                        if p2p_manager and getattr(p2p_manager, 'local_identity', None)
                        else None
                    )
                except Exception:
                    local_private_key = None
                content_resolved, meta_payload, resolved_security = unwrap_dm_transport_bundle(
                    content or '',
                    meta_payload,
                    local_peer_id_for_dm,
                    local_private_key,
                )

                # Process DM attachments with embedded data (if any)
                if meta_payload and meta_payload.get('attachments'):
                    try:
                        processed_attachments = []
                        for att in meta_payload.get('attachments') or []:
                            normalized = _normalize_incoming_attachment_entry(
                                att,
                                uploaded_by=sender_id,
                                default_source_peer_id=from_peer,
                                source_context={
                                    'source_type': 'dm',
                                    'source_id': message_id,
                                },
                            )
                            if normalized:
                                processed_attachments.append(normalized)
                        meta_payload = dict(meta_payload)
                        meta_payload['attachments'] = processed_attachments
                    except Exception:
                        pass

                if meta_payload is not None:
                    meta_payload = dict(meta_payload)
                    meta_payload.setdefault('origin_peer', from_peer)
                    meta_payload['security'] = dict(resolved_security)

                dm_target_ids = []
                if isinstance(meta_payload, dict) and meta_payload.get('group_members'):
                    dm_target_ids = filter_local_dm_targets(
                        db_manager,
                        p2p_manager,
                        [
                            member_id
                            for member_id in (meta_payload.get('group_members') or [])
                            if str(member_id or '').strip() and str(member_id).strip() != sender_id
                        ],
                    )
                if not dm_target_ids:
                    dm_target_ids = filter_local_dm_targets(db_manager, p2p_manager, [recipient_id])

                storage_recipient_id = (
                    str(meta_payload.get('group_id') or '').strip()
                    if isinstance(meta_payload, dict) and meta_payload.get('group_id')
                    else recipient_id
                )

                from canopy.core.messaging import MessageType as MsgType
                if update_only and existing_msg:
                    if existing_msg['sender_id'] != sender_id:
                        logger.warning(
                            f"Ignoring DM update for {message_id}: author mismatch "
                            f"({existing_msg['sender_id']} != {sender_id})"
                        )
                        return
                    if meta_payload and meta_payload.get('attachments'):
                        msg_type = MsgType.FILE
                    else:
                        try:
                            msg_type = MsgType(existing_msg['message_type'])
                        except Exception:
                            msg_type = MsgType.TEXT
                    success = message_manager.update_message(
                        message_id=mid,
                        user_id=sender_id,
                        content=content_resolved or '',
                        message_type=msg_type,
                        metadata=meta_payload,
                        allow_admin=False,
                        edited_at=edited_at,
                    )
                    if success:
                        channel_manager.mark_message_processed(mid)
                        try:
                            if inbox_manager:
                                dm_preview = build_dm_preview(
                                    content_resolved or '',
                                    (meta_payload or {}).get('attachments') or [],
                                )
                                payload = {
                                    'content': content_resolved or '',
                                    'message_id': mid,
                                    'edited_at': edited_at,
                                    'attachments': (meta_payload or {}).get('attachments') or [],
                                    'security': (meta_payload or {}).get('security'),
                                }
                                if (meta_payload or {}).get('reply_to'):
                                    payload['reply_to'] = meta_payload.get('reply_to')
                                if (meta_payload or {}).get('group_id'):
                                    payload['group_id'] = meta_payload.get('group_id')
                                if (meta_payload or {}).get('group_members'):
                                    payload['group_members'] = meta_payload.get('group_members')
                                if (meta_payload or {}).get('is_group') is not None:
                                    payload['is_group'] = bool(meta_payload.get('is_group'))
                                inbox_manager.sync_source_triggers(
                                    source_type='dm',
                                    source_id=mid,
                                    trigger_type='dm',
                                    target_ids=dm_target_ids or [recipient_id],
                                    sender_user_id=sender_id,
                                    origin_peer=from_peer,
                                    preview=dm_preview,
                                    payload=payload,
                                    message_id=mid,
                                    source_content=content_resolved or '',
                                )
                        except Exception as inbox_err:
                            logger.warning(f"Failed to refresh P2P DM inbox trigger for {mid}: {inbox_err}")
                        logger.info(f"Updated P2P DM {mid} from {sender_id} to {recipient_id}")
                    else:
                        logger.warning(f"Failed to update P2P DM {mid} from {sender_id}")
                    return

                # Store the DM using message_manager
                if meta_payload is None:
                    meta_payload = {}
                meta_payload = dict(meta_payload)
                meta_payload.setdefault('origin_peer', from_peer)
                msg_type = MsgType.FILE if meta_payload.get('attachments') else MsgType.TEXT

                msg = message_manager.create_message(
                    sender_id=sender_id,
                    content=content_resolved,
                    recipient_id=storage_recipient_id,
                    message_type=msg_type,
                    metadata=meta_payload,
                )
                if msg:
                    # Override the auto-generated ID with the sender's ID for dedup
                    sent_ok = _finalize_inbound_dm_message(
                        db_manager,
                        message_manager,
                        msg,
                        mid,
                    )
                    if not sent_ok:
                        logger.warning(f"Failed to finalize P2P DM {mid} from {sender_id}")
                        return
                    channel_manager.mark_message_processed(mid)
                    try:
                        if inbox_manager:
                            dm_preview = build_dm_preview(
                                content_resolved or '',
                                (meta_payload or {}).get('attachments') or [],
                            )
                            payload = {
                                'content': content_resolved or '',
                                'message_id': mid,
                                'attachments': (meta_payload or {}).get('attachments') or [],
                                'security': (meta_payload or {}).get('security'),
                            }
                            if (meta_payload or {}).get('reply_to'):
                                payload['reply_to'] = meta_payload.get('reply_to')
                            if (meta_payload or {}).get('group_id'):
                                payload['group_id'] = meta_payload.get('group_id')
                            if (meta_payload or {}).get('group_members'):
                                payload['group_members'] = meta_payload.get('group_members')
                            if (meta_payload or {}).get('is_group') is not None:
                                payload['is_group'] = bool(meta_payload.get('is_group'))
                            inbox_manager.sync_source_triggers(
                                source_type='dm',
                                source_id=mid,
                                trigger_type='dm',
                                target_ids=dm_target_ids or [recipient_id],
                                sender_user_id=sender_id,
                                origin_peer=from_peer,
                                preview=dm_preview,
                                payload=payload,
                                message_id=mid,
                                source_content=content_resolved or '',
                            )
                    except Exception as inbox_err:
                        logger.warning(f"Failed to create P2P DM inbox trigger for {mid}: {inbox_err}")
                    logger.info(f"Stored P2P DM {mid} from {sender_id} to {recipient_id}")
                else:
                    logger.warning(f"Failed to store P2P DM from {sender_id}")

            except Exception as e:
                logger.error(f"Failed to handle P2P DM: {e}", exc_info=True)

        p2p_manager.on_direct_message = _on_p2p_direct_message

        # --- Delete signal handler ---
        def _on_delete_signal(signal_id, data_type, data_id, reason,
                              requester_peer, is_ack, ack_status, from_peer):
            """Handle incoming DELETE_SIGNAL from a peer.

            Two cases:
            1. is_ack=False  → a peer is asking us to delete some data.
               We attempt the deletion, store a record, and send an ack.
            2. is_ack=True   → a peer is confirming they handled our signal.
               We update our local signal status and adjust trust score.
            """
            try:
                if is_ack:
                    # --- Acknowledgment from a peer ---
                    status = ack_status or 'acknowledged'
                    logger.info(f"Delete signal ACK from {from_peer}: "
                                f"signal={signal_id}, status={status}")
                    if status == 'complied':
                        trust_manager.comply_with_delete_signal(signal_id, from_peer)
                    elif status == 'rejected':
                        trust_manager.violate_delete_signal(signal_id, from_peer)
                    else:
                        trust_manager.acknowledge_delete_signal(signal_id)
                    return

                # --- Incoming deletion request ---
                logger.info(f"Delete signal from {from_peer}: "
                            f"type={data_type}, id={data_id}, reason={reason}")

                deleted = False
                if data_type in ('direct_message', 'message'):
                    # Legacy `message` delete signals originated from the DM UI.
                    # Channel-message deletes now use the explicit `channel_message` type.
                    try:
                        deleted = _apply_inbound_dm_delete(
                            db_manager,
                            message_manager,
                            inbox_manager,
                            data_id,
                        )
                    except Exception as del_err:
                        logger.error(f"Failed to delete direct message {data_id}: {del_err}")

                elif data_type == 'channel_message':
                    # Delete a specific channel message (explicit type).
                    # Remove FK references first: likes and parent_message_id.
                    try:
                        with db_manager.get_connection() as conn:
                            conn.execute("DELETE FROM likes WHERE message_id = ?", (data_id,))
                            conn.execute(
                                "UPDATE channel_messages SET parent_message_id = NULL WHERE parent_message_id = ?",
                                (data_id,),
                            )
                            cur = conn.execute(
                                "DELETE FROM channel_messages WHERE id = ?",
                                (data_id,))
                            conn.commit()
                            deleted = cur.rowcount > 0
                    except Exception as del_err:
                        logger.error(f"Failed to delete channel message {data_id}: {del_err}")

                elif data_type == 'file':
                    # Remove a file from the file manager
                    try:
                        deleted = file_manager.delete_file(data_id, 'system')
                    except Exception:
                        try:
                            with db_manager.get_connection() as conn:
                                conn.execute("DELETE FROM files WHERE id = ?", (data_id,))
                                conn.commit()
                                deleted = True
                        except Exception as del_err:
                            logger.error(f"Failed to delete file {data_id}: {del_err}")

                elif data_type in ('feed_post', 'post'):
                    # Delete a feed post
                    try:
                        with db_manager.get_connection() as conn:
                            cur = conn.execute(
                                "DELETE FROM feed_posts WHERE id = ?",
                                (data_id,))
                            conn.commit()
                            deleted = cur.rowcount > 0
                    except Exception as del_err:
                        logger.error(f"Failed to delete feed post {data_id}: {del_err}")

                elif data_type == 'channel':
                    # Delete an entire channel on replicas.
                    # Security: only allow the channel origin peer to request this.
                    try:
                        with db_manager.get_connection() as conn:
                            ch_row = conn.execute(
                                "SELECT origin_peer FROM channels WHERE id = ?",
                                (data_id,),
                            ).fetchone()
                            if not ch_row:
                                deleted = True  # Idempotent: already gone.
                            else:
                                origin_peer = (
                                    ch_row['origin_peer']
                                    if hasattr(ch_row, 'keys') and 'origin_peer' in ch_row.keys()
                                    else ch_row[0]
                                ) or ''
                                requester = str(requester_peer or from_peer or '').strip()
                                local_peer = p2p_manager.get_peer_id() if p2p_manager else None
                                authorized = bool(requester) and (
                                    (origin_peer and requester == origin_peer)
                                    or (not origin_peer and local_peer and requester == local_peer)
                                )
                                if str(data_id) == 'general':
                                    authorized = False
                                if not authorized:
                                    logger.warning(
                                        "SECURITY: Rejected channel delete signal for %s "
                                        "(requester=%s, origin=%s, from=%s)",
                                        data_id,
                                        requester,
                                        origin_peer,
                                        from_peer,
                                    )
                                    deleted = False
                                else:
                                    conn.execute(
                                        "UPDATE channel_messages SET parent_message_id = NULL WHERE channel_id = ?",
                                        (data_id,),
                                    )
                                    conn.execute(
                                        "DELETE FROM likes WHERE message_id IN (SELECT id FROM channel_messages WHERE channel_id = ?)",
                                        (data_id,),
                                    )
                                    conn.execute(
                                        "DELETE FROM channel_messages WHERE channel_id = ?",
                                        (data_id,),
                                    )
                                    conn.execute(
                                        "DELETE FROM channel_members WHERE channel_id = ?",
                                        (data_id,),
                                    )
                                    cur = conn.execute(
                                        "DELETE FROM channels WHERE id = ?",
                                        (data_id,),
                                    )
                                    conn.commit()
                                    deleted = cur.rowcount > 0
                    except Exception as del_err:
                        logger.error(f"Failed to purge channel {data_id}: {del_err}")

                # Store the incoming signal locally for audit
                try:
                    db_manager.create_delete_signal(
                        signal_id, requester_peer, data_type, data_id, reason)
                    if deleted:
                        db_manager.update_delete_signal_status(signal_id, 'complied')
                except Exception:
                    pass  # best-effort audit record

                # Send compliance / rejection acknowledgment back
                ack_status_out = 'complied' if deleted else 'rejected'
                if p2p_manager and p2p_manager.is_running():
                    try:
                        p2p_manager.send_delete_signal_ack(
                            from_peer, signal_id, ack_status_out)
                    except Exception as ack_err:
                        logger.warning(f"Failed to send delete ack: {ack_err}")

                logger.info(f"Delete signal {signal_id}: {ack_status_out} "
                            f"(data_type={data_type}, data_id={data_id})")

            except Exception as e:
                logger.error(f"Failed to handle delete signal: {e}", exc_info=True)

        p2p_manager.on_delete_signal = _on_delete_signal

        # Wire trust score lookup so P2P relay can gate by trust
        p2p_manager.get_trust_score = trust_manager.get_trust_score

        # Start P2P network (skip if CANOPY_DISABLE_MESH=true, e.g. for isolated testnet)
        import os as _os
        if _os.getenv('CANOPY_DISABLE_MESH', '').strip().lower() in ('1', 'true', 'yes'):
            logger.info("P2P mesh disabled via CANOPY_DISABLE_MESH — running in standalone (API-only) mode")
        else:
            logger.info("Starting P2P network...")
            p2p_manager.start()
            logger.info("P2P network started successfully")

            # Override the activity event recorder to suppress notifications
            # for private/confidential channels where no local user is a
            # member.  Without E2E encryption the messages still relay through
            # this peer, but non-members should not see them in the bell.
            _orig_record_activity = p2p_manager._record_activity_event

            def _filtered_activity_event(event):
                try:
                    kind = event.get('kind', '')
                    ref = event.get('ref') or {}
                    ch_id: Optional[str] = None
                    if kind == 'channel_message':
                        ch_id = ref.get('channel_id')
                    elif kind == 'interaction':
                        ch_id = ref.get('channel_id')
                        if not ch_id:
                            item_id = ref.get('item_id')
                            if item_id:
                                try:
                                    with db_manager.get_connection() as conn:
                                        r = conn.execute(
                                            "SELECT channel_id FROM channel_messages WHERE id = ?",
                                            (item_id,),
                                        ).fetchone()
                                    if r:
                                        ch_id = r['channel_id']
                                except Exception:
                                    pass
                    if ch_id:
                        with db_manager.get_connection() as conn:
                            row = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (ch_id,),
                            ).fetchone()
                            if row:
                                mode = (row['privacy_mode'] or 'open').strip().lower()
                                if mode in ('private', 'confidential'):
                                    local_peer = p2p_manager.get_peer_id()
                                    has_local = conn.execute(
                                        "SELECT 1 FROM channel_members cm "
                                        "JOIN users u ON cm.user_id = u.id "
                                        "WHERE cm.channel_id = ? "
                                        "AND (u.origin_peer IS NULL OR u.origin_peer = '' "
                                        "     OR u.origin_peer = ?) "
                                        "LIMIT 1",
                                        (ch_id, local_peer or ''),
                                    ).fetchone()
                                    if not has_local:
                                        return
                except Exception:
                    pass
                _orig_record_activity(event)

            p2p_manager._record_activity_event = _filtered_activity_event
        
        # Initialize data-at-rest encryption using the peer identity
        logger.info("Initializing data-at-rest encryption...")
        identity_path = Path(config.storage.database_path).parent / 'peer_identity.json'
        data_encryptor = DataEncryptor(identity_path)
        app.config['DATA_ENCRYPTOR'] = data_encryptor
        if data_encryptor.is_enabled:
            logger.info("Data-at-rest encryption is ACTIVE")
            # Inject encryptor into managers that need it
            message_manager.data_encryptor = data_encryptor
            feed_manager.data_encryptor = data_encryptor
        else:
            logger.warning("Data-at-rest encryption is DISABLED (identity not yet created)")
        
        # Prune old dedup records on startup (keep 7 days)
        try:
            channel_manager.prune_processed_messages(keep_days=7)
        except Exception:
            pass

        # Start TTL maintenance loop (purge expired content + cleanup files)
        def _start_maintenance_loop():
            if app.config.get('TESTING'):
                return
            if app.config.get('MAINTENANCE_THREAD_STARTED'):
                return
            app.config['MAINTENANCE_THREAD_STARTED'] = True

            try:
                interval = int(os.getenv('CANOPY_MAINTENANCE_INTERVAL_SECONDS', '900'))
            except Exception:
                interval = 900
            interval = max(300, interval)  # minimum 5 minutes
            initial_delay = min(30, interval)

            def _loop():
                time.sleep(initial_delay)
                while True:
                    try:
                        with app.app_context():
                            # Identify local users (exclude shadow/system)
                            local_user_ids = set()
                            try:
                                with db_manager.get_connection() as conn:
                                    rows = conn.execute(
                                        "SELECT id FROM users "
                                        "WHERE id != 'system' AND id != 'local_user' "
                                        "AND password_hash IS NOT NULL AND password_hash != ''"
                                    ).fetchall()
                                    for row in rows:
                                        try:
                                            local_user_ids.add(row['id'])
                                        except Exception:
                                            local_user_ids.add(row[0])
                            except Exception:
                                pass

                            expired_messages = channel_manager.purge_expired_channel_messages()
                            expired_posts = feed_manager.purge_expired_posts()

                            # Purge expired signals
                            try:
                                signal_manager = app.config.get('SIGNAL_MANAGER')
                                if signal_manager:
                                    purged_signals = signal_manager.purge_expired_signals()
                                    if purged_signals:
                                        logger.info(f"Purged {purged_signals} expired signal(s)")
                            except Exception as sig_purge_err:
                                logger.warning(f"Signal purge failed: {sig_purge_err}")

                            # Clean up attachments for expired channel messages
                            if expired_messages and file_manager:
                                for msg in expired_messages:
                                    owner_id = msg.get('user_id')
                                    msg_id = msg.get('id')
                                    for file_id in msg.get('attachment_ids') or []:
                                        try:
                                            file_info = file_manager.get_file(file_id)
                                            if not file_info or file_info.uploaded_by != owner_id:
                                                continue
                                            if file_manager.is_file_referenced(
                                                file_id,
                                                exclude_channel_message_id=msg_id,
                                            ):
                                                continue
                                            file_manager.delete_file(file_id, owner_id)
                                        except Exception:
                                            continue

                            # Clean up attachments for expired feed posts
                            if expired_posts and file_manager:
                                for post in expired_posts:
                                    owner_id = post.get('author_id')
                                    post_id = post.get('id')
                                    for file_id in post.get('attachment_ids') or []:
                                        try:
                                            file_info = file_manager.get_file(file_id)
                                            if not file_info or file_info.uploaded_by != owner_id:
                                                continue
                                            if file_manager.is_file_referenced(
                                                file_id,
                                                exclude_feed_post_id=post_id,
                                            ):
                                                continue
                                            file_manager.delete_file(file_id, owner_id)
                                        except Exception:
                                            continue

                            # Broadcast delete signals for expired content authored locally
                            if p2p_manager and p2p_manager.is_running():
                                import secrets as _sec
                                for msg in expired_messages or []:
                                    if msg.get('user_id') not in local_user_ids:
                                        continue
                                    msg_id = cast(str, msg.get('id'))
                                    try:
                                        signal_id = f"DS{_sec.token_hex(8)}"
                                        p2p_manager.broadcast_delete_signal(
                                            signal_id=signal_id,
                                            data_type='channel_message',
                                            data_id=msg_id,
                                            reason='expired_ttl',
                                        )
                                    except Exception:
                                        continue
                                for post in expired_posts or []:
                                    if post.get('author_id') not in local_user_ids:
                                        continue
                                    post_id = cast(str, post.get('id'))
                                    try:
                                        signal_id = f"DS{_sec.token_hex(8)}"
                                        p2p_manager.broadcast_delete_signal(
                                            signal_id=signal_id,
                                            data_type='feed_post',
                                            data_id=post_id,
                                            reason='expired_ttl',
                                        )
                                    except Exception:
                                        continue
                    except Exception as loop_err:
                        logger.debug(f"Maintenance loop error: {loop_err}")

                    time.sleep(interval)

            thread = threading.Thread(target=_loop, daemon=True, name='canopy_maintenance')
            thread.start()
            logger.info(f"TTL maintenance loop started (interval={interval}s)")

        _start_maintenance_loop()

        logger.info("All core components initialized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize core components: {e}", exc_info=True)
        raise
    
    # Register blueprints
    try:
        logger.info("Registering API blueprint...")
        api_bp = create_api_blueprint()
        app.register_blueprint(api_bp, url_prefix='/api/v1')
        app.register_blueprint(api_bp, url_prefix='/api', name='api_legacy')
        logger.info("API blueprint registered successfully")
        
        logger.info("Registering UI blueprint...")
        ui_bp = create_ui_blueprint()
        app.register_blueprint(ui_bp)
        logger.info("UI blueprint registered successfully")
        
        logger.info("All blueprints registered successfully")
        
    except Exception as e:
        logger.error(f"Failed to register blueprints: {e}", exc_info=True)
        raise
    
    # Register error handlers
    register_error_handlers(app)

    # Install rate limiting
    _install_rate_limiting(app)
    
    # Register shutdown handlers
    register_shutdown_handlers(app)
    
    # Register template filters
    register_template_filters(app)
    
    # Log startup information
    logger.info(f"Canopy application created successfully")
    logger.info(f"Configuration: {config.to_dict()}")
    
    return app


# Old setup_logging function removed - using new comprehensive logging system


class _RateLimiter:
    """Simple in-process token-bucket rate limiter.

    Each bucket is identified by a string key (typically the client's
    IP address or API key).  Tokens are replenished at *rate* tokens
    per second, up to *capacity*.  If the bucket is empty the request
    is denied.
    """

    def __init__(self, rate: float = 10.0, capacity: int = 30):
        self.rate = rate        # tokens per second
        self.capacity = capacity
        self._buckets: dict = {}  # key -> [tokens, last_refill]

    def allow(self, key: str) -> bool:
        import time as _t
        now = _t.monotonic()
        if key not in self._buckets:
            self._buckets[key] = [self.capacity - 1, now]
            return True
        tokens, last = self._buckets[key]
        # Refill
        elapsed = now - last
        tokens = min(self.capacity, tokens + elapsed * self.rate)
        if tokens >= 1:
            self._buckets[key] = [tokens - 1, now]
            return True
        self._buckets[key][1] = now
        return False

    def prune(self, max_age: float = 3600.0) -> None:
        """Remove stale buckets to prevent memory growth."""
        import time as _t
        now = _t.monotonic()
        stale = [k for k, v in self._buckets.items() if now - v[1] > max_age]
        for k in stale:
            del self._buckets[k]


# Global rate limiter instances
_api_limiter = _RateLimiter(rate=5, capacity=15)         # Stricter: 5 req/s burst 15
_upload_limiter = _RateLimiter(rate=1, capacity=3)       # Stricter: 1 req/s burst 3
_register_limiter = _RateLimiter(rate=0.1, capacity=3)   # Very strict: 1 per 10s, burst 3
_login_limiter = _RateLimiter(rate=0.2, capacity=5)      # Login: ~1 per 5s, burst 5 (per IP)
_ui_ajax_limiter = _RateLimiter(rate=10, capacity=30)    # UI AJAX: 10 req/s burst 30 (per IP/session)
_p2p_limiter = _RateLimiter(rate=20, capacity=60)        # Stricter P2P: 20 req/s burst 60


def _install_rate_limiting(app: Flask) -> None:
    """Install a before_request hook that enforces rate limits."""
    from flask import request as _req, abort, session as _session

    @app.before_request
    def _rate_limit_check():
        # Identify caller by IP (or API key if present)
        # For registration/login, always use IP to prevent automated abuse
        path = _req.path or ''
        key = _req.remote_addr or 'unknown'

        if '/register' in path or '/keys' in path:
            limiter = _register_limiter
        elif path.rstrip('/') == '/login' and _req.method == 'POST':
            limiter = _login_limiter
        elif path.startswith('/ajax/'):
            # Per-IP (and optionally session) for UI AJAX (login, content creation, uploads)
            session_marker = _session.get('_id') or _session.get('user_id') or ''
            key = f"{key}:{session_marker}" if session_marker else key
            limiter = _ui_ajax_limiter
        elif '/files/upload' in path:
            key = _req.headers.get('X-API-Key', key)
            limiter = _upload_limiter
        elif path.startswith('/api/'):
            key = _req.headers.get('X-API-Key', key)
            limiter = _api_limiter
        else:
            return  # Other UI pages are not rate-limited

        if not limiter.allow(key):
            logger.warning(f"Rate limit exceeded for {key} on {path}")
            abort(429)

    # Periodic prune (piggyback on after_request)
    _prune_counter = {'n': 0}

    @app.after_request
    def _prune_buckets(response):
        _prune_counter['n'] += 1
        if _prune_counter['n'] >= 500:
            _prune_counter['n'] = 0
            _api_limiter.prune()
            _upload_limiter.prune()
            _register_limiter.prune()
            _login_limiter.prune()
            _ui_ajax_limiter.prune()
            _p2p_limiter.prune()
        return response


def register_error_handlers(app: Flask) -> None:
    """Register error handlers for the application."""
    
    @app.errorhandler(400)
    def bad_request(error):
        return {'error': 'Bad request', 'message': str(error)}, 400
    
    @app.errorhandler(401)
    def unauthorized(error):
        return {'error': 'Unauthorized', 'message': 'Invalid or missing API key'}, 401
    
    @app.errorhandler(403)
    def forbidden(error):
        return {'error': 'Forbidden', 'message': 'Insufficient permissions'}, 403
    
    @app.errorhandler(404)
    def not_found(error):
        return {'error': 'Not found', 'message': 'Resource not found'}, 404
    
    @app.errorhandler(429)
    def rate_limit_exceeded(error):
        retry_after = str(int(float(os.getenv('CANOPY_RETRY_AFTER_SECONDS', '1') or '1')))
        response = jsonify({'error': 'Rate limit exceeded', 'message': 'Too many requests'})
        response.status_code = 429
        response.headers['Retry-After'] = retry_after
        return response
    
    @app.errorhandler(500)
    def internal_error(error):
        logger.error(f"Internal server error: {error}")
        return {'error': 'Internal server error', 'message': 'An unexpected error occurred'}, 500
    
    logger.info("Error handlers registered")


def register_shutdown_handlers(app: Flask) -> None:
    """Register shutdown handlers for cleanup."""
    
    @app.teardown_appcontext
    def close_db(error):
        """Close database connections on app context teardown."""
        # Database connections are handled by context managers
        # This is here for any future cleanup needs
        pass
    
    def cleanup_on_shutdown():
        """Cleanup function called on app shutdown."""
        try:
            # Close any open connections
            # Cleanup temporary files
            # Save any pending data
            logger.info("Application shutdown cleanup completed")
        except Exception as e:
            logger.error(f"Error during shutdown cleanup: {e}")
    
    # Register cleanup function
    import atexit
    atexit.register(cleanup_on_shutdown)
    
    logger.info("Shutdown handlers registered")


def register_template_filters(app: Flask) -> None:
    """Register custom template filters."""
    
    @app.template_filter('filesizeformat')
    def filesizeformat(num_bytes):
        """
        Format a file size in bytes as a human readable string.
        """
        if num_bytes is None:
            return "Unknown size"
        
        try:
            num_bytes = int(num_bytes)
        except (ValueError, TypeError):
            return "Unknown size"
            
        if num_bytes == 0:
            return "0 bytes"
        
        for unit in ['bytes', 'KB', 'MB', 'GB', 'TB']:
            if num_bytes < 1024.0:
                if unit == 'bytes':
                    return f"{num_bytes} {unit}"
                return f"{num_bytes:.1f} {unit}"
            num_bytes /= 1024.0
        return f"{num_bytes:.1f} PB"
    
    logger.info("Template filters registered")


# get_app_components moved to canopy.core.utils to avoid circular imports
