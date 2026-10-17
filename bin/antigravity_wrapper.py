#!/usr/bin/env python3
"""AGY dispatch with explicit native-tool profiles for read-only calls.

No path writes or locks machine settings. Optional project permissions and
requested-web conflicts are checked read-only; formal results retain bindings.
Formal plan-mode calls pass the review-bound native finish schema, admit terminal
structured_output, and repeat strict local verdict validation.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import _common
import _agy_settings
import _agy_agent
import review_round
from _common import load_pydantic_class, validate_response


AGY_VERSION_FLOOR = (1, 1, 20)
READONLY_AGY_VERSION_FLOOR = (1, 2, 1)
OFFSET_S = 10
MIN_PRINT_TIMEOUT_S = 5
HEADLESS_SOFTDENY_FLOOR = (1, 1, 3)
FORMAL_AGY_MODEL = "gemini-3.1-pro-high"
FORMAL_AGY_EFFORT = "high"
FORMAL_AGY_TIMEOUT = 600
_WEB_EVIDENCE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "investigation.md"
# Compatibility subset; the shared builder owns the complete effective set.
FORMAL_AGY_ENV_REMOVE = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_ENTERPRISE",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_CLOUD_REGION",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "AGY_ADC_AUTH",
)


def _load_web_evidence_clause() -> str:
    """Load the one vendored C29 clause only for an authorized investigation."""
    return _common.load_web_evidence_clause(_WEB_EVIDENCE_PATH)


def _parse_agy_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _probe_agy_version(agy_bin: str) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [agy_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            env=_common.scrubbed_child_env(cli="antigravity"),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_agy_version(result.stdout)


def _route_args(
    model: str | None, effort: str | None, project: str | None = None
) -> list[str]:
    args: list[str] = []
    if model:
        args += ["--model=" + model] if model.startswith("-") else ["--model", model]
    if effort:
        args += ["--effort", effort]
    if project is not None:
        args += ["--project", project]
    return args


def _agy_needs_skip_permissions(version: tuple[int, int, int] | None) -> bool:
    if os.environ.get("AGY_NO_HEADLESS_AUTOAPPROVE") == "1":
        return False
    return version is not None and version >= HEADLESS_SOFTDENY_FLOOR


def _build_cmd(
    agy_bin: str,
    prompt: str,
    *,
    model: str | None,
    effort: str | None,
    timeout: int,
    json_schema: str | None,
    sandbox: bool = False,
    skip_permissions: bool = True,
    project: str | None = None,
    add_dirs: tuple[str, ...] = (),
    agent: str | None = None,
) -> list[str]:
    print_timeout = max(timeout - OFFSET_S, MIN_PRINT_TIMEOUT_S)
    cmd = [agy_bin]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd += [
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{print_timeout}s",
    ]
    if json_schema is not None:
        cmd += ["--json-schema", json_schema]
    if sandbox:
        cmd += ["--mode", "plan", "--sandbox"]
    for directory in add_dirs:
        cmd += ["--add-dir", directory]
    if agent is not None:
        cmd += ["--agent", agent]
    return cmd + _route_args(model, effort, project)


def parse_agy_stream(text: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return parsed events and the last complete terminal result payload."""
    events: list[dict[str, Any]] = []
    terminal: dict[str, Any] | None = None
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        payload = event.get("result")
        if event.get("event") == "result" and isinstance(payload, dict):
            terminal = payload
    return events, terminal


def _project_agy_done_view_files(events: list[dict[str, Any]], cwd: str | None) -> dict | None:
    """Bounded diagnostics from observed events, never evidence of read coverage."""
    try:
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return None
        cwd.encode("utf-8")
        root = Path(cwd).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    count = 0
    paths: list[str] = []
    for event in events:
        step = event.get("step_update")
        if (event.get("event") != "step_update" or not isinstance(step, dict)
                or step.get("state") != "DONE" or step.get("step_type") != "tool"
                or step.get("tool_name") != "view_file"):
            continue
        count += 1
        info = step.get("tool_info")
        params = info.get("parameters") if isinstance(info, dict) else None
        raw = params.get("AbsolutePath") if isinstance(params, dict) else None
        if not isinstance(raw, str) or len(paths) >= 128:
            continue
        try:
            path = Path(raw)
            if not path.is_absolute() or ".." in path.parts:
                continue
            relative = path.resolve(strict=False).relative_to(root).as_posix()
            relative.encode("utf-8")
            if len(relative) <= 1024 and relative not in paths:
                paths.append(relative)
        except (OSError, RuntimeError, ValueError):
            continue
    return {"done_view_file_event_count": count, "relative_paths": paths} if count else None


