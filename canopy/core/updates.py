"""Admin-controlled Git update checks for a local Canopy instance.

The updater is deliberately conservative: it stores repo settings locally,
checks a configured Git remote/branch, and only applies clean fast-forward
updates. It never broadcasts settings across the mesh and never persists
credentials into Git remotes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger('canopy.updates')

UPDATE_SETTINGS_KEY = 'instance_update_settings_v1'
UPDATE_STATUS_KEY = 'instance_update_status_v1'
DEFAULT_PUBLIC_REPO_URL = (os.getenv('CANOPY_UPDATE_DEFAULT_REPO') or 'https://github.com/kwalus/Canopy.git').strip()
DEFAULT_BRANCH = (os.getenv('CANOPY_UPDATE_DEFAULT_BRANCH') or 'main').strip() or 'main'
ENV_UPDATE_TOKEN = 'CANOPY_UPDATE_GITHUB_TOKEN'
_MAX_REPO_URL_CHARS = 600
_MAX_BRANCH_CHARS = 160
_BRANCH_RE = re.compile(r'^[A-Za-z0-9._/\-]+$')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {'1', 'true', 'yes', 'on', 'enabled'}


class UpdateError(RuntimeError):
    """User-facing updater failure with an HTTP-appropriate status."""

    def __init__(self, message: str, *, status_code: int = 400, reason: str = 'update_error') -> None:
        super().__init__(message)
        self.status_code = int(status_code or 400)
        self.reason = reason


class UpdateManager:
    """Manage local Git update settings and fast-forward source updates."""

    def __init__(
        self,
        db_manager: Any,
        config: Any = None,
        secret_key: str | bytes | None = None,
        *,
        project_root: str | Path | None = None,
    ) -> None:
        self.db_manager = db_manager
        self.config = config
        self.secret_key = self._normalize_secret(secret_key)
        self.project_root = Path(project_root).expanduser().resolve() if project_root else Path(__file__).resolve().parents[2]
        self._fernet: Optional[Fernet] = None

    def get_settings(self) -> dict[str, Any]:
        raw = self._load_settings_raw()
        repo_url = str(raw.get('repo_url') or '').strip() or self._current_origin_url() or DEFAULT_PUBLIC_REPO_URL
        branch = str(raw.get('branch') or '').strip() or self._current_branch() or DEFAULT_BRANCH
        token_ciphertext = str(raw.get('github_token_ciphertext') or '').strip()
        env_token_available = bool(os.getenv(ENV_UPDATE_TOKEN))
        return {
            'repo_url': self._sanitize_repo_url(repo_url)[:_MAX_REPO_URL_CHARS],
            'branch': branch[:_MAX_BRANCH_CHARS],
            'check_enabled': _coerce_bool(raw.get('check_enabled'), True),
            'github_token_configured': bool(token_ciphertext or env_token_available),
            'github_token_saved': bool(token_ciphertext),
            'environment_token_available': env_token_available,
            'updated_at': raw.get('updated_at') or '',
            'updated_by': raw.get('updated_by') or '',
            'default_public_repo_url': DEFAULT_PUBLIC_REPO_URL,
            'environment_token_name': ENV_UPDATE_TOKEN,
        }

    def save_settings(
        self,
        admin_user_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        current_raw = self._load_settings_raw()
        current = self.get_settings()
        repo_url = self._normalize_repo_url(payload.get('repo_url') or current.get('repo_url') or DEFAULT_PUBLIC_REPO_URL)
        branch = self._normalize_branch(payload.get('branch') or current.get('branch') or DEFAULT_BRANCH)
        check_enabled = _coerce_bool(payload.get('check_enabled'), current.get('check_enabled', True))

        token_ciphertext = str(current_raw.get('github_token_ciphertext') or '').strip()
        token_value = str(payload.get('github_token') or '').strip() if 'github_token' in payload else ''
        if _coerce_bool(payload.get('clear_github_token'), False):
            token_ciphertext = ''
        elif token_value:
            token_ciphertext = self._encrypt(token_value)

        settings = {
            'repo_url': repo_url,
            'branch': branch,
            'check_enabled': check_enabled,
            'github_token_ciphertext': token_ciphertext,
            'updated_at': _now_iso(),
            'updated_by': str(admin_user_id or '').strip(),
        }
        if not self.db_manager.set_system_state(UPDATE_SETTINGS_KEY, json.dumps(settings, sort_keys=True)):
            raise UpdateError('Could not save update settings.', status_code=500, reason='settings_save_failed')
        return self.get_settings()

    def get_status(self) -> dict[str, Any]:
        settings = self.get_settings()
        last_check = self._load_status_raw()
        local = self._local_git_summary()
        status = {
            'success': True,
            'settings': settings,
            'project_root': str(self.project_root),
            'git_available': bool(shutil.which('git')),
            'git_repo_available': self._is_git_repo(),
            'last_check': last_check,
        }
        status.update(local)
        if last_check:
            status.update({
                'last_checked_at': last_check.get('checked_at') or '',
                'update_available': bool(last_check.get('update_available')),
                'can_apply': bool(last_check.get('can_apply')),
                'behind_count': int(last_check.get('behind_count') or 0),
                'ahead_count': int(last_check.get('ahead_count') or 0),
                'remote_commit': last_check.get('remote_commit') or '',
                'remote_commit_short': last_check.get('remote_commit_short') or '',
                'recent_commits': last_check.get('recent_commits') or [],
                'last_error': last_check.get('error') or '',
            })
        return status

    def check_for_updates(self) -> dict[str, Any]:
        settings = self.get_settings()
        if not self._is_git_repo():
            raise UpdateError('This Canopy instance is not running from a Git working tree.', status_code=409, reason='not_git_repo')
        if not shutil.which('git'):
            raise UpdateError('Git is not available on this system path.', status_code=503, reason='git_unavailable')

        repo_url = self._normalize_repo_url(settings.get('repo_url') or DEFAULT_PUBLIC_REPO_URL)
        branch = self._normalize_branch(settings.get('branch') or DEFAULT_BRANCH)
        token = self._get_update_token() if repo_url.lower().startswith(('https://', 'http://')) else ''

        checked_at = _now_iso()
        try:
            self._run_git(['fetch', '--quiet', '--no-tags', repo_url, branch], token=token, timeout=90)
            head = self._git_stdout(['rev-parse', 'HEAD'], timeout=8)
            remote = self._git_stdout(['rev-parse', 'FETCH_HEAD'], timeout=8)
            counts_raw = self._git_stdout(['rev-list', '--left-right', '--count', 'HEAD...FETCH_HEAD'], timeout=15)
            ahead_count, behind_count = self._parse_counts(counts_raw)
            recent_commits = self._recent_commit_lines('HEAD..FETCH_HEAD')
            dirty_entries = self._dirty_entries()
            changed_files = self._changed_files(head, remote) if remote and head and head != remote else []
            result = {
                'success': True,
                'checked_at': checked_at,
                'repo_url': self._sanitize_repo_url(repo_url),
                'branch': branch,
                'local_commit': head,
                'local_commit_short': head[:10],
                'remote_commit': remote,
                'remote_commit_short': remote[:10],
                'ahead_count': ahead_count,
                'behind_count': behind_count,
                'update_available': behind_count > 0,
                'diverged': bool(ahead_count > 0 and behind_count > 0),
                'dirty': bool(dirty_entries),
                'dirty_count': len(dirty_entries),
                'can_apply': bool(behind_count > 0 and ahead_count == 0 and not dirty_entries),
                'recent_commits': recent_commits,
                'changed_files': changed_files[:80],
                'dependencies_changed': any(path in {'pyproject.toml', 'requirements.txt'} or path.startswith('requirements/') for path in changed_files),
                'message': 'Updates available.' if behind_count > 0 else 'This instance is up to date.',
                'error': '',
            }
            self._save_status(result)
            return {**result, 'settings': settings, 'project_root': str(self.project_root)}
        except UpdateError as exc:
            status = {
                'success': False,
                'checked_at': checked_at,
                'repo_url': self._sanitize_repo_url(repo_url),
                'branch': branch,
                'error': str(exc),
                'reason': exc.reason,
            }
            self._save_status(status)
            raise
        except Exception as exc:
            status = {
                'success': False,
                'checked_at': checked_at,
                'repo_url': self._sanitize_repo_url(repo_url),
                'branch': branch,
                'error': str(exc),
                'reason': 'check_failed',
            }
            self._save_status(status)
            raise UpdateError(str(exc) or 'Could not check for updates.', status_code=500, reason='check_failed') from exc

    def apply_update(self, *, allow_dirty: bool = False) -> dict[str, Any]:
        check = self.check_for_updates()
        if check.get('dirty') and not allow_dirty:
            raise UpdateError(
                'Local files have uncommitted changes. Refusing to update until the working tree is clean.',
                status_code=409,
                reason='dirty_worktree',
            )
        if int(check.get('ahead_count') or 0) > 0:
            raise UpdateError(
                'Local Git history is ahead of or diverged from the configured branch. Refusing automatic update.',
                status_code=409,
                reason='diverged_branch',
            )
        if not check.get('update_available'):
            return {
                'success': True,
                'changed': False,
                'message': 'This instance is already up to date.',
                'restart_required': False,
                'check': check,
                'settings': self.get_settings(),
            }

        before = str(check.get('local_commit') or '')
        self._run_git(['merge', '--ff-only', 'FETCH_HEAD'], timeout=120)
        after = self._git_stdout(['rev-parse', 'HEAD'], timeout=8)
        changed_files = self._changed_files(before, after) if before and after and before != after else []
        result = {
            'success': True,
            'changed': before != after,
            'message': 'Update applied. Restart this Canopy instance to run the new code.' if before != after else 'No source changes were applied.',
            'restart_required': before != after,
            'before_commit': before,
            'before_commit_short': before[:10],
            'after_commit': after,
            'after_commit_short': after[:10],
            'changed_files': changed_files[:80],
            'dependencies_changed': any(path in {'pyproject.toml', 'requirements.txt'} or path.startswith('requirements/') for path in changed_files),
            'settings': self.get_settings(),
            'applied_at': _now_iso(),
        }
        saved = dict(check)
        saved.update({
            'success': True,
            'update_available': False,
            'can_apply': False,
            'local_commit': after,
            'local_commit_short': after[:10],
            'last_update_at': result['applied_at'],
            'last_update_from': before,
            'last_update_to': after,
            'message': result['message'],
            'error': '',
        })
        self._save_status(saved)
        return result

    def _load_settings_raw(self) -> dict[str, Any]:
        try:
            value = self.db_manager.get_system_state(UPDATE_SETTINGS_KEY)
            if value:
                loaded = json.loads(value)
                if isinstance(loaded, dict):
                    return loaded
        except Exception as exc:
            logger.warning('Could not load update settings: %s', exc)
        return {}

    def _load_status_raw(self) -> dict[str, Any]:
        try:
            value = self.db_manager.get_system_state(UPDATE_STATUS_KEY)
            if value:
                loaded = json.loads(value)
                if isinstance(loaded, dict):
                    return loaded
        except Exception as exc:
            logger.warning('Could not load update status: %s', exc)
        return {}

    def _save_status(self, status: dict[str, Any]) -> None:
        try:
            clean = self._redact_payload(status)
            self.db_manager.set_system_state(UPDATE_STATUS_KEY, json.dumps(clean, sort_keys=True))
        except Exception as exc:
            logger.warning('Could not save update status: %s', exc)

    def _is_git_repo(self) -> bool:
        try:
            if not self.project_root.exists():
                return False
            result = self._run_git(['rev-parse', '--is-inside-work-tree'], timeout=5, check=False)
            return result.returncode == 0 and result.stdout.strip().lower() == 'true'
        except Exception:
            return False

    def _local_git_summary(self) -> dict[str, Any]:
        if not self._is_git_repo() or not shutil.which('git'):
            return {
                'current_branch': '',
                'current_commit': '',
                'current_commit_short': '',
                'origin_url': '',
                'dirty': False,
                'dirty_count': 0,
            }
        dirty_entries = self._dirty_entries()
        commit = self._safe_git_stdout(['rev-parse', 'HEAD'])
        return {
            'current_branch': self._safe_git_stdout(['rev-parse', '--abbrev-ref', 'HEAD']),
            'current_commit': commit,
            'current_commit_short': commit[:10] if commit else '',
            'origin_url': self._sanitize_repo_url(self._current_origin_url()),
            'dirty': bool(dirty_entries),
            'dirty_count': len(dirty_entries),
        }

    def _current_origin_url(self) -> str:
        return self._safe_git_stdout(['config', '--get', 'remote.origin.url'])

    def _current_branch(self) -> str:
        return self._safe_git_stdout(['rev-parse', '--abbrev-ref', 'HEAD'])

    def _safe_git_stdout(self, args: list[str], *, timeout: int = 5) -> str:
        try:
            return self._git_stdout(args, timeout=timeout)
        except Exception:
            return ''

    def _git_stdout(self, args: list[str], *, timeout: int = 10) -> str:
        result = self._run_git(args, timeout=timeout)
        return str(result.stdout or '').strip()

    def _run_git(
        self,
        args: list[str],
        *,
        timeout: int = 30,
        token: str = '',
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if not shutil.which('git'):
            raise UpdateError('Git is not available on this system path.', status_code=503, reason='git_unavailable')
        env = os.environ.copy()
        env['GIT_TERMINAL_PROMPT'] = '0'
        token_clean = str(token or '').strip()
        with tempfile.TemporaryDirectory(prefix='canopy-git-askpass-') as tmpdir:
            if token_clean:
                askpass = Path(tmpdir) / 'askpass.sh'
                askpass.write_text(
                    '#!/bin/sh\n'
                    'case "$1" in\n'
                    '  *Username*) printf "%s" "x-access-token" ;;\n'
                    '  *) printf "%s" "$CANOPY_GIT_TOKEN" ;;\n'
                    'esac\n',
                    encoding='utf-8',
                )
                askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
                env['GIT_ASKPASS'] = str(askpass)
                env['CANOPY_GIT_TOKEN'] = token_clean
            result = subprocess.run(
                ['git', *args],
                cwd=str(self.project_root),
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        if check and result.returncode != 0:
            stderr = self._redact_text(result.stderr or result.stdout or '')
            message = stderr.strip() or f"git {' '.join(args[:2])} failed with exit code {result.returncode}"
            raise UpdateError(message, status_code=502, reason='git_command_failed')
        if result.stdout:
            result.stdout = self._redact_text(result.stdout)
        if result.stderr:
            result.stderr = self._redact_text(result.stderr)
        return result

    def _dirty_entries(self) -> list[str]:
        status = self._safe_git_stdout(['status', '--porcelain'], timeout=8)
        return [line for line in status.splitlines() if line.strip()]

    def _recent_commit_lines(self, rev_range: str, limit: int = 8) -> list[dict[str, str]]:
        raw = self._safe_git_stdout(['log', f'--max-count={limit}', '--pretty=format:%H%x1f%h%x1f%s', rev_range], timeout=15)
        commits: list[dict[str, str]] = []
        for line in raw.splitlines():
            parts = line.split('\x1f')
            if len(parts) >= 3:
                commits.append({'commit': parts[0], 'short': parts[1], 'subject': parts[2][:220]})
        return commits

    def _changed_files(self, before: str, after: str) -> list[str]:
        before = str(before or '').strip()
        after = str(after or '').strip()
        if not before or not after or before == after:
            return []
        raw = self._safe_git_stdout(['diff', '--name-only', f'{before}..{after}'], timeout=20)
        return [line.strip() for line in raw.splitlines() if line.strip()][:500]

    @staticmethod
    def _parse_counts(value: str) -> tuple[int, int]:
        parts = str(value or '').strip().split()
        if len(parts) < 2:
            return 0, 0
        try:
            ahead = int(parts[0])
        except Exception:
            ahead = 0
        try:
            behind = int(parts[1])
        except Exception:
            behind = 0
        return max(0, ahead), max(0, behind)

    def _get_update_token(self) -> str:
        env_token = str(os.getenv(ENV_UPDATE_TOKEN) or '').strip()
        if env_token:
            return env_token
        raw = self._load_settings_raw()
        ciphertext = str(raw.get('github_token_ciphertext') or '').strip()
        return self._decrypt(ciphertext) if ciphertext else ''

    @staticmethod
    def _normalize_secret(secret_key: str | bytes | None) -> str:
        if isinstance(secret_key, bytes):
            try:
                return secret_key.decode('utf-8')
            except Exception:
                return base64.urlsafe_b64encode(secret_key).decode('ascii')
        return str(secret_key or 'canopy-local-secret-fallback')

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            material = hashlib.sha256(f'canopy-update-settings-v1:{self.secret_key}'.encode('utf-8')).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(material))
        return self._fernet

    def _encrypt(self, value: str) -> str:
        return self._get_fernet().encrypt(value.encode('utf-8')).decode('ascii')

    def _decrypt(self, value: str) -> str:
        try:
            return self._get_fernet().decrypt(str(value or '').encode('ascii')).decode('utf-8')
        except InvalidToken as exc:
            raise UpdateError(
                'Saved GitHub token could not be decrypted on this node. Re-enter the token in Admin > Instance Updates.',
                status_code=400,
                reason='github_token_decrypt_failed',
            ) from exc

    @staticmethod
    def _normalize_repo_url(value: Any) -> str:
        repo_url = str(value or '').strip()
        if not repo_url:
            repo_url = DEFAULT_PUBLIC_REPO_URL
        if len(repo_url) > _MAX_REPO_URL_CHARS:
            raise UpdateError('Repository URL is too long.', status_code=400, reason='repo_url_too_long')
        parsed = urlparse(repo_url)
        if parsed.scheme == 'http':
            raise UpdateError('Use HTTPS, SSH, git@, file://, or a local path for update repositories.', status_code=400, reason='insecure_repo_url')
        if parsed.scheme == 'https':
            if parsed.username or parsed.password:
                raise UpdateError('Do not paste credentials into the repository URL. Use the token field instead.', status_code=400, reason='repo_url_contains_credentials')
            if not parsed.netloc:
                raise UpdateError('Repository URL is missing a host.', status_code=400, reason='invalid_repo_url')
            return repo_url
        if parsed.scheme in {'ssh', 'git', 'file'}:
            return repo_url
        if repo_url.startswith('git@'):
            return repo_url
        if repo_url.startswith('/') or repo_url.startswith('./') or repo_url.startswith('../'):
            return repo_url
        raise UpdateError('Use an HTTPS, SSH, git@, file://, or local Git repository URL.', status_code=400, reason='invalid_repo_url')

    @staticmethod
    def _normalize_branch(value: Any) -> str:
        branch = str(value or '').strip() or DEFAULT_BRANCH
        if len(branch) > _MAX_BRANCH_CHARS:
            raise UpdateError('Branch name is too long.', status_code=400, reason='branch_too_long')
        if branch.startswith('-') or '..' in branch or branch.endswith('/') or not _BRANCH_RE.match(branch):
            raise UpdateError('Branch name contains unsupported characters.', status_code=400, reason='invalid_branch')
        return branch

    @staticmethod
    def _sanitize_repo_url(value: Any) -> str:
        text = str(value or '').strip()
        if not text:
            return ''
        parsed = urlparse(text)
        if parsed.scheme in {'http', 'https'} and (parsed.username or parsed.password):
            host = parsed.hostname or ''
            if parsed.port:
                host = f'{host}:{parsed.port}'
            return parsed._replace(netloc=host).geturl()
        return re.sub(r'(://)([^/@:\s]+):([^/@\s]+)@', r'\1***:***@', text)

    def _redact_text(self, text: str) -> str:
        redacted = str(text or '')
        token = ''
        try:
            token = self._get_update_token()
        except Exception:
            token = ''
        if token:
            redacted = redacted.replace(token, '***')
        return redacted

    def _redact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        clean = json.loads(json.dumps(payload, default=str))
        if isinstance(clean, dict):
            clean.pop('github_token', None)
            clean.pop('github_token_ciphertext', None)
            if 'repo_url' in clean:
                clean['repo_url'] = self._sanitize_repo_url(clean.get('repo_url'))
        return clean
