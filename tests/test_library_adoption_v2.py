import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


class TestLibraryAdoptionV2(unittest.TestCase):
    def test_verify_library_pins_exact_commit_and_bundle_ids(self):
        import library_adoption
        meta = library_adoption.verify_library(ROOT)
        self.assertEqual(meta["version"], "0.1.0.dev4")
        self.assertEqual(meta["source_commit"], "087af94a7078de0ae9701fb057840b5492f51f66")
        self.assertEqual(
            meta["wheel_sha256"],
            "c5bfdf10c4ce81f431ec7f77106e4d77b1556ac9c7b6b01bdb6616f988e8b2cc",
        )
        self.assertEqual(
            meta["bundle_ids"],
            {
                "prompt": "c332d95688b9db3dac187a0bebac6ddc0eddfdbdc5401a649697d0601455e843",
                "receipt": "26bf981f9fe1b3798508908fe648806a33db365d308c8424a11d2ed06c96d9e8",
                "roster": "a99fb58dbd920dfed0d88d8c8b9117bd07633b76b5d2565e05f50a5b5c423ed8",
                "verdict": "455dfd8a7d586d04eedb60ee492cf421e076a77bafcccbba12de37cc12bf0340",
            },
        )

    def test_ambient_library_override_is_rejected(self):
        import types
        import validate_v2
        fake = types.ModuleType("triad_dispatch_lib")
        fake.__file__ = "/tmp/fake_triad_dispatch_lib/__init__.py"
        saved = sys.modules.get("triad_dispatch_lib")
        sys.modules["triad_dispatch_lib"] = fake
        try:
            with self.assertRaisesRegex(RuntimeError, "ambient shared library"):
                validate_v2._library()
        finally:
            if saved is None:
                sys.modules.pop("triad_dispatch_lib", None)
            else:
                sys.modules["triad_dispatch_lib"] = saved

    def test_roster_resolution_merges_agents_override_and_preserves_defaults(self):
        import review_roster
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            roster = review_roster.resolve_roster(project)
            self.assertEqual(roster["enabled"], ["claude", "codex", "google"])
            self.assertEqual(roster["families"], ["claude", "codex", "google"])


DEV4_COMMIT = "087af94a7078de0ae9701fb057840b5492f51f66"
DEV4_WHEEL_SHA256 = "c5bfdf10c4ce81f431ec7f77106e4d77b1556ac9c7b6b01bdb6616f988e8b2cc"
DEV4_RECEIPT_BUNDLE_ID = "26bf981f9fe1b3798508908fe648806a33db365d308c8424a11d2ed06c96d9e8"


def _copy_adoption_root(tmp):
    """Copy only the managed adoption inventory into a resolved temp root."""
    root = Path(tmp).resolve()
    shutil.copytree(
        ROOT / "bin/triad_dispatch_lib",
        root / "bin/triad_dispatch_lib",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / "contracts").mkdir(parents=True)
    shutil.copy2(ROOT / "contracts/library-adoption.json", root / "contracts/library-adoption.json")
    (root / "vendor").mkdir(parents=True)
    shutil.copy2(ROOT / "vendor/triad-dispatch-lib.LICENSE", root / "vendor/triad-dispatch-lib.LICENSE")
    return root


class TestLibraryAdoptionDev4(unittest.TestCase):
    def test_adoption_pins_dev4(self):
        import json
        record = json.loads((ROOT / "contracts/library-adoption.json").read_text(encoding="utf-8"))
        self.assertEqual(record["version"], "0.1.0.dev4")
        self.assertEqual(record["source_commit"], DEV4_COMMIT)
        self.assertEqual(record["wheel_sha256"], DEV4_WHEEL_SHA256)
        self.assertEqual(record["bundle_ids"]["receipt"], DEV4_RECEIPT_BUNDLE_ID)

    def test_receipt_module_and_bundle_required(self):
        import library_adoption
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_adoption_root(tmp)
            library_adoption.verify_library(root)
            module = root / "bin/triad_dispatch_lib/receipt.py"
            original = module.read_bytes()
            module.unlink()
            with self.assertRaises(ValueError):
                library_adoption.verify_library(root)
            module.write_bytes(original)
            library_adoption.verify_library(root)
            fields = root / "bin/triad_dispatch_lib/data/receipt-bundle/contracts/receipt-fields.json"
            fields.write_bytes(fields.read_bytes() + b" ")
            with self.assertRaises(ValueError):
                library_adoption.verify_library(root)

    def test_stale_unexpected_file_refused(self):
        import library_adoption
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_adoption_root(tmp)
            library_adoption.verify_library(root)
            (root / "bin/triad_dispatch_lib/data/stale.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                library_adoption.verify_library(root)
