"""Bounded in-memory HTTP request diagnostics for Canopy.

The recorder is intentionally lightweight: it keeps only route names, status
classes, timings, and short hashed caller fingerprints. It does not persist user
content, request bodies, query strings, cookies, API keys, or IP addresses.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import Counter, defaultdict, deque
from typing import Any, Deque, Optional


class RequestMetricsRecorder:
    """Collect bounded request timing and rate-limit diagnostics."""

    def __init__(
        self,
        *,
        max_recent: int = 320,
        max_slow: int = 160,
        max_rate_limits: int = 160,
        slow_ms: float = 750.0,
    ) -> None:
        self.max_recent = max(40, int(max_recent or 320))
        self.max_slow = max(20, int(max_slow or 160))
        self.max_rate_limits = max(20, int(max_rate_limits or 160))
        self.slow_ms = max(50.0, float(slow_ms or 750.0))
        self.started_at = time.time()
        self._lock = threading.RLock()
        self._recent: Deque[dict[str, Any]] = deque(maxlen=self.max_recent)
        self._slow: Deque[dict[str, Any]] = deque(maxlen=self.max_slow)
        self._rate_limits: Deque[dict[str, Any]] = deque(maxlen=self.max_rate_limits)
        self._endpoint_counts: Counter[str] = Counter()
        self._endpoint_total_ms: Counter[str] = Counter()
        self._endpoint_max_ms: dict[str, float] = {}
        self._endpoint_statuses: dict[str, Counter[str]] = defaultdict(Counter)
        self._endpoint_recent_ms: dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=96))
        self._status_counts: Counter[str] = Counter()
        self._method_counts: Counter[str] = Counter()
        self._rate_limit_counts: Counter[str] = Counter()

    @staticmethod
    def fingerprint(value: Any) -> str:
        text = str(value or '').strip()
        if not text:
            return 'unknown'
        return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:12]

    @staticmethod
    def status_class(status_code: Any) -> str:
        try:
            code = int(status_code or 0)
        except Exception:
            code = 0
        if code <= 0:
            return 'unknown'
        return f"{code // 100}xx"

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
        lower = math.floor(rank)
        upper = math.ceil(rank)
        if lower == upper:
            return ordered[int(rank)]
        return ordered[lower] * (upper - rank) + ordered[upper] * (rank - lower)

    def record_request(
        self,
        *,
        method: str,
        path: str,
        route: str,
        endpoint: str,
        status_code: int,
        duration_ms: float,
        content_length: Optional[int] = None,
        user_agent: str = '',
    ) -> None:
        method_text = str(method or 'GET').upper()
        path_text = str(path or '').split('?', 1)[0]
        if path_text.startswith('/static/'):
            return
        route_text = str(route or path_text or 'unknown').strip() or 'unknown'
        endpoint_text = str(endpoint or '').strip()
        key = f"{method_text} {route_text}"
        status = int(status_code or 0)
        duration = max(0.0, float(duration_ms or 0.0))
        status_group = self.status_class(status)
        ua = str(user_agent or '')[:160]
        event = {
            'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'method': method_text,
            'route': route_text,
            'endpoint': endpoint_text,
            'status': status,
            'status_class': status_group,
            'duration_ms': round(duration, 1),
        }
        if content_length is not None:
            try:
                event['content_length'] = max(0, int(content_length))
            except Exception:
                pass
        if ua:
            event['client_class'] = self._client_class(ua)

        with self._lock:
            self._recent.append(event)
            self._endpoint_counts[key] += 1
            self._endpoint_total_ms[key] += duration
            self._endpoint_max_ms[key] = max(duration, self._endpoint_max_ms.get(key, 0.0))
            self._endpoint_statuses[key][status_group] += 1
            self._endpoint_recent_ms[key].append(duration)
            self._status_counts[status_group] += 1
            self._method_counts[method_text] += 1
            if duration >= self.slow_ms or status >= 500:
                self._slow.append(event)

    def record_rate_limit(self, *, method: str, path: str, limiter: str, caller_key: Any) -> str:
        method_text = str(method or 'GET').upper()
        path_text = str(path or '').split('?', 1)[0]
        limiter_text = str(limiter or 'unknown').strip() or 'unknown'
        caller_fp = self.fingerprint(caller_key)
        event = {
            'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'method': method_text,
            'path': path_text,
            'limiter': limiter_text,
            'caller_fingerprint': caller_fp,
        }
        with self._lock:
            self._rate_limits.append(event)
            self._rate_limit_counts[f"{method_text} {path_text}"] += 1
            self._rate_limit_counts[f"limiter:{limiter_text}"] += 1
        return caller_fp

    def snapshot(self, *, limit_recent: int = 30, limit_slow: int = 30, limit_endpoints: int = 20) -> dict[str, Any]:
        with self._lock:
            endpoint_rows: list[dict[str, Any]] = []
            for key, count in self._endpoint_counts.items():
                if count <= 0:
                    continue
                recent_ms = list(self._endpoint_recent_ms.get(key) or [])
                avg_ms = float(self._endpoint_total_ms.get(key, 0.0)) / float(count)
                p95 = self._percentile(recent_ms, 95.0)
                endpoint_rows.append({
                    'route': key,
                    'count': int(count),
                    'avg_ms': round(avg_ms, 1),
                    'max_ms': round(float(self._endpoint_max_ms.get(key, 0.0)), 1),
                    'recent_p95_ms': round(p95, 1) if p95 is not None else None,
                    'status_classes': dict(self._endpoint_statuses.get(key) or {}),
                    'total_ms': round(float(self._endpoint_total_ms.get(key, 0.0)), 1),
                })
            endpoint_rows.sort(key=lambda row: (float(row.get('total_ms') or 0), int(row.get('count') or 0)), reverse=True)

            rate_rows = [
                {'key': key, 'count': int(count)}
                for key, count in self._rate_limit_counts.most_common(max(1, int(limit_endpoints or 20)))
            ]
            recent_limit = max(1, int(limit_recent or 30))
            slow_limit = max(1, int(limit_slow or 30))
            return {
                'available': True,
                'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.started_at)),
                'slow_threshold_ms': round(self.slow_ms, 1),
                'total_requests': int(sum(self._endpoint_counts.values())),
                'status_classes': dict(self._status_counts),
                'method_counts': dict(self._method_counts),
                'top_routes_by_total_time': endpoint_rows[: max(1, int(limit_endpoints or 20))],
                'recent_slow_or_error_requests': list(self._slow)[-slow_limit:],
                'recent_requests': list(self._recent)[-recent_limit:],
                'rate_limit_counts': rate_rows,
                'recent_rate_limits': list(self._rate_limits)[-recent_limit:],
            }

    @staticmethod
    def _client_class(user_agent: str) -> str:
        ua = user_agent.lower()
        if not ua:
            return 'unknown'
        if 'python-requests' in ua or 'httpx' in ua or 'curl' in ua or 'aiohttp' in ua:
            return 'agent-or-script'
        if 'mozilla/' in ua:
            return 'browser'
        return 'other'
