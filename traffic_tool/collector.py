"""Standalone sampling collector for Canopy traffic/activity telemetry."""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


@dataclass
class EndpointProbe:
    name: str
    path: str
    requires_key: bool


_PROBES: tuple[EndpointProbe, ...] = (
    EndpointProbe('health', '/api/v1/health', False),
    EndpointProbe('p2p_status', '/api/v1/p2p/status', False),
    EndpointProbe('system_info', '/api/v1/info', True),
    EndpointProbe('relay_status', '/api/v1/p2p/relay_status', True),
)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class CanopyTrafficCollector:
    """Poll Canopy endpoints and maintain rolling telemetry history."""

    def __init__(
        self,
        *,
        target_base_url: str,
        api_key: str = '',
        poll_seconds: int = 5,
        retention_minutes: int = 60 * 24,
    ):
        base = target_base_url.rstrip('/')
        if not base.startswith('http://') and not base.startswith('https://'):
            base = f'http://{base}'

        self.target_base_url = base
        self.api_key = api_key.strip()
        self.poll_seconds = max(1, int(poll_seconds))
        self.retention_minutes = max(1, int(retention_minutes))
        # +2 minutes slack
        self._max_samples = int((self.retention_minutes * 60) / self.poll_seconds) + 120

        self._samples: deque[dict[str, Any]] = deque(maxlen=self._max_samples)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_poll_error = ''

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='canopy_traffic_collector', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _http_get(self, path: str, *, use_key: bool = False) -> dict[str, Any]:
        url = urljoin(f'{self.target_base_url}/', path.lstrip('/'))
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'canopy-traffic-tool/1.0',
        }
        if use_key and self.api_key:
            headers['X-API-Key'] = self.api_key

        req = Request(url, headers=headers, method='GET')
        started = time.perf_counter()
        try:
            with urlopen(req, timeout=8.0) as resp:
                payload = resp.read()
                latency_ms = (time.perf_counter() - started) * 1000.0
                status_code = getattr(resp, 'status', 200)
                data: Any
                try:
                    data = json.loads(payload.decode('utf-8')) if payload else {}
                except Exception:
                    data = {}
                return {
                    'ok': True,
                    'status_code': int(status_code),
                    'latency_ms': round(latency_ms, 3),
                    'response_bytes': len(payload),
                    'data': data,
                    'error': '',
                }
        except HTTPError as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            raw = b''
            try:
                raw = exc.read() or b''
            except Exception:
                raw = b''
            message = f'HTTP {exc.code}'
            return {
                'ok': False,
                'status_code': int(exc.code),
                'latency_ms': round(latency_ms, 3),
                'response_bytes': len(raw),
                'data': {},
                'error': message,
            }
        except URLError as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            return {
                'ok': False,
                'status_code': 0,
                'latency_ms': round(latency_ms, 3),
                'response_bytes': 0,
                'data': {},
                'error': str(exc.reason) if hasattr(exc, 'reason') else str(exc),
            }
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            return {
                'ok': False,
                'status_code': 0,
                'latency_ms': round(latency_ms, 3),
                'response_bytes': 0,
                'data': {},
                'error': str(exc),
            }

    @staticmethod
    def _extract_metric(sample: dict[str, Any], metric: str) -> float:
        latest = sample
        probes = latest.get('probes', {})
        derived = latest.get('derived', {})
        p2p = latest.get('p2p', {})

        if metric == 'message_delta':
            return float(derived.get('messages_delta', 0))
        if metric == 'feed_delta':
            return float(derived.get('feed_posts_delta', 0))
        if metric == 'peer_delta':
            return float(derived.get('peers_delta', 0))
        if metric == 'connected_peers':
            return float(p2p.get('connected_peers', 0))
        if metric == 'discovered_peers':
            return float(p2p.get('discovered_peers', 0))
        if metric == 'probe_errors':
            return float(derived.get('probe_error_count', 0))
        if metric == 'system_info_latency_ms':
            return float((probes.get('system_info') or {}).get('latency_ms', 0.0))
        if metric == 'p2p_status_latency_ms':
            return float((probes.get('p2p_status') or {}).get('latency_ms', 0.0))
        if metric == 'relay_routes':
            return float(derived.get('relay_route_count', 0))
        return float(derived.get('traffic_score', 0.0))

    def _build_sample(self) -> dict[str, Any]:
        now = time.time()
        probes: dict[str, dict[str, Any]] = {}

        for probe in _PROBES:
            if probe.requires_key and not self.api_key:
                probes[probe.name] = {
                    'ok': False,
                    'status_code': 0,
                    'latency_ms': 0.0,
                    'response_bytes': 0,
                    'data': {},
                    'error': 'missing_api_key',
                    'skipped': True,
                }
                continue
            probes[probe.name] = self._http_get(probe.path, use_key=probe.requires_key)

        info_data = (probes.get('system_info') or {}).get('data') or {}
        p2p_data = (probes.get('p2p_status') or {}).get('data') or {}
        relay_data = (probes.get('relay_status') or {}).get('data') or {}

        db_stats = info_data.get('database_stats') or {}
        p2p_snapshot = {
            'running': bool(p2p_data.get('running', False)),
            'connected_peers': int(p2p_data.get('connected_peers', 0) or 0),
            'discovered_peers': int(p2p_data.get('discovered_peers', 0) or 0),
            'relay_policy': p2p_data.get('relay_policy') or relay_data.get('relay_policy') or 'unknown',
            'active_relays': relay_data.get('active_relays') or {},
        }

        probe_error_count = sum(
            1 for p in probes.values()
            if (not p.get('ok')) and (not p.get('skipped'))
        )
        relay_route_count = len(p2p_snapshot['active_relays']) if isinstance(p2p_snapshot['active_relays'], dict) else 0

        sample = {
            'timestamp': now,
            'timestamp_iso': _iso(now),
            'target_base_url': self.target_base_url,
            'poll_seconds': self.poll_seconds,
            'probes': probes,
            'database_stats': db_stats,
            'p2p': p2p_snapshot,
            'derived': {
                'probe_error_count': probe_error_count,
                'relay_route_count': relay_route_count,
                'messages_delta': 0,
                'feed_posts_delta': 0,
                'peers_delta': 0,
                'traffic_score': 0.0,
            },
        }

        return sample

    def _apply_deltas(self, current: dict[str, Any], previous: dict[str, Any] | None) -> None:
        if not previous:
            return

        cur_db = current.get('database_stats') or {}
        prev_db = previous.get('database_stats') or {}

        msg_delta = int(cur_db.get('messages', 0) or 0) - int(prev_db.get('messages', 0) or 0)
        feed_delta = int(cur_db.get('feed_posts', 0) or 0) - int(prev_db.get('feed_posts', 0) or 0)

        cur_peers = int((current.get('p2p') or {}).get('connected_peers', 0) or 0)
        prev_peers = int((previous.get('p2p') or {}).get('connected_peers', 0) or 0)
        peers_delta = cur_peers - prev_peers

        elapsed = max(1e-6, float(current['timestamp']) - float(previous['timestamp']))
        msg_rate = max(0.0, msg_delta) / elapsed
        feed_rate = max(0.0, feed_delta) / elapsed
        probe_errors = int((current.get('derived') or {}).get('probe_error_count', 0) or 0)
        # Composite traffic score: throughput + connectivity churn + probe errors pressure.
        traffic_score = (msg_rate * 6.0) + (feed_rate * 4.0) + (abs(peers_delta) * 0.75) + (probe_errors * 2.0)

        current['derived']['messages_delta'] = msg_delta
        current['derived']['feed_posts_delta'] = feed_delta
        current['derived']['peers_delta'] = peers_delta
        current['derived']['traffic_score'] = round(traffic_score, 4)

    def _run(self) -> None:
        # First sample immediately.
        previous: dict[str, Any] | None = None
        while not self._stop.is_set():
            try:
                sample = self._build_sample()
                self._apply_deltas(sample, previous)
                with self._lock:
                    self._samples.append(sample)
                previous = sample
                self._last_poll_error = ''
            except Exception as exc:
                self._last_poll_error = str(exc)

            self._stop.wait(self.poll_seconds)

    def collect_once(self) -> dict[str, Any]:
        """Force one immediate collection cycle and return the sample."""
        with self._lock:
            previous = self._samples[-1] if self._samples else None
        sample = self._build_sample()
        self._apply_deltas(sample, previous)
        with self._lock:
            self._samples.append(sample)
        self._last_poll_error = ''
        return sample

    def latest(self) -> dict[str, Any]:
        with self._lock:
            if not self._samples:
                return {
                    'ok': False,
                    'error': 'No telemetry samples yet',
                    'target_base_url': self.target_base_url,
                    'poll_seconds': self.poll_seconds,
                }
            latest = self._samples[-1]
            return {
                'ok': True,
                'sample': latest,
                'sample_count': len(self._samples),
                'last_poll_error': self._last_poll_error,
                'retention_minutes': self.retention_minutes,
            }

    def samples(self, *, window_seconds: int = 3600, limit: int = 300) -> list[dict[str, Any]]:
        now = time.time()
        since = now - max(1, int(window_seconds))
        lim = max(1, min(5000, int(limit)))

        with self._lock:
            data = [s for s in self._samples if float(s.get('timestamp', 0.0)) >= since]

        if not data:
            return []
        return data[-lim:]

    def endpoint_breakdown(self, *, window_seconds: int = 3600) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = defaultdict(lambda: {
            'endpoint': '',
            'samples': 0,
            'ok_count': 0,
            'error_count': 0,
            'skipped_count': 0,
            'avg_latency_ms': 0.0,
            'max_latency_ms': 0.0,
            'avg_response_bytes': 0.0,
            '_lat_sum': 0.0,
            '_resp_sum': 0,
        })

        for sample in self.samples(window_seconds=window_seconds, limit=5000):
            probes = sample.get('probes') or {}
            for name, probe in probes.items():
                row = rows[name]
                row['endpoint'] = name
                row['samples'] += 1
                if probe.get('skipped'):
                    row['skipped_count'] += 1
                elif probe.get('ok'):
                    row['ok_count'] += 1
                else:
                    row['error_count'] += 1
                lat = float(probe.get('latency_ms', 0.0) or 0.0)
                row['_lat_sum'] += lat
                row['max_latency_ms'] = max(row['max_latency_ms'], lat)
                row['_resp_sum'] += int(probe.get('response_bytes', 0) or 0)

        out: list[dict[str, Any]] = []
        for row in rows.values():
            samples = max(1, int(row['samples']))
            non_skipped = max(1, samples - int(row['skipped_count']))
            row['avg_latency_ms'] = round(row['_lat_sum'] / samples, 3)
            row['avg_response_bytes'] = int(row['_resp_sum'] / samples)
            row['error_rate'] = round(row['error_count'] / non_skipped, 5)
            del row['_lat_sum']
            del row['_resp_sum']
            out.append(row)

        out.sort(key=lambda r: (r['error_count'], r['avg_latency_ms']), reverse=True)
        return out

    def timeseries(
        self,
        *,
        metric: str,
        window_seconds: int = 3600,
        bucket_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        bucket = max(1, int(bucket_seconds))
        samples = self.samples(window_seconds=window_seconds, limit=5000)
        if not samples:
            return []

        buckets: dict[int, dict[str, Any]] = {}
        for sample in samples:
            ts = float(sample.get('timestamp', 0.0))
            b = int(ts // bucket) * bucket
            row = buckets.setdefault(b, {
                'bucket_start': float(b),
                'bucket_start_iso': _iso(float(b)),
                'value_sum': 0.0,
                'count': 0,
                'value': 0.0,
            })
            row['value_sum'] += self._extract_metric(sample, metric)
            row['count'] += 1

        keys = sorted(buckets.keys())
        if not keys:
            return []

        out: list[dict[str, Any]] = []
        current = keys[0]
        end = keys[-1]
        while current <= end:
            row = buckets.get(current)
            if not row:
                out.append({
                    'bucket_start': float(current),
                    'bucket_start_iso': _iso(float(current)),
                    'value': 0.0,
                    'count': 0,
                })
            else:
                count = max(1, int(row['count']))
                out.append({
                    'bucket_start': row['bucket_start'],
                    'bucket_start_iso': row['bucket_start_iso'],
                    'value': round(row['value_sum'] / count, 5),
                    'count': row['count'],
                })
            current += bucket

        return out
