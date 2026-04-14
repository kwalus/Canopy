import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from canopy.core import meshspaces
from canopy.core.meshspaces import MeshspaceRegistryManager


class MeshspaceRuntimeHostFilterRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.registry_root = Path(self.tempdir.name) / "registry"

    def test_windows_listener_pid_for_port_ignores_other_loopback_host(self) -> None:
        result = SimpleNamespace(
            stdout=(
                "  TCP    127.0.0.2:7800       0.0.0.0:0              LISTENING       106220\n"
            ),
            stderr="",
        )

        with patch("canopy.core.meshspaces._is_windows_platform", return_value=True), \
             patch("canopy.core.meshspaces.subprocess.run", return_value=result):
            self.assertEqual(meshspaces._windows_listener_pid_for_port(7800, host="127.0.0.1"), 0)
            self.assertEqual(meshspaces._windows_listener_pid_for_port(7800, host="127.0.0.2"), 106220)

    def test_probe_meshspace_runtime_ignores_foreign_loopback_listener_and_stale_pid(self) -> None:
        manager = MeshspaceRegistryManager(registry_root=self.registry_root)
        manager.create_meshspace(
            name="Canopy Mesh",
            meshspace_id="canopy-mesh",
            description="Official Canopy Mesh",
            http_port=7800,
            mesh_port=7801,
            discovery_port=7802,
        )
        record = manager.get_meshspace("canopy-mesh")
        self.assertIsNotNone(record)
        record["http_host"] = "127.0.0.1"
        record["status"] = "stopping"
        record["pid"] = 106220
        manager._upsert_record(record)

        result = SimpleNamespace(
            stdout=(
                "  TCP    127.0.0.2:7800       0.0.0.0:0              LISTENING       106220\n"
            ),
            stderr="",
        )

        with patch("canopy.core.meshspaces._is_windows_platform", return_value=True), \
             patch("canopy.core.meshspaces.subprocess.run", return_value=result), \
             patch("canopy.core.meshspaces._pid_is_alive", return_value=True), \
             patch("canopy.core.meshspaces._port_accepts_connections", return_value=False), \
             patch("canopy.core.meshspaces._http_json", return_value={}):
            probe = manager.probe_meshspace_runtime("canopy-mesh")

        self.assertFalse(probe["live"])
        self.assertEqual(probe["detected_pid"], 0)
        self.assertEqual(probe["effective_status"], "stopped")


if __name__ == "__main__":
    unittest.main()
