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
        self.assertNotIn("skills", plugin_json)

        antigravity_plugin_json = json.loads(
            (ROOT / ".antigravity-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(antigravity_plugin_json["name"], "agy-triad-dispatch")
        self.assertEqual(antigravity_plugin_json["version"], "0.1.0")
        self.assertNotIn("skills", antigravity_plugin_json)

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
        sys.path.insert(0, str(ROOT / "scripts"))
        import review_round_v2
        import verify_distribution

        toolkit_hashes = review_round_v2._toolkit()
        self.assertIn("bin/codex_wrapper.py", toolkit_hashes)
        self.assertIn("bin/claude_wrapper.py", toolkit_hashes)
        self.assertIn("bin/antigravity_wrapper.py", toolkit_hashes)
        self.assertIn("bin/gemini_wrapper.py", toolkit_hashes)
        self.assertIn("contracts/library-adoption.json", toolkit_hashes)
        self.assertEqual(set(toolkit_hashes) - set(verify_distribution.HASH_TARGETS), set())
        for rel in verify_distribution.HASH_TARGETS:
            self.assertTrue((ROOT / rel).is_file(), f"missing HASH_TARGET: {rel}")

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

    def test_no_dev3_references_in_shipped_text(self):
        targets = [
            ROOT / "README.md",
            ROOT / "README.ko.md",
            ROOT / "SECURITY.md",
            ROOT / "scripts" / "bootstrap.sh",
            ROOT / "plugin.json",
            ROOT / ".antigravity-plugin" / "plugin.json",
        ]
        targets.extend(sorted((ROOT / "skills").glob("**/SKILL.md")))

        forbidden = [
            "0.1.0.dev3",
            "2d1e62e7de6f33fa13a918a39579c6b5e74def00",
        ]

        violations = []
        for target in targets:
            self.assertTrue(target.is_file(), f"missing target {target}")
            content = target.read_text(encoding="utf-8")
            for token in forbidden:
                if token in content:
                    violations.append(f"{target.relative_to(ROOT)} contains forbidden token '{token}'")

        self.assertEqual(violations, [])

    def test_classifier_extension_default_path_uses_agy_triad_dispatch(self):
        import os
        import tempfile
        sys.path.insert(0, str(ROOT / "bin"))
        import _common

        orig_env = dict(os.environ)
        try:
            os.environ.pop("TRIAD_CLASSIFIER_EXTENSION", None)
            with tempfile.TemporaryDirectory() as tmp:
                xdg = Path(tmp).resolve()
                os.environ["XDG_CONFIG_HOME"] = str(xdg)
                self.assertEqual(
                    _common._classifier_extension_path(),
                    xdg / "agy-triad-dispatch" / "classifier-patches.json",
                )
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

    def test_apply_patch_stored_failure_classification_supports_codex(self):
        sys.path.insert(0, str(ROOT / "bin"))
        import apply_patch

        record = {
            "cli": "codex",
            "classification": "unknown",
            "vendor_exit_code": 1,
            "exit_code": 1,
            "stdout": "",
            "stderr": "too many requests",
            "transport": {"stdin_delivery": "complete"},
        }
        result = apply_patch._stored_failure_classification("codex", record)
        self.assertEqual(result, "server-capacity")

        zero_exit_record = dict(record, vendor_exit_code=0, exit_code=1, classification="extraction-error")
        with self.assertRaisesRegex(ValueError, "zero-exit unknown run has no reproducible extraction failure"):
            apply_patch._stored_failure_classification("codex", zero_exit_record)

    def test_bootstrap_repair_recognizes_legacy_triad_codex_dispatch_markers(self):
        import shlex
        import tempfile
        sys.path.insert(0, str(ROOT / "bin"))
        import bootstrap_repair

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            analyzer = tmp_path / "triad-repair-analyzer.toml"
            config_path = tmp_path / "config.toml"

            # 1. split_registration with legacy triad-codex-dispatch markers
            legacy_reg = (
                '[model]\nname = "gpt-5"\n\n'
                "# >>> triad-codex-dispatch managed repair analyzer registration >>>\n"
                "# original config existed = true\n"
                f"[agents.{bootstrap_repair.NAME}]\n"
                f"description = {json.dumps(bootstrap_repair.REG_DESCRIPTION)}\n"
                f"config_file = {json.dumps(str(analyzer), ensure_ascii=False)}\n"
                "# <<< triad-codex-dispatch managed repair analyzer registration <<<\n"
            )
            before, after, had_reg, orig_existed = bootstrap_repair.split_registration(
                legacy_reg, config_path, analyzer
            )
            self.assertTrue(had_reg)
            self.assertTrue(orig_existed)
            self.assertEqual(before + after, '[model]\nname = "gpt-5"\n')

            # 2. _legacy_profile_data_is_owned with legacy marker
            profile_lines = [
                "# triad-codex-dispatch managed runtime profile",
                "# Generated by scripts/bootstrap.sh --install.",
                "# Re-run with TRIAD_BOOTSTRAP_INSTALL_CODEX_PROFILE=1 to refresh.",
                "# Explicit external-CLI consent profile: triad dispatch may send relevant",
                "# prompt/repo review material to authenticated claude, agy, and gemini CLIs.",
                "# Permission-profile system (developers.openai.com/codex/permissions).",
                "# Do NOT reintroduce legacy sandbox_mode / [sandbox_workspace_write] in this",
                "# or any loaded config layer: legacy sandbox settings disable",
                "# default_permissions, which would neutralize the triad_leader permission",
                "# profile's scoping.",
                "",
                'approval_policy = "on-request"',
                'approvals_reviewer = "auto_review"',
                'default_permissions = "triad_leader"',
                "",
                "[permissions.triad_leader]",
                'description = "Triad leader session: workspace writes plus triad runtime dirs; network on."',
                'extends = ":workspace"',
                "",
                "[permissions.triad_leader.filesystem]",
                "# --- SEC-3 exec-target write-denies (wrapper .py / launchers / python3 / vendor CLIs) ---",
                '"/opt/triad/bin" = "read"',
                '"/opt/triad/launchers" = "read"',
                '"/usr/bin/python3" = "read"',
                "# --- re-allows: log_dir/debug_dir are nested under bin_dir (more-specific-wins survives that deny); classifier_dir is a separate, non-nested directory allowed independently ---",
                '"/opt/triad/classifier" = "write"',
                '"/opt/triad/bin/_logs" = "write"',
                '"/opt/triad/bin/_debug" = "write"',
                "",
                "[permissions.triad_leader.network]",
                "enabled = true",
            ]
            profile_bytes = ("\n".join(profile_lines) + "\n").encode("utf-8")
            self.assertTrue(bootstrap_repair._legacy_profile_data_is_owned(profile_bytes))

            # 3. _legacy_rules_data_is_owned with legacy marker
            rules_sections = [
                "\n".join(
                    (
                        "# triad-codex-dispatch managed command rules",
                        "# Generated by scripts/bootstrap.sh --install.",
                        "# Re-run with TRIAD_BOOTSTRAP_INSTALL_CODEX_RULES=1 to refresh.",
                        "# These rules prompt on wrapper-specific command prefixes for approval review.",
                        "# They do not allow broad shell entrypoints such as bash -lc or zsh -lc.",
                    )
                )
            ]
            wrappers = (
                ("claude_wrapper.py", "Claude wrapper"),
                ("antigravity_wrapper.py", "Antigravity wrapper"),
                ("gemini_wrapper.py", "Gemini business-tier wrapper"),
            )
            for wrapper, label in wrappers:
                launcher = f"/opt/triad/launchers/{wrapper}"
                repo_wrapper = f"/opt/triad/bin/{wrapper}"
                prompt_path = "/opt/triad/_runs/prompts/triad-prompt.txt"
                justification = (
                    "Require approval review; approve only an owner-authorized triad review "
                    f"through the {label} when the worktree, scope, and named provider match "
                    "the owner's request and provider-visible input excludes credentials, "
                    "tokens, cookies, authentication files, environment dumps, provider "
                    "logs, and unrelated paths. This does not authorize "
                    "commit, push, install, merge, or release."
                )
                lit = bootstrap_repair._canonical_legacy_string_literal
                not_triad_cmd = "python3 -c print('not a triad wrapper')"
                sec_lines = [
                    "prefix_rule(",
                    f"    pattern = [[{lit(launcher)}]],",
                    '    decision = "prompt",',
                    f"    justification = {lit(justification)},",
                    "    match = [",
                    f"        {lit(shlex.join([launcher, '--prompt', 'hi', '--sandbox', 'read-only']))},",
                    f"        {lit(shlex.join([launcher, '--prompt-file', prompt_path, '--sandbox', 'read-only']))},",
                    "    ],",
                    "    not_match = [",
                    f"        {lit(f'{wrapper} --prompt hi --sandbox read-only')},",
                    f"        {lit(shlex.join([repo_wrapper, '--prompt', 'hi', '--sandbox', 'read-only']))},",
                    f"        {lit(f'bash -lc {wrapper} --prompt hi')},",
                    f"        {lit(f'zsh -lc {wrapper} --prompt hi')},",
                    f"        {lit(shlex.join(['python3', repo_wrapper, '--prompt', 'hi', '--sandbox', 'read-only']))},",
                    f"        {lit(shlex.join(['/usr/bin/env', 'python3', repo_wrapper, '--prompt', 'hi', '--sandbox', 'read-only']))},",
                    f"        {lit(not_triad_cmd)},",
                    "    ],",
                    ")",
                ]
                rules_sections.append("\n".join(sec_lines))
            rules_bytes = ("\n\n".join(rules_sections) + "\n").encode("utf-8")
            self.assertTrue(bootstrap_repair._legacy_rules_data_is_owned(rules_bytes))

            # 4. remove_config_fragment with legacy markers + CURRENT_CONFIG_FRAGMENT_TEXT + legacy separator
            frag_file = tmp_path / "frag_config.toml"
            prefix = b'model = "gpt-5"\n'
            legacy_current_block = (
                bootstrap_repair.LEGACY_CONFIG_FRAGMENT_BEGIN
                + b"\n"
                + bootstrap_repair.CURRENT_CONFIG_FRAGMENT_TEXT
                + bootstrap_repair.LEGACY_CONFIG_FRAGMENT_END
                + b"\n"
            )
            frag_file.write_bytes(
                prefix
                + b"\n# triad-codex-dispatch managed inserted separator\n"
                + legacy_current_block
            )
            status = bootstrap_repair.remove_config_fragment(frag_file)
            self.assertEqual(status, "removed")
            self.assertEqual(frag_file.read_bytes(), prefix)


if __name__ == "__main__":
    unittest.main()
