import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


class TestV2LifecycleE2E(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._orig_env = dict(os.environ)
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        os.environ["XDG_CONFIG_HOME"] = "/tmp"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        super().tearDown()

    def test_neutral_directory_installed_copy_verifies_library_and_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp).resolve() / "installed-plugin"
            shutil.copytree(
                ROOT,
                installed,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "_runs", "_logs", "_debug", ".superpowers"),
            )
            cmd = [
                sys.executable,
                "-c",
                (
                    f"import sys; sys.path.insert(0, {str(installed / 'bin')!r}); "
                    "import library_adoption, validate_v2; "
                    "meta = library_adoption.verify_library(); "
                    "contracts = validate_v2.load_contracts(); "
                    "assert meta['version'] == '0.1.0.dev4'; "
                    "assert 'leg-verdict.schema.json' in contracts; "
                    "assert 'review-legs.schema.json' in contracts; "
                    "assert 'receipt-fields.json' in contracts"
                ),
            ]
            proc = subprocess.run(cmd, cwd="/tmp", capture_output=True, text=True, check=True)
            self.assertEqual(proc.returncode, 0)

    def test_missing_or_mutated_library_file_fails_closed(self):
        import library_adoption

        with tempfile.TemporaryDirectory() as tmp:
            copied_root = Path(tmp).resolve() / "pkg"
            (copied_root / "contracts").mkdir(parents=True)
            (copied_root / "vendor").mkdir(parents=True)
            (copied_root / "bin").mkdir(parents=True)

            shutil.copy2(ROOT / "contracts/library-adoption.json", copied_root / "contracts/library-adoption.json")
            shutil.copy2(ROOT / "vendor/triad-dispatch-lib.LICENSE", copied_root / "vendor/triad-dispatch-lib.LICENSE")
            shutil.copytree(
                ROOT / "bin/triad_dispatch_lib",
                copied_root / "bin/triad_dispatch_lib",
                ignore=shutil.ignore_patterns("__pycache__"),
            )

            # Initially valid
            meta = library_adoption.verify_library(root=copied_root)
            self.assertEqual(meta["version"], "0.1.0.dev4")

            target_file = copied_root / "bin/triad_dispatch_lib/verdict.py"
            original_bytes = target_file.read_bytes()

            # 1. Missing file fails closed with missing diagnostics
            target_file.unlink()
            with self.assertRaises(ValueError) as cm_missing:
                library_adoption.verify_library(root=copied_root)
            self.assertIn("missing", str(cm_missing.exception))

            # Restore file
            target_file.write_bytes(original_bytes)
            self.assertEqual(library_adoption.verify_library(root=copied_root)["version"], "0.1.0.dev4")

            # 2. Unexpected file fails closed with unexpected diagnostics
            unexpected_file = copied_root / "bin/triad_dispatch_lib/unexpected_module.py"
            unexpected_file.write_text("# unexpected\n", encoding="utf-8")
            with self.assertRaises(ValueError) as cm_unexpected:
                library_adoption.verify_library(root=copied_root)
            self.assertIn("unexpected", str(cm_unexpected.exception))

            # Remove unexpected file
            unexpected_file.unlink()
            self.assertEqual(library_adoption.verify_library(root=copied_root)["version"], "0.1.0.dev4")

            # 3. Mutated file fails closed with digest mismatch
            target_file.write_bytes(original_bytes + b"\n# mutated\n")
            with self.assertRaises(ValueError) as cm_mutated:
                library_adoption.verify_library(root=copied_root)
            self.assertIn("generated hash mismatch", str(cm_mutated.exception))

    def test_c23_negative_without_allowed_read_or_with_forbidden_tool_is_rejected(self):
        import antigravity_wrapper
        import _common
        import verdict_v2

        review_id = "rev-c23-test-01"
        digest = "c" * 64
        binding = {
            "review_id": review_id,
            "family": "google",
            "content_digest": digest,
            "leg_name": "google",
            "attempt": 1,
            "route": "agy",
        }
        bound_agy = verdict_v2.bound_verdict(binding)

        valid_verdict = {
            "schema_version": 2,
            "review_id": review_id,
            "family": "google",
            "content_digest": digest,
            "leg_name": "google",
            "attempt": 1,
            "route": "agy",
            "verdict": "SAFE TO MERGE",
            "criteria_checked": ["All good"],
            "findings": [],
            "affected_surfaces_inspected": ["main.py"],
            "open_questions": [],
        }

        # Sub-case A: Positive recovery - errored finish, then allowed read (view_file DONE), then successful finish
        positive_events = [
            {"event": "init", "init": {"model": "gemini-3.1-pro-high"}},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "finish",
                    "state": "ERROR",
                    "step_index": 0,
                    "tool_info": {"error": {"message": "first attempt rejected"}},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "state": "DONE",
                    "step_index": 1,
                    "tool_info": {"parameters": {"AbsolutePath": "/tmp/main.py"}},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "finish",
                    "tool_name": "finish",
                    "state": "DONE",
                    "step_index": 2,
                    "tool_info": {},
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "structured_output": valid_verdict,
                },
            },
        ]
        self.assertTrue(antigravity_wrapper._agy_finish_resubmitted(positive_events, web=False))

        stdout_positive = "\n".join(json.dumps(ev) for ev in positive_events) + "\n"
        run_pos = _common.RunResult(exit_code=0, stdout=stdout_positive, stderr="", elapsed_s=1.0, vendor_exit_code=0)
        run_pos._effective_cwd = "/tmp"
        run_pos._stdin_delivery_failed = False
        run_pos._output_transport_failed = False
        run_pos = antigravity_wrapper._interpret_run(
            run_pos,
            bound_agy,
            "gemini-3.1-pro-high",
            expected_review_id=review_id,
            expected_family="google",
            expected_content_digest=digest,
            plan_mode=True,
            web=False,
        )
        self.assertEqual(run_pos.exit_code, 0)
        self.assertEqual(run_pos.classification, "ok")
        self.assertIsNotNone(run_pos.validated)
        self.assertEqual(run_pos.validated["route"], "agy")

        # Sub-case B: Negative case 1 - NO allowed read between errored finish and successful finish
        neg_no_read_events = [
            {"event": "init", "init": {"model": "gemini-3.1-pro-high"}},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "finish",
                    "state": "ERROR",
                    "step_index": 0,
                    "tool_info": {"error": {"message": "first attempt rejected"}},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "finish",
                    "tool_name": "finish",
                    "state": "DONE",
                    "step_index": 1,
                    "tool_info": {},
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "structured_output": valid_verdict,
                },
            },
        ]
        self.assertFalse(antigravity_wrapper._agy_finish_resubmitted(neg_no_read_events, web=False))

        stdout_no_read = "\n".join(json.dumps(ev) for ev in neg_no_read_events) + "\n"
        run_no_read = _common.RunResult(exit_code=0, stdout=stdout_no_read, stderr="", elapsed_s=1.0, vendor_exit_code=0)
        run_no_read._effective_cwd = "/tmp"
        run_no_read._stdin_delivery_failed = False
        run_no_read._output_transport_failed = False
        run_no_read = antigravity_wrapper._interpret_run(
            run_no_read,
            bound_agy,
            "gemini-3.1-pro-high",
            expected_review_id=review_id,
            expected_family="google",
            expected_content_digest=digest,
            plan_mode=True,
            web=False,
        )
        self.assertEqual(run_no_read.exit_code, _common.EXIT_TERMINAL)
        self.assertEqual(run_no_read.classification, "vendor-error")
        self.assertIsNone(run_no_read.validated)

        # Sub-case C: Negative case 2 - Forbidden tool (run_command) executed (state == DONE)
        neg_forbidden_events = [
            {"event": "init", "init": {"model": "gemini-3.1-pro-high"}},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "finish",
                    "state": "ERROR",
                    "step_index": 0,
                    "tool_info": {"error": {"message": "first attempt rejected"}},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "state": "DONE",
                    "step_index": 1,
                    "tool_info": {},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "state": "DONE",
                    "step_index": 2,
                    "tool_info": {},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "finish",
                    "tool_name": "finish",
                    "state": "DONE",
                    "step_index": 3,
                    "tool_info": {},
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "structured_output": valid_verdict,
                },
            },
        ]
        self.assertFalse(antigravity_wrapper._agy_finish_resubmitted(neg_forbidden_events, web=False))

        stdout_forbidden = "\n".join(json.dumps(ev) for ev in neg_forbidden_events) + "\n"
        run_forbidden = _common.RunResult(exit_code=0, stdout=stdout_forbidden, stderr="", elapsed_s=1.0, vendor_exit_code=0)
        run_forbidden._effective_cwd = "/tmp"
        run_forbidden._stdin_delivery_failed = False
        run_forbidden._output_transport_failed = False
        run_forbidden = antigravity_wrapper._interpret_run(
            run_forbidden,
            bound_agy,
            "gemini-3.1-pro-high",
            expected_review_id=review_id,
            expected_family="google",
            expected_content_digest=digest,
            plan_mode=True,
            web=False,
        )
        self.assertEqual(run_forbidden.exit_code, _common.EXIT_TERMINAL)
        self.assertEqual(run_forbidden.classification, "vendor-error")
        self.assertIsNone(run_forbidden.validated)

        # Sub-case D: Negative case 3 - Forbidden tool denied by hook, but without allowed read
        neg_denied_no_read_events = [
            {"event": "init", "init": {"model": "gemini-3.1-pro-high"}},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "finish",
                    "state": "ERROR",
                    "step_index": 0,
                    "tool_info": {"error": {"message": "first attempt rejected"}},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "state": "ERROR",
                    "step_index": 1,
                    "tool_info": {"error": {"message": "tool call denied by pre-tool hook"}},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "finish",
                    "tool_name": "finish",
                    "state": "DONE",
                    "step_index": 2,
                    "tool_info": {},
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "structured_output": valid_verdict,
                },
            },
        ]
        self.assertFalse(antigravity_wrapper._agy_finish_resubmitted(neg_denied_no_read_events, web=False))

    def test_full_three_family_v2_round_agrees_end_to_end(self):
        import review_round
        import review_round_v2

        review_id = f"rev-e2e-{uuid.uuid4().hex[:8]}"
        temp_base = review_round._temp_base()
        root = temp_base / f"triad-review-{review_id}"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            worktree = tmp_path / "worktree"
            worktree.mkdir()

            # Initialize a real git repo for worktree
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "E2E Tester"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "core.excludesFile", "/dev/null"], cwd=worktree, check=True)
            (worktree / "main.py").write_text("def run():\n    return 42\n", encoding="utf-8")
            subprocess.run(["git", "add", "main.py"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=E2E Tester", "-c", "user.email=tester@example.com", "commit", "-m", "initial commit"],
                cwd=worktree,
                check=True,
                capture_output=True,
            )

            # Member list for workspace preparation
            member_list_path = tmp_path / "members.json"
            member_list_path.write_bytes(json.dumps(["main.py"], sort_keys=True, separators=(",", ":")).encode("utf-8"))

            # Create mock binaries for claude, codex, and agy
            fake_bin_dir = tmp_path / "bin"
            fake_bin_dir.mkdir()

            fake_claude = fake_bin_dir / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('claude-code 2.1.205')\n"
                "elif '--help' in sys.argv:\n"
                "    print('flags: --print --model --effort --no-session-persistence --permission-mode --output-format --allowedTools')\n"
                "elif any(arg.startswith('/model') for arg in sys.argv):\n"
                "    print('Set model to Opus 5.5 (effort: xhigh) for this session only')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)

            fake_codex = fake_bin_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('codex-cli 0.135.0')\n"
                "elif 'exec' in sys.argv and '--help' in sys.argv:\n"
                "    print('options: --json --output-schema --sandbox --ephemeral --skip-git-repo-check')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

            fake_agy = fake_bin_dir / "agy"
            fake_agy.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('antigravity 1.2.1')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IXUSR)

            # Prepare the managed review workspace
            prep = review_round.prepare_review_workspace(
                review_id,
                worktree,
                member_list_path,
                required_members_json=json.dumps(["main.py"]),
            )
            prepared = Path(prep.shared_dir)

            try:
                # Add TASK.md and REVIEW.diff to prepared packet
                (prepared / "TASK.md").write_text("Review main.py for correctness.\n", encoding="utf-8")
                (prepared / "REVIEW.diff").write_text(
                    "--- a/main.py\n+++ b/main.py\n@@ -1,2 +1,2 @@\n def run():\n-    return 42\n+    return 43\n",
                    encoding="utf-8",
                )
                # Generate packet manifest (SOURCE_SHA256SUMS)
                review_round.create_source_manifest(prepared)

                # Set up environment with pinned reviewer binaries
                saved_env = dict(os.environ)
                os.environ["TRIAD_REQUIRE_PINNED_VENDOR"] = "1"
                os.environ["TRIAD_CLAUDE_BIN"] = str(fake_bin_dir / "claude")
                os.environ["TRIAD_CODEX_BIN"] = str(fake_bin_dir / "codex")
                os.environ["TRIAD_AGY_BIN"] = str(fake_bin_dir / "agy")

                try:
                    request = {
                        "review_id": review_id,
                        "project_root": str(worktree),
                        "prepared_dir": str(prepared),
                        "worktree": str(worktree),
                        "mode": "prepared-directory",
                        "objective": "Complete review round verification",
                        "criteria": ["Functionality", "Security"],
                        "approved_boundary": ["main.py"],
                        "authentication_class": "personal-google",
                    }

                    # Step 1: create_basis
                    basis = review_round_v2.create_basis(request, root=root)
                    basis_file = Path(basis["basis_file"])
                    self.assertTrue(basis_file.is_file())
                    self.assertEqual(basis["enabled"], ["claude", "codex", "google"])

                    # Step 2: allocate_attempt and record_cli_attempt for each of the 3 legs
                    for name in ("claude", "codex", "google"):
                        alloc = review_round_v2.allocate_attempt(basis_file, name)
                        binding = alloc["binding"]
                        adapter = alloc["adapter"]
                        cli = "antigravity" if adapter["transport_route"] == "agy" else adapter["transport_route"]
                        folder = Path(alloc["folder"])

                        verdict_payload = {
                            "schema_version": 2,
                            "review_id": binding["review_id"],
                            "family": binding["family"],
                            "content_digest": binding["content_digest"],
                            "leg_name": binding["leg_name"],
                            "attempt": binding["attempt"],
                            "route": binding["route"],
                            "verdict": "SAFE TO MERGE",
                            "criteria_checked": ["Clean structure and robust logic"],
                            "findings": [],
                            "affected_surfaces_inspected": ["main.py"],
                            "open_questions": [],
                        }
                        verdict_bytes = (
                            json.dumps(verdict_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                            + b"\n"
                        )
                        Path(alloc["invocation"]["stdout_file"]).write_bytes(verdict_bytes)
                        Path(alloc["invocation"]["stderr_file"]).write_bytes(b"")

                        runs_dir = folder / "logs" / cli / "runs"
                        runs_dir.mkdir(parents=True, exist_ok=True)
                        run_log = runs_dir / "test-run.json"
                        receipt_data = {
                            "cli": cli,
                            "wrapper_cmd": alloc["invocation"]["argv"][1:],
                            "exit_code": 0,
                            "vendor_exit_code": 0,
                            "classification": "ok",
                            "stdout": verdict_bytes.decode("utf-8"),
                            "stderr": "",
                            "validated": verdict_payload,
                            "review_binding": binding,
                            "transport": {
                                "schema_version": 2,
                                "route": adapter["transport_route"],
                                "binary": adapter["binary"],
                                "cli_version": adapter["cli_version"],
                                "attempt": binding["attempt"],
                                "stdin_delivery": "not-used" if adapter["transport_route"] in ("agy", "gemini") else "complete",
                            },
                        }
                        run_log.write_bytes(
                            json.dumps(receipt_data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                            + b"\n"
                        )

                        recorded = review_round_v2.record_cli_attempt(basis_file, name, run_log=run_log)
                        self.assertEqual(recorded["state"], "COMPLETE")
                        self.assertEqual(recorded["verdict"]["verdict"], "SAFE TO MERGE")

                    # Step 3: collect
                    collected = review_round_v2.collect(basis_file)
                    self.assertEqual(collected["status"], "AGREED")
                    self.assertEqual(collected["content_digest"], basis["content_digest"])
                    self.assertEqual(collected["families"], ["claude", "codex", "google"])
                    self.assertEqual(collected["missing"], [])
                    self.assertEqual(collected["selection_deviations"], [])

                    # Receipt field assertions
                    for leg_name, expected_family, expected_route in (
                        ("claude", "claude", None),
                        ("codex", "codex", None),
                        ("google", "google", "agy"),
                    ):
                        leg_info = collected["legs"][leg_name]
                        self.assertEqual(leg_info["state"], "COMPLETE")
                        self.assertEqual(leg_info["attempt"], 1)
                        v = leg_info["verdict"]
                        self.assertEqual(v["review_id"], review_id)
                        self.assertEqual(v["family"], expected_family)
                        self.assertEqual(v["leg_name"], leg_name)
                        self.assertEqual(v["route"], expected_route)
                        self.assertEqual(v["content_digest"], basis["content_digest"])
                        self.assertEqual(v["verdict"], "SAFE TO MERGE")
                        self.assertEqual(v["findings"], [])
                        self.assertEqual(v["open_questions"], [])

                finally:
                    os.environ.clear()
                    os.environ.update(saved_env)

            finally:
                if root.exists():
                    review_round.export_review_workspace(review_id, root, tmp_path / "evidence")
                review_round.cleanup_review_workspace(review_id, root)

    def test_failed_attempt_retry_and_cancellation_cleanup(self):
        import review_round
        import review_round_v2

        review_id = f"rev-fail-{uuid.uuid4().hex[:8]}"
        temp_base = review_round._temp_base()
        root = temp_base / f"triad-review-{review_id}"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            worktree = tmp_path / "worktree"
            worktree.mkdir()

            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "E2E Tester"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "core.excludesFile", "/dev/null"], cwd=worktree, check=True)
            (worktree / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "main.py"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=E2E Tester", "-c", "user.email=tester@example.com", "commit", "-m", "init"],
                cwd=worktree,
                check=True,
                capture_output=True,
            )

            member_list_path = tmp_path / "members.json"
            member_list_path.write_bytes(json.dumps(["main.py"], sort_keys=True, separators=(",", ":")).encode("utf-8"))

            fake_bin_dir = tmp_path / "bin"
            fake_bin_dir.mkdir()

            fake_claude = fake_bin_dir / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "if '--version' in sys.argv: print('claude-code 2.1.205')\n"
                "elif '--help' in sys.argv: print('flags: --print --model --effort --no-session-persistence --permission-mode --output-format --allowedTools')\n"
                "elif any(arg.startswith('/model') for arg in sys.argv): print('Set model to Opus 5.5 (effort: xhigh) for this session only')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)

            fake_codex = fake_bin_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "if '--version' in sys.argv: print('codex-cli 0.135.0')\n"
                "elif 'exec' in sys.argv and '--help' in sys.argv: print('options: --json --output-schema --sandbox --ephemeral --skip-git-repo-check')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

            fake_agy = fake_bin_dir / "agy"
            fake_agy.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "if '--version' in sys.argv: print('antigravity 1.2.1')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IXUSR)

            prep = review_round.prepare_review_workspace(
                review_id,
                worktree,
                member_list_path,
                required_members_json=json.dumps(["main.py"]),
            )
            prepared = Path(prep.shared_dir)

            try:
                (prepared / "TASK.md").write_text("Test retry.\n", encoding="utf-8")
                (prepared / "REVIEW.diff").write_text("--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-1\n+2\n", encoding="utf-8")
                review_round.create_source_manifest(prepared)

                saved_env = dict(os.environ)
                os.environ["TRIAD_REQUIRE_PINNED_VENDOR"] = "1"
                os.environ["TRIAD_CLAUDE_BIN"] = str(fake_bin_dir / "claude")
                os.environ["TRIAD_CODEX_BIN"] = str(fake_bin_dir / "codex")
                os.environ["TRIAD_AGY_BIN"] = str(fake_bin_dir / "agy")

                try:
                    request = {
                        "review_id": review_id,
                        "project_root": str(worktree),
                        "prepared_dir": str(prepared),
                        "worktree": str(worktree),
                        "mode": "prepared-directory",
                        "objective": "Test retry and failure accounting",
                        "criteria": ["Test"],
                        "approved_boundary": ["main.py"],
                        "authentication_class": "personal-google",
                    }
                    basis = review_round_v2.create_basis(request, root=root)
                    basis_file = Path(basis["basis_file"])

                    # Allocate google attempt 1
                    alloc = review_round_v2.allocate_attempt(basis_file, "google")
                    folder = Path(alloc["folder"])

                    # Record a host start failure
                    fail_receipt_path = folder / "start_failure.json"
                    fail_receipt = {
                        "review_binding": alloc["binding"],
                        "provider_started": False,
                        "exit_code": 1,
                        "stderr": "spawn error: binary failed to execute",
                    }
                    fail_receipt_path.write_bytes(
                        json.dumps(fail_receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                    )

                    recorded = review_round_v2.record_start_failure(basis_file, "google", host_receipt=fail_receipt_path)
                    self.assertEqual(recorded["state"], "FAILED_TO_RUN")

                    # Verify collect reflects incomplete status
                    col = review_round_v2.collect(basis_file)
                    self.assertEqual(col["status"], "INCOMPLETE")
                    self.assertIn("google", col["missing"])

                    # Retry without diagnosis fails closed
                    with self.assertRaises(ValueError):
                        review_round_v2.allocate_attempt(basis_file, "google", diagnosis=None)

                    # Same-basis retry with diagnosis succeeds and allocates attempt-2
                    alloc2 = review_round_v2.allocate_attempt(
                        basis_file, "google", diagnosis="repaired binary permissions"
                    )
                    self.assertEqual(alloc2["binding"]["attempt"], 2)
                    self.assertTrue(Path(alloc2["folder"]).name.endswith("attempt-2"))

                finally:
                    os.environ.clear()
                    os.environ.update(saved_env)

            finally:
                if root.exists():
                    review_round.export_review_workspace(review_id, root, tmp_path / "evidence")
                res = review_round.cleanup_review_workspace(review_id, root)
                self.assertTrue(res.removed)


if __name__ == "__main__":
    unittest.main()