def _agy_step_denied(step: dict[str, Any]) -> bool:
    """Measured denial heads are separate from structured finish recovery."""
    info = step.get("tool_info")
    error = info.get("error") if isinstance(info, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if step.get("state") != "ERROR" or not isinstance(message, str):
        return False
    first = next((line.strip().casefold() for line in message.splitlines() if line.strip()), "")
    if first.startswith(("tool call denied by pre-tool hook", "user denied permission")):
        return True
    if first.startswith("permission check failed for ") and '": ' in first:
        return first.rsplit('": ', 1)[-1].startswith(("user denied permission", "permission denied for"))
    return False


def _agy_finish_resubmitted(events: list[dict[str, Any]], *, web: bool) -> bool:
    """Only ERROR recovery needs tool outcomes; normal success has no census."""
    results = [pos for pos, event in enumerate(events) if event.get("event") == "result"]
    if len(results) != 1:
        return False
    updates = [(pos, event["step_update"]) for pos, event in enumerate(events)
               if event.get("event") == "step_update" and isinstance(event.get("step_update"), dict)]
    if any(pos > results[0] for pos, _ in updates):
        return False
    reads = {"view_file", "grep_search", "list_dir", "find_by_name"}
    if web:
        reads.update(("read_url_content", "search_web"))
    tools = []
    last_success = last_error = -1
    for pos, step in updates:
        info = step.get("tool_info")
        info = info if isinstance(info, dict) else {}
        state, kind = step.get("state"), step.get("step_type")
        error = bool(info.get("error"))
        if kind == "finish" and state == "DONE" and not error:
            last_success = pos
        elif kind != "tool" and (state == "ERROR" or error):
            return False
        if kind != "tool":
            continue
        names = {name for name in (step.get("tool_name"), info.get("name"))
                 if isinstance(name, str) and name}
        if not names:
            return False
        index = step.get("step_index")
        index = index if isinstance(index, int) and not isinstance(index, bool) else None
        tools.append((pos, step, names, index, error))
    terminal = {(index, name): pos for pos, step, names, index, _ in tools
                if index is not None and step.get("state") in ("DONE", "ERROR") for name in names}
    read_succeeded = False
    for pos, step, names, index, error in tools:
        state = step.get("state")
        if state == "ACTIVE" and index is not None and all(
                terminal.get((index, name), -1) > pos for name in names):
            continue
        if state not in ("DONE", "ERROR"):
            return False
        if names - reads - {"finish"}:
            if _agy_step_denied(step):
                continue
            return False
        if "finish" in names and (state == "ERROR" or error):
            last_error = pos
        if names <= reads and state == "DONE" and not error:
            read_succeeded = True
    return read_succeeded and last_error >= 0 and last_success > last_error


def _terminal_error_suffix(result: dict[str, Any] | None) -> str:
    error = result.get("error") if result is not None else None
    if isinstance(error, str):
        candidates = (error,)
    elif isinstance(error, dict):
        candidates = tuple(
            error.get(key) for key in ("message", "error", "detail", "code")
        )
    else:
        return ""
    for value in candidates:
        if not isinstance(value, str):
            continue
        for raw_line in value.splitlines():
            line = raw_line.strip()
            if line:
                return f"; terminal_error={line[:512]}"
    return ""


def _fail(
    run: _common.RunResult,
    classification: str,
    exit_code: int,
    detail: str,
) -> _common.RunResult:
    run.classification = classification
    run.exit_code = exit_code
    run.final_answer = ""
    run.extraction_error = detail
    run.dispatch_phase = "post-dispatch-result"
    return run


def _all_nonempty_lines_are_json_objects(stdout: str) -> bool:
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        if not (line.startswith("{") and line.endswith("}")):
            return False
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                return False
        except Exception:
            return False
    return True


def _find_terminal_result_line(stdout: str) -> str:
    candidates = []
    for line in (stdout or "").splitlines():
        trimmed = line.strip()
        if not (trimmed.startswith("{") and trimmed.endswith("}")):
            continue
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, dict) and (parsed.get("type") == "result" or parsed.get("event") == "result"):
                candidates.append(trimmed)
        except Exception:
            continue
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("missing terminal result line in agy stream")
    raise ValueError("multiple terminal result lines in agy stream")


