from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import package_readerpc_server as package  # noqa: E402


class ReaderPCPackageTests(unittest.TestCase):
    def synthetic_archive(self, root: Path, *, tamper: bool = False) -> Path:
        payload = {
            name: (b"MZ-test" if name == package.EXE_REL else f"# {name}\n".encode())
            for name in package.PAYLOAD_PATHS
        }
        manifest = package._build_manifest(
            "0.1.0",
            payload,
            [{"path": "source.py", "sha256": "a" * 64}],
            "6.20.0",
        )
        prefix = "ReaderPC-Server-0.1.0"
        entries = {
            f"{prefix}/{package.MANIFEST_REL}": package._manifest_bytes(manifest),
            **{f"{prefix}/{name}": content for name, content in payload.items()},
        }
        if tamper:
            entries[f"{prefix}/{package.EXE_REL}"] += b"tampered"
        archive = root / "candidate.zip"
        package._zip_write(archive, entries)
        return archive

    def test_verify_rejects_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package.verify_archive(self.synthetic_archive(root))
            with self.assertRaises(package.PackageError):
                package.verify_archive(self.synthetic_archive(root, tamper=True))

    def test_install_is_versioned_and_writes_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.synthetic_archive(root)
            install_root = root / "install"
            shortcut = root / "ReaderPC.lnk"
            with patch.object(package, "_write_start_menu_shortcut", return_value=shortcut):
                release = package.install_archive(
                    archive,
                    install_root=install_root,
                )
            self.assertEqual(release, install_root.resolve() / "releases" / "0.1.0")
            self.assertTrue((release / package.EXE_REL).is_file())
            self.assertTrue((install_root / "current.json").is_file())
            with self.assertRaises(package.PackageError):
                package.install_archive(archive, install_root=install_root)


if __name__ == "__main__":
    unittest.main()
