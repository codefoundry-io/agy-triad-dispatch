#!/usr/bin/env python3
"""Thin CLI over `_common.apply_classifier_patch` — the owner-run applier.

The fresh native proposal-only child returns a structured patch proposal. The
leader stores only that proposal as a UTF-8 JSON file, and the owner passes the
file plus bootstrap's install-resolved classifier path to this command from
their normal terminal. This is the single deterministic, validated, no-provider
write path to the classifier extension JSON.

Usage:
  python3 apply_patch.py --cli <name> --classifier-file <absolute-path> < proposal.json
  python3 apply_patch.py --cli <name> --classifier-file <absolute-path> \
      --proposal-file <path>

Proposal shape (see apply_classifier_patch):
  {"classification": <enum>, "reason": <str>,
   "vendor_exit_code": <int>            # OR
   "pattern_list": <NAME>, "substring": <str>}

Exit codes:
  0  applied, or stored failure classification verified in --verify-run-log mode
  3  invalid proposal (ValueError) or bad input (arg / JSON parse) — file untouched

With --verify-run-log <absolute-path>, read the current classifier and saved
failure only. No patch is applied and no recorded command or vendor is run.
Verification is classification evidence, not a successful review or admission.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

import _common
from _common import apply_classifier_patch

EXIT_OK = 0
EXIT_INVALID = 3


def _stored_failure_classification(cli: str, record: dict) -> str:
    """Check failure classification only; never execute saved commands."""
    if not isinstance(record, dict) or record.get("cli") != cli:
        raise ValueError("run log must belong to the selected CLI")
    if record.get("classification") not in ("unknown", "extraction-error"):
        raise ValueError("run log is not a repair-eligible failure")
    if type(record.get("exit_code")) is not int or record["exit_code"] != 1:
        raise ValueError("run log has an unsupported wrapper exit")
    raw = record.get("vendor_exit_code")
    if type(raw) is not int or raw < 0:
        raise ValueError("run log has no usable vendor exit")
    if not all(isinstance(record.get(key), str) for key in ("stdout", "stderr")):
        raise ValueError("run log streams must be strings")
    transport = record.get("transport")
    if not isinstance(transport, dict) or transport.get("stdin_delivery") not in (
        "complete", "not-used", "unexposed"
    ):
        raise ValueError("run log has unavailable or failed input delivery")
    diagnostic = record.get("extraction_error")
    if isinstance(diagnostic, str) and diagnostic.startswith((
        "wrapper interrupted (", "stdout collection failed (",
        "stderr collection failed (", "transport setup/wait failed (",
        "process-group cleanup incomplete",
    )):
        raise ValueError("interrupted or transport-failed runs cannot verify classifier repair")
    run = _common.RunResult(0 if raw == 0 else 1, record["stdout"],
                            record["stderr"], 0, vendor_exit_code=raw)
    if cli == "antigravity":
        # Reuse the real route: its failed-exit classifier deliberately omits stdout.
        from antigravity_wrapper import _interpret_run
        return _interpret_run(run, None).classification
    if cli not in ("claude", "gemini"):
        raise ValueError("this host has no replayable wrapper for the selected CLI")
    if raw:
        return _common.classify(cli, run.stderr, run.stdout, run.exit_code,
                                vendor_exit_code=raw)
    if record["classification"] != "extraction-error":
        raise ValueError("zero-exit unknown run has no reproducible extraction failure")
    extract = (_common.extract_claude_answer if cli == "claude"
               else _common.extract_gemini_answer)
    _, error = extract(run.stdout, run.stderr)
    if not isinstance(error, str) or not error:
        raise ValueError("stored streams no longer reproduce an extraction failure")
    classification = _common.classify_extraction(cli, run, error)
    # Shared extraction promotes terminal errors. Claude additionally retries an
    # is_error envelope's capacity result; Gemini does not promote that result.
    if classification in ("cli-subscription-cap", "token-limit", "oauth-env",
                          "config-conflict", "task-blocked",
                          "schema-rejected"):
        return classification
    if cli == "claude" and error.startswith("is_error=true") and classification == "server-capacity":
        return classification
    return "extraction-error"


def _validated_classifier_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"classifier file must be absolute: {path}")
    if ".." in path.parts:
        raise ValueError(f"classifier path must not contain parent traversal: {path}")

    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], start=1):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as error:
            raise ValueError(
                f"could not inspect classifier path: {current}: {error}"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"classifier path must not contain symlinks: {current}")
        if index < len(path.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"classifier ancestor is not a directory: {current}")
        if index == len(path.parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise ValueError(f"classifier file is not regular: {current}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply a validated classifier patch proposal.")
    ap.add_argument("--cli", required=True, help="Target CLI name (codex/gemini/claude/antigravity).")
    ap.add_argument(
        "--classifier-file",
        required=True,
        help="Install-resolved absolute classifier extension path.",
    )
    ap.add_argument(
        "--proposal-file",
        default=None,
        help="Read the proposal JSON from this file instead of stdin.",
    )
    ap.add_argument("--verify-run-log", help="Verify a stored failure against the current extension without applying or calling a vendor.")
    args = ap.parse_args()

    try:
        classifier_path = _validated_classifier_path(args.classifier_file)
    except ValueError as e:
        print(f"[apply_patch] rejected: {e}", file=sys.stderr)
        return EXIT_INVALID

    try:
        if args.proposal_file:
            with open(args.proposal_file, "r", encoding="utf-8") as f:
                raw = f.read()
        else:
            raw = sys.stdin.read()
    except OSError as e:
        print(f"[apply_patch] cannot read proposal: {e}", file=sys.stderr)
        return EXIT_INVALID

    try:
        proposal = json.loads(raw)
    except ValueError as e:
        print(f"[apply_patch] invalid proposal JSON: {e}", file=sys.stderr)
        return EXIT_INVALID

    if not isinstance(proposal, dict):
        print("[apply_patch] proposal must be a JSON object", file=sys.stderr)
        return EXIT_INVALID

    if args.verify_run_log is not None:
        previous = os.environ.get("TRIAD_CLASSIFIER_EXTENSION")
        try:
            expected = proposal.get("classification")
            if not isinstance(expected, str) or expected not in _common.REPAIR_CLASSIFICATION_TOKENS or expected == "oauth-env":
                raise ValueError("proposal has no eligible repair classification")
            path = Path(args.verify_run_log)
            if not path.is_absolute():
                raise ValueError("run-log path must be absolute")
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and expected == record.get("classification"):
                raise ValueError("unchanged classification does not verify a learned repair")
            os.environ["TRIAD_CLASSIFIER_EXTENSION"] = str(classifier_path)
            actual = _stored_failure_classification(args.cli, record)
            if actual != expected:
                raise ValueError(f"stored failure remains {actual}; expected {expected}")
        except (OSError, ValueError) as error:
            print(f"[apply_patch] not verified: {error}", file=sys.stderr)
            return EXIT_INVALID
        finally:
            if previous is None:
                os.environ.pop("TRIAD_CLASSIFIER_EXTENSION", None)
            else:
                os.environ["TRIAD_CLASSIFIER_EXTENSION"] = previous
        print(f"[apply_patch] verified stored classification: {actual}")
        return EXIT_OK

    try:
        result = apply_classifier_patch(
            args.cli,
            proposal,
            extension_path=classifier_path,
        )
    except ValueError as e:
        print(f"[apply_patch] rejected: {e}", file=sys.stderr)
        return EXIT_INVALID

    print(result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
