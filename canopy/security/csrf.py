"""CSRF protection for Canopy UI routes.

Generates per-session tokens and validates them on state-changing requests
(POST, PUT, PATCH, DELETE). API routes using X-API-Key auth are exempt
because API keys already prove intent — CSRF only applies to cookie/session auth.
"""

from __future__ import annotations

import hmac
import hashlib
import secrets
from typing import cast
from flask import session, request, abort


_TOKEN_BYTE_LENGTH = 32


def generate_csrf_token() -> str:
    """Return the CSRF token for the current session, creating one if needed."""
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(_TOKEN_BYTE_LENGTH)
    return cast(str, session['_csrf_token'])


def _tokens_match(submitted: str, stored: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return hmac.compare_digest(submitted.encode(), stored.encode())


def validate_csrf_request() -> None:
    """Validate CSRF token on the current request.

    Call this from a ``before_request`` hook on the UI blueprint.
    Safe (GET/HEAD/OPTIONS) methods are skipped.
    """
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return

    stored = session.get('_csrf_token')
    if not stored:
        abort(403, description='CSRF session token missing — reload the page')

    submitted = (
        request.headers.get('X-CSRFToken')
        or request.form.get('csrf_token')
    )
    if not submitted or not _tokens_match(submitted, stored):
        abort(403, description='CSRF token invalid or missing')
