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
        self.assertEqual(meta["version"], "0.1.0.dev3")
        self.assertEqual(meta["source_commit"], "2d1e62e7de6f33fa13a918a39579c6b5e74def00")
        self.assertEqual(
            meta["wheel_sha256"],
            "df1e84b56a7238bab2a019064c850c30bde5b6b15268a81a1e74d76df0c187b8",
        )
        self.assertEqual(
            meta["bundle_ids"],
            {
                "prompt": "c332d95688b9db3dac187a0bebac6ddc0eddfdbdc5401a649697d0601455e843",
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
            project = Path(tmp)
            roster = review_roster.resolve_roster(project)
            self.assertEqual(roster["enabled"], ["claude", "codex", "google"])
            self.assertEqual(roster["families"], ["claude", "codex", "google"])
