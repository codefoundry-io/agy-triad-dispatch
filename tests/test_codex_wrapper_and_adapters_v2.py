import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

DIGEST = "b" * 64


class TestCodexWrapperAndAdaptersV2(unittest.TestCase):
    def test_codex_wrapper_v2_admits_native_final_and_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "codex"
            verdict = {
                "schema": "triad-leg-verdict.v2",
                "review_id": "rev-codex-01",
                "family": "codex",
                "leg_name": "codex",
                "route": None,
                "attempt": 1,
                "content_digest": DIGEST,
                "verdict": "SAFE TO MERGE",
                "ApproveForMerge": True,
                "inspected_surfaces": ["bin/codex_wrapper.py"],
                "findings": [],
                "strengths": ["Solid"],
                "open_questions": [],
            }
            fake_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "args = sys.argv[1:]\n"
                "out_idx = args.index('-o') + 1\n"
                "out_file = args[out_idx]\n"
                "payload = os.environ['FAKE_CODEX_PAYLOAD']\n"
                "with open(out_file, 'w', encoding='utf-8') as f:\n"
                "    f.write(payload)\n"
                "print('{\"type\":\"turn.completed\"}')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

            import review_prompts_v2
            no_web_clause = review_prompts_v2.review_web_clause(False)
            prompt_file = tmp_path / "prompt.md"
            prompt_file.write_text(f"Header\n\n{no_web_clause}\n", encoding="utf-8")

            log_dir = tmp_path / "logs"
            env = {
                **os.environ,
                "TRIAD_REQUIRE_PINNED_VENDOR": "1",
                "TRIAD_CODEX_BIN": str(fake_bin),
                "TRIAD_DISPATCH_LOG_DIR": str(log_dir),
                "FAKE_CODEX_PAYLOAD": json.dumps(verdict),
            }
            cmd = [
                sys.executable,
                str(ROOT / "bin" / "codex_wrapper.py"),
                "--prompt-file",
                str(prompt_file),
                "--cwd",
                str(tmp_path),
                "--sandbox",
                "read-only",
                "--pydantic",
                "verdict_v2:LegVerdict",
                "--model",
                "gpt-5.6-terra",
                "--effort",
                "xhigh",
                "--timeout",
                "900",
                "--expected-review-id",
                "rev-codex-01",
                "--expected-family",
                "codex",
                "--expected-content-digest",
                DIGEST,
                "--expected-leg-name",
                "codex",
                "--expected-attempt",
                "1",
                "--expected-route",
                "null",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            parsed = json.loads(proc.stdout)
            self.assertEqual(parsed["family"], "codex")

            # Duplicate key in -o file must fail closed with exit 66 (schema-fail)
            body = json.dumps(verdict)
            dup_body = '{"review_id":"wrong",' + body[1:]
            env["FAKE_CODEX_PAYLOAD"] = dup_body
            proc_dup = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(proc_dup.returncode, 66, proc_dup.stderr)

    def test_review_adapters_v2_codex_cli_preflight_and_native_fallback(self):
        import review_adapters_v2

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            folder = tmp_path / "folder"
            folder.mkdir()

            # 1. Native spawn-tool mode
            native_caps = {
                "source": "native-spawn-tool",
                "models": {"gpt-5.6-terra": ["xhigh", "high"]},
                "default_model": "gpt-5.6-terra",
                "default_effort": "xhigh",
            }
            native_res = review_adapters_v2._codex(
                {"model": "gpt-5.6-terra", "reasoning": "xhigh"},
                cwd=tmp_path,
                folder=folder,
                native_capabilities=native_caps,
            )
            self.assertEqual(native_res["transport_route"], "native")
            self.assertEqual(native_res["capability_source"], "native-spawn-tool")
            self.assertIsNone(native_res["binary"])

            # 2. CLI mode when native_capabilities is None or source == 'cli'
            fake_bin = tmp_path / "fake_codex"
            fake_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('codex-cli 0.135.0')\n"
                "elif 'exec' in sys.argv and '--help' in sys.argv:\n"
                "    print('options: --json --output-schema --sandbox --ephemeral --skip-git-repo-check')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

            orig_env = dict(os.environ)
            try:
                os.environ["TRIAD_CODEX_BIN"] = str(fake_bin)
                os.environ["TRIAD_REQUIRE_PINNED_VENDOR"] = "1"

                for idx, caps in enumerate((None, {"source": "cli"})):
                    sub_folder = tmp_path / f"cli_folder_{idx}"
                    sub_folder.mkdir()
                    cli_res = review_adapters_v2._codex(
                        {"model": "gpt-5.6-terra", "reasoning": "xhigh"},
                        cwd=tmp_path,
                        folder=sub_folder,
                        native_capabilities=caps,
                    )
                    self.assertEqual(cli_res["transport_route"], "codex")
                    self.assertEqual(cli_res["capability_source"], "cli-selection")
                    self.assertEqual(cli_res["binary"], str(fake_bin))
                    self.assertEqual(cli_res["cli_version"], "0.135.0")
                    cap_file = Path(cli_res["capability_file"])
                    self.assertTrue(cap_file.exists())
                    data = json.loads(cap_file.read_text(encoding="utf-8"))
                    self.assertEqual(data["transport_route"], "codex")
            finally:
                os.environ.clear()
                os.environ.update(orig_env)

    def test_review_round_v2_invocation_for_codex_cli_and_native(self):
        import review_round_v2

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            folder = tmp_path / "attempt-1"
            folder.mkdir()

            binding = {
                "review_id": "rev-test-01",
                "family": "codex",
                "content_digest": DIGEST,
                "leg_name": "codex",
                "attempt": 1,
                "route": None,
            }

            # 1. Native route
            native_adapter = {
                "family": "codex",
                "transport_route": "native",
                "model": "gpt-5.6-terra",
                "effort": "xhigh",
            }
            inv_native = review_round_v2._invocation(binding, native_adapter, folder, "hello prompt")
            self.assertEqual(inv_native.get("tool"), "collaboration.spawn_agent")
            self.assertEqual(inv_native["arguments"]["model"], "gpt-5.6-terra")

            # 2. CLI route
            cli_adapter = {
                "family": "codex",
                "transport_route": "codex",
                "binary": "/path/to/codex",
                "cwd": str(tmp_path),
                "model": "gpt-5.6-terra",
                "effort": "xhigh",
                "timeout_s": 900,
                "review_web_authorized": False,
            }
            inv_cli = review_round_v2._invocation(binding, cli_adapter, folder, "hello prompt")
            self.assertIn("argv", inv_cli)
            argv = inv_cli["argv"]
            self.assertTrue(any("codex_wrapper.py" in arg for arg in argv))
            self.assertIn("--sandbox", argv)
            self.assertIn("read-only", argv)
            self.assertIn("--pydantic", argv)
            self.assertIn("verdict_v2:LegVerdict", argv)
            self.assertIn("--model=gpt-5.6-terra", argv)
            self.assertIn("--effort", argv)
            self.assertIn("xhigh", argv)
            self.assertIn("--expected-review-id", argv)
            self.assertIn("rev-test-01", argv)
            self.assertIn("--expected-family", argv)
            self.assertIn("codex", argv)
            self.assertIn("--expected-route", argv)
            self.assertIn("null", argv)
            self.assertEqual(inv_cli["env"]["TRIAD_CODEX_BIN"], "/path/to/codex")
            self.assertEqual(inv_cli["env"]["TRIAD_REQUIRE_PINNED_VENDOR"], "1")
            self.assertEqual(inv_cli["env"]["TRIAD_DISPATCH_LOG_DIR"], str(folder / "logs"))

    def test_preflight_defaults_native_capabilities_none(self):
        import inspect
        import review_adapters_v2

        sig_preflight = inspect.signature(review_adapters_v2.preflight)
        self.assertIn("native_capabilities", sig_preflight.parameters)
        self.assertIsNone(sig_preflight.parameters["native_capabilities"].default)

        sig_prepare = inspect.signature(review_adapters_v2.prepare_adapters)
        self.assertIn("native_capabilities", sig_prepare.parameters)
        self.assertIsNone(sig_prepare.parameters["native_capabilities"].default)


if __name__ == "__main__":
    unittest.main()
