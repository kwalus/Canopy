"""Standalone Canopy traffic monitor with UI and analysis endpoints."""

from __future__ import annotations

import argparse
import atexit
import os
import sys
from typing import Any

from flask import Flask, jsonify, render_template, request

if __package__ in (None, ""):
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from collector import CanopyTrafficCollector
else:
    from .collector import CanopyTrafficCollector


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _query_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def create_app(*, target: str, api_key: str, poll_seconds: int, retention_minutes: int) -> Flask:
    app = Flask(__name__, template_folder='templates')

    collector = CanopyTrafficCollector(
        target_base_url=target,
        api_key=api_key,
        poll_seconds=poll_seconds,
        retention_minutes=retention_minutes,
    )
    collector.start()

    app.config['COLLECTOR'] = collector
    app.config['MONITOR_CONFIG'] = {
        'target': target,
        'poll_seconds': poll_seconds,
        'retention_minutes': retention_minutes,
        'has_api_key': bool(api_key),
    }

    @atexit.register
    def _shutdown_collector() -> None:
        try:
            collector.stop()
        except Exception:
            pass

    @app.route('/')
    def index() -> str:
        return render_template('index.html', monitor_config=app.config['MONITOR_CONFIG'])

    @app.route('/api/metrics/config')
    def metrics_config():
        return jsonify(app.config['MONITOR_CONFIG'])

    @app.route('/api/metrics/latest')
    def metrics_latest():
        return jsonify(collector.latest())

    @app.route('/api/metrics/samples')
    def metrics_samples():
        window_seconds = _query_int('window_minutes', 60, 1, 60 * 24 * 7) * 60
        limit = _query_int('limit', 200, 1, 5000)
        return jsonify({
            'window_seconds': window_seconds,
            'limit': limit,
            'samples': collector.samples(window_seconds=window_seconds, limit=limit),
        })

    @app.route('/api/metrics/timeseries')
    def metrics_timeseries():
        metric = (request.args.get('metric') or 'traffic_score').strip().lower()
        window_seconds = _query_int('window_minutes', 60, 1, 60 * 24 * 7) * 60
        bucket_seconds = _query_int('bucket_seconds', 60, 5, 3600)
        return jsonify({
            'metric': metric,
            'window_seconds': window_seconds,
            'bucket_seconds': bucket_seconds,
            'series': collector.timeseries(
                metric=metric,
                window_seconds=window_seconds,
                bucket_seconds=bucket_seconds,
            ),
        })

    @app.route('/api/metrics/breakdown')
    def metrics_breakdown():
        window_seconds = _query_int('window_minutes', 60, 1, 60 * 24 * 7) * 60
        return jsonify({
            'window_seconds': window_seconds,
            'rows': collector.endpoint_breakdown(window_seconds=window_seconds),
        })

    @app.route('/api/metrics/dashboard')
    def metrics_dashboard():
        metric = (request.args.get('metric') or 'traffic_score').strip().lower()
        window_seconds = _query_int('window_minutes', 60, 1, 60 * 24 * 7) * 60
        bucket_seconds = _query_int('bucket_seconds', 60, 5, 3600)

        return jsonify({
            'config': app.config['MONITOR_CONFIG'],
            'latest': collector.latest(),
            'timeseries': collector.timeseries(
                metric=metric,
                window_seconds=window_seconds,
                bucket_seconds=bucket_seconds,
            ),
            'breakdown': collector.endpoint_breakdown(window_seconds=window_seconds),
            'query': {
                'metric': metric,
                'window_seconds': window_seconds,
                'bucket_seconds': bucket_seconds,
            },
        })

    @app.route('/api/metrics/refresh', methods=['POST'])
    def metrics_refresh():
        sample = collector.collect_once()
        return jsonify({'ok': True, 'sample': sample})

    @app.errorhandler(500)
    def _internal_error(error: Any):
        return jsonify({'error': 'internal_error', 'message': str(error)}), 500

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description='Standalone Canopy traffic monitor')
    parser.add_argument('--target', default=os.getenv('CANOPY_MONITOR_TARGET', 'http://127.0.0.1:7770'))
    parser.add_argument('--api-key', default=os.getenv('CANOPY_MONITOR_API_KEY', ''))
    parser.add_argument('--poll-seconds', type=int, default=_env_int('CANOPY_MONITOR_POLL_SECONDS', 5, 1, 120))
    parser.add_argument('--retention-minutes', type=int, default=_env_int('CANOPY_MONITOR_RETENTION_MINUTES', 1440, 5, 60 * 24 * 14))
    parser.add_argument('--host', default=os.getenv('CANOPY_MONITOR_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=_env_int('CANOPY_MONITOR_PORT', 9095, 1, 65535))
    args = parser.parse_args()

    app = create_app(
        target=args.target,
        api_key=args.api_key,
        poll_seconds=args.poll_seconds,
        retention_minutes=args.retention_minutes,
    )

    print('Canopy Traffic Tool')
    print('===================')
    print(f'  Target:      {args.target}')
    print(f'  Poll every:  {args.poll_seconds}s')
    print(f'  Retention:   {args.retention_minutes} min')
    api_key_status = 'configured' if bool(args.api_key) else 'not configured'
    print(f'  API key:     {api_key_status}')
    print(f'  Open:        http://{args.host}:{args.port}')

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
