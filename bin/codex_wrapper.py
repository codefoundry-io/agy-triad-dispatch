#!/usr/bin/env python3
"""Single-shot Codex CLI transport wrapper.

Forwards a prompt to `codex exec` in JSON mode with OpenAI structured outputs,
supporting standalone consults and formal v2 bound review calls.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import _common
from _common import (
    EXIT_ARG_ERROR,
    EXIT_BINARY_MISSING,
    EXIT_CLI_FAIL,
    EXIT_OK,
    EXIT_SCHEMA_FAIL,
    EXIT_SCHEMA_REJECTED,
    EXIT_TIMEOUT,
    RunResult,
    load_pydantic_class,
    load_prompt_text,
    log,
    persist_result_artifacts,
    pydantic_to_codex_schema,
    require_binary,
    resolve_binary,
    validate_wrapper_cwd,
)

EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Codex CLI single-shot wrapper",
        allow_abbrev=False,
    )
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="User prompt text")
    prompt_group.add_argument(
        "--prompt-file",
        help="Path to UTF-8 file containing user prompt",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Process working directory for codex invocation",
    )
    parser.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write"),
        default="read-only",
        help="Execution sandbox mode (default: read-only)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Codex model identifier (-m flag passthrough)",
    )
    parser.add_argument(
        "--reasoning",
        "--effort",
        dest="effort",
        choices=EFFORT_CHOICES,
        default=None,
        help="Reasoning effort level (dest: effort)",
    )
    parser.add_argument(
        "--search",
        "--web",
        dest="web",
        action="store_true",
        help="Enable live web search capability",
    )
    parser.add_argument(
        "--pydantic",
        default=None,
        help="pydantic class spec (module.path:ClassName) for schema enforcement",
    )
    parser.add_argument(
        "--output-schema-file",
        default=None,
        help="Path to pre-built JSON schema file",
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Additional image inputs for consult mode",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Append debug row to _debug log",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="Max retries for capacity errors on standalone consults",
    )
    parser.add_argument(
        "--backoff-base",
        type=float,
        default=1.0,
        help="Exponential backoff base for retries in seconds",
    )
    parser.add_argument(
        "--repair-mode",
        action="store_true",
        help="Compatibility flag: single attempt with repair mode",
    )
    parser.add_argument("--expected-review-id", default=None)
    parser.add_argument("--expected-family", choices=("codex",), default=None)
    parser.add_argument("--expected-content-digest", default=None)
    parser.add_argument("--expected-leg-name", default=None)
    parser.add_argument("--expected-attempt", type=int, default=None)
    parser.add_argument("--expected-route", choices=("null",), default=None)

    args = parser.parse_args()

    process_cwd = None
    try:
        process_cwd = Path.cwd()
        _prompt_text = load_prompt_text(args.prompt, args.prompt_file, process_cwd=process_cwd)
    except Exception as exc:
        log(_common.input_path_error("prompt load", args.prompt_file, process_cwd, exc))
        return EXIT_ARG_ERROR
    args.prompt = _prompt_text

    raw_cwd = args.cwd
    try:
        args.cwd = validate_wrapper_cwd(args.cwd, process_cwd=process_cwd)
    except Exception as exc:
        log(_common.input_path_error("--cwd", args.cwd, process_cwd, exc))
        return EXIT_ARG_ERROR

    if not args.prompt.strip():
        log("empty prompt")
        return EXIT_ARG_ERROR
    if args.timeout <= 0:
        log("--timeout must be a positive integer")
        return EXIT_ARG_ERROR
    if args.model is not None and not args.model.strip():
        log("--model must not be empty")
        return EXIT_ARG_ERROR
    if args.sandbox == "workspace-write" and not raw_cwd:
        log("--sandbox workspace-write requires explicit --cwd")
        return EXIT_ARG_ERROR

    pydantic_cls = None
    if args.pydantic:
        try:
            pydantic_cls = load_pydantic_class(args.pydantic)
        except Exception as exc:
            log(f"--pydantic load failed: {exc}")
            return EXIT_ARG_ERROR

    binding_attrs = getattr(_common, "REVIEW_BINDING_ARGS", (
        "expected_review_id",
        "expected_family",
        "expected_content_digest",
        "expected_leg_name",
        "expected_attempt",
        "expected_route",
    ))
    is_formal_v2 = (
        args.pydantic in _common.PACKAGED_V2_VERDICT_SPECS
        or any(getattr(args, attr, None) is not None for attr in binding_attrs)
    )

    if is_formal_v2:
        if args.pydantic not in _common.PACKAGED_V2_VERDICT_SPECS:
            log("v2 bindings require --pydantic verdict_v2:LegVerdict")
            return EXIT_ARG_ERROR
        if args.sandbox != "read-only":
            log("v2 review requires --sandbox read-only")
            return EXIT_ARG_ERROR
        if args.image:
            log("v2 review forbids --image")
            return EXIT_ARG_ERROR
        if args.output_schema_file is not None:
            log("v2 review forbids --output-schema-file")
            return EXIT_ARG_ERROR

        import verdict_v2
        try:
            pydantic_cls = verdict_v2.bound_wrapper(args, family="codex", route=None)
        except ValueError as exc:
            log(str(exc))
            return EXIT_ARG_ERROR

        try:
            _common.validate_review_web(args, args.prompt)
        except ValueError as exc:
            log(str(exc))
            return EXIT_ARG_ERROR

        codex_bin = resolve_binary("codex")
        schema_dict = pydantic_to_codex_schema(pydantic_cls)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            schema_file = tmp_path / "schema.json"
            schema_file.write_text(json.dumps(schema_dict, ensure_ascii=False), encoding="utf-8")
            last_msg_file = tmp_path / "last_msg.txt"

            cmd = [
                codex_bin,
                "exec",
                "--json",
                "-o",
                str(last_msg_file),
                "--output-schema",
                str(schema_file),
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-rules",
                "-c",
                "approval_policy=never",
                "-c",
                f"web_search={'live' if args.web else 'disabled'}",
            ]
            if args.model:
                cmd += ["-m", args.model]
            if args.effort:
                cmd += ["-c", f"model_reasoning_effort={args.effort}"]
            cmd.append("-")

            _common.prune_stale_run_logs("codex")
            result = _common._run_once(
                "codex",
                cmd,
                cwd=args.cwd,
                timeout=args.timeout,
                stdin_text=args.prompt,
                classify_and_log=False,
            )

            if result._stdin_delivery_failed or result._output_transport_failed or (
                result.exit_code == EXIT_TIMEOUT
            ):
                if result.exit_code == EXIT_TIMEOUT:
                    result.classification = "timeout"
                log(
                    f"[wrapper] codex {result.classification} "
                    f"exit={result.exit_code} vendor={result.vendor_exit_code} "
                    f"elapsed={result.elapsed_s:.1f}s"
                )
            elif result.exit_code != EXIT_OK:
                classification = _common.classify(
                    "codex",
                    result.stderr,
                    result.stdout,
                    result.exit_code,
                    vendor_exit_code=result.vendor_exit_code,
                )
                result.classification = classification
                result.exit_code = _common.map_classification_to_exit(classification)
                result.final_answer = ""
                log(
                    f"[wrapper] codex {result.classification} "
                    f"exit={result.exit_code} vendor={result.vendor_exit_code} "
                    f"elapsed={result.elapsed_s:.1f}s"
                )
            else:
                if not last_msg_file.exists():
                    result.classification = "extraction-error"
                    result.exit_code = EXIT_CLI_FAIL
                    result.extraction_error = "last message output file missing"
                    result.final_answer = ""
                else:
                    raw_last_msg = last_msg_file.read_text(encoding="utf-8")
                    result.final_answer = raw_last_msg
                    try:
                        admitted = verdict_v2.admit_carrier_output(
                            pydantic_cls,
                            carrier="native-final",
                            stdout=raw_last_msg,
                            stderr=result.stderr,
                            exit_code=result.vendor_exit_code,
                            completed=True,
                        )
                        pydantic_cls.model_validate(admitted)
                        result.classification = "ok"
                        result.exit_code = EXIT_OK
                        result.validated = admitted
                        result.final_answer = json.dumps(admitted, ensure_ascii=False)
                    except Exception as exc:
                        result.classification = "schema-fail"
                        result.exit_code = EXIT_SCHEMA_FAIL
                        result.validation_error = f"{type(exc).__name__}: {exc}"
                        result.final_answer = ""
                        result.validated = None
                log(
                    f"[wrapper] codex {result.classification} "
                    f"exit={result.exit_code} vendor={result.vendor_exit_code} "
                    f"elapsed={result.elapsed_s:.1f}s"
                )

            result.review_binding = dict(pydantic_cls._binding)
            stdin_delivery = (result.transport or {}).get(
                "stdin_delivery",
                "not-started" if result._stdin_delivery_failed else "complete",
            )
            result.transport = {
                "schema_version": 2,
                "route": "codex",
                "stdin_delivery": stdin_delivery,
                "binary": codex_bin,
                "cli_version": None,
                "attempt": args.expected_attempt if args.expected_attempt is not None else 1,
            }
            _common.record_wrapper_paths(result, args.prompt_file, args.cwd, process_cwd)
            persist_result_artifacts("codex", sys.argv, cmd, args.prompt, result, debug=args.debug)

            if result.exit_code == EXIT_OK and result.validated is not None:
                sys.stdout.write(json.dumps(result.validated, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            return result.exit_code

    # Standalone consult mode
    codex_bin = resolve_binary("codex")
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        last_msg_file = tmp_path / "last_msg.txt"
        schema_file = None
        if args.output_schema_file:
            schema_file = Path(args.output_schema_file).resolve(strict=True)
        elif pydantic_cls is not None:
            schema_dict = pydantic_to_codex_schema(pydantic_cls)
            schema_file = tmp_path / "schema.json"
            schema_file.write_text(json.dumps(schema_dict, ensure_ascii=False), encoding="utf-8")

        cmd = [
            codex_bin,
            "exec",
            "--json",
            "-o",
            str(last_msg_file),
            "--sandbox",
            args.sandbox,
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "-c",
            "approval_policy=never",
            "-c",
            f"web_search={'live' if args.web else 'disabled'}",
        ]
        if schema_file is not None:
            cmd += ["--output-schema", str(schema_file)]
        if args.model:
            cmd += ["-m", args.model]
        if args.effort:
            cmd += ["-c", f"model_reasoning_effort={args.effort}"]
        for img in args.image:
            cmd += ["--image", img]
        cmd.append("-")

        max_attempts = 1 + max(0, args.max_retries)
        backoff = args.backoff_base

        for attempt_idx in range(max_attempts):
            _common.prune_stale_run_logs("codex")
            result = _common._run_once(
                "codex",
                cmd,
                cwd=args.cwd,
                timeout=args.timeout,
                stdin_text=args.prompt,
                classify_and_log=False,
            )

            if result._stdin_delivery_failed or result._output_transport_failed or (
                result.exit_code == EXIT_TIMEOUT
            ):
                if result.exit_code == EXIT_TIMEOUT:
                    result.classification = "timeout"
                log(
                    f"[wrapper] codex {result.classification} "
                    f"exit={result.exit_code} vendor={result.vendor_exit_code} "
                    f"elapsed={result.elapsed_s:.1f}s"
                )
            elif result.exit_code != EXIT_OK:
                classification = _common.classify(
                    "codex",
                    result.stderr,
                    result.stdout,
                    result.exit_code,
                    vendor_exit_code=result.vendor_exit_code,
                )
                result.classification = classification
                result.exit_code = _common.map_classification_to_exit(classification)
                result.final_answer = ""
                log(
                    f"[wrapper] codex {result.classification} "
                    f"exit={result.exit_code} vendor={result.vendor_exit_code} "
                    f"elapsed={result.elapsed_s:.1f}s"
                )
            else:
                if not last_msg_file.exists():
                    result.classification = "extraction-error"
                    result.exit_code = EXIT_CLI_FAIL
                    result.extraction_error = "last message output file missing"
                    result.final_answer = ""
                else:
                    raw_last_msg = last_msg_file.read_text(encoding="utf-8")
                    result.final_answer = raw_last_msg
                    if pydantic_cls is not None:
                        valid, validated_or_err = _common.validate_response(raw_last_msg, pydantic_cls)
                        if valid:
                            result.classification = "ok"
                            result.exit_code = EXIT_OK
                            result.validated = validated_or_err
                            result.final_answer = json.dumps(validated_or_err, ensure_ascii=False)
                        else:
                            result.classification = "schema-fail"
                            result.exit_code = EXIT_SCHEMA_FAIL
                            result.validation_error = str(validated_or_err)
                            result.final_answer = ""
                            result.validated = None
                    else:
                        result.classification = "ok"
                        result.exit_code = EXIT_OK

                log(
                    f"[wrapper] codex {result.classification} "
                    f"exit={result.exit_code} vendor={result.vendor_exit_code} "
                    f"elapsed={result.elapsed_s:.1f}s"
                )

            if result.classification != "server-capacity" or attempt_idx == max_attempts - 1:
                break
            time.sleep(backoff)
            backoff *= 2

        stdin_delivery = (result.transport or {}).get(
            "stdin_delivery",
            "not-started" if result._stdin_delivery_failed else "complete",
        )
        result.transport = {
            "schema_version": 2,
            "route": "codex",
            "stdin_delivery": stdin_delivery,
            "binary": codex_bin,
            "cli_version": None,
            "attempt": 1,
        }
        _common.record_wrapper_paths(result, args.prompt_file, args.cwd, process_cwd)
        persist_result_artifacts("codex", sys.argv, cmd, args.prompt, result, debug=args.debug)

        if pydantic_cls and result.validated is not None:
            sys.stdout.write(json.dumps(result.validated, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(result.final_answer or "")
            if result.final_answer and not result.final_answer.endswith("\n"):
                sys.stdout.write("\n")
        sys.stdout.flush()
        return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
