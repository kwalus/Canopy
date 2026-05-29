"""Placeholder file IDs should not create noisy file lookups."""

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

from canopy.core.files import is_obvious_placeholder_file_id


def test_obvious_placeholder_file_ids_are_detected():
    assert is_obvious_placeholder_file_id('FILE_ID')
    assert is_obvious_placeholder_file_id('FILE_ID_HERE')
    assert is_obvious_placeholder_file_id('FAIL')
    assert not is_obvious_placeholder_file_id('F64ce8d47abcdef123456')
