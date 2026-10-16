import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestAgyPluginAndBootstrap(unittest.TestCase):
    def test_plugin_manifests_and_skills_exist(self):
        plugin_json = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin_json["name"], "agy-triad-dispatch")
        self.assertEqual(plugin_json["version"], "0.1.0")
        self.assertEqual(plugin_json["skills"], "./skills/")

        antigravity_plugin_json = json.loads(
            (ROOT / ".antigravity-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(antigravity_plugin_json["name"], "agy-triad-dispatch")
        self.assertEqual(antigravity_plugin_json["version"], "0.1.0")
        self.assertEqual(antigravity_plugin_json["skills"], "./skills/")

        for skill in (
            "triad-claude-dispatch",
            "triad-codex-dispatch",
            "triad-antigravity-dispatch",
            "triad-gemini-dispatch",
            "triad-cross-family-review",
        ):
            skill_md = ROOT / "skills" / skill / "SKILL.md"
            self.assertTrue(skill_md.is_file(), f"missing {skill_md}")

    def test_toolkit_inventory_hashes_cleanly_in_review_round_v2(self):
        sys.path.insert(0, str(ROOT / "bin"))
        try:
            import review_round_v2
        except ImportError:
            self.skipTest("review_round_v2 not yet created")
            return
        toolkit_hashes = review_round_v2._toolkit()
        self.assertIn("bin/codex_wrapper.py", toolkit_hashes)
        self.assertIn("bin/claude_wrapper.py", toolkit_hashes)
        self.assertIn("bin/antigravity_wrapper.py", toolkit_hashes)
        self.assertIn("bin/gemini_wrapper.py", toolkit_hashes)
        self.assertIn("contracts/library-adoption.json", toolkit_hashes)

    def test_bootstrap_managed_launchers_and_library_verification(self):
        sys.path.insert(0, str(ROOT / "bin"))
        import bootstrap_repair

        expected_launchers = {
            "claude_wrapper.py",
            "codex_wrapper.py",
            "gemini_wrapper.py",
            "antigravity_wrapper.py",
            "review_round.py",
        }
        self.assertEqual(set(bootstrap_repair.MANAGED_LAUNCHERS), expected_launchers)

        # Check bootstrap.sh contains MANAGED_LAUNCHERS with all 5 wrappers
        bootstrap_sh = (ROOT / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
        for launcher in expected_launchers:
            self.assertIn(launcher, bootstrap_sh)

        # Check bootstrap.sh and bootstrap_repair verify library_adoption
        self.assertIn("library_adoption", bootstrap_sh)
        meta = bootstrap_repair.verify_library(ROOT)
        self.assertEqual(meta["version"], "0.1.0.dev4")

    def test_references_and_docs_exist(self):
        for path in (
            ROOT / "skills" / "triad-cross-family-review" / "references" / "convergence.md",
            ROOT / "skills" / "triad-cross-family-review" / "references" / "review-web.md",
            ROOT / "README.md",
            ROOT / "README.ko.md",
            ROOT / "SECURITY.md",
            ROOT / "scripts" / "verify_distribution.py",
        ):
            self.assertTrue(path.is_file(), f"missing {path}")


if __name__ == "__main__":
    unittest.main()
