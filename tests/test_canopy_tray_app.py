"""Tests for Windows tray app auto-start command selection."""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "pystray" not in sys.modules:
    pystray_stub = types.ModuleType("pystray")

    class _Icon:
        def __init__(self, *args, **kwargs):
            pass

    class _Menu:
        SEPARATOR = object()

        def __init__(self, *args, **kwargs):
            pass

    class _MenuItem:
        def __init__(self, *args, **kwargs):
            pass

    pystray_stub.Icon = _Icon
    pystray_stub.Menu = _Menu
    pystray_stub.MenuItem = _MenuItem
    sys.modules["pystray"] = pystray_stub

from canopy_tray.app import _windows_preferred_pythonw_executable


class TestCanopyTrayApp(unittest.TestCase):
    def test_windows_preferred_pythonw_executable_uses_pythonw_when_present(self) -> None:
        with patch("canopy_tray.app.os.name", "nt"), patch("canopy_tray.app.os.path.exists", return_value=True):
            resolved = _windows_preferred_pythonw_executable(r"C:\Python311\python.exe")
        self.assertEqual(resolved, r"C:\Python311\pythonw.exe")

    def test_windows_preferred_pythonw_executable_leaves_non_python_executable(self) -> None:
        with patch("canopy_tray.app.os.name", "nt"):
            resolved = _windows_preferred_pythonw_executable(r"C:\Canopy\Canopy.exe")
        self.assertEqual(resolved, r"C:\Canopy\Canopy.exe")


if __name__ == "__main__":
    unittest.main()
