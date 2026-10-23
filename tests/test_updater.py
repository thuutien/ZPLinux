"""Tests for the update checker (no network access)."""

import io
import os
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from labelix import updater  # noqa: E402


class UpdaterTests(unittest.TestCase):
    def test_versions(self):
        self.assertTrue(updater.is_newer("0.2.0", "0.1.9"))
        self.assertTrue(updater.is_newer("v1.0", "0.9.9"))
        self.assertTrue(updater.is_newer("0.1.10", "0.1.9"))
        self.assertFalse(updater.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(updater.is_newer("0.1.0-rc1", "0.1.0"))

    def test_due(self):
        self.assertTrue(updater.due(0, now=100000))
        self.assertFalse(updater.due(1000, now=1000 + 3600))

    def test_asset_for(self):
        rel = updater.Release("0.2.0", "u", assets={
            "labelix_0.2.0_all.deb": ("x", "a"), "labelix-0.2.0.tar.gz": ("y", "b")})
        self.assertEqual(updater.asset_for(rel, "deb"), "labelix_0.2.0_all.deb")
        self.assertEqual(updater.asset_for(rel, "tarball"), "labelix-0.2.0.tar.gz")
        self.assertIsNone(updater.asset_for(rel, "manual"))

    def test_running_from_source_is_manual(self):
        self.assertEqual(updater.install_method(), "manual")

    def test_download_rejects_other_hosts(self):
        rel = updater.Release("0.2.0", "u", assets={"a.deb": ("https://evil.example/a.deb", "00")})
        with self.assertRaises(ValueError):
            updater.download(rel, "a.deb", tempfile.gettempdir())

    def test_tarball_with_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "labelix-0.2.0.tar.gz")
            with tarfile.open(path, "w:gz") as tar:
                data = b"x"
                info = tarfile.TarInfo("../evil.sh")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            with self.assertRaises(ValueError):
                updater.install_command("tarball", path, d)


if __name__ == "__main__":
    unittest.main()
