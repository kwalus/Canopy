"""Workstream API blueprint.

Kept outside canopy/api/routes.py so Workstream endpoints can evolve without
adding more weight to the main API module.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable, Dict, Optional

from flask import Blueprint, current_app, g, jsonify, request, session

from ..core.workstreams import (
    WORKSTREAM_ARTIFACT_TYPES,
    WORKSTREAM_EVENT_TYPES,
    WORKSTREAM_PRIORITIES,
    WORKSTREAM_ROLES,
    WORKSTREAM_STATUSES,
)
from ..security.api_keys import Permission
from ..security.csrf import validate_csrf_request
from .routes import _extract_api_key_from_headers

logger = logging.getLogger('canopy.api.workstreams')


def _request_user_id() -> str:
    key_info = getattr(g, 'api_key_info', None)
    if key_info is not None:
        return str(getattr(key_info, 'user_id', '') or '').strip()
    return str(session.get('user_id') or '').strip()


def _manager() -> Any:
    return current_app.config.get('WORKSTREAM_MANAGER')


def _channel_manager() -> Any:
    return current_app.config.get('CHANNEL_MANAGER')


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _validate_channel_access(channel_id: Optional[str], user_id: str) -> Optional[tuple]:
    if not channel_id:
        return None
    channel_manager = _channel_manager()
    if not channel_manager:
        return jsonify({'error': 'Channel manager unavailable', 'code': 'channel_manager_unavailable'}), 500
    try:
        decision = channel_manager.get_channel_access_decision(channel_id, user_id, require_membership=True)
        if not decision.get('allowed'):
            return jsonify({'error': 'Channel access denied', 'code': decision.get('reason') or 'channel_access_denied'}), 403
    except Exception as exc:
        logger.warning("Workstream channel access check failed: %s", exc, exc_info=True)
        return jsonify({'error': 'Channel access check failed'}), 500
    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _require_auth(required_permission: Optional[Permission] = None, *, allow_session: bool = True) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapped(*args: Any, **kwargs: Any):
            api_key_manager = current_app.config.get('API_KEY_MANAGER')
            raw_key = _extract_api_key_from_headers(request)
            if raw_key:
                if not api_key_manager:
                    return jsonify({'error': 'API key manager unavailable'}), 500
                key_info = api_key_manager.validate_key(raw_key, required_permission)
                if not key_info:
                    return jsonify({'error': 'Invalid or insufficient permissions'}), 403
                if getattr(key_info, 'account_pending', False):
                    return jsonify({'error': 'Account pending approval', 'status': 'pending_approval'}), 403
                g.api_key_info = key_info
                return func(*args, **kwargs)

            if allow_session and session.get('authenticated') and session.get('user_id'):
                if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                    validate_csrf_request()
                g.api_key_info = None
                return func(*args, **kwargs)

            return jsonify({'error': 'API key required'}), 401
        return wrapped
    return decorator


def create_workstream_api_blueprint() -> Blueprint:
    api = Blueprint('workstreams_api', __name__)

    @api.route('/workstreams/schema', methods=['GET'])
    @_require_auth(Permission.READ_FEED)
    def workstream_schema():
        return jsonify({
            'statuses': list(WORKSTREAM_STATUSES),
            'priorities': list(WORKSTREAM_PRIORITIES),
            'roles': list(WORKSTREAM_ROLES),
            'event_types': list(WORKSTREAM_EVENT_TYPES),
            'artifact_types': list(WORKSTREAM_ARTIFACT_TYPES),
        })

    @api.route('/workstreams', methods=['GET'])
    @_require_auth(Permission.READ_FEED)
    def list_workstreams():
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        user_id = _request_user_id()
        channel_id = (request.args.get('channel_id') or '').strip() or None
        if channel_id:
            access_error = _validate_channel_access(channel_id, user_id)
            if access_error:
                return access_error
        status = (request.args.get('status') or '').strip() or None
        include_closed = _coerce_bool(request.args.get('include_closed'))
        try:
            limit = max(1, min(int(request.args.get('limit') or 50), 200))
        except Exception:
            limit = 50
        workstreams = manager.list_workstreams(
            user_id=user_id,
            channel_id=channel_id,
            status=status,
            include_closed=include_closed,
            limit=limit,
        )
        return jsonify({'workstreams': [ws.to_dict(include_details=False) for ws in workstreams], 'count': len(workstreams)})

    @api.route('/workstreams', methods=['POST'])
    @_require_auth(Permission.WRITE_FEED)
    def create_workstream():
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        actor_id = _request_user_id()
        data = _json_body()
        title = str(data.get('title') or '').strip()
        if not title:
            return jsonify({'error': 'title required', 'code': 'missing_title'}), 400
        owner_user_id = str(data.get('owner_user_id') or actor_id).strip()
        channel_id = str(data.get('channel_id') or '').strip() or None
        if channel_id:
            access_error = _validate_channel_access(channel_id, actor_id)
            if access_error:
                return access_error
        try:
            ws = manager.create_workstream(
                title=title,
                owner_user_id=owner_user_id,
                created_by=actor_id,
                objective=data.get('objective'),
                required_output=data.get('required_output'),
                status=data.get('status') or 'active',
                priority=data.get('priority') or 'normal',
                channel_id=channel_id,
                source_type=data.get('source_type'),
                source_id=data.get('source_id'),
                participants=data.get('participants') if isinstance(data.get('participants'), list) else [],
                artifacts=data.get('artifacts') if isinstance(data.get('artifacts'), list) else [],
                summary=data.get('summary'),
                next_action=data.get('next_action'),
                visibility=data.get('visibility') or 'channel',
                metadata=data.get('metadata') if isinstance(data.get('metadata'), dict) else None,
            )
            return jsonify({'workstream': ws.to_dict(), 'agent_reference': manager.to_agent_reference(ws.id)}), 201
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except Exception as exc:
            logger.error("Create workstream failed: %s", exc, exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/workstreams/<workstream_id>', methods=['GET'])
    @_require_auth(Permission.READ_FEED)
    def get_workstream(workstream_id: str):
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        user_id = _request_user_id()
        if not manager.user_can_view(workstream_id, user_id):
            return jsonify({'error': 'Not found or not shared with this user'}), 404
        ws = manager.get_workstream(workstream_id)
        if not ws:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'workstream': ws.to_dict(), 'agent_reference': manager.to_agent_reference(workstream_id)})

    @api.route('/workstreams/<workstream_id>', methods=['PATCH'])
    @_require_auth(Permission.WRITE_FEED)
    def update_workstream(workstream_id: str):
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        actor_id = _request_user_id()
        if not manager.user_can_edit(workstream_id, actor_id):
            return jsonify({'error': 'Permission denied', 'code': 'workstream_edit_denied'}), 403
        data = _json_body()
        ws = manager.update_workstream(workstream_id, actor_user_id=actor_id, updates=data)
        if not ws:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'workstream': ws.to_dict()})

    @api.route('/workstreams/<workstream_id>/participants', methods=['POST'])
    @_require_auth(Permission.WRITE_FEED)
    def set_workstream_participants(workstream_id: str):
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        actor_id = _request_user_id()
        if not manager.user_can_edit(workstream_id, actor_id):
            return jsonify({'error': 'Permission denied', 'code': 'workstream_edit_denied'}), 403
        data = _json_body()
        participants = data.get('participants') if isinstance(data.get('participants'), list) else []
        if not participants:
            return jsonify({'error': 'participants required', 'code': 'missing_participants'}), 400
        ws = manager.set_participants(
            workstream_id,
            actor_user_id=actor_id,
            participants=participants,
            replace=_coerce_bool(data.get('replace')),
        )
        if not ws:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'workstream': ws.to_dict()})

    @api.route('/workstreams/<workstream_id>/claim', methods=['POST'])
    @_require_auth(Permission.WRITE_FEED)
    def claim_workstream(workstream_id: str):
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        actor_id = _request_user_id()
        if not manager.user_can_view(workstream_id, actor_id):
            return jsonify({'error': 'Not found or not shared with this user'}), 404
        ws = manager.set_participants(
            workstream_id,
            actor_user_id=actor_id,
            participants=[{'user_id': actor_id, 'role': 'contributor'}],
            replace=False,
        )
        manager.add_event(
            workstream_id,
            actor_user_id=actor_id,
            event_type='progress',
            title='Workstream claimed',
            body='Actor joined this workstream as a contributor.',
        )
        return jsonify({'workstream': ws.to_dict() if ws else None})

    @api.route('/workstreams/<workstream_id>/events', methods=['POST'])
    @_require_auth(Permission.WRITE_FEED)
    def add_workstream_event(workstream_id: str):
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        actor_id = _request_user_id()
        if not manager.user_can_edit(workstream_id, actor_id):
            return jsonify({'error': 'Permission denied', 'code': 'workstream_edit_denied'}), 403
        data = _json_body()
        try:
            event = manager.add_event(
                workstream_id,
                actor_user_id=actor_id,
                event_type=data.get('event_type') or 'progress',
                title=data.get('title'),
                body=data.get('body') or data.get('summary'),
                status=data.get('status'),
                summary=data.get('summary'),
                next_action=data.get('next_action'),
                metadata=data.get('metadata') if isinstance(data.get('metadata'), dict) else None,
                dedupe_key=data.get('dedupe_key') or data.get('client_update_id'),
            )
            ws = manager.get_workstream(workstream_id, event_limit=20)
            return jsonify({'event': event.to_dict(), 'workstream': ws.to_dict() if ws else None}), 201
        except Exception as exc:
            logger.error("Add workstream event failed: %s", exc, exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/workstreams/<workstream_id>/artifacts', methods=['POST'])
    @_require_auth(Permission.WRITE_FEED)
    def add_workstream_artifact(workstream_id: str):
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        actor_id = _request_user_id()
        if not manager.user_can_edit(workstream_id, actor_id):
            return jsonify({'error': 'Permission denied', 'code': 'workstream_edit_denied'}), 403
        data = _json_body()
        artifact_data = data.get('artifact') if isinstance(data.get('artifact'), dict) else data
        artifact = manager.add_artifact(workstream_id, actor_user_id=actor_id, artifact=artifact_data)
        if not artifact:
            return jsonify({'error': 'artifact ref_id required', 'code': 'missing_artifact_ref'}), 400
        ws = manager.get_workstream(workstream_id, event_limit=20)
        return jsonify({'artifact': artifact.to_dict(), 'workstream': ws.to_dict() if ws else None}), 201

    @api.route('/workstreams/<workstream_id>/agent-reference', methods=['GET'])
    @_require_auth(Permission.READ_FEED)
    def get_workstream_agent_reference(workstream_id: str):
        manager = _manager()
        if not manager:
            return jsonify({'error': 'Workstream manager unavailable'}), 500
        actor_id = _request_user_id()
        if not manager.user_can_view(workstream_id, actor_id):
            return jsonify({'error': 'Not found or not shared with this user'}), 404
        ref = manager.to_agent_reference(workstream_id)
        if not ref:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(ref)

    return api
