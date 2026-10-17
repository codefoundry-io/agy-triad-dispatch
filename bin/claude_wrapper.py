#!/usr/bin/env python3
"""Single-shot Claude CLI transport wrapper.

Forwards a prompt to Claude's JSON output mode along with only native model,
effort, fallback-model, working-directory, timeout, schema, and debug controls.
Provider-owned permission and trust settings are left to the native CLI for
ordinary calls. The exact formally bound ``LegVerdict`` route adds native
``--permission-mode plan`` and fails before provider resolution unless it uses
``opus`` with ``xhigh`` effort, a 1,200-second timeout, and no fallback model.

Stdout is the final answer text from envelope `.result` (or, with
``--pydantic``, the validated JSON object). Stderr is wrapper logging and
Claude's progress noise. Audit results are written to
``_logs/claude/audit.jsonl`` (gitignored).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import _common
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


EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")
FORMAL_CLAUDE_MODEL = "opus"
FORMAL_CLAUDE_EFFORT = "xhigh"
FORMAL_CLAUDE_TIMEOUT = 1200


def _claude_receipt(stdout: str) -> dict | None:
    """Project bounded, provider-reported metadata from the final envelope.

    This is audit evidence, not model attestation, billing or a verdict input.
    Invalid fields are omitted independently; no arbitrary provider text is copied.
    """
    raw = (stdout or "").strip()
    # Match the ordinary answer extractor's supported fence normalization.
    if raw.startswith("```"):
        newline = raw.find("\n")
        if newline != -1:
            raw = raw[newline + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(envelope, dict):
        return None

    def usage(value: object, *, per_model: bool = False) -> dict:
        if not isinstance(value, dict):
            return {}
        fields = (
            ("input_tokens", "inputTokens"),
            ("output_tokens", "outputTokens"),
            ("cache_read_input_tokens", "cacheReadInputTokens"),
            ("cache_creation_input_tokens", "cacheCreationInputTokens"),
        )
        selected = {}
        for name, model_name in fields:
            count = value.get(model_name if per_model else name)
            if type(count) is int and 0 <= count <= 2**53 - 1:
                selected[name] = count
        return selected

    def cost(value: object) -> bool:
        # The bounded comparison also rejects NaN/infinity without converting
        # potentially enormous JSON integers to float.
        return type(value) in (int, float) and 0 <= value <= 1_000_000

    receipt = {}
    session = envelope.get("session_id")
    if isinstance(session, str) and re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session
    ):
        receipt["session_id"] = session.lower()
    totals = usage(envelope.get("usage"))
    if totals:
        receipt["usage"] = totals
    models = envelope.get("modelUsage")
    if isinstance(models, dict) and len(models) <= 16:
        selected_models = {}
        for name, values in models.items():
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", name) is None:
                continue
            if not isinstance(values, dict):
                continue
            selected = usage(values, per_model=True)
            if cost(values.get("costUSD")):
                selected["estimated_cost_usd"] = values["costUSD"]
            if selected:
                selected_models[name] = selected
        if selected_models:
            receipt["model_usage"] = selected_models
    if cost(envelope.get("total_cost_usd")):
        receipt["estimated_cost_usd"] = envelope["total_cost_usd"]
    denials = envelope.get("permission_denials")
    if isinstance(denials, list) and len(denials) <= 1_000_000:
        receipt["permission_denial_count"] = len(denials)
    return receipt or None


def _run_native_structured_once(
    cmd: list[str],
    cwd: str,
    timeout: int,
    pydantic_cls,
    *,
    stdin_text: str | None = None,
    expected_review_id: str | None = None,
    expected_family: str | None = None,
    expected_content_digest: str | None = None,
) -> _common.RunResult:
    """Run one Claude call and validate its native structured output."""
    _common.prune_stale_run_logs("claude")
    input_kwargs = {} if stdin_text is None else {"stdin_text": stdin_text}
    result = _common._run_once(
        "claude", cmd, cwd, timeout, classify_and_log=False, **input_kwargs
    )
    if result._stdin_delivery_failed or result._output_transport_failed or (
        stdin_text is not None and result.exit_code == _common.EXIT_TIMEOUT
    ):
        if result.exit_code == _common.EXIT_TIMEOUT:
            result.classification = "timeout"
        log(
            f"[wrapper] claude {result.classification} "
            f"exit={result.exit_code} vendor={result.vendor_exit_code} "
            f"elapsed={result.elapsed_s:.1f}s"
        )
        return result
    answer, extraction_error = _common.extract_claude_answer(
        result.stdout, result.stderr
    )
    try:
        envelope = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        envelope = None
    has_native_structured_output = (
        isinstance(envelope, dict)
        and envelope.get("structured_output") is not None
    )

    if result.exit_code != _common.EXIT_OK:
        classification = _common.classify(
            "claude",
            result.stderr,
            result.stdout,
            result.exit_code,
            vendor_exit_code=result.vendor_exit_code,
        )
        result.classification = classification
        result.exit_code = _common.map_classification_to_exit(classification)
        result.final_answer = ""
    elif extraction_error is not None:
        result.extraction_error = extraction_error
        classification = _common.classify_extraction("claude", result, extraction_error)
        if classification == "unknown":
            classification = "extraction-error"
        result.classification = classification
        result.exit_code = _common.map_classification_to_exit(classification)
        result.final_answer = ""
    elif not has_native_structured_output:
        result.classification = "schema-fail"
        result.exit_code = _common.EXIT_SCHEMA_FAIL
        result.validation_error = "claude native structured_output is missing"
        result.final_answer = ""
    else:
        if getattr(pydantic_cls, "_binding", None) is not None:
            import verdict_v2
            try:
                admitted = verdict_v2.admit_carrier_output(
                    pydantic_cls,
                    carrier="claude-json",
                    stdout=_common.strip_markdown_fences(result.stdout),
                    stderr=result.stderr,
                    exit_code=result.vendor_exit_code,
                    completed=True,
                )
                pydantic_cls.model_validate(admitted)
                result.classification = "ok"
                result.validated = admitted
                result.final_answer = json.dumps(admitted, ensure_ascii=False)
            except Exception as exc:
                result.classification = "schema-fail"
                result.exit_code = _common.EXIT_SCHEMA_FAIL
                result.validation_error = f"{type(exc).__name__}: {exc}"
                result.final_answer = ""
        else:
            try:
                _common._check_original_json(
                    _common.strip_markdown_fences(result.stdout), pydantic_cls
                )
            except ValueError as error:
                valid, validated_or_error = False, str(error)
            else:
                valid, validated_or_error = _common.validate_response(answer, pydantic_cls)
            if valid:
                for field, expected in (
                    ("review_id", expected_review_id),
                    ("family", expected_family),
                    ("content_digest", expected_content_digest),
                ):
                    if expected is not None and validated_or_error.get(field) != expected:
                        result.classification = "schema-fail"
                        result.exit_code = _common.EXIT_SCHEMA_FAIL
                        result.validation_error = f"{field.replace('_', ' ')} mismatch"
                        result.final_answer = ""
                        break
                else:
                    result.classification = "ok"
                    result.validated = validated_or_error
                    result.final_answer = json.dumps(
                        validated_or_error, ensure_ascii=False
                    )
            else:
                result.classification = "schema-fail"
                result.exit_code = _common.EXIT_SCHEMA_FAIL
                result.validation_error = str(validated_or_error)
                result.final_answer = ""

    log(
        f"[wrapper] claude {result.classification} "
        f"exit={result.exit_code} vendor={result.vendor_exit_code} "
        f"elapsed={result.elapsed_s:.1f}s"
    )
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="Claude CLI single-shot wrapper",
                                allow_abbrev=False)
    prompt_group = p.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="User prompt")
    prompt_group.add_argument(
        "--prompt-file",
        help="Read the user prompt from a UTF-8 file (>=50K-char prompts: pass "
             "a file, not inline argv — L12; containment applies under "
             "TRIAD_WRAPPER_ALLOWED_ROOTS)")
    p.add_argument(
        "--cwd",
        default=None,
        help="Process working directory (caller-owned — use a sibling directory for isolation)",
    )
    p.add_argument("--timeout", type=int, default=600, help="Timeout in seconds")
    p.add_argument("--add-dir", action="append", default=[], help="Authorized additional raw-call input directory")
    p.add_argument("--agent", default=None, help="Native Claude agent for an explicit v2 or raw invocation")
    p.add_argument(
        "--model",
        default=None,
        help="Model for the session — claude CLI --model passthrough (an alias "
             "for the latest, or a full model name). Unrestricted FREE STRING "
             "caller passthrough; no generic wrapper default. Formal-review "
             "callers must follow the cross-family routing contract.",
    )
    p.add_argument(
        "--effort",
        default=None,
        choices=EFFORT_CHOICES,
        help="Reasoning effort level (default: vendor default)",
    )
    p.add_argument(
        "--fallback-model",
        default=None,
        help="Auto-fallback model name when default overloaded",
    )
    p.add_argument(
        "--pydantic",
        default=None,
        help="pydantic class spec (module.path:ClassName) for schema enforcement",
    )
    p.add_argument("--expected-review-id", default=None)
    p.add_argument("--expected-family", choices=("claude",), default=None)
    p.add_argument("--expected-content-digest", default=None)
    p.add_argument("--expected-leg-name", default=None)
    p.add_argument("--expected-attempt", type=int, default=None)
    p.add_argument("--expected-route", choices=("null", "agy", "gemini"), default=None)
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
             "_debug/<UTC-YYYY-MM-DD>/claude.md (per-call summary)",
    )
    p.add_argument("--web", action="store_true", help="Explicit owner-authorized native web verification")
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

    try:
        args.add_dir = _common.validate_extra_directories(args.add_dir, process_cwd=process_cwd)
    except Exception as e:
        log(_common.input_path_error("--add-dir", None, process_cwd, e))
        return EXIT_ARG_ERROR
    if args.add_dir and (args.pydantic in (_common.PACKAGED_VERDICT_SPECS | _common.PACKAGED_V2_VERDICT_SPECS) or any(
        value is not None for value in (args.expected_review_id, args.expected_family, args.expected_content_digest)
    )):
        log("--add-dir is for raw INVESTIGATION; REVIEW inputs must be bound in the round")
        return EXIT_ARG_ERROR

    if not args.prompt.strip():
        log("empty prompt")
        return EXIT_ARG_ERROR

    pydantic_cls = None
    if args.pydantic:
        try:
            pydantic_cls = load_pydantic_class(args.pydantic)
        except Exception as e:
            log(f"--pydantic load failed: {e}")
            return EXIT_ARG_ERROR

    if args.repair_mode and pydantic_cls is not None:
        log("--repair-mode is unavailable with --pydantic structured output")
        return EXIT_ARG_ERROR

    binding_values = (
        args.expected_review_id,
        args.expected_family,
        args.expected_content_digest,
    )
    formal_bindings_complete = all(value is not None for value in binding_values)
    v2_verdict = args.pydantic in _common.PACKAGED_V2_VERDICT_SPECS
    formal_verdict = args.pydantic in _common.PACKAGED_VERDICT_SPECS or v2_verdict
    if v2_verdict:
        from verdict_v2 import bound_wrapper, producer_schema
        try:
            pydantic_cls = bound_wrapper(args, family="claude", route=None)
            if args.fallback_model is not None or args.timeout <= 0:
                raise ValueError("v2 review requires positive timeout and forbids fallback models")
        except ValueError as error:
            log(str(error))
            return EXIT_ARG_ERROR
    elif any(value is not None for value in (
        args.expected_leg_name, args.expected_attempt, args.expected_route
    )):
        log("v2 bindings require --pydantic verdict_v2:LegVerdict")
        return EXIT_ARG_ERROR
    if formal_verdict and not all(
        value is not None for value in binding_values
    ):
        log("formal verdict schema requires all formal verdict bindings")
        return EXIT_ARG_ERROR
    if any(value is not None for value in binding_values):
        if not all(value is not None for value in binding_values):
            log("formal verdict bindings must be supplied together")
            return EXIT_ARG_ERROR
        if not formal_verdict:
            log("formal verdict bindings require --pydantic verdict_schema:LegVerdict")
            return EXIT_ARG_ERROR
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.expected_review_id) is None:
            log("expected review ID has invalid syntax")
            return EXIT_ARG_ERROR
        if re.fullmatch(r"[0-9a-f]{64}", args.expected_content_digest) is None:
            log("expected content digest must be 64 lowercase hexadecimal characters")
            return EXIT_ARG_ERROR
    if formal_bindings_complete and not v2_verdict and (
        args.model != FORMAL_CLAUDE_MODEL
        or args.effort != FORMAL_CLAUDE_EFFORT
        or args.timeout != FORMAL_CLAUDE_TIMEOUT
        or args.fallback_model is not None
    ):
        log(
            "formal Claude route requires --model opus --effort xhigh "
            "--timeout 1200 and forbids --fallback-model"
        )
        return EXIT_ARG_ERROR

    if args.agent is not None and (
        not args.agent.strip() or any(char in args.agent for char in ("\x00", "\r", "\n"))
        or (formal_bindings_complete and not v2_verdict)
    ):
        log("Claude agent must be a nonblank native name; legacy formal review does not select agents")
        return EXIT_ARG_ERROR

    if formal_bindings_complete:
        try:
            _common.validate_review_web(args, args.prompt)
        except ValueError as error:
            log(str(error))
            return EXIT_ARG_ERROR

    claude_bin = require_binary("claude")

    def build_cmd(
        effective_prompt: str, native_schema: str | None = None
    ) -> list[str]:
        cmd = [
            claude_bin,   # resolved/pinned path (finding #3) — never a bare name
            "--print", "--input-format", "text",
            "--output-format", "json",
        ]
        if args.model:
            cmd += ["--model", args.model]
        if args.effort:
            cmd += ["--effort", args.effort]
        if args.agent is not None:
            cmd += ["--agent", args.agent]
        for directory in args.add_dir:
            cmd += ["--add-dir", directory]
        if args.fallback_model:
            cmd += ["--fallback-model", args.fallback_model]
        if args.web:
            cmd += ["--allowedTools", "WebSearch", "WebFetch"]
        if formal_bindings_complete:
            cmd += ["--permission-mode", "plan"]
        if native_schema is not None:
            cmd += ["--json-schema", native_schema]
        return cmd

    native_schema = None
    if pydantic_cls is not None:
        schema_object = (producer_schema(pydantic_cls) if v2_verdict
                         else pydantic_cls.model_json_schema())
        if formal_bindings_complete:
            properties = schema_object["properties"]
            properties["review_id"]["const"] = args.expected_review_id
            properties["family"]["const"] = args.expected_family
            properties["content_digest"]["const"] = args.expected_content_digest
        native_schema = json.dumps(
            schema_object,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = _run_native_structured_once(
            build_cmd(args.prompt, native_schema),
            args.cwd,
            args.timeout,
            pydantic_cls,
            stdin_text=args.prompt,
            expected_review_id=args.expected_review_id,
            expected_family=args.expected_family,
            expected_content_digest=args.expected_content_digest,
        )
    else:
        result = run_cli_with_retry(
            "claude",
            build_cmd,
            args.prompt,
            cwd=args.cwd,
            timeout=args.timeout,
            pydantic_cls=None,
            last_msg_path=None,
            repair_mode=args.repair_mode,
            prompt_via_stdin=True,
        )

    result._claude_receipt = _claude_receipt(result.stdout)
    if v2_verdict:
        result.review_binding = dict(pydantic_cls._binding)
        result.transport = {**_common.transport_receipt("claude", result),
                            "attempt": args.expected_attempt}
    _common.record_wrapper_paths(result, args.prompt_file, args.cwd, process_cwd)
    audit_cmd = build_cmd(args.prompt, native_schema)
    persist_result_artifacts(
        "claude", sys.argv, audit_cmd, args.prompt, result, debug=args.debug
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
