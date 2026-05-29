"""Tests for bounded request timing diagnostics."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import types

if 'zeroconf' not in sys.modules:
    zeroconf_stub = types.ModuleType('zeroconf')

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    zeroconf_stub.ServiceBrowser = _Dummy
    zeroconf_stub.ServiceInfo = _Dummy
    zeroconf_stub.Zeroconf = _Dummy
    zeroconf_stub.ServiceStateChange = _Dummy
    sys.modules['zeroconf'] = zeroconf_stub

from canopy.core.request_metrics import RequestMetricsRecorder


def test_request_metrics_records_routes_and_slow_requests():
    recorder = RequestMetricsRecorder(slow_ms=25)

    recorder.record_request(
        method='GET',
        path='/ajax/channels/C123/messages?after=1',
        route='/ajax/channels/<channel_id>/messages',
        endpoint='ui.ajax_channel_messages',
        status_code=200,
        duration_ms=10.5,
        content_length=1234,
        user_agent='Mozilla/5.0',
    )
    recorder.record_request(
        method='POST',
        path='/api/v1/digestions/Dg123/query',
        route='/api/v1/digestions/<digestion_id>/query',
        endpoint='api.query_digestion',
        status_code=502,
        duration_ms=1500.0,
        user_agent='python-requests/2.0',
    )

    snapshot = recorder.snapshot()

    assert snapshot['available'] is True
    assert snapshot['total_requests'] == 2
    assert snapshot['status_classes']['2xx'] == 1
    assert snapshot['status_classes']['5xx'] == 1
    assert any(
        row['route'] == 'POST /api/v1/digestions/<digestion_id>/query'
        for row in snapshot['top_routes_by_total_time']
    )
    assert snapshot['recent_slow_or_error_requests'][-1]['endpoint'] == 'api.query_digestion'
    assert snapshot['recent_requests'][-1]['client_class'] == 'agent-or-script'


def test_request_metrics_rate_limit_redacts_caller_key():
    recorder = RequestMetricsRecorder()
    secret = 'sk-this-should-never-appear'

    fingerprint = recorder.record_rate_limit(
        method='GET',
        path='/api/v1/events',
        limiter='api',
        caller_key=secret,
    )
    snapshot = recorder.snapshot()

    assert secret not in str(snapshot)
    assert fingerprint in str(snapshot)
    assert snapshot['recent_rate_limits'][-1]['caller_fingerprint'] == fingerprint
    assert snapshot['rate_limit_counts'][0]['count'] >= 1