def _admit_v2_agy_output(
    pydantic_cls: Any,
    *,
    stdout: str,
    stderr: str = "",
    vendor_exit_code: int,
    c23_recovered: bool,
) -> dict[str, Any]:
    del stderr
    import verdict_v2
    if c23_recovered:
        terminal_line = _find_terminal_result_line(stdout)
        from triad_dispatch_lib.json_input import json_member_span
        span = json_member_span(terminal_line, "result", "structured_output")
        return verdict_v2.admit_carrier_output(
            pydantic_cls,
            carrier="native-final",
            stdout=span,
            exit_code=0,
            completed=True,
        )
    else:
        if _all_nonempty_lines_are_json_objects(stdout):
            return verdict_v2.admit_carrier_output(
                pydantic_cls,
                carrier="agy-stream-json",
                stdout=stdout,
                exit_code=vendor_exit_code,
                completed=True,
            )
        terminal_line = _find_terminal_result_line(stdout)
        return verdict_v2.admit_carrier_output(
            pydantic_cls,
            carrier="agy-stream-json",
            stdout=terminal_line,
            exit_code=vendor_exit_code,
            completed=True,
        )


def _interpret_run(
    run: _common.RunResult,
    pydantic_cls: Any,
    expected_model: str | None = None,
    *,
    expected_review_id: str | None = None,
    expected_family: str | None = None,
    expected_content_digest: str | None = None,
    plan_mode: bool = False,
    web: bool = False,
) -> _common.RunResult:
    """Admit one terminal AGY result through local verdict and binding checks."""
    run.runtime_identity = "unexposed"
    events, result = parse_agy_stream(run.stdout)
    if plan_mode:
        run._agy_read_telemetry = _project_agy_done_view_files(events, run._effective_cwd)
    exposed_model = None
    route_conflict = None
    for event in events:
        if event.get("event") != "init":
            continue
        init = event.get("init")
        if isinstance(init, dict) and isinstance(init.get("model"), str):
            exposed_model = init["model"]
            if (
                expected_model is not None
                and exposed_model != expected_model
                and route_conflict is None
            ):
                route_conflict = exposed_model
    run.runtime_identity = route_conflict or exposed_model or run.runtime_identity
    status = result.get("status") if isinstance(result, dict) else None
    if _common._auth_carrier_stop("antigravity", run.stderr, run.stdout, run.vendor_exit_code):
        return _fail(run, "oauth-env", _common.EXIT_TERMINAL,
                     "provider authentication failed" + _terminal_error_suffix(result))
    if run.exit_code == _common.EXIT_TIMEOUT:
        return _fail(run, "timeout", _common.EXIT_TIMEOUT, "provider timed out")
    if run._stdin_delivery_failed or run._output_transport_failed:
        run.dispatch_phase = "post-dispatch-result"
        return run
    if _common.agy_print_timed_out(run.stderr):
        return _fail(run, "timeout", _common.EXIT_TIMEOUT,
                     "AGY print timed out; partial output withheld")

    terminal_error = result.get("error") if isinstance(result, dict) else None
    if (
        run.vendor_exit_code == 1
        and status == "ERROR"
        and result.get("response") == ""
        and isinstance(terminal_error, str)
        and re.match(r"^timeout waiting for response\b", terminal_error)
    ):
        return _fail(
            run, "vendor-timeout", _common.EXIT_TERMINAL,
            "AGY turn timed out" + _terminal_error_suffix(result),
        )

    if run.vendor_exit_code != 0:
        detail = "\n".join(
            part for part in (run.stderr, f"agy result status={status}") if part
        )
        classification = _common.classify(
            "antigravity",
            stderr=detail,
            stdout="",
            exit_code=_common.EXIT_CLI_FAIL,
            vendor_exit_code=run.vendor_exit_code,
            agy_result=result,
        )
        return _fail(
            run,
            classification,
            _common.map_classification_to_exit(classification),
            (detail or "AGY exited without an admissible result")
            + _terminal_error_suffix(result),
        )

    if result is None:
        return _fail(
            run,
            "extraction-error",
            _common.EXIT_CLI_FAIL,
            "missing terminal result event",
        )
    denied_post_completion = (
        plan_mode
        and pydantic_cls is not None
        and status == "ERROR"
        and isinstance(result.get("error"), str)
        and result["error"].startswith("permission check failed for command ")
        and isinstance(result.get("response"), str)
        and bool(result["response"].strip())
    )
    resubmitted = (
        plan_mode and pydantic_cls is not None and status == "ERROR"
        and not denied_post_completion and _agy_finish_resubmitted(events, web=web)
    )
    if status != "SUCCESS" and not denied_post_completion and not resubmitted:
        return _fail(
            run,
            "vendor-error",
            _common.EXIT_TERMINAL,
            f"terminal result status is {status!r}" + _terminal_error_suffix(result),
        )
    if expected_model is not None and route_conflict is not None:
        return _fail(
            run,
            "route-mismatch",
            _common.EXIT_TERMINAL,
            f"requested model {expected_model!r} but AGY exposed {route_conflict!r}",
        )
    if pydantic_cls is not None:
        if getattr(pydantic_cls, "_binding", None) is not None:
            try:
                payload = _admit_v2_agy_output(
                    pydantic_cls,
                    stdout=run.stdout,
                    vendor_exit_code=run.vendor_exit_code,
                    c23_recovered=bool(denied_post_completion or resubmitted),
                )
                run.validated = payload
                run.final_answer = json.dumps(payload, ensure_ascii=False)
            except Exception as exc:
                run.validation_error = f"{type(exc).__name__}: {exc}"
                return _fail(
                    run,
                    "schema-fail",
                    _common.EXIT_SCHEMA_FAIL,
                    run.validation_error,
                )
        else:
            original_check = getattr(pydantic_cls, "_triad_check_original_json", None)
            if original_check is not None:
                try:
                    for line in (run.stdout or "").split("\n"):
                        if not line.lstrip().startswith("{"):
                            continue
                        try:
                            # Hooks can fail before the decoder detects malformed suffixes.
                            json.loads(line)
                            original_check(line)
                        except json.JSONDecodeError:
                            continue  # Preserve the stream parser's malformed/noise handling.
                except ValueError as error:
                    run.validation_error = str(error)
                    return _fail(run, "schema-fail", _common.EXIT_SCHEMA_FAIL, str(error))
            structured = result.get("structured_output")
            if structured is None:
                run.validation_error = "terminal result has no structured_output"
                return _fail(
                    run,
                    "schema-fail",
                    _common.EXIT_SCHEMA_FAIL,
                    run.validation_error,
                )
            encoded = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
            valid, payload = validate_response(encoded, pydantic_cls)
            if not valid:
                run.validation_error = (
                    f"plan-mode native structured output does not match "
                    f"LegVerdict: {payload}"
                    if plan_mode
                    else str(payload)
                )
                return _fail(
                    run,
                    "schema-fail",
                    _common.EXIT_SCHEMA_FAIL,
                    f"local schema validation failed: {payload}",
                )
            for field, expected in (
                ("review_id", expected_review_id),
                ("family", expected_family),
                ("content_digest", expected_content_digest),
            ):
                if expected is not None and payload.get(field) != expected:
                    run.validation_error = f"{field.replace('_', ' ')} mismatch"
                    return _fail(
                        run,
                        "schema-fail",
                        _common.EXIT_SCHEMA_FAIL,
                        run.validation_error,
                    )
            run.validated = payload
            run.final_answer = json.dumps(payload, ensure_ascii=False)
    else:
        response = result.get("response")
        if not isinstance(response, str) or not response.strip():
            return _fail(
                run,
                "extraction-error",
                _common.EXIT_CLI_FAIL,
                "terminal result has no non-empty response",
            )
        if any(re.fullmatch(r"[ \t]*<truncated \d+ (?:bytes|lines)>[ \t]*", line)
               for line in response.splitlines()):
            return _fail(
                run, "truncated-answer", _common.EXIT_TERMINAL,
                "AGY folded the raw answer (own-line truncation marker)",
            )
        run.final_answer = response.rstrip()

    run.exit_code = _common.EXIT_OK
    run.classification = "ok"
    run.dispatch_phase = "post-dispatch-result"
    return run


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AGY native stream-json single-shot wrapper",
        allow_abbrev=False,
    )
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="User prompt")
    prompt_group.add_argument("--prompt-file", help="Read a UTF-8 prompt file")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--sandbox", choices=("read-only",), default=None)
    parser.add_argument("--web", action="store_true",
                        help="Explicitly authorized web; REVIEW binds this option; requires --sandbox read-only")
    parser.add_argument(
        "--project",
        help="Existing AGY project UUID; requires --cwd and --sandbox read-only",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", choices=("low", "medium", "high"), default=None)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--add-dir", action="append", default=[], help="Authorized additional raw-call input directory")
    parser.add_argument("--pydantic", default=None, help="module:Class schema contract")
    parser.add_argument("--expected-review-id", default=None)
    parser.add_argument(
        "--expected-family",
        choices=("google",),
        default=None,
    )
    parser.add_argument("--expected-content-digest", default=None)
    parser.add_argument("--expected-leg-name", default=None)
    parser.add_argument("--expected-attempt", type=int, default=None)
    parser.add_argument("--expected-route", choices=("null", "agy", "gemini"), default=None)
    parser.add_argument("--google-selector-receipt", type=Path, default=None)
    parser.add_argument("--google-preflight-receipt", type=Path, default=None)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.model is not None and not isinstance(args.model, str):
        _common.log("model argument was not preserved as a string; refusing dispatch")
        return _common.EXIT_ARG_ERROR

    process_cwd = None
    try:
        process_cwd = Path.cwd()
        prompt = _common.load_prompt_text(args.prompt, args.prompt_file, process_cwd=process_cwd)
    except Exception as exc:
        _common.log(_common.input_path_error("prompt load", args.prompt_file, process_cwd, exc))
        return _common.EXIT_ARG_ERROR
    try:
        cwd = _common.validate_wrapper_cwd(args.cwd, process_cwd=process_cwd)
        args.add_dir = _common.validate_extra_directories(args.add_dir, process_cwd=process_cwd)
        if args.project is not None:
            _agy_settings.validate_project_id(args.project)
            if cwd is None or args.sandbox != "read-only":
                raise ValueError("--project requires --cwd and --sandbox read-only")
    except Exception as exc:
        _common.log(_common.input_path_error("argument", args.cwd, process_cwd, exc))
        return _common.EXIT_ARG_ERROR
    if args.add_dir and (args.preflight_only or args.pydantic in (_common.PACKAGED_VERDICT_SPECS | _common.PACKAGED_V2_VERDICT_SPECS) or any(
        value is not None for value in (args.expected_review_id, args.expected_family,
            args.expected_content_digest, args.google_selector_receipt, args.google_preflight_receipt)
    )):
        _common.log("--add-dir is for raw INVESTIGATION; REVIEW inputs must be bound in the round")
        return _common.EXIT_ARG_ERROR
    if not prompt.strip() or args.timeout <= 0:
        _common.log("prompt must be non-empty and timeout must be positive")
        return _common.EXIT_ARG_ERROR
    if args.web and args.sandbox != "read-only":
        _common.log("--web requires --sandbox read-only")
        return _common.EXIT_ARG_ERROR
    _common.prune_stale_run_logs("antigravity")

    pydantic_cls = None
    if args.pydantic:
        try:
            pydantic_cls = load_pydantic_class(args.pydantic)
        except Exception as exc:
            _common.log(f"--pydantic load failed: {exc}")
            return _common.EXIT_ARG_ERROR

    binding_values = (
        args.expected_review_id,
        args.expected_family,
        args.expected_content_digest,
    )
    v2_verdict = args.pydantic in _common.PACKAGED_V2_VERDICT_SPECS
    formal_verdict = args.pydantic in _common.PACKAGED_VERDICT_SPECS or v2_verdict
    if v2_verdict:
        from verdict_v2 import bound_wrapper
        import google_preflight_v2
        if not args.preflight_only:
            try:
                pydantic_cls = bound_wrapper(args, family="google", route="agy")
            except ValueError as exc:
                _common.log(str(exc))
                return _common.EXIT_ARG_ERROR
    elif any(value is not None for value in (
        args.expected_leg_name, args.expected_attempt, args.expected_route
    )):
        _common.log("v2 bindings require --pydantic verdict_v2:LegVerdict")
        return _common.EXIT_ARG_ERROR
    if (not args.preflight_only and formal_verdict
            and not any(value is not None for value in binding_values)):
        _common.log("formal verdict schema requires all formal verdict bindings")
        return _common.EXIT_ARG_ERROR
    if args.preflight_only:
        if args.sandbox != "read-only":
            _common.log("AGY formal preflight requires --sandbox read-only")
            return _common.EXIT_ARG_ERROR
        if (
            args.expected_review_id is None
            or args.expected_family is not None
            or args.expected_content_digest is not None
        ):
            _common.log("AGY formal preflight requires only --expected-review-id")
            return _common.EXIT_ARG_ERROR
    elif any(value is not None for value in binding_values):
        if not all(value is not None for value in binding_values):
            _common.log("formal verdict bindings must be supplied together")
            return _common.EXIT_ARG_ERROR
        if not formal_verdict:
            _common.log(
                "formal verdict bindings require --pydantic verdict_schema:LegVerdict"
            )
            return _common.EXIT_ARG_ERROR
        if re.fullmatch(r"[0-9a-f]{64}", args.expected_content_digest) is None:
            _common.log(
                "expected content digest must be 64 lowercase hexadecimal characters"
            )
            return _common.EXIT_ARG_ERROR
        if args.sandbox != "read-only":
            _common.log("formal verdict bindings require --sandbox read-only")
            return _common.EXIT_ARG_ERROR
    if (
        args.expected_review_id is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.expected_review_id) is None
    ):
        _common.log("expected review ID has invalid syntax")
        return _common.EXIT_ARG_ERROR

    formal_bindings = all(value is not None for value in binding_values)
    if formal_bindings and not v2_verdict and args.timeout != FORMAL_AGY_TIMEOUT:
        _common.log("formal AGY review requires --timeout 600")
        return _common.EXIT_ARG_ERROR
    selected_context = args.preflight_only or formal_bindings
    try:
        if formal_bindings:
            _common.validate_review_web(args, prompt)
        elif args.web and not args.preflight_only:
            prompt += "\n\n" + _load_web_evidence_clause()
    except (OSError, UnicodeError, ValueError) as error:
        message = str(error) if formal_bindings else f"web evidence clause unavailable: {error}"
        _common.log(message)
        return _common.EXIT_ARG_ERROR
    if selected_context and args.google_selector_receipt is None:
        _common.log("formal AGY route requires --google-selector-receipt")
        return _common.EXIT_ARG_ERROR
    if not selected_context and args.google_selector_receipt is not None:
        _common.log(
            "Google selector receipt is reserved for formal review and preflight"
        )
        return _common.EXIT_ARG_ERROR
    if formal_bindings and args.google_preflight_receipt is None:
        _common.log("formal AGY review requires --google-preflight-receipt")
        return _common.EXIT_ARG_ERROR
    if not formal_bindings and args.google_preflight_receipt is not None:
        _common.log("Google preflight receipt is reserved for formal dispatch")
        return _common.EXIT_ARG_ERROR
    selector_receipt = None
    v2_fields = None
    agent_control = {}
    readonly = args.sandbox == "read-only"
    profile_web = args.web if selected_context else True
    if readonly:
        try:
            agent_control = _agy_agent.controls(profile_web)
        except (ValueError, OSError) as exc:
            _common.log(f"AGY config-conflict: {exc}")
            return _common.EXIT_TERMINAL
    if args.google_selector_receipt is not None:
        try:
            selector_receipt = review_round.load_google_selector_receipt(
                args.google_selector_receipt,
                expected_review_id=args.expected_review_id,
                expected_route="agy",
                expected_wrapper=Path(__file__).resolve(),
            )
            if v2_verdict:
                v2_fields = google_preflight_v2.fields(args, cwd, selector_receipt)
            if formal_bindings and v2_verdict:
                google_preflight_v2.load_receipt(
                    args.google_preflight_receipt, selector_receipt, args, cwd, prompt)
            elif formal_bindings:
                selector_receipt = review_round.validate_google_preflight_receipt(
                    args.google_preflight_receipt,
                    selector_receipt,
                    expected_review_id=args.expected_review_id,
                )
                if (
                    args.web != selector_receipt.review_web_authorized
                    or args.model != selector_receipt.model
                    or args.effort != selector_receipt.effort
                    or tuple(_route_args(args.model, args.effort, args.project))
                    != selector_receipt.route_args
                ):
                    raise review_round.RoundIntegrityError(
                        "formal AGY arguments do not match bound preflight"
                    )
                review_round.validate_google_selector_prompt(
                    prompt,
                    selector_receipt,
                    expected_review_id=args.expected_review_id,
                    expected_content_digest=args.expected_content_digest,
                )
        except (review_round.RoundIntegrityError, ValueError, OSError) as exc:
            _common.log(f"Google selector receipt rejected: {exc}")
            return _common.EXIT_ARG_ERROR

    if formal_bindings:
        try:
            record = json.loads(args.google_preflight_receipt.read_bytes())
            if not isinstance(record, dict) or any(record.get(key) != value for key, value in agent_control.items()):
                raise ValueError("profile differs from bound preflight")
        except (ValueError, OSError) as exc:
            _common.log(f"AGY config-conflict: {exc}")
            return _common.EXIT_TERMINAL

    if args.preflight_only and not v2_verdict and (
        args.model not in (FORMAL_AGY_MODEL, "gemini-3.8-flash-high") or args.effort != FORMAL_AGY_EFFORT
    ):
        _common.log(
            "formal AGY preflight requires Pro High or Flash High with --effort high"
        )
        return _common.EXIT_ARG_ERROR

    agy_bin = (
        str(selector_receipt.executable)
        if selector_receipt is not None
        else _common.require_binary("agy")
    )
    version = _probe_agy_version(agy_bin)
    floor = READONLY_AGY_VERSION_FLOOR if readonly else AGY_VERSION_FLOOR
    if version is None or version < floor:
        found = _version_text(version) if version is not None else "unprobeable"
        _common.log(f"agy {found} is below required {_version_text(floor)}")
        return _common.EXIT_TERMINAL

    if args.project is not None:
        settings_guard = _agy_settings.agy_project_guard(
            args.project, cwd, formal_review=selected_context and not args.web,
            require_web=args.web,
        )
    else:
        settings_guard = contextlib.nullcontext()
    try:
        if args.web:
            _agy_settings.check_web_settings()
    except (ValueError, OSError) as exc:
        _common.log(f"AGY settings/config conflict: {exc}")
        return _common.EXIT_TERMINAL

    if args.preflight_only:
        try:
            with settings_guard:
                if _agy_agent.controls(profile_web) != agent_control:
                    raise ValueError("AGY profile changed during preflight")
        except (TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
            _common.log(f"AGY settings/config conflict: {exc}")
            return _common.EXIT_TERMINAL
        receipt = {
            "agy_version": _version_text(version),
            "effort": args.effort,
            "executable": agy_bin,
            "google_selector_receipt_sha256": selector_receipt.receipt_sha256,
            "model": args.model,
            "provider_started": False,
            "review_id": selector_receipt.review_id,
            "route": "agy",
            "route_args": _route_args(args.model, args.effort, args.project),
            **agent_control,
            **({"review_web_authorized": True} if args.web else {}),
            **(v2_fields or {}),
        }
        sys.stdout.write(
            json.dumps(
                receipt,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return _common.EXIT_OK

    local_plan_response = args.sandbox == "read-only" and formal_bindings
    try:
        schema_object = (
            pydantic_cls.model_json_schema() if pydantic_cls is not None else None
        )
        if schema_object is not None:
            properties = schema_object["properties"]
            if formal_bindings:
                properties["review_id"]["const"] = args.expected_review_id
                properties["family"]["const"] = args.expected_family
                properties["content_digest"]["const"] = args.expected_content_digest
        schema = (
            json.dumps(schema_object, ensure_ascii=True, separators=(",", ":"))
            if schema_object is not None
            else None
        )
    except Exception as exc:
        _common.log(f"schema generation failed: {exc}")
        return _common.EXIT_ARG_ERROR

    cmd = _build_cmd(
        agy_bin,
        prompt,
        model=args.model,
        effort=args.effort,
        timeout=args.timeout,
        json_schema=schema,
        sandbox=args.sandbox == "read-only",
        skip_permissions=not readonly and _agy_needs_skip_permissions(version),
        project=args.project,
        add_dirs=tuple(args.add_dir),
        agent=agent_control.get("agent"),
    )
    run_options: dict[str, Any] = {"classify_and_log": False}
    if formal_bindings:
        run_options["remove_env"] = FORMAL_AGY_ENV_REMOVE
    raw = None
    configuration_error = None
    try:
        with settings_guard:
            if readonly and _agy_agent.controls(profile_web) != agent_control:
                raise ValueError("AGY profile changed before dispatch")
            raw = _common._run_once(
                "antigravity",
                cmd,
                cwd,
                args.timeout,
                **run_options,
            )
            if readonly and _agy_agent.controls(profile_web) != agent_control:
                raise ValueError("AGY profile changed during dispatch")
    except (TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
        _common.log(f"AGY settings/config conflict: {exc}")
        if raw is None:
            return _common.EXIT_TERMINAL
        configuration_error = str(exc)
    result = _interpret_run(
        raw,
        pydantic_cls,
        args.model,
        expected_review_id=args.expected_review_id,
        expected_family=args.expected_family,
        expected_content_digest=args.expected_content_digest,
        plan_mode=local_plan_response,
        web=args.web,
    )
    if configuration_error is not None:
        if result.exit_code == _common.EXIT_OK:
            result = _fail(result, "config-conflict", _common.EXIT_TERMINAL,
                           f"AGY configuration failed after provider completion: {configuration_error}")
        else:
            result.extraction_error = (result.extraction_error or "") + f"; AGY configuration also failed: {configuration_error}"
        result.final_answer = ""
        result.validated = None
    result.transport = {**_common.transport_receipt("antigravity", result),
                        "cli_version": _version_text(version)}
    if v2_verdict:
        result.review_binding = dict(pydantic_cls._binding)
        result.transport["attempt"] = args.expected_attempt
    _common.record_wrapper_paths(result, args.prompt_file, cwd, process_cwd)
    _common.log(
        f"[wrapper] antigravity {result.classification} "
        f"exit={result.exit_code} vendor={result.vendor_exit_code} "
        f"elapsed={result.elapsed_s:.1f}s"
    )
    _common.persist_result_artifacts(
        "antigravity",
        sys.argv,
        cmd,
        prompt,
        result,
        debug=args.debug,
    )

    if result.exit_code == _common.EXIT_OK:
        sys.stdout.write(result.final_answer)
        if result.final_answer and not result.final_answer.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
