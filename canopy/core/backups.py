"""Instance backup management for Canopy.

Creates consistent, local ZIP snapshots containing the SQLite database,
operator recovery metadata, and user file storage. Backups are node-local and
admin controlled; they are not broadcast across the mesh.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .large_attachments import get_large_attachment_store_root, resolve_large_attachment_store_root

logger = logging.getLogger('canopy.backups')

BACKUP_SETTINGS_KEY = 'instance_backup_settings_v1'
BACKUP_STATUS_KEY = 'instance_backup_status_v1'

_DEFAULT_INTERVAL_HOURS = 24
_DEFAULT_RETENTION_COUNT = 5
_MIN_INTERVAL_HOURS = 1
_MAX_INTERVAL_HOURS = 24 * 30
_MIN_RETENTION_COUNT = 1
_MAX_RETENTION_COUNT = 60
_METADATA_FILES = (
    'peer_identity.json',
    'secret_key.json',
    'known_peers.json',
    'relay_settings.json',
    'transport_security.json',
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {'1', 'true', 'yes', 'on', 'enabled'}


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except Exception:
        return path.expanduser().absolute()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except Exception:
        return False


class BackupManager:
    """Create and schedule recoverable local instance backup snapshots."""

    def __init__(self, db_manager: Any, file_manager: Any, config: Any) -> None:
        self.db_manager = db_manager
        self.file_manager = file_manager
        self.config = config
        self._run_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running_since: Optional[str] = None

    def _data_dir(self) -> Path:
        raw = str(getattr(getattr(self.config, 'storage', None), 'data_dir', '') or '').strip()
        if raw:
            return _safe_resolve(Path(raw))
        db_path = Path(str(getattr(self.db_manager, 'db_path', '') or 'canopy.db'))
        return _safe_resolve(db_path.parent)

    def _default_backup_root(self) -> Path:
        env_root = str(os.getenv('CANOPY_BACKUP_ROOT') or '').strip()
        if env_root:
            return _safe_resolve(Path(env_root))
        return self._data_dir() / 'backups'

    def get_settings(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        try:
            value = self.db_manager.get_system_state(BACKUP_SETTINGS_KEY)
            if value:
                loaded = json.loads(value)
                if isinstance(loaded, dict):
                    raw = loaded
        except Exception as exc:
            logger.warning('Could not load backup settings: %s', exc)

        env_enabled = os.getenv('CANOPY_BACKUP_ENABLED')
        enabled_default = _coerce_bool(env_enabled, False) if env_enabled is not None else False
        interval_default = _coerce_int(
            os.getenv('CANOPY_BACKUP_INTERVAL_HOURS'),
            _DEFAULT_INTERVAL_HOURS,
            _MIN_INTERVAL_HOURS,
            _MAX_INTERVAL_HOURS,
        )
        retention_default = _coerce_int(
            os.getenv('CANOPY_BACKUP_RETENTION_COUNT'),
            _DEFAULT_RETENTION_COUNT,
            _MIN_RETENTION_COUNT,
            _MAX_RETENTION_COUNT,
        )
        backup_root = str(raw.get('backup_root') or self._default_backup_root())
        return {
            'enabled': _coerce_bool(raw.get('enabled'), enabled_default),
            'backup_root': str(_safe_resolve(Path(backup_root))),
            'interval_hours': _coerce_int(raw.get('interval_hours'), interval_default, _MIN_INTERVAL_HOURS, _MAX_INTERVAL_HOURS),
            'retention_count': _coerce_int(raw.get('retention_count'), retention_default, _MIN_RETENTION_COUNT, _MAX_RETENTION_COUNT),
            'include_files': _coerce_bool(raw.get('include_files'), True),
            'include_large_attachments': _coerce_bool(raw.get('include_large_attachments'), False),
        }

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_settings()
        requested_root = str(payload.get('backup_root') or current.get('backup_root') or self._default_backup_root()).strip()
        if not requested_root:
            requested_root = str(self._default_backup_root())
        settings = {
            'enabled': _coerce_bool(payload.get('enabled'), current['enabled']),
            'backup_root': str(_safe_resolve(Path(requested_root))),
            'interval_hours': _coerce_int(payload.get('interval_hours'), current['interval_hours'], _MIN_INTERVAL_HOURS, _MAX_INTERVAL_HOURS),
            'retention_count': _coerce_int(payload.get('retention_count'), current['retention_count'], _MIN_RETENTION_COUNT, _MAX_RETENTION_COUNT),
            'include_files': _coerce_bool(payload.get('include_files'), current['include_files']),
            'include_large_attachments': _coerce_bool(payload.get('include_large_attachments'), current['include_large_attachments']),
        }
        root = Path(settings['backup_root']).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / '.canopy-backup-write-check'
        probe.write_text('ok', encoding='utf-8')
        try:
            probe.unlink()
        except Exception:
            pass
        if not self.db_manager.set_system_state(BACKUP_SETTINGS_KEY, json.dumps(settings, sort_keys=True)):
            raise RuntimeError('Could not save backup settings')
        return self.get_settings()

    def _load_status(self) -> dict[str, Any]:
        try:
            value = self.db_manager.get_system_state(BACKUP_STATUS_KEY)
            if value:
                loaded = json.loads(value)
                if isinstance(loaded, dict):
                    return loaded
        except Exception:
            pass
        return {}

    def _save_status(self, status: dict[str, Any]) -> None:
        try:
            self.db_manager.set_system_state(BACKUP_STATUS_KEY, json.dumps(status, sort_keys=True))
        except Exception as exc:
            logger.warning('Could not save backup status: %s', exc)

    def _backup_root(self, settings: Optional[dict[str, Any]] = None) -> Path:
        active = settings or self.get_settings()
        return _safe_resolve(Path(str(active.get('backup_root') or self._default_backup_root())))

    def list_backups(self, settings: Optional[dict[str, Any]] = None, limit: int = 20) -> list[dict[str, Any]]:
        root = self._backup_root(settings)
        if not root.exists():
            return []
        backups: list[dict[str, Any]] = []
        for path in sorted(root.glob('canopy-backup-*.zip'), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
            try:
                stat = path.stat()
                backups.append({
                    'name': path.name,
                    'path': str(path),
                    'size_bytes': int(stat.st_size),
                    'created_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    'download_url': f'/ajax/admin/backups/download/{path.name}',
                })
            except Exception:
                continue
            if len(backups) >= limit:
                break
        return backups

    def _backup_root_size(self, settings: dict[str, Any]) -> int:
        root = self._backup_root(settings)
        if not root.exists():
            return 0
        total = 0
        for path in root.glob('canopy-backup-*.zip'):
            try:
                total += path.stat().st_size
            except Exception:
                continue
        return total

    def get_status(self) -> dict[str, Any]:
        settings = self.get_settings()
        status = self._load_status()
        all_backups = self.list_backups(settings=settings, limit=_MAX_RETENTION_COUNT)
        backups = all_backups[:12]
        last_success = _parse_dt(status.get('last_success_at'))
        next_due_at = ''
        if settings.get('enabled'):
            if last_success:
                next_due_at = (last_success + timedelta(hours=int(settings['interval_hours']))).isoformat()
            else:
                next_due_at = 'due now'
        latest = backups[0] if backups else None
        return {
            'success': True,
            'settings': settings,
            'running': bool(self._running_since),
            'running_since': self._running_since or '',
            'last_run_at': status.get('last_run_at') or '',
            'last_success_at': status.get('last_success_at') or '',
            'last_error_at': status.get('last_error_at') or '',
            'last_error': status.get('last_error') or '',
            'last_backup_name': status.get('last_backup_name') or (latest or {}).get('name', ''),
            'last_backup_path': status.get('last_backup_path') or (latest or {}).get('path', ''),
            'last_backup_size_bytes': int(status.get('last_backup_size_bytes') or (latest or {}).get('size_bytes') or 0),
            'last_duration_seconds': float(status.get('last_duration_seconds') or 0),
            'last_warning_count': int(status.get('last_warning_count') or 0),
            'next_due_at': next_due_at,
            'backup_count': len(all_backups),
            'backup_root_size_bytes': self._backup_root_size(settings),
            'backups': backups,
        }

    def _sqlite_snapshot(self, destination: Path) -> None:
        source_path = Path(str(getattr(self.db_manager, 'db_path', '') or '')).expanduser()
        if not source_path.exists():
            raise FileNotFoundError(f'Database file not found: {source_path}')
        src = sqlite3.connect(str(source_path), timeout=30)
        dst = sqlite3.connect(str(destination), timeout=30)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            try:
                dst.close()
            finally:
                src.close()

    def _write_tree(
        self,
        zf: zipfile.ZipFile,
        root: Path,
        arc_prefix: str,
        *,
        exclude_roots: list[Path],
        warnings: list[str],
    ) -> dict[str, int]:
        stats = {'files': 0, 'bytes': 0, 'skipped': 0}
        if not root.exists():
            return stats
        resolved_root = _safe_resolve(root)
        for path in sorted(resolved_root.rglob('*')):
            try:
                resolved = _safe_resolve(path)
                if any(_is_relative_to(resolved, ex) for ex in exclude_roots):
                    continue
                if resolved.is_symlink() or not resolved.is_file():
                    continue
                rel = resolved.relative_to(resolved_root).as_posix()
                zf.write(resolved, f'{arc_prefix}/{rel}')
                stat = resolved.stat()
                stats['files'] += 1
                stats['bytes'] += int(stat.st_size)
            except Exception as exc:
                stats['skipped'] += 1
                warnings.append(f'Skipped {path}: {exc}')
        return stats

    def _prune_backups(self, settings: dict[str, Any]) -> list[str]:
        root = self._backup_root(settings)
        retention = int(settings.get('retention_count') or _DEFAULT_RETENTION_COUNT)
        backups = sorted(root.glob('canopy-backup-*.zip'), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        removed: list[str] = []
        for path in backups[retention:]:
            try:
                path.unlink()
                removed.append(path.name)
            except Exception as exc:
                logger.warning('Could not prune old backup %s: %s', path, exc)
        return removed

    def resolve_backup_path(self, backup_name: str) -> Optional[Path]:
        clean_name = Path(str(backup_name or '').strip()).name
        if not clean_name or clean_name != str(backup_name or '').strip():
            return None
        if not clean_name.startswith('canopy-backup-') or not clean_name.endswith('.zip'):
            return None
        root = self._backup_root()
        path = _safe_resolve(root / clean_name)
        if not _is_relative_to(path, root) or not path.exists() or not path.is_file():
            return None
        return path

    def run_backup(self, *, trigger: str = 'manual') -> dict[str, Any]:
        settings = self.get_settings()
        if trigger == 'scheduled' and not settings.get('enabled'):
            return {'success': True, 'skipped': True, 'reason': 'disabled'}
        if not self._run_lock.acquire(blocking=False):
            return {'success': False, 'running': True, 'error': 'Backup already running'}

        started = _now()
        self._running_since = started.isoformat()
        backup_root = self._backup_root(settings)
        backup_root.mkdir(parents=True, exist_ok=True)
        status = self._load_status()
        status.update({'last_run_at': started.isoformat(), 'last_error': '', 'last_error_at': ''})
        self._save_status(status)
        warnings: list[str] = []
        temp_dir_obj: Optional[tempfile.TemporaryDirectory[str]] = None
        partial_path: Optional[Path] = None
        final_path: Optional[Path] = None

        try:
            stamp = f"{started.strftime('%Y%m%d-%H%M%S')}-{int(started.microsecond / 1000):03d}"
            suffix = str(trigger or 'manual').strip().lower().replace(' ', '-') or 'manual'
            final_path = backup_root / f'canopy-backup-{stamp}-{suffix}.zip'
            if final_path.exists():
                final_path = backup_root / f'canopy-backup-{stamp}-{suffix}-{os.getpid()}.zip'
            partial_path = final_path.with_suffix('.zip.partial')
            temp_dir_obj = tempfile.TemporaryDirectory(prefix='canopy-backup-', dir=str(backup_root))
            temp_dir = Path(temp_dir_obj.name)
            db_snapshot = temp_dir / 'canopy.db'
            self._sqlite_snapshot(db_snapshot)

            db_size = db_snapshot.stat().st_size if db_snapshot.exists() else 0
            files_stats = {'files': 0, 'bytes': 0, 'skipped': 0}
            large_stats = {'files': 0, 'bytes': 0, 'skipped': 0}
            metadata_files: list[str] = []
            data_dir = self._data_dir()
            files_root = _safe_resolve(Path(getattr(self.file_manager, 'storage_path', data_dir / 'files')))
            exclude_roots = [_safe_resolve(backup_root)]

            with zipfile.ZipFile(partial_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
                zf.write(db_snapshot, 'database/canopy.db')

                for name in _METADATA_FILES:
                    candidate = data_dir / name
                    if candidate.exists() and candidate.is_file():
                        try:
                            zf.write(candidate, f'metadata/{name}')
                            metadata_files.append(name)
                        except Exception as exc:
                            warnings.append(f'Skipped metadata/{name}: {exc}')

                if settings.get('include_files'):
                    files_stats = self._write_tree(
                        zf,
                        files_root,
                        'files',
                        exclude_roots=exclude_roots,
                        warnings=warnings,
                    )

                if settings.get('include_large_attachments'):
                    large_root_raw = get_large_attachment_store_root(self.db_manager)
                    large_root = resolve_large_attachment_store_root(large_root_raw)
                    if large_root:
                        resolved_large = _safe_resolve(large_root)
                        if resolved_large == files_root or _is_relative_to(resolved_large, files_root):
                            warnings.append('Large attachment store is already inside normal file storage; skipped duplicate large attachment pass.')
                        else:
                            large_stats = self._write_tree(
                                zf,
                                resolved_large,
                                'large_attachments',
                                exclude_roots=exclude_roots,
                                warnings=warnings,
                            )

                manifest = {
                    'kind': 'canopy_instance_backup_v1',
                    'created_at': _iso(started),
                    'trigger': trigger,
                    'meshspace_id': str(getattr(getattr(self.config, 'meshspace', None), 'meshspace_id', '') or ''),
                    'meshspace_name': str(getattr(getattr(self.config, 'meshspace', None), 'name', '') or ''),
                    'data_dir': str(data_dir),
                    'database_path': str(getattr(self.db_manager, 'db_path', '') or ''),
                    'file_storage_path': str(files_root),
                    'settings': settings,
                    'database': {'bytes': int(db_size)},
                    'files': files_stats,
                    'large_attachments': large_stats,
                    'metadata_files': metadata_files,
                    'warnings': warnings[:200],
                    'contains_sensitive_identity_material': any(name in metadata_files for name in ('peer_identity.json', 'secret_key.json')),
                    'restore_note': 'Restore by stopping Canopy, unpacking database/canopy.db and files/ into the target meshspace data root, then restarting. Keep this archive private.',
                }
                zf.writestr('manifest.json', json.dumps(manifest, indent=2, sort_keys=True))
                zf.writestr(
                    'RESTORE_README.txt',
                    'Canopy instance backup. Stop Canopy before restoring. Copy database/canopy.db to the active data root as canopy.db, copy files/* to the files directory, and preserve metadata JSON if recovering the same node identity. Store this archive securely.\n',
                )

            os.replace(partial_path, final_path)
            duration = max(0.0, time.time() - started.timestamp())
            stat = final_path.stat()
            removed = self._prune_backups(settings)
            result = {
                'success': True,
                'backup_name': final_path.name,
                'backup_path': str(final_path),
                'size_bytes': int(stat.st_size),
                'duration_seconds': duration,
                'warning_count': len(warnings),
                'warnings': warnings[:20],
                'pruned': removed,
                'settings': settings,
            }
            self._save_status({
                'last_run_at': started.isoformat(),
                'last_success_at': _iso(),
                'last_backup_name': final_path.name,
                'last_backup_path': str(final_path),
                'last_backup_size_bytes': int(stat.st_size),
                'last_duration_seconds': duration,
                'last_warning_count': len(warnings),
                'last_trigger': trigger,
                'last_error': '',
                'last_error_at': '',
            })
            logger.info('Instance backup created: %s (%s bytes)', final_path, stat.st_size)
            return result
        except Exception as exc:
            logger.error('Instance backup failed: %s', exc, exc_info=True)
            self._save_status({
                **status,
                'last_run_at': started.isoformat(),
                'last_error_at': _iso(),
                'last_error': str(exc),
                'last_trigger': trigger,
            })
            try:
                if partial_path and partial_path.exists():
                    partial_path.unlink()
            except Exception:
                pass
            return {'success': False, 'error': str(exc), 'settings': settings}
        finally:
            self._running_since = None
            if temp_dir_obj:
                try:
                    temp_dir_obj.cleanup()
                except Exception:
                    pass
            self._run_lock.release()

    def backup_due(self) -> bool:
        settings = self.get_settings()
        if not settings.get('enabled'):
            return False
        status = self._load_status()
        last_success = _parse_dt(status.get('last_success_at'))
        if not last_success:
            return True
        return _now() >= last_success + timedelta(hours=int(settings.get('interval_hours') or _DEFAULT_INTERVAL_HOURS))

    def start(self) -> None:
        if getattr(self.config, 'testing', False):
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='canopy_instance_backup')
        self._thread.start()
        logger.info('Instance backup scheduler started')

    def _loop(self) -> None:
        # Give startup, migrations, and mesh handshake a quiet window before doing IO.
        initial_delay = _coerce_int(os.getenv('CANOPY_BACKUP_INITIAL_DELAY_SECONDS'), 180, 30, 3600)
        if self._stop_event.wait(initial_delay):
            return
        while not self._stop_event.is_set():
            try:
                if self.backup_due():
                    self.run_backup(trigger='scheduled')
            except Exception as exc:
                logger.warning('Scheduled backup check failed: %s', exc)
            # Wake periodically, but let settings interval control actual run cadence.
            if self._stop_event.wait(300):
                return

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
