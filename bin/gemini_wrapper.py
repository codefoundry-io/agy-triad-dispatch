#!/usr/bin/env python3
"""Single-shot Gemini CLI transport wrapper.

Forwards ordinary prompts with provider-native controls. The exact formally
bound ``LegVerdict`` route is reserved for preselected Gemini Enterprise OAuth:
it consumes one selector receipt, explicitly requests CLI Auto and native Plan
Mode, uses a mode-independent packaged read/search-only policy as its enforcement
boundary, scrubs competing selectors, makes one provider call, records literal
``unexposed`` mode/model identity, and performs local review-binding admission.

Stdout is Gemini's final response text (or, with ``--pydantic``, the validated
JSON object). Stderr is wrapper logging and Gemini's warning noise. Audit
results are written to ``_logs/gemini/audit.jsonl`` (gitignored).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import _common
import review_round
from _common import (
    validate_wrapper_cwd,
    load_prompt_text,
    EXIT_ARG_ERROR,
    persist_result_artifacts,
    load_pydantic_class,
    log,
    require_binary,
    run_cli_with_retry,
)


FORMAL_VERDICT_SPECS = _common.PACKAGED_VERDICT_SPECS
FORMAL_GEMINI_TIMEOUT = 600
# Compatibility subset; the shared builder owns the complete effective set.
FORMAL_GEMINI_REMOVED_ENV = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GEMINI_BASE_URL",
    "GOOGLE_VERTEX_BASE_URL",
    "CLOUD_SHELL",
    "GEMINI_CLI_USE_COMPUTE_ADC",
)
FORMAL_READ_TOOLS = {
    "read_file",
    "read_many_files",
    "list_directory",
    "glob",
    "grep_search",
    "get_internal_docs",
}
FORMAL_DENIED_TOOLS = {
    "write_file",
    "replace",
    "run_shell_command",
    "enter_plan_mode",
    "exit_plan_mode",
    "google_web_search",
    "web_fetch",
}


def _formal_policy_path() -> Path:
    return Path(__file__).resolve().parent / "policies" / "gemini-formal-readonly.toml"


def _rule_tools(rule: dict[str, object]) -> set[str]:
    value = rule.get("toolName")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def _validate_formal_policy(path: Path, *, web: bool = False) -> str:
    try:
        policy_bytes = path.read_bytes()
        payload = tomllib.loads(policy_bytes.decode("utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(
            f"formal Gemini policy is unavailable or invalid: {error}"
        ) from error
    rules = payload.get("rule")
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise ValueError("formal Gemini policy has no valid rules")
    actual_rules: list[tuple[str, int, frozenset[str]]] = []
    for rule in rules:
        tools = _rule_tools(rule)
        raw_tools = rule.get("toolName")
        decision = rule.get("decision")
        priority = rule.get("priority")
        if (
            set(rule) - {"toolName", "decision", "priority", "denyMessage"}
            or not tools
            or (isinstance(raw_tools, list) and len(raw_tools) != len(tools))
            or not isinstance(decision, str)
            or not isinstance(priority, int)
        ):
            raise ValueError(
                "formal Gemini policy is not the exact fail-closed rule set"
            )
        actual_rules.append((decision, priority, frozenset(tools)))
    web_tools = {"google_web_search", "web_fetch"} if web else set()
    expected_rules = {
        ("allow", 999, frozenset(set(FORMAL_READ_TOOLS) | web_tools)),
        ("deny", 999, frozenset(set(FORMAL_DENIED_TOOLS) - web_tools)),
        ("deny", 998, frozenset({"*"})),
    }
    if len(actual_rules) != len(expected_rules) or set(actual_rules) != expected_rules:
        raise ValueError("formal Gemini policy is not the exact fail-closed rule set")
    digest = hashlib.sha256(policy_bytes).hexdigest()
    from validate_v2 import _json
    try:
        manifest = _json(review_round._canonical_regular_file_bytes(
            path.with_name("web-source-manifest.json" if web else "source-manifest.json"), "policy provenance"))
    except (OSError, ValueError):
        raise ValueError("formal Gemini policy digest provenance is unavailable") from None
    if web:
        if digest != "361b653b7f431f12542bbda7da7ee4af320ca70cd6ac0ba249e6c1b52259480b" or manifest != {
            "source_repository": "https://github.com/codefoundry-io/triad-dispatch-spec",
            "source_commit": "7f527ef1777336b93ca626744aedcd0c7d90aff9", "status": "candidate",
            "source_path": "contracts/gemini-readonly-web-b.toml",
            "sha256": {"gemini-formal-web.toml": digest},
        }:
            raise ValueError("formal Gemini web policy digest does not match the selected candidate")
        return digest
    if digest != "01a267f591518964e30f94ffc45d496aae594435678380179b679d1aa8de189b" or manifest != {
        "source_repository": "https://github.com/codefoundry-io/triad-dispatch-spec",
        "source_commit": "6f0f2746f0bd74e16cf6df7c9ee5e0750d42d7d5", "status": "candidate",
        "source_path": "contracts/gemini-readonly-b.toml",
        "sha256": {"gemini-formal-readonly.toml": digest},
    }:
        raise ValueError("formal Gemini policy digest does not match the selected candidate")
    return digest


def _supports_formal_help_contract(help_text: str) -> bool:
    model = re.search(
        r"(?m)^\s*(?:-m,\s*)?--model\b[^\n]*\bModel\b[^\n]*\[string\]\s*$",
        help_text,
    )
    approval_mode = re.search(
        r"(?m)^\s*--approval-mode\b[^\n]*Set the approval mode[^\n]*"
        r'\[choices:[^\n]*"plan"[^\n]*\]\s*$',
        help_text,
    )
    policy = re.search(
        r"(?m)^\s*--policy\b[^\n]*Additional policy files or directories to load"
        r"[^\n]*\[array\]\s*$",
        help_text,
    )
    return model is not None and approval_mode is not None and policy is not None


def _probe_version(gemini_bin: str, cwd: str | None, timeout: int) -> str:
    result = subprocess.run(
        [gemini_bin, "--version"], cwd=cwd,
        env=_common.scrubbed_child_env(cli="gemini", remove=FORMAL_GEMINI_REMOVED_ENV),
        text=True, capture_output=True, check=False, timeout=min(timeout, 15),
    )
    if result.returncode != 0:
        raise ValueError("Gemini --version probe failed")
    return review_round.validate_formal_gemini_version(result.stdout.strip())


def _run_preflight(
    gemini_bin: str,
    cwd: str | None,
    timeout: int,
    selector_receipt: review_round.GoogleSelectorReceipt,
    *,
    v2_fields: dict | None = None,
    web: bool = False,
) -> int:
    policy = _formal_policy_path().with_name("gemini-formal-web.toml") if web else _formal_policy_path()
    try:
        policy_sha256 = _validate_formal_policy(policy, **({"web": True} if web else {}))
        gemini_version = _probe_version(gemini_bin, cwd, timeout)
        completed = subprocess.run(
            [gemini_bin, "--help"],
            cwd=cwd,
            env=_common.scrubbed_child_env(cli="gemini", remove=FORMAL_GEMINI_REMOVED_ENV),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        log(f"Gemini formal preflight failed: {error}")
        return EXIT_ARG_ERROR
    help_text = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or not _supports_formal_help_contract(help_text):
        log(
            "Gemini formal preflight did not prove Plan Mode, policy, and Auto model support"
        )
        return EXIT_ARG_ERROR
    if (v2_fields and v2_fields.get("review_packet_dir") is not None
            and re.search(r"(?m)^\s*--include-directories\b[^\n]*\[array\]\s*$", help_text) is None):
        log("Gemini formal preflight requires --include-directories for the bound packet")
        return EXIT_ARG_ERROR
    sys.stdout.write(
        json.dumps(
            {
                "effective_approval_mode": "unexposed",
                "gemini_version": gemini_version,
                "executable": gemini_bin,
                "google_selector_receipt_sha256": selector_receipt.receipt_sha256,
                "model": "auto",
                "policy": str(policy),
                "policy_sha256": policy_sha256,
                "provider_started": False,
                "read_only_enforcement": "packaged-mode-independent-policy",
                "requested_approval_mode": "plan",
                "review_id": selector_receipt.review_id,
                "route": "gemini",
                **({"review_web_authorized": True} if web else {}),
                **(v2_fields or {}),
            },
            ensure_ascii=v2_fields is not None,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Gemini CLI single-shot wrapper", allow_abbrev=False
    )
    prompt_group = p.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="User prompt")
    prompt_group.add_argument(
        "--prompt-file",
        help="Read the user prompt from a UTF-8 file (>=50K-char prompts: pass "
        "a file, not inline argv — L12; containment applies under "
        "TRIAD_WRAPPER_ALLOWED_ROOTS)",
    )
    p.add_argument("--cwd", default=None, help="Process working directory")
    p.add_argument("--timeout", type=int, default=600, help="Timeout in seconds")
    p.add_argument("--add-dir", action="append", default=[], help="Authorized additional raw-call input directory")
    p.add_argument("--review-packet-dir", help="Exact bound shared packet directory (formal v2 only)")
    p.add_argument("--web", action="store_true", help="Explicitly authorize web; REVIEW requires matching bound metadata and preflight")
    p.add_argument(
        "--model",
        default=None,
        help="Pin a specific model (free-form). Default = CLI Auto router.",
    )
    p.add_argument(
        "--pydantic",
        default=None,
        help="pydantic class spec (module.path:ClassName) for schema enforcement",
    )
    p.add_argument("--expected-review-id", default=None)
    p.add_argument("--expected-family", choices=("google",), default=None)
    p.add_argument("--expected-content-digest", default=None)
    p.add_argument("--expected-leg-name", default=None)
    p.add_argument("--expected-attempt", type=int, default=None)
    p.add_argument("--expected-route", choices=("null", "agy", "gemini"), default=None)
    p.add_argument("--google-selector-receipt", type=Path, default=None)
    p.add_argument("--google-preflight-receipt", type=Path, default=None)
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument(
        "--repair-mode",
        action="store_true",
        help="Compatibility diagnostic: one provider attempt with retries disabled; "
        "the fresh native proposal-only repair child does not invoke providers",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Append a human-readable markdown row to "
        "_debug/<UTC-YYYY-MM-DD>/gemini.md (per-call summary)",
    )
    args = p.parse_args()
    if args.model is not None and not isinstance(args.model, str):
        log("model argument was not preserved as a string; refusing dispatch")
        return EXIT_ARG_ERROR

    process_cwd = None
    try:
        process_cwd = Path.cwd()
        _prompt_text = load_prompt_text(args.prompt, args.prompt_file, process_cwd=process_cwd)
    except Exception as e:
        log(_common.input_path_error("prompt load", args.prompt_file, process_cwd, e))
        return EXIT_ARG_ERROR
    args.prompt = _prompt_text  # downstream code keeps using args.prompt

    try:
        args.cwd = validate_wrapper_cwd(args.cwd, process_cwd=process_cwd)
    except Exception as e:
        log(_common.input_path_error("--cwd", args.cwd, process_cwd, e))
        return EXIT_ARG_ERROR

    if not args.prompt.strip():
        log("empty prompt")
        return EXIT_ARG_ERROR

    try:
        args.add_dir = _common.validate_extra_directories(args.add_dir, process_cwd=process_cwd)
        if any("," in directory for directory in args.add_dir):
            raise ValueError("Gemini --include-directories cannot represent a comma in one path")
    except Exception as e:
        log(_common.input_path_error("--add-dir", None, process_cwd, e))
        return EXIT_ARG_ERROR
    if args.add_dir:
        if (args.preflight_only or args.pydantic in (FORMAL_VERDICT_SPECS | _common.PACKAGED_V2_VERDICT_SPECS) or any(
            value is not None for value in (args.expected_review_id, args.expected_family,
                args.expected_content_digest, args.google_selector_receipt, args.google_preflight_receipt)
        )):
            log("--add-dir is for raw INVESTIGATION, not formal REVIEW or preflight")
            return EXIT_ARG_ERROR
    web_clause = ""
    if args.web and not args.preflight_only and args.pydantic not in (FORMAL_VERDICT_SPECS | _common.PACKAGED_V2_VERDICT_SPECS):
        try:
            web_clause = _common.load_web_evidence_clause(
                Path(__file__).resolve().parents[1] / "prompts/investigation.md", gemini=True)
        except (OSError, UnicodeError, ValueError) as e:
            log(f"web evidence clause unavailable: {e}")
            return EXIT_ARG_ERROR

    v2_verdict = args.pydantic in _common.PACKAGED_V2_VERDICT_SPECS
    if args.review_packet_dir is not None and not v2_verdict:
        log("--review-packet-dir requires formal v2 review or its preflight")
        return EXIT_ARG_ERROR
    formal_verdict = (args.pydantic in FORMAL_VERDICT_SPECS or v2_verdict) and not args.preflight_only
    pydantic_cls = None
    if args.pydantic:
        try:
            pydantic_cls = load_pydantic_class(args.pydantic)
        except Exception as e:
            log(f"--pydantic load failed: {e}")
            return EXIT_ARG_ERROR

    if v2_verdict:
        from verdict_v2 import bound_wrapper
        import google_preflight_v2
        if args.repair_mode:
            log("v2 review uses explicit per-entry attempts, not repair-mode")
            return EXIT_ARG_ERROR
        if not args.preflight_only:
            try:
                pydantic_cls = bound_wrapper(args, family="google", route="gemini")
            except ValueError as error:
                log(str(error))
                return EXIT_ARG_ERROR
    elif any(value is not None for value in (
        args.expected_leg_name, args.expected_attempt, args.expected_route
    )):
        log("v2 bindings require --pydantic verdict_v2:LegVerdict")
        return EXIT_ARG_ERROR

    binding_values = (
        args.expected_review_id,
        args.expected_family,
        args.expected_content_digest,
    )
    if args.preflight_only:
        if (
            args.expected_review_id is None
            or args.expected_family is not None
            or args.expected_content_digest is not None
        ):
            log("Gemini formal preflight requires only --expected-review-id")
            return EXIT_ARG_ERROR
    else:
        if formal_verdict and not all(value is not None for value in binding_values):
            log("formal verdict schema requires all formal verdict bindings")
            return EXIT_ARG_ERROR
        if any(value is not None for value in binding_values):
            if not all(value is not None for value in binding_values):
                log("formal verdict bindings must be supplied together")
                return EXIT_ARG_ERROR
            if not formal_verdict:
                log("formal verdict bindings require packaged LegVerdict schema")
                return EXIT_ARG_ERROR
            if re.fullmatch(r"[0-9a-f]{64}", args.expected_content_digest) is None:
                log(
                    "expected content digest must be 64 lowercase hexadecimal characters"
                )
                return EXIT_ARG_ERROR
    if (
        args.expected_review_id is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.expected_review_id) is None
    ):
        log("expected review ID has invalid syntax")
        return EXIT_ARG_ERROR
    if formal_verdict and not v2_verdict and (
        args.timeout != FORMAL_GEMINI_TIMEOUT or args.model is not None
    ):
        log(
            "formal Gemini review requires --timeout 600 and CLI Auto model routing"
        )
        return EXIT_ARG_ERROR
    if args.preflight_only and not v2_verdict and (
        args.pydantic is not None or args.model is not None or args.repair_mode
    ):
        log("Gemini formal preflight does not accept model, schema, or repair controls")
        return EXIT_ARG_ERROR

    selected_context = formal_verdict or args.preflight_only
    if formal_verdict:
        try:
            _common.validate_review_web(args, args.prompt)
        except ValueError as error:
            log(str(error))
            return EXIT_ARG_ERROR
    if selected_context and args.google_selector_receipt is None:
        log("formal Gemini route requires --google-selector-receipt")
        return EXIT_ARG_ERROR
    if not selected_context and args.google_selector_receipt is not None:
        log("Google selector receipt is reserved for formal review and preflight")
        return EXIT_ARG_ERROR
    if formal_verdict and args.google_preflight_receipt is None:
        log("formal Gemini review requires --google-preflight-receipt")
        return EXIT_ARG_ERROR
    if not formal_verdict and args.google_preflight_receipt is not None:
        log("Google preflight receipt is reserved for formal dispatch")
        return EXIT_ARG_ERROR
    selector_receipt = None
    v2_fields = None
    v2_preflight = None
    if args.google_selector_receipt is not None:
        try:
            selector_receipt = review_round.load_google_selector_receipt(
                args.google_selector_receipt,
                expected_review_id=args.expected_review_id,
                expected_route="gemini",
                expected_wrapper=Path(__file__).resolve(),
            )
            if v2_verdict:
                v2_fields = google_preflight_v2.fields(args, args.cwd, selector_receipt)
            if formal_verdict and v2_verdict:
                v2_preflight = google_preflight_v2.load_receipt(
                    args.google_preflight_receipt, selector_receipt, args, args.cwd, args.prompt)
            elif formal_verdict:
                selector_receipt = review_round.validate_google_preflight_receipt(
                    args.google_preflight_receipt,
                    selector_receipt,
                    expected_review_id=args.expected_review_id,
                )
                if args.web != selector_receipt.review_web_authorized:
                    raise ValueError("Gemini web authorization does not match preflight")
                review_round.validate_google_selector_prompt(
                    args.prompt,
                    selector_receipt,
                    expected_review_id=args.expected_review_id,
                    expected_content_digest=args.expected_content_digest,
                )
        except (review_round.RoundIntegrityError, ValueError, OSError) as error:
            log(f"Google selector receipt rejected: {error}")
            return EXIT_ARG_ERROR

    gemini_bin = (
        str(selector_receipt.executable)
        if selector_receipt is not None
        else require_binary("gemini")
    )
    policy = _formal_policy_path().with_name("gemini-formal-web.toml") if args.web else _formal_policy_path()
    if formal_verdict:
        try:
            _validate_formal_policy(policy, **({"web": True} if args.web else {}))
        except ValueError as error:
            log(str(error))
            return EXIT_ARG_ERROR
    if args.preflight_only:
        return _run_preflight(gemini_bin, args.cwd, args.timeout, selector_receipt,
                              **({"v2_fields": v2_fields} if v2_verdict else {}),
                              **({"web": True} if args.web else {}))
    if not formal_verdict:
        try:
            _probe_version(gemini_bin, args.cwd, args.timeout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            log(f"Gemini version check failed: {error}")
            return EXIT_ARG_ERROR

    def build_cmd(effective_prompt: str) -> list[str]:
        cmd = [
            gemini_bin,  # resolved/pinned path (finding #3) — never a bare name
            "-p",
            effective_prompt,
            "--output-format",
            "json",
        ]
        if formal_verdict:
            cmd += (["--model=" + args.model] if v2_verdict else ["-m", "auto"]) + [
                "--approval-mode",
                "plan",
                "--policy",
                str(policy),
            ]
        elif args.model:
            cmd += ["--model=" + args.model]
        for directory in args.add_dir:
            cmd += ["--include-directories", directory]
        if args.review_packet_dir is not None:
            cmd += ["--include-directories", args.review_packet_dir]
        return cmd

    result = run_cli_with_retry(
        "gemini",
        build_cmd,
        args.prompt,
        cwd=args.cwd,
        timeout=args.timeout,
        pydantic_cls=pydantic_cls,
        last_msg_path=None,
        repair_mode=args.repair_mode,
        single_provider_call=formal_verdict,
        remove_env=FORMAL_GEMINI_REMOVED_ENV if formal_verdict else (),
        prompt_suffix=web_clause,
    )

    if formal_verdict:
        result.runtime_identity = result.runtime_identity or "unexposed"
        result.transport = _common.transport_receipt("gemini", result)
        if not v2_verdict:
            result.transport = {**result.transport, "cli_version": selector_receipt.cli_version}
        if v2_verdict:
            result.review_binding = dict(pydantic_cls._binding)
            result.transport["attempt"] = args.expected_attempt

    if formal_verdict and result.exit_code == _common.EXIT_OK:
        for field, expected in (
            ("review_id", args.expected_review_id),
            ("family", args.expected_family),
            ("content_digest", args.expected_content_digest),
        ):
            if result.validated is None or result.validated.get(field) != expected:
                result.exit_code = _common.EXIT_SCHEMA_FAIL
                result.classification = "schema-fail"
                result.validation_error = f"{field.replace('_', ' ')} mismatch"
                result.final_answer = ""
                result.validated = None
                break

    audit_prompt = args.prompt + ("\n\n" + web_clause if web_clause else "")
    audit_cmd = build_cmd(audit_prompt)
    _common.record_wrapper_paths(result, args.prompt_file, args.cwd, process_cwd)
    persist_result_artifacts(
        "gemini", sys.argv, audit_cmd, audit_prompt, result, debug=args.debug
    )

    if pydantic_cls and result.validated is not None:
        sys.stdout.write(json.dumps(result.validated, ensure_ascii=False))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(result.final_answer or "")
        if result.final_answer and not result.final_answer.endswith("\n"):
            sys.stdout.write("\n")
    sys.stdout.flush()
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
