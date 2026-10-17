"""Shared helpers for codex/gemini subprocess wrappers.

Framework — vendor-JSON IO + 5-class classification + noise-tag extraction
+ optional local pydantic validation after one CLI inference invocation.

Per-CLI vendor JSON modes (always on):
- Codex: `codex exec --json -o <last_msg> --ephemeral`
  (native user/project permissions remain in effect; no wrapper approval override)
  → stdout = JSONL events stream (vendor schema), stderr ≈ 39 B (vendor quiet).
- Gemini: `gemini -p ... --output-format json`
  → stdout = single JSON object {response, stats, error}, stderr ≈ 189 B.

Schema enforcement (`--pydantic module:Class`) uses the verified prompt-side
few-shot pattern: JSON-only instruction + shape line
+ dummy example + USER REQUEST. Vendor settings.json `responseSchema` path
NOT used (Issue #13388 = open / settings silent-ignored).

Audit log schema = the RunResult dataclass + audit() body. There is no
separate schema file. _logs/<cli>/audit.jsonl is the output; bounded runtime
rotation owns its cleanup.
"""
from __future__ import annotations

import fcntl
import errno
import importlib
import io
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

# pydantic optional — only required when --pydantic flag is given.
try:
    from pydantic import BaseModel  # type: ignore
    PYDANTIC_OK = True
except ImportError:  # pragma: no cover
    BaseModel = None  # type: ignore
    PYDANTIC_OK = False


# ─── Exit codes ────────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_CLI_FAIL = 1
EXIT_TIMEOUT = 2
EXIT_ARG_ERROR = 3
EXIT_BINARY_MISSING = 4
EXIT_RATE_GIVE_UP = 64   # capacity failure; historical constant name retained
EXIT_TERMINAL = 65       # cli-sub-cap / token-limit / oauth-env → user escalate
EXIT_SCHEMA_FAIL = 66    # local pydantic validation failed
EXIT_SCHEMA_REJECTED = 67  # codex refused --output-schema at submit (massage/strict-rule drift)


def map_classification_to_exit(cls: str) -> int:
    """Map a classify() result string to a wrapper EXIT_* code (pure helper)."""
    return {
        "ok": EXIT_OK,
        "server-capacity": EXIT_RATE_GIVE_UP,
        "cli-subscription-cap": EXIT_TERMINAL,
        "token-limit": EXIT_TERMINAL,
        "oauth-env": EXIT_TERMINAL,
        "timeout": EXIT_TIMEOUT,
        "extraction-error": EXIT_CLI_FAIL,
        "schema-fail": EXIT_SCHEMA_FAIL,
        "schema-rejected": EXIT_SCHEMA_REJECTED,
        "config-conflict": EXIT_TERMINAL,
        "task-blocked": EXIT_TERMINAL,
        "vendor-error": EXIT_TERMINAL,  # current AGY stream driver emission; surface, never classifier-patched
        "admission-refused": EXIT_TERMINAL,  # shared table parity; not currently emitted by this host
        "vendor-timeout": EXIT_TERMINAL,  # AGY typed terminal turn-timeout; not a wrapper deadline
        "input-delivery-failed": EXIT_TERMINAL,  # shared table parity; retain existing stdin phase handling
        "truncated-answer": EXIT_TERMINAL,  # AGY own-line marker in the emitted raw answer
        "permission-unavailable": EXIT_TERMINAL,  # inert legacy exit-map compatibility alias; current stream driver never emits it
        "unknown": EXIT_CLI_FAIL,
    }.get(cls, EXIT_CLI_FAIL)


# ─── Pattern lists (seed — extended only through validated proposals) ─────
# Lowercase substring match. Terminal-first ordering when classifying.

SERVER_CAPACITY_PATTERNS: tuple[str, ...] = (
    "model_capacity_exhausted",
    "resource_exhausted",
    "ratelimitexceeded",
    "model overloaded",
    "overloaded_error",  # 2026-07-05: Anthropic 529 overload api_error_status enum,
    # surfaced by extract_claude_answer into the claude is_error ext_err blob
    # (`is_error=true (api_error_status=overloaded_error): ...`). Capacity outcome.
    # Specific vendor enum token — passes the false-positive guard (unlike bare
    # `"529"`). `rate_limit_error` (429) deliberately NOT added: it is ambiguous
    # between a transient rate limit (retry) and a subscription cap (terminal),
    # and mis-routing it either way costs a wasted cycle.
    # `"503"`, `"429"` removed 2026-05-03 (later-2): standalone numeric matched
    # natural occurrences in answer text (line numbers, byte counts, timestamps,
    # spec docs e.g. "see RFC 429"). The phrase forms below already cover real
    # capacity errors. If a future failure surfaces a status-only stderr without
    # the phrase form, add a more specific substring (e.g. `"http 429"`,
    # `"status: 503"`) — never bare `"429"` / `"503"`.
    "service unavailable",
    "too many requests",
    "aborterror",  # 2026-05-02: Gemini CLI _recoverFromLoop tool-call loop detection abort; transient — retry eligible. github.com/google-gemini/gemini-cli/issues/23509
)

CLI_SUB_CAP_PATTERNS: tuple[str, ...] = (
    "your quota will reset after",
    "5h limit reached",
    "weekly limit reached",
    "subscription limit reached",
    "usage limit reached",
    "no longer supported for gemini code assist for individuals",
    # L10 union (twin→SoT 2026-07-05): the raw error CLASS token — distinctive,
    # exception-name form, FP-safe. (Twin's third token "migrate to antigravity"
    # was DROPPED: prose form could match ordinary migration discussions.)
    "ineligibletiererror",  # 2026-06-30: IneligibleTierError — Gemini Code Assist individuals tier discontinued 2026-06-18; user must migrate to Antigravity. github.com/google-gemini/gemini-cli/discussions/28017
)

# Documented vendor sentences apply only to their emitting CLI (C43 / DL-117,
# triad-dispatch-spec 3afc4d7, contracts/vendor-failure-lines.json).
# The AGY credits wording is vendor-sourced; its runtime carrier is unmeasured.
CLI_PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    "codex": {
        "CLI_SUB_CAP_PATTERNS": ("you've hit your usage limit",),
    },
    "antigravity": {
        "CLI_SUB_CAP_PATTERNS": ("your ai credits balance is too low to continue",),
    },
}

TOKEN_LIMIT_PATTERNS: tuple[str, ...] = (
    "payload size exceeds",
    "token count exceeds",
    # `"context window"`, `"maximum context"` removed 2026-05-03 (later-2):
    # generic LLM jargon that naturally appears in answer text (e.g. user asks
    # "explain context window in Claude" → response text matches the substring
    # → token-limit mis-classify on otherwise OK call). Replaced with the
    # exceeded-form which only appears in real token-limit errors.
    "context window exceeded",
    "exceeds maximum context",
    "context length exceeded",
    "400 bad request",
    "400 invalid",
)

SCHEMA_REJECTED_PATTERNS: tuple[str, ...] = (
    "invalid output schema",
    "output schema rejected",
    "schema validation failed",
    "unsupported schema",
    # NOTE: bare "schema" is NOT added — it appears in normal answer text.
    # Only schema-REJECTION phrases (submit-time refusal) belong here.
)

# Config-alive: an inherited ~/.codex config that breaks the call.
# Phrases are anchored to "config.toml" to avoid false positives on
# natural answer text (bare "invalid profile" / "unknown config key"
# are too broad — same guard that removed "401"/"oauth"/"context window").
CONFIG_CONFLICT_PATTERNS: tuple[str, ...] = (
    "failed to parse config.toml",
    "error loading config.toml",
    "invalid config.toml",
)

# SEMANTIC classification of stderr (tool-not-installed / vendor warning /
# normal chatter) is the LEADER's job (the AI that receives the mirrored
# stderr via its shell tool). The wrapper only records raw stderr into the
# audit log; the leader judges it and alerts the user. The dispatch SKILLs
# carry the stderr-interpretation guidance.


# ─── Vendor exit code maps (measured captures or official vendor source) ───
# Unknown codes remain repair-routed. Gemini authentication code 41 is a
# source-confirmed carrier handled before timeout and every map/phrase rung.

ANTIGRAVITY_VENDOR_EXIT_MAP: dict[int, str] = {
    0: "extraction-error",  # 2026-06-25: agy rc=0 + no-sentinel (answer present, sentinel not emitted);
                            # classify() is called ONLY on the no-answer path so rc=0 + no-sentinel
                            # → extraction-error is safe (answer-present path returns "ok" before calling classify).
                            # Source: run-log 20260625T082029Z-98429-e4610255.json (vendor_exit_code=0,
                            # extraction_error=no-sentinel, full Korean answer in stdout, classification=unknown).
}
# Populated only from empirically observed Antigravity behavior.

# AGY own-line banner carrier; never a whole-output substring match.
AGY_AUTH_BANNER_PATTERNS = ("authentication required. please visit the url",)


# ─── Audit rotation policy ────────────────────────────────────────────────
# audit.jsonl is append-only operational telemetry, so it must be bounded too.
# Rotate the active file after append once it crosses 10 MB, then keep at most
# five archives / 50 MB per CLI. The per-call run-log remains the detailed IPC
# artifact; audit is durable routing telemetry, not an unbounded datastore.
AUDIT_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MB
AUDIT_MAX_ARCHIVES = 5
AUDIT_ARCHIVE_MAX_BYTES = AUDIT_ROTATE_BYTES * AUDIT_MAX_ARCHIVES


# ─── Run-log policy (per-execution artifact, dispatch-SKILL input) ────────
# Separate from audit.jsonl: one file per FAILED call (rc != 0) at
# _logs/<cli>/runs/<UTC-ts>-<pid>-<uuid8>.json; successes create no file. The
# dispatch skill keeps the path opaque and passes it only to the fresh native
# proposal-only child with prompt-controlled no-edit behavior, isolating large
# vendor stdout and arbitrary characters from prompt transport.
# 2-layer cleanup:
#   Primary  = later normal-dispatch age-floor cleanup removes old files
#   Failsafe = cap pruning removes eligible files oldest-first
_RUN_LOG_MAX_FILES = 100
_RUN_LOG_MAX_BYTES = 20 * 1024 * 1024  # 20 MB total cap


@dataclass
class RunResult:
    exit_code: int                  # wrapper-normalized (0/1/2/3/4/64/65/66)
    stdout: str
    stderr: str
    elapsed_s: float
    classification: str = "ok"
    mode: str = "normal"            # normal | repair | schema_repair
    repair_attempt: int = 0
    # Final-answer + schema layer
    final_answer: str = ""
    validated: Optional[dict] = None
    schema_repair_attempt: int = 0
    extraction_error: Optional[str] = None
    validation_error: Optional[str] = None
    # Vendor raw exit code retained as evidence for unobserved classifications.
    vendor_exit_code: int = -1
    # Provider/result custody phase when the selected route exposes one.
    dispatch_phase: Optional[str] = None
    # Provider-exposed runtime identity, or ``unexposed`` when the route omits it.
    runtime_identity: Optional[str] = None
    # Common observed transport; shared unchanged by audit and failure IPC.
    transport: Optional[dict] = None
    # Explicit v2 invocation custody; absent for legacy and raw investigations.
    review_binding: Optional[dict] = None
    # Private failure controls remain distinct from the public delivery observation.
    _stdin_delivery_failed: bool = False
    _output_transport_failed: bool = False
    # Sanitized Claude audit evidence only; excluded from failure/repair IPC.
    _claude_receipt: Optional[dict] = None
    # Host-resolved launch cwd; audit-only evidence, not provider attestation.
    _effective_cwd: Optional[str] = None
    _resolved_prompt_file: Optional[str] = None
    # Diagnostic event projection only; excluded from result/repair IPC.
    _agy_read_telemetry: Optional[dict] = None


# ─── Helpers ──────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        try:
            fd = sys.stderr.fileno()
        except (AttributeError, io.UnsupportedOperation):
            sys.stderr.write(line)  # In-memory display/test sinks have no fd.
        else:
            os.write(fd, line.encode("utf-8", errors="replace"))
    except (OSError, ValueError):
        pass  # Best effort: never buffer/replay a failed line or replace a result.


def transport_receipt(cli: str, result: RunResult) -> dict:
    """Expose observations only; custom/unobserved transports are not inferred."""
    if result.transport is None:
        return {
            "schema_version": 2,
            "stdin_delivery": "unexposed",
            "route": "agy" if cli == "antigravity" else cli,
            "binary": None,
            "cli_version": None,
            "attempt": 1,
        }
    return result.transport


PINNED_ENV_VARS: dict[str, str] = {
    "codex": "TRIAD_CODEX_BIN",
    "claude": "TRIAD_CLAUDE_BIN",
    "gemini": "TRIAD_GEMINI_BIN",
    "antigravity": "TRIAD_AGY_BIN",
    "agy": "TRIAD_AGY_BIN",
}


def require_binary(name: str) -> str:
    """Resolve the vendor binary, honoring an install-time pin (finding #3).

    A codex-host launcher execs the wrapper with
    `TRIAD_<name.upper()>_BIN=<resolved absolute path>` and
    `TRIAD_REQUIRE_PINNED_VENDOR=1`, so a workspace-planted `<name>` earlier on
    PATH cannot shadow the real vendor CLI an approval-reviewed, policy-matched
    launcher executes.
    Lab default (neither env set) = `shutil.which` (PATH), unchanged.

    - a valid pin (absolute, existing, executable) always wins over PATH;
    - `TRIAD_REQUIRE_PINNED_VENDOR=1` with the pin unset OR invalid fails closed
      (`EXIT_BINARY_MISSING`) — NEVER a silent PATH fallback (that is the vuln);
      - an invalid pin WITHOUT the require flag falls through to PATH (lab convenience).
    """
    pin_var = PINNED_ENV_VARS.get(name, f"TRIAD_{name.upper()}_BIN")
    pin = os.environ.get(pin_var)
    if pin is None and name == "antigravity":
        pin = os.environ.get("TRIAD_ANTIGRAVITY_BIN")
    require_pinned = os.environ.get("TRIAD_REQUIRE_PINNED_VENDOR") == "1"
    if pin:
        if os.path.isabs(pin) and os.path.isfile(pin) and os.access(pin, os.X_OK):
            return pin
        log(
            f"pinned vendor binary {pin_var} is not an executable "
            f"absolute path: {pin}"
        )
        if require_pinned:
            sys.exit(EXIT_BINARY_MISSING)
    elif require_pinned:
        log(
            f"TRIAD_REQUIRE_PINNED_VENDOR=1 but {pin_var} is unset "
            f"for '{name}' — refusing PATH fallback"
        )
        sys.exit(EXIT_BINARY_MISSING)
    path = shutil.which(name)
    if not path:
        log(f"binary '{name}' not found on PATH")
        sys.exit(EXIT_BINARY_MISSING)
    return path


def resolve_binary(name: str) -> str:
    """Resolve the vendor binary, honoring an install-time pin."""
    return require_binary(name)


def _classifier_extension_path() -> Path:
    """Persistent, env-independent location for the user-writable classifier
    extension. Distributed plugin self-improvement persists HERE (user home),
    not in the ephemeral plugin dir. `TRIAD_CLASSIFIER_EXTENSION` overrides
    (tests / custom location)."""
    override = os.environ.get("TRIAD_CLASSIFIER_EXTENSION")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "agy-triad-dispatch" / "classifier-patches.json"


def _load_classifier_extension() -> dict:
    """Load + SANITIZE the user classifier extension. Shape:
        { "<cli>": { "vendor_exit_map": {"<int-str>": "<class-str>"},
                     "patterns": {"<LIST_NAME>": ["<substr>", ...]} } }
    This file is trusted user-curated input, but it is hand/agent-editable, so the
    loader is defensive: any wrong-typed node is dropped (never propagated into
    classify()). Missing / unreadable / corrupt / non-dict -> {}. A
    structurally-malformed-but-valid-JSON file yields only its well-typed entries,
    so classify() can never raise on it."""
    p = _classifier_extension_path()
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict = {}
    for cli, entry in data.items():
        if not isinstance(entry, dict):
            continue
        cleaned: dict = {}
        vmap = entry.get("vendor_exit_map")
        if isinstance(vmap, dict):
            cleaned["vendor_exit_map"] = {}
            for k, v in vmap.items():
                if not isinstance(v, str):
                    continue
                if v not in VENDOR_EXIT_PROPOSAL_CLASSES:
                    log(f"ignored {cli} extension exit {k!r}: unsupported class {v!r}")
                    continue
                try:
                    zero = int(k) == 0
                except (TypeError, ValueError):
                    zero = False
                if zero:
                    log(f"ignored {cli} extension exit {k!r}: zero is not a failure")
                    continue
                cleaned["vendor_exit_map"][k] = v
        pats = entry.get("patterns")
        if isinstance(pats, dict):
            cleaned["patterns"] = {
                name: [s for s in lst if isinstance(s, str)]
                for name, lst in pats.items()
                if isinstance(lst, list) and name != "OAUTH_ENV_PATTERNS"
            }
            if "OAUTH_ENV_PATTERNS" in pats:
                log("ignored raw authentication extension OAUTH_ENV_PATTERNS")
        if cleaned:
            clean[cli] = cleaned
    return clean


# ─── Product hardening mode (L8 twin→SoT port, owner adjudications 2026-07-05) ───
# The lab (SoT callers, skill contracts) runs UNRESTRICTED by default.
# Compatibility callers may explicitly set TRIAD_WRAPPER_HARDENED=1 and pin
# TRIAD_WRAPPER_ALLOWED_ROOTS to activate allowed-roots containment, the
# pydantic import gate, and audit prompt redaction. Each control also has an
# individual env so it can be engaged on its own.
# Bootstrap does not activate these compatibility controls.

def _wrapper_hardened() -> bool:
    return os.environ.get("TRIAD_WRAPPER_HARDENED") == "1"


def _audit_redact_enabled() -> bool:
    return _wrapper_hardened() or os.environ.get("TRIAD_AUDIT_REDACT_PROMPTS") == "1"


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False



def runtime_allowed_roots() -> list[Path]:
    """Containment roots for --cwd / --prompt-file. Env unset → NO containment
    in the lab (callers own isolation per the SKILL contracts); under
    TRIAD_WRAPPER_HARDENED=1 the env is REQUIRED (refuse rather than guess —
    an explicit compatibility caller must pin it; a hardened run without
    pinned roots must not silently fall back to cwd)."""
    raw = os.environ.get("TRIAD_WRAPPER_ALLOWED_ROOTS", "")
    if not raw:
        if _wrapper_hardened():
            raise ValueError(
                "TRIAD_WRAPPER_HARDENED=1 requires TRIAD_WRAPPER_ALLOWED_ROOTS "
                "(colon-separated absolute paths)")
        return []
    roots = []
    for item in raw.split(os.pathsep):
        if not item:
            continue
        path = Path(item).expanduser()
        if not path.is_absolute():
            raise ValueError(
                "TRIAD_WRAPPER_ALLOWED_ROOTS entries must be absolute paths")
        roots.append(path.resolve(strict=False))
    result: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        text = str(root)
        if text in seen:
            continue
        seen.add(text)
        result.append(root)
    return result


def _ensure_within_runtime_roots(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    roots = runtime_allowed_roots()
    if not roots:
        return resolved          # lab default: no containment
    if not any(_path_is_within(resolved, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ValueError(f"{label} must be under an allowed runtime root: {allowed}")
    return resolved


def load_prompt_text(
    prompt: Optional[str], prompt_file: Optional[str], *, process_cwd: Path | None = None
) -> str:
    """Load text or a UTF-8 file relative to the wrapper's entry directory.

    argparse enforces the XOR at the CLI; this re-check is defense-in-depth
    for direct callers."""
    if prompt is not None and prompt_file:
        raise ValueError("--prompt and --prompt-file are mutually exclusive")
    if prompt is not None:
        return prompt
    if not prompt_file:
        raise ValueError("either --prompt or --prompt-file is required")
    path = Path(prompt_file).expanduser()
    if not path.is_absolute():
        path = (process_cwd if process_cwd is not None else Path.cwd()) / path
    resolved = _ensure_within_runtime_roots(path, "--prompt-file")
    if not resolved.is_file():
        raise ValueError(f"--prompt-file must be a file: {resolved}")
    return resolved.read_text(encoding="utf-8")



def validate_wrapper_cwd(cwd: Optional[str], *, process_cwd: Path | None = None) -> Optional[str]:
    """Validate a vendor cwd without expanding the no-prompt trust boundary."""
    if not cwd:
        return None
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        path = (process_cwd if process_cwd is not None else Path.cwd()) / path
    resolved = _ensure_within_runtime_roots(path, "--cwd")
    if not resolved.is_dir():
        raise ValueError(f"--cwd must be an existing directory: {resolved}")
    return str(resolved)


def input_path_error(label: str, value: str | None, process_cwd: Path | None, error: Exception) -> str:
    """Report the resolved candidate through the existing audit privacy mode."""
    if _audit_redact_enabled():
        return f"{label} validation failed: {type(error).__name__}; candidate=<redacted:path>"
    if process_cwd is None:
        return f"{label} validation failed: {type(error).__name__}; entry cwd unavailable"
    try:
        path = Path(value).expanduser() if value else process_cwd
        candidate = (path if path.is_absolute() else process_cwd / path).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return f"{label} validation failed: {type(error).__name__}"
    return f"{label} validation failed: {error}; candidate={candidate}"


def validate_extra_directories(values: list[str], *, process_cwd: Path) -> list[str]:
    """Resolve explicitly supplied raw-call inputs with existing containment."""
    resolved = []
    for value in values:
        if not value.strip():
            raise ValueError("--add-dir requires a nonempty directory")
        path = validate_wrapper_cwd(value, process_cwd=process_cwd)
        if path not in resolved:
            resolved.append(path)
    return resolved


def record_wrapper_paths(result: RunResult, prompt_file: str | None, cwd: str | None,
                         process_cwd: Path) -> None:
    if prompt_file is not None:
        path = Path(prompt_file).expanduser()
        result._resolved_prompt_file = str((path if path.is_absolute() else process_cwd / path).resolve())
    if result.exit_code == EXIT_OK:
        result._effective_cwd = result._effective_cwd or cwd or str(process_cwd)
        redact = _audit_redact_enabled()
        prompt_path = ("<redacted:prompt-file-path>" if redact and prompt_file is not None
                       else result._resolved_prompt_file)
        child_path = "<redacted:cwd-path>" if redact else result._effective_cwd
        log(f"resolved_prompt_file={prompt_path} effective_cwd={child_path}")


def validate_review_web(args, prompt: str) -> None:
    """A review flag is the caller's attestation of its bound owner request."""
    from validate_v2 import _json
    prefixes = ("Review metadata: ", "Review v2 metadata: ")
    records = [line[len(prefix):] for line in prompt.splitlines()
               for prefix in prefixes if line.startswith(prefix)]
    if not records and not args.web:
        return  # Existing no-web callers may supply their own prompt.
    if len(records) != 1:
        raise ValueError("review web authorization requires one bound metadata record")
    metadata = _json(records[0])
    authorized = metadata.get("review_web_authorized", False) if isinstance(metadata, dict) else None
    if type(authorized) is not bool or authorized != args.web:
        raise ValueError("--web must match bound review_web_authorized")
    fields = ["review_id", "family", "content_digest"]
    if args.pydantic in PACKAGED_V2_VERDICT_SPECS:
        fields += ["leg_name", "attempt", "route"]
    for field in fields:
        expected = getattr(args, "expected_" + field)
        if field == "route" and expected == "null":
            expected = None
        if (field not in metadata or type(metadata[field]) is not type(expected)
                or metadata[field] != expected):
            raise ValueError("review web authorization binding mismatch: " + field)


def load_web_evidence_clause(path: Path, *, gemini: bool = False) -> str:
    document = path.read_text(encoding="utf-8")
    clauses = re.findall(r"^```text\n(.*?)\n```(?:\n|$)", document, re.MULTILINE | re.DOTALL)
    if document.count("```text") != 1 or len(clauses) != 1 or not clauses[0].strip():
        raise ValueError("expected one nonempty terminated text clause")
    clause = clauses[0]
    return (clause.replace("search_web", "google_web_search").replace("read_url_content", "web_fetch")
            if gemini else clause)


def _redact_prompt_args(cmd: list[str]) -> list[str]:
    """Keep argv shape in durable audit logs without storing prompt payloads."""
    redacted: list[str] = []
    redact_next: str | None = None
    for arg in cmd:
        if redact_next is not None:
            if redact_next in {"prompt", "schema"}:
                redacted.append(f"<redacted:{len(arg)} chars>")
            elif redact_next == "input-directory":
                redacted.append("<redacted:input-directory-path>")
            else:
                redacted.append("<redacted:prompt-file-path>")
            redact_next = None
            continue
        if arg in {"-p", "--prompt"}:
            redacted.append(arg)
            redact_next = "prompt"
            continue
        if arg == "--prompt-file":
            redacted.append(arg)
            redact_next = "prompt-file"
            continue
        if arg in {"--add-dir", "--include-directories"}:
            redacted.append(arg)
            redact_next = "input-directory"
            continue
        if arg == "--json-schema":
            redacted.append(arg)
            redact_next = "schema"
            continue
        if arg.startswith("--prompt="):
            value = arg.split("=", 1)[1]
            redacted.append(f"--prompt=<redacted:{len(value)} chars>")
            continue
        if arg.startswith("--prompt-file="):
            redacted.append("--prompt-file=<redacted:prompt-file-path>")
            continue
        if arg.startswith("--json-schema="):
            value = arg.split("=", 1)[1]
            redacted.append(f"--json-schema=<redacted:{len(value)} chars>")
            continue
        redacted.append(arg)
    if redact_next is not None:
        redacted.append("<redacted:missing-value>")
    return redacted



def _authentication_exit(cli: str, vendor_exit_code: int | None) -> bool:
    """Measured code carrier; no text or other CLI borrows Gemini's code."""
    return cli == "gemini" and vendor_exit_code == 41


# R-CLASSIFY/C37: vocabulary applies only inside measured vendor error carriers.
# Shapes and captures: shared contracts/vendor-failure-lines.json and review-rules.
_AUTH_VOCABULARY = re.compile(
    r"(?<![a-z])api[ _-]?key"
    r"|\b(?:unauthori[sz]ed|unauthenticated|401|not logged in"
    r"|log ?in|sign ?in|log ?out|authenticat\w*|credentials?"
    r"|(?:auth|access|refresh|session|bearer|oauth) token|token data"
    r"|token (?:is |has |has been )?expired|expired (?:token|session)"
    r"|session (?:has )?expired|could not be refreshed|credit balance)\b", re.I)
_AGY_SCHEMA_REPORT_RE = re.compile(r"^(?=.*\bschema\b)(?=.*\bvalidat)", re.I)
_AGY_PRINT_TIMEOUT_RE = re.compile(
    r"^\[agy\] print timeout after \S+ with turn in progress; "
    r"returning partial output[ \t\r]*$", re.I | re.M)


def agy_print_timed_out(stderr: str) -> bool:
    """C43/DL-62: the measured own stderr line marks an incomplete answer."""
    return bool(_AGY_PRINT_TIMEOUT_RE.search(stderr))


def _error_document(text: str) -> dict:
    try:
        obj = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _error_events(text: str) -> list[dict]:
    # JSON strings may contain raw Unicode line separators: split only on LF.
    return [obj for line in text.split("\n") if (obj := _error_document(line))]


def _gemini_error_envelope(stderr: str) -> dict:
    # Gemini errors.ts/nonInteractiveCli.ts: [ERROR] {json} or a pretty object
    # at the end of stderr. Do not discover objects embedded in other prose.
    starts = list(re.finditer(r"(?m)^[ \t]*(?:\[ERROR\][ \t]*)?(?=\{)", stderr))
    for match in reversed(starts):
        obj = _error_document(stderr[match.end():])
        if obj:
            return obj
    return {}


def _auth_carrier_stop(cli: str, stderr: str, stdout: str,
                       vendor_exit_code: int | None = None) -> bool:
    """Read original streams of a failed/no-answer call, never flattened text."""
    def said(text):
        return isinstance(text, str) and bool(_AUTH_VOCABULARY.search(text))

    # Text-mode pipes normalize CR on stderr. JSON message strings keep CR.
    lines = (stderr or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if _authentication_exit(cli, vendor_exit_code):
        return True
    if cli == "claude":
        obj = _error_document(stdout)
        if obj.get("structured_output") is not None or obj.get("is_error") is not True:
            return False
        return obj.get("api_error_status") == 401 or said(obj.get("result"))
    if cli == "codex":
        events = _error_events(stdout)
        completed = any(e.get("type") == "turn.completed" for e in events)
        failed = any(e.get("type") == "turn.failed" for e in events)
        answered = any(e.get("type") == "item.completed" and
                       isinstance(item := e.get("item"), dict) and
                       item.get("type") == "agent_message" and
                       isinstance(item.get("text"), str) and item["text"].strip()
                       for e in events)
        if vendor_exit_code == 0 and completed and answered and not failed:
            return False
        if any(line.lower().startswith("error loading configuration:") and said(line)
               for line in lines):
            return True
        for event in events:
            if event.get("type") == "error" and said(event.get("message")):
                return True
            error = event.get("error")
            if (event.get("type") == "turn.failed" and isinstance(error, dict)
                    and said(error.get("message"))):
                return True
    elif cli == "gemini":
        if any(line.lower().startswith("cached credentials are not valid:") for line in lines):
            return True
        for obj in (_error_document(stdout), _gemini_error_envelope(stderr)):
            error = obj.get("error")
            if "type" in obj or not isinstance(error, dict):
                continue
            code = error.get("code")
            if type(code) is int and code in (41, 401):
                return True
            message = error.get("message")
            if isinstance(message, str) and message.lower().startswith("error executing tool "):
                continue
            if said(message):
                return True
    elif cli == "antigravity":
        events = _error_events(stdout)
        results = [e["result"] for e in events if e.get("event") == "result"
                   and isinstance(e.get("result"), dict)]
        # A completed usable answer is not stopped by a sign-in banner.
        if (vendor_exit_code == 0 and results and results[-1].get("status") == "SUCCESS"
                and not agy_print_timed_out(stderr)
                and (results[-1].get("structured_output") is not None or
                     isinstance(results[-1].get("response"), str) and results[-1]["response"].strip())):
            return False
        extra = _load_classifier_extension().get(cli, {}).get("patterns", {})
        banners = AGY_AUTH_BANNER_PATTERNS + tuple(extra.get("AGY_AUTH_BANNER_PATTERNS", ()))

        def banner(text):
            return any(line.lower().startswith(banners) for line in text.split("\n"))

        if banner("\n".join(lines)):
            return True
        for result in results:
            error = result.get("error")
            if isinstance(error, dict):
                error = error.get("message")
            if not isinstance(error, str):
                continue
            first = next((line for line in error.split("\n") if line.strip()), "")
            if first.startswith("permission check failed for command "):
                continue  # C74: the entire denied command is reflected model text.
            if _AGY_SCHEMA_REPORT_RE.match(first):
                if banner(error):
                    return True
            elif said(error):
                return True
    return False


def classify_extraction(cli: str, result: RunResult, error: str) -> str:
    """Select auth from captured carriers; keep other classes on extracted error."""
    if _auth_carrier_stop(cli, result.stderr, result.stdout, result.vendor_exit_code):
        return "oauth-env"
    return classify(cli, error, "", EXIT_CLI_FAIL, vendor_exit_code=result.vendor_exit_code)


def classify(
    cli: str,
    stderr: str,
    stdout: str,
    exit_code: int,
    vendor_exit_code: Optional[int] = None,
    *,
    agy_result: Optional[dict] = None,
) -> str:
    """Success, authentication carrier STOP, timeout, then other failures.
      L1 — measured vendor exit code map
      L2 — non-authentication substring fallback
      L3 — "unknown" (repair-routed classification signal)

    `vendor_exit_code` is the raw CLI subprocess exit (e.g. 7, 130). When
    omitted, falls back to `exit_code` for legacy callers. Pass the raw exit
    explicitly for measured vendor-code mappings in the classifier extension.
    """
    if exit_code == 0:
        return "ok"
    raw = vendor_exit_code if vendor_exit_code is not None else exit_code
    if _auth_carrier_stop(cli, stderr, stdout, raw):
        return "oauth-env"
    # Wrapper-level timeout: do NOT fall through to L2 substring matching.
    # Partial stderr captured before SIGTERM often contains capacity-class
    # phrases (Gemini "_OAuth2Client.requestAsync" + retry chatter, Codex
    # mid-stream events) which would mis-classify a hung call as transient
    # and trigger a 3× full-timeout retry (worst case 46 min @ timeout=900s).
    # Vendor's own retry logic already ran inside that timeout window —
    # wrapper retry on top is redundant. Surface as "timeout" → fail-fast.
    # 2026-05-03 (later-3) framework gap fix.
    if exit_code == EXIT_TIMEOUT:
        return "timeout"
    # L1 — vendor exit code map (empirical only). Use vendor_exit_code when
    # available; legacy callers fall back to exit_code (dead-code path).
    _ext = _load_classifier_extension().get(cli, {})
    _ext_vmap = {}
    for _k, _v in _ext.get("vendor_exit_map", {}).items():
        try:
            _ext_vmap[int(_k)] = _v
        except (TypeError, ValueError):
            pass
    _ext_pat = _ext.get("patterns", {})

    def _p(name, builtin):
        """Shared patterns, CLI-specific built-ins, then CLI extensions."""
        own = CLI_PATTERNS.get(cli, {}).get(name, ())
        extra = _ext_pat.get(name, ())
        return tuple(builtin) + tuple(own) + tuple(extra)

    vmap = ANTIGRAVITY_VENDOR_EXIT_MAP if cli == "antigravity" else {}
    vmap = {**_ext_vmap, **vmap}
    # A vmap entry of "extraction-error" is a WEAK no-answer fallback (e.g.
    # ANTIGRAVITY_VENDOR_EXIT_MAP[0], 2026-06-25 repair patch): the specific
    # carrier STOP and remaining capacity / sub-cap / token-limit classes
    # must keep winning — an early return here swallowed ALL of them on the
    # agy no-answer path (t14/t15/f9 regression found on the 2026-07-04
    # backport pass). The weak entry replaces only the terminal "unknown", so
    # a pattern-less no-sentinel answer still routes to repair as
    # extraction-error instead of unknown.
    _weak_fallback = None
    if raw in vmap and vmap[raw] != "ok":
        if vmap[raw] == "extraction-error":
            _weak_fallback = "extraction-error"
        else:
            return vmap[raw]
    # Remaining non-authentication classes retain their existing priority.
    blob = ((stderr or "") + "\n" + (stdout or "")).lower()
    stderr_blob = (stderr or "").lower()
    if any(p in blob for p in _p("CLI_SUB_CAP_PATTERNS", CLI_SUB_CAP_PATTERNS)):
        return "cli-subscription-cap"
    # C43/DL-101: only the measured typed AGY network carrier joins this rung.
    # Do not concatenate arbitrary result errors (including unmeasured quotas).
    agy_network = (
        cli == "antigravity" and raw == 1 and isinstance(agy_result, dict)
        and agy_result.get("status") == "ERROR" and agy_result.get("response") == ""
        and isinstance(agy_result.get("error"), str)
        and "network issue connecting to the server" in agy_result["error"].lower()
    )
    if agy_network or any(p in blob for p in _p("SERVER_CAPACITY_PATTERNS", SERVER_CAPACITY_PATTERNS)):
        return "server-capacity"
    if any(p in blob for p in _p("TOKEN_LIMIT_PATTERNS", TOKEN_LIMIT_PATTERNS)):
        return "token-limit"
    # schema-rejected checked LAST in L2 — capacity/terminal classes win.
    # submit-time --output-schema refusal: surfaced to caller as terminal-like,
    # not mapped to a repair-routed classification.
    if any(p in blob for p in _p("SCHEMA_REJECTED_PATTERNS", SCHEMA_REJECTED_PATTERNS)):
        return "schema-rejected"
    if any(p in stderr_blob for p in _p("CONFIG_CONFLICT_PATTERNS", CONFIG_CONFLICT_PATTERNS)):
        return "config-conflict"
    # L3 — weak vmap fallback (extraction-error) wins over the repair-dispatch
    # "unknown" ONLY when no L2 class matched.
    return _weak_fallback or "unknown"


# ─── Pydantic helpers ─────────────────────────────────────────────────────

PACKAGED_VERDICT_SPECS = frozenset({
    "verdict_schema:LegVerdict", "verdict_schema.LegVerdict",
})
PACKAGED_V2_VERDICT_SPECS = frozenset({
    "verdict_v2:LegVerdict", "verdict_v2.LegVerdict",
})
REVIEW_BINDING_ARGS: tuple[str, ...] = (
    "expected_review_id",
    "expected_family",
    "expected_content_digest",
    "expected_leg_name",
    "expected_attempt",
    "expected_route",
)


def _load_packaged_verdict_class(*, v2: bool = False):
    """Load the shipped verdict model from its exact sibling path."""
    name = "verdict_v2" if v2 else "verdict_schema"
    source = Path(__file__).resolve(strict=True).with_name(name + ".py")
    if source.is_symlink() or not source.is_file():
        raise ImportError("packaged verdict schema must be a regular sibling file")
    module_name = "_triad_packaged_" + name
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ImportError("unable to load packaged verdict schema")
    module = importlib.util.module_from_spec(spec)
    prior = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if prior is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = prior
        raise
    return module.LegVerdict


def load_pydantic_class(spec: str):
    """Parse 'module.path:ClassName' or 'module.path.ClassName' →
    pydantic BaseModel subclass.

    Raises ImportError / AttributeError / TypeError on failure.
    """
    if not PYDANTIC_OK:
        requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
        install_command = shlex.join(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements)]
        )
        raise RuntimeError(
            "pydantic 2 is unavailable in this Python runtime; run "
            f"`{install_command}` in your normal terminal"
        )
    if spec in PACKAGED_VERDICT_SPECS | PACKAGED_V2_VERDICT_SPECS:
        cls = _load_packaged_verdict_class(v2=spec in PACKAGED_V2_VERDICT_SPECS)
        if not (
            isinstance(cls, type)
            and BaseModel is not None
            and issubclass(cls, BaseModel)
        ):
            raise TypeError(f"{spec} is not a pydantic BaseModel subclass")
        return cls
    if _wrapper_hardened() and os.environ.get("TRIAD_ALLOW_PYDANTIC_IMPORT") != "1":
        # Hardened installs (public codex-host product) must opt in explicitly:
        # --pydantic imports arbitrary Python outside the vendor sandbox.
        raise PermissionError(
            "--pydantic imports Python code outside the sandbox; under "
            "TRIAD_WRAPPER_HARDENED=1 set TRIAD_ALLOW_PYDANTIC_IMPORT=1 only "
            "for trusted schema modules")
    if ":" in spec:
        mod_path, cls_name = spec.rsplit(":", 1)
    else:
        mod_path, cls_name = spec.rsplit(".", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    if not (isinstance(cls, type) and BaseModel is not None and issubclass(cls, BaseModel)):
        raise TypeError(f"{spec} is not a pydantic BaseModel subclass")
    return cls


def _dummy_for_type(t: str) -> Any:
    return {
        "string": "<value>",
        "number": 0.0,
        "integer": 0,
        "boolean": False,
        "array": [],
        "object": {},
        "null": None,
    }.get(t, None)


def schema_block_for_prompt(
    cls,
    *,
    body_semantics_only: bool = False,
) -> str:
    """Build the generic schema injection block for a provider prompt."""
    schema = cls.model_json_schema()
    fields = schema.get("properties", {})
    required = set(schema.get("required", []))

    shape_parts = []
    dummy: dict = {}
    for name, sch in fields.items():
        t = sch.get("type", "any")
        marker = "" if name in required else "?"
        shape_parts.append(f'"{name}{marker}": <{t}>')
        dummy[name] = _dummy_for_type(t)
    shape_line = "{" + ", ".join(shape_parts) + "}"

    if body_semantics_only:
        return (
            "The JSON body must match exactly this shape:\n"
            f"{shape_line}\n\n"
            "JSON body example:\n"
            f"{json.dumps(dummy, ensure_ascii=False)}\n\n"
            "Use the user's request below to determine the JSON body values."
        )

    return (
        "You are a JSON-only response API. Your output MUST be valid JSON "
        "and nothing else. No markdown fences. No prose. No commentary. "
        "Just a single JSON object.\n\n"
        f"The JSON object must match exactly this shape:\n{shape_line}\n\n"
        "JSON output example:\n"
        f"{json.dumps(dummy, ensure_ascii=False)}\n\n"
        "Now produce the JSON output for the user's request below. "
        "Return ONLY the JSON object — no ```, no explanation."
    )


def inject_schema_to_prompt(
    prompt: str,
    cls,
    *,
    body_semantics_only: bool = False,
) -> str:
    block = schema_block_for_prompt(
        cls,
        body_semantics_only=body_semantics_only,
    )
    if body_semantics_only:
        return (
            f"{block}\n\n=== USER REQUEST ===\n"
            f"<runtime_review_input>\n{prompt}\n</runtime_review_input>"
        )
    return f"{block}\n\n=== USER REQUEST ===\n{prompt}\n\nJSON:"


def _strictify_schema_node(node: Any) -> None:
    """Recursively enforce codex (OpenAI strict structured-output) object rules:
    every object node gets `additionalProperties: false` and `required` = all
    property keys. Recurse into nested properties, array `items`, and unions.
    Mutates in place.
    """
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties", {})
        node["additionalProperties"] = False
        node["required"] = list(props.keys())
        for sub in props.values():
            _strictify_schema_node(sub)
    items = node.get("items")
    if isinstance(items, dict):
        _strictify_schema_node(items)
    for union_key in ("anyOf", "oneOf", "allOf"):
        for sub in node.get(union_key, []):
            _strictify_schema_node(sub)


def pydantic_to_codex_schema(cls) -> dict:
    """Derive a codex `--output-schema` JSON Schema from a pydantic BaseModel.

    `model_json_schema()` does not set `additionalProperties:false` or list
    every field as required; codex's strict structured-output validator
    demands both on every object. This strictifies the root and every
    `$defs` entry (nested models). `$defs`/`$ref` are kept — confirmed
    accepted by real codex 0.135.0.
    """
    schema = cls.model_json_schema()
    _strictify_schema_node(schema)
    for d in schema.get("$defs", {}).values():
        _strictify_schema_node(d)
    return schema


def strip_markdown_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        s = s[nl + 1:] if nl != -1 else ""
    if s.endswith("```"):
        s = s[:-3].rstrip()
    return s.strip()


def _check_original_json(text: str, cls) -> None:
    check = getattr(cls, "_triad_check_original_json", None)
    if check is not None:
        check(text)


def validate_response(
    answer_text: str,
    cls,
) -> Tuple[bool, Any]:
    """(ok, validated_dict_or_error_string)."""
    cleaned = strip_markdown_fences(answer_text)
    try:
        _check_original_json(cleaned, cls)
        obj = cls.model_validate_json(cleaned)
        return True, obj.model_dump(mode="json")
    except Exception as e:
        return False, str(e)


# ─── CLI-aware answer extraction (NEW) ────────────────────────────────────

def extract_gemini_answer(stdout: str, stderr: str) -> Tuple[str, Optional[str]]:
    """Gemini `--output-format json` extraction.

    Success: stdout = `{response, stats}`. Returns (response, None).
    Failure: stdout often empty, stderr ends with `{...,"error":{...}}`.
    """
    s = stdout.strip()
    if s:
        try:
            obj = json.loads(s)
            err = obj.get("error")
            if err:
                if isinstance(err, dict):
                    return "", err.get("message", str(err))
                return "", str(err)
            response = obj.get("response", "")
            if not isinstance(response, str):
                response = json.dumps(response, ensure_ascii=False)
            # Empty response is silent failure surface — vendor returned
            # valid JSON envelope but no answer. caller (run_cli_with_retry
            # line 637) propagates this to RunResult.extraction_error +
            # exit_code=EXIT_CLI_FAIL so the leader sees explicit failure
            # instead of a silent empty stdout (2026-05-03 later-2 fix).
            if not response:
                return "", "vendor JSON valid but response field empty"
            return response, None
        except Exception as e:
            return "", f"stdout is not valid JSON: {e}"

    # stdout empty — look for a trailing JSON object in stderr. Do not use
    # rfind("{"): Gemini errors are nested as {"error": {"message": ...}}, so
    # the last brace starts the INNER object, not the envelope (L5 twin→SoT
    # port, 2026-07-05 — reverse-scan raw_decode picks the outer envelope).
    decoder = json.JSONDecoder()
    starts = [idx for idx, ch in enumerate(stderr) if ch == "{"]
    for start in reversed(starts):
        candidate = stderr[start:].strip()
        try:
            obj, end = decoder.raw_decode(candidate)
        except ValueError:
            continue
        if candidate[end:].strip():
            continue
        if not isinstance(obj, dict):
            continue
        err = obj.get("error", {})
        if isinstance(err, dict):
            return "", err.get("message", str(err))
        return "", str(err)
    return "", "empty stdout and no parseable error in stderr"


def extract_claude_answer(stdout: str, stderr: str) -> Tuple[str, Optional[str]]:
    """Claude `-p ... --output-format json` extraction.

    Envelope shape (verified 2026-05-05 via spike):
      {"type": "result", "subtype": "success",
       "is_error": bool, "api_error_status": <str|null>,
       "result": "<final answer text>",
       "stop_reason": "...", "session_id": "...",
       "permission_denials": [...], "terminal_reason": "...",
       "total_cost_usd": <float>, "usage": {...}, "modelUsage": {...},
       ...}

    Success: `is_error == false` → returns (result, None).
    Failure surfaces:
      - `is_error == true` (e.g. "Not logged in", API error) → ext_err = result text
      - permission_denials non-empty → ext_err = denial summary (objective signal)
      - JSON parse fail / empty stdout → ext_err = parse description

    Markdown fence-strip safety: `--print` emits no fence (envelope = raw
    JSON) but `--agent` mode can fence-wrap (haiku pattern, recorded in the
    empirical observations). This helper strips a fence safely.
    """
    s = (stdout or "").strip()
    if not s:
        # stdout empty — claude's envelope always arrives on stdout (rc=0
        # case); stderr carries only progress/warnings. A missing envelope
        # is abnormal.
        return "", "empty stdout — claude envelope missing"

    # Fence-strip safety (--agent mode can markdown-wrap the envelope).
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()

    try:
        obj = json.loads(s)
    except Exception as e:
        return "", f"stdout is not valid JSON: {e}"
    if not isinstance(obj, dict):
        return "", "stdout JSON is not an object"

    subtype = obj.get("subtype", "")
    if subtype == "error_max_structured_output_retries":
        return "", "schema-retries-exhausted: structured output failed validation"
    structured = obj.get("structured_output")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False), None

    is_error = obj.get("is_error", False)
    result = obj.get("result", "")
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False)

    if is_error:
        # Vendor returned an envelope with is_error=true. The result field
        # carries the detailed message (e.g. "Not logged in · Please run
        # /login", an API error description). Classification uses this message.
        api_status = obj.get("api_error_status")
        prefix = f"is_error=true (api_error_status={api_status})"
        if result:
            return "", f"{prefix}: {result}"
        return "", prefix

    permission_denials = obj.get("permission_denials")
    if permission_denials and not result.strip():
        return "", (
            "task-blocked: permission_denials: "
            f"{json.dumps(permission_denials, ensure_ascii=False)}"
        )

    # A permission_denials entry = a tool block was observed (an objective
    # signal from the claude worker, not the leader's framing). With a
    # NON-EMPTY result the answer is returned first and denials are never
    # surfaced as failure; the EMPTY-result + denials case above promotes to
    # task-blocked (owner adjudication 2026-07-05 — the two rules compose).
    if not result:
        return "", "vendor JSON valid but result field empty"
    return result, None


# ─── Subprocess core ──────────────────────────────────────────────────────

# Loader / interpreter injection env vars scrubbed from the vendor child (I-2/I-3).
# `_run_once` is the one `subprocess.Popen` site for every provider wrapper.
# Every vendor child receives the same scrubbed environment, so a poisoned
# parent env cannot reach a provider CLI. The classic
# vectors: the dynamic loader (LD_PRELOAD / LD_AUDIT / the macOS DYLD_* family),
# the Node runtime (NODE_OPTIONS=--require=<evil.js> would run workspace code
# OUTSIDE any sandbox; NODE_PATH), the Python / shell / Perl / Ruby interpreters
# (PYTHONPATH / BASH_ENV / ENV / PERL5LIB / RUBYOPT ...). PATH is deliberately
# NOT scrubbed here — the vendor-binary pin (`require_binary` / `TRIAD_<CLI>_BIN`)
# fixes the vendor bin, and PATH policy belongs to the install leg, not this
# shared engine change.
_CHILD_ENV_SCRUB = (
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    "NODE_OPTIONS", "NODE_PATH",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
    "BASH_ENV", "ENV", "PERL5LIB", "RUBYOPT", "RUBYLIB",
)


# R-NOCOST/C11/C37: the agreed cross-family names apply to every vendor child.
# This is environment hygiene, not proof of the CLI's selected billing route.
_CHILD_ENV_CREDENTIALS = (
    'GOOGLE_API_KEY', 'GEMINI_API_KEY',
    'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_GENAI_USE_VERTEXAI',
    'GEMINI_CLI_USE_COMPUTE_ADC', 'CLOUD_SHELL',
    'GOOGLE_GEMINI_BASE_URL', 'GOOGLE_VERTEX_BASE_URL',
    'GEMINI_DEFAULT_AUTH_TYPE',
    'GOOGLE_CLOUD_ACCESS_TOKEN', 'AGY_ADC_AUTH',
    'GOOGLE_GENAI_USE_ENTERPRISE', 'OPENAI_API_KEY',
    'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN',
    'OPENAI_BASE_URL', 'OPENAI_ORGANIZATION',
    'OPENAI_PROJECT', 'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
    'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_USE_ANTHROPIC_AWS',
    'CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD', 'CLAUDE_CODE_USE_GATEWAY',
    'CLAUDE_CODE_USE_MANTLE', 'CLAUDE_CODE_API_BASE_URL',
    'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR', 'CLAUDE_CODE_OAUTH_TOKEN',
    'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR', 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN',
    'CLAUDE_CODE_GATEWAY_TOKEN', 'CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR',
    'ANTHROPIC_FOUNDRY_API_KEY', 'ANTHROPIC_FOUNDRY_AUTH_TOKEN',
    'ANTHROPIC_AWS_API_KEY', 'AWS_BEARER_TOKEN_BEDROCK',
    'ANTHROPIC_IDENTITY_TOKEN', 'ANTHROPIC_IDENTITY_TOKEN_FILE',
    'ANTHROPIC_FEDERATION_RULE_ID', 'ANTHROPIC_SERVICE_ACCOUNT_ID',
    'ANTHROPIC_PROFILE', 'ANTHROPIC_ORGANIZATION_ID',
    'ANTHROPIC_WORKSPACE_ID',
)

# Only Gemini keeps this project group for its supported Google login.
_GEMINI_PROJECT_ENV = (
    'GOOGLE_CLOUD_PROJECT', 'GOOGLE_CLOUD_LOCATION',
    'GOOGLE_CLOUD_REGION', 'GOOGLE_CLOUD_QUOTA_PROJECT',
)


def scrubbed_child_env(base=None, *, cli=None, remove=()) -> dict:
    """Return a fresh child mapping; reject names before retrieving their values.

    Every route removes the agreed loader and credential/endpoint names.
    Gemini alone keeps its project group; explicit extra removals still win.
    Model/effort settings, PATH, config pointers and GOOGLE_GENAI_USE_GCA remain unchanged.
    """
    src = base if base is not None else os.environ
    omitted = set(_CHILD_ENV_SCRUB) | set(_CHILD_ENV_CREDENTIALS) | set(remove)
    if cli != "gemini":
        omitted.update(_GEMINI_PROJECT_ENV)
    return {key: src[key] for key in src if key not in omitted}


def _read_capture(stream, size: int) -> bytes:
    """Read a finite snapshot without moving a vendor's shared file offset."""
    chunks = []
    offset = 0
    while offset < size:
        chunk = os.pread(stream.fileno(), min(size - offset, 1024 * 1024), offset)
        if not chunk:
            raise OSError("capture ended before its snapshot length")
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _terminate_provider_process(proc, reason: str) -> bool:
    """Bounded direct-child termination/reaping; descendants are vendor-owned."""
    log(f"{reason}; terminating direct child")
    for action in (proc.terminate, proc.kill):
        try:
            action()
        except ProcessLookupError:
            pass
        except OSError as error:
            log(f"direct-child termination failed ({type(error).__name__})")
        try:
            proc.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            pass
        except OSError as error:
            log(f"direct-child wait failed ({type(error).__name__})")
    log("direct child did not complete within termination deadline")
    return False


def _run_once(
    cli: str,
    cmd: list[str],
    cwd: Optional[str],
    timeout: int,
    stdin_text: Optional[str] = None,
    classify_and_log: bool = True,
    remove_env=(),
) -> RunResult:
    """Capture catchable termination for this owned invocation, then restore it.

    The handler only records state: raising between Popen and its assignment
    could lose the child handle. The engine observes cancellation in its wait
    loop and terminates/reaps only the direct child with bounded waits.
    Library calls from non-main threads retain their caller's signal policy.
    """
    received: list[int] = []
    previous = {}

    def remember(signum, _frame):
        if not received:
            received.append(signum)

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.signal(signum, remember)
        result = _run_once_owned(
            cli, cmd, cwd, timeout, stdin_text, classify_and_log, remove_env,
            received,
        )
        # Covers a signal arriving after the engine's final collection check.
        if received and not result._output_transport_failed and result.exit_code != EXIT_TIMEOUT:
            _mark_signal_failure(result, received[0], cli)
            log(f"[wrapper] {cli} {result.classification} exit={result.exit_code} "
                f"vendor={result.vendor_exit_code} elapsed={result.elapsed_s:.1f}s")
        return result
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _mark_signal_failure(result: RunResult, signum: int, cli: str) -> None:
    auth_stop = _auth_carrier_stop(cli, result.stderr, result.stdout, result.vendor_exit_code)
    result.exit_code = EXIT_TERMINAL if auth_stop else EXIT_CLI_FAIL
    result.classification = "oauth-env" if auth_stop else "unknown"
    result.extraction_error = f"wrapper interrupted ({signal.Signals(signum).name})"
    result._output_transport_failed = True
    result.final_answer = ""
    result.validated = None


def _run_once_owned(
    cli: str, cmd: list[str], cwd: Optional[str], timeout: int,
    stdin_text: Optional[str], classify_and_log: bool, remove_env,
    received: list[int],
) -> RunResult:
    """Capture one direct invocation through owned anonymous stream files.

    Input completion means availability, not vendor consumption. Output is a
    finite snapshot after direct-child completion; no descendant EOF or mirror.
    """
    effective_cwd = os.path.realpath(cwd or os.getcwd())
    log(
        f"exec cwd={'<redacted:cwd-path>' if _audit_redact_enabled() else effective_cwd} timeout={timeout}s "
        f"argv={_redact_prompt_args(cmd)}"
    )
    start = time.monotonic()

    def observed(result: RunResult, delivery: str) -> RunResult:
        result.transport = {
            **transport_receipt(cli, result),
            "stdin_delivery": delivery,
            "binary": cmd[0] if cmd else None,
        }
        return result

    stdin_bytes = None
    if stdin_text is not None:
        try:
            stdin_bytes = stdin_text.encode("utf-8")
        except UnicodeError:
            diagnostic = "stdin delivery failed (encoding)"
            log(diagnostic)
            return observed(RunResult(
                EXIT_CLI_FAIL, "", "", time.monotonic() - start,
                classification="unknown", extraction_error=diagnostic,
                _stdin_delivery_failed=True,
            ), "not-started")

    transport_error = None
    timed_out = setup_failed = False
    captured = {"stdout": "", "stderr": ""}
    with ExitStack() as resources:
        try:
            streams = {name: resources.enter_context(tempfile.TemporaryFile(mode="w+b", buffering=0))
                       for name in ("stdout", "stderr")}
            input_stream = subprocess.DEVNULL
            if stdin_bytes is not None:
                input_stream = resources.enter_context(tempfile.TemporaryFile(mode="w+b", buffering=0))
                if input_stream.write(stdin_bytes) != len(stdin_bytes):
                    raise OSError("incomplete input preparation")
                input_stream.flush()
                input_stream.seek(0)
        except OSError as error:
            diagnostic = f"stream preparation failed ({type(error).__name__})"
            log(diagnostic)
            return observed(RunResult(
                EXIT_CLI_FAIL, "", "", time.monotonic() - start,
                classification="unknown", extraction_error=diagnostic,
                _output_transport_failed=True,
            ), "not-started")
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=scrubbed_child_env(cli=cli, remove=remove_env),
                stdin=input_stream, stdout=streams["stdout"], stderr=streams["stderr"],
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            log(f"OSError on spawn: {error}")
            return observed(RunResult(
                EXIT_ARG_ERROR, "", f"spawn failed: {error}\n", time.monotonic() - start,
                classification="unknown",
            ), "not-started")
        try:
            deadline = time.monotonic() + timeout
            while not received:
                try:
                    proc.wait(timeout=min(0.1, max(0, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
            if timed_out or received:
                if not _terminate_provider_process(
                    proc, f"timeout after {timeout}s" if timed_out else "wrapper termination requested",
                ):
                    transport_error = "direct-child termination incomplete"
        except BaseException as error:
            _terminate_provider_process(proc, "wrapper interrupted")
            if not isinstance(error, Exception):
                raise
            setup_failed = True
            transport_error = f"transport setup/wait failed ({type(error).__name__})"

        # Freeze lengths before reading. Descendant writes cannot extend the
        # collection or be overwritten by a seek on the inherited offset.
        sizes = {}
        for name, stream in streams.items():
            try:
                sizes[name] = os.fstat(stream.fileno()).st_size
            except OSError as error:
                transport_error = transport_error or f"{name} collection failed ({type(error).__name__})"
        for name, size in sizes.items():
            try:
                data = _read_capture(streams[name], size)
                try:
                    captured[name] = data.decode("utf-8")
                except UnicodeDecodeError as error:
                    captured[name] = data[:error.start].decode("utf-8")
                    raise
                finally:
                    captured[name] = captured[name].replace("\r\n", "\n").replace("\r", "\n")
            except (OSError, UnicodeError) as error:
                transport_error = transport_error or f"{name} collection failed ({type(error).__name__})"

    if transport_error:
        log(transport_error)
    elapsed = time.monotonic() - start
    stdout, stderr = captured["stdout"], captured["stderr"]
    rc = proc.returncode if proc.returncode is not None else -1
    if timed_out:
        log(f"timed out elapsed={elapsed:.1f}s")
        result = RunResult(EXIT_TIMEOUT, stdout, stderr, elapsed)
    else:
        log(f"exit={rc} elapsed={elapsed:.1f}s")
        result = RunResult(EXIT_OK if rc == 0 else EXIT_CLI_FAIL, stdout, stderr, elapsed)
        if transport_error and (rc == 0 or setup_failed):
            result.exit_code = EXIT_CLI_FAIL
            result.classification = "unknown"
            result.extraction_error = transport_error
            result._output_transport_failed = True
    result._effective_cwd = effective_cwd
    result.vendor_exit_code = rc
    if received and not timed_out:
        _mark_signal_failure(result, received[0], cli)
    if (result.exit_code != EXIT_OK and
            _auth_carrier_stop(cli, stderr, stdout, rc)):
        result.classification = "oauth-env"
        result.exit_code = EXIT_TERMINAL
    elif classify_and_log:
        if not (result._stdin_delivery_failed or result._output_transport_failed):
            result.classification = classify(
                cli, stderr, stdout, result.exit_code, vendor_exit_code=rc,
            )
    elif not (result._stdin_delivery_failed or result._output_transport_failed):
        result.classification = "unclassified"
    if classify_and_log:
        log(f"[wrapper] {cli} {result.classification} exit={result.exit_code} "
            f"vendor={result.vendor_exit_code} elapsed={elapsed:.1f}s")
    return observed(result, "not-used" if stdin_bytes is None else "complete")


def _run_gemini_native_structured_once(
    result: RunResult,
    pydantic_cls: Any,
) -> RunResult:
    """Validate original Gemini carrier output for bound v2 verdict."""
    if (
        getattr(pydantic_cls, "_binding", None) is not None
        and result.exit_code == EXIT_OK
        and result.extraction_error is None
    ):
        import verdict_v2
        try:
            admitted = verdict_v2.admit_carrier_output(
                pydantic_cls,
                carrier="gemini-json",
                stdout=result.stdout,
                exit_code=result.vendor_exit_code,
                completed=True,
            )
            result.validated = admitted
            result.final_answer = json.dumps(admitted, ensure_ascii=False)
            result.classification = "ok"
            result.exit_code = EXIT_OK
            return result
        except Exception as exc:
            result.exit_code = EXIT_SCHEMA_FAIL
            result.classification = "schema-fail"
            result.validation_error = f"{type(exc).__name__}: {exc}"
            result.final_answer = ""
            result.validated = None
            log(
                f"[wrapper] gemini schema-fail "
                f"exit={result.exit_code} vendor={result.vendor_exit_code} "
                f"elapsed={result.elapsed_s:.1f}s"
            )
            return result
    return result


def run_cli_with_retry(
    cli: str,
    cmd_builder: Callable[[str], list[str]],
    prompt: str,
    cwd: Optional[str],
    timeout: int,
    pydantic_cls: Any = None,
    last_msg_path: Optional[str] = None,
    repair_mode: bool = False,
    prompt_via_stdin: bool = False,
    single_provider_call: bool = False,
    remove_env=(),
    prompt_suffix: str = "",
) -> RunResult:
    """Run once, extract the vendor answer, and validate locally.

    The historical name and single_provider_call parameter remain compatible;
    every route is now single-call. Vendor-internal retries and explicit later
    review retries are separate. cmd_builder receives the initial schema-injected
    prompt plus the caller's final clause, without correction-prompt mutation.
    """
    # Legacy repair mode skips normal next-run IPC cleanup. It does not restart
    # inference, and the current native repair child only proposes a patch.
    if not repair_mode:
        prune_stale_run_logs(cli)

    effective_prompt = (
        inject_schema_to_prompt(prompt, pydantic_cls) if pydantic_cls else prompt
    )

    def promote_schema_fail(r: RunResult) -> RunResult:
        r.exit_code = EXIT_SCHEMA_FAIL
        r.classification = "schema-fail"
        r.final_answer = ""
        log(
            f"[wrapper] {cli} schema-fail "
            f"exit={r.exit_code} vendor={r.vendor_exit_code} "
            f"elapsed={r.elapsed_s:.1f}s"
        )
        return r

    terminal_classes = (
        "cli-subscription-cap",
        "token-limit",
        "oauth-env",
        "config-conflict",
        "task-blocked",
    )

    def promote_terminal(r: RunResult, cls: str) -> RunResult:
        r.exit_code = EXIT_TERMINAL
        r.classification = cls
        r.final_answer = ""
        log(
            f"[wrapper] {cli} {cls} "
            f"exit={r.exit_code} vendor={r.vendor_exit_code} "
            f"elapsed={r.elapsed_s:.1f}s"
        )
        return r

    def promote_claude_extraction(r: RunResult, ext_err: str) -> Optional[RunResult]:
        if ext_err.startswith("schema-retries-exhausted:"):
            log(f"answer extraction error: {ext_err}")
            r.extraction_error = ext_err
            return promote_schema_fail(r)
        if ext_err.startswith("task-blocked:"):
            log(f"answer extraction error: {ext_err}")
            r.extraction_error = ext_err
            return promote_terminal(r, "task-blocked")
        if ext_err.startswith("is_error=true"):
            cls = classify_extraction("claude", r, ext_err)
            if cls in terminal_classes:
                log(f"answer extraction error: {ext_err}")
                r.extraction_error = ext_err
                return promote_terminal(r, cls)
        return None

    def promote_extraction_classification(
        r: RunResult, ext_err: str
    ) -> Optional[RunResult]:
        if cli == "claude":
            promoted = promote_claude_extraction(r, ext_err)
            if promoted is not None:
                return promoted
        cls = classify_extraction(cli, r, ext_err)
        if cls in terminal_classes:
            r.extraction_error = ext_err
            return promote_terminal(r, cls)
        if cls == "schema-rejected":
            r.exit_code = EXIT_SCHEMA_REJECTED
            r.classification = cls
            r.final_answer = ""
            log(
                f"[wrapper] {cli} {cls} "
                f"exit={r.exit_code} vendor={r.vendor_exit_code} "
                f"elapsed={r.elapsed_s:.1f}s"
            )
            return r
        return None

    # The caller's final clause follows initial schema instructions.
    sent_prompt = effective_prompt + ("\n\n" + prompt_suffix if prompt_suffix else "")
    cmd = cmd_builder(sent_prompt)
    run_once_kwargs = {"stdin_text": sent_prompt if prompt_via_stdin else None}
    if remove_env:
        run_once_kwargs["remove_env"] = remove_env
    result = _run_once(cli, cmd, cwd=cwd, timeout=timeout, **run_once_kwargs)
    result.repair_attempt = 0
    result.schema_repair_attempt = 0
    result.mode = "repair" if repair_mode else "normal"
    if result._stdin_delivery_failed or result._output_transport_failed or (
        prompt_via_stdin and result.exit_code == EXIT_TIMEOUT
    ):
        return result

    cls = result.classification
    if cli == "claude":
        _answer, ext_err = extract_claude_answer(result.stdout, result.stderr)
        if ext_err:
            promoted = promote_claude_extraction(result, ext_err)
            if promoted is not None:
                return promoted
            # A zero-exit Claude error envelope can still report capacity.
            if ext_err.startswith("is_error=true"):
                if classify_extraction("claude", result, ext_err) == "server-capacity":
                    result.classification = cls = "server-capacity"
    if cls in terminal_classes:
        return promote_terminal(result, cls)
    if cls in ("schema-rejected", "server-capacity"):
        result.exit_code = (EXIT_SCHEMA_REJECTED if cls == "schema-rejected"
                            else EXIT_RATE_GIVE_UP)
        log(
            f"[wrapper] {cli} {cls} "
            f"exit={result.exit_code} vendor={result.vendor_exit_code} "
            f"elapsed={result.elapsed_s:.1f}s"
        )
        return result
    if cls != "ok":
        return result

    # Extract only this invocation's final answer.
    if cli == "claude":
        answer, ext_err = extract_claude_answer(result.stdout, result.stderr)
    else:
        answer, ext_err = extract_gemini_answer(result.stdout, result.stderr)

    if ext_err:
        log(f"answer extraction error: {ext_err}")
        result.extraction_error = ext_err
        result.final_answer = ""
        promoted = promote_extraction_classification(result, ext_err)
        if promoted is not None:
            return promoted
        if result.exit_code == EXIT_OK:
            # Vendor returned rc=0 but extractor found no answer (empty
            # JSON envelope, missing last-message file, etc.). Promote
            # to wrapper failure AND re-classify — `_run_once` had set
            # classification="ok" based on rc alone, which is now stale.
            # Re-emit the one-line final summary so the dispatch contract
            # receives the corrected classification token.
            result.exit_code = EXIT_CLI_FAIL
            result.classification = "extraction-error"
            log(
                f"[wrapper] {cli} extraction-error "
                f"exit={result.exit_code} vendor={result.vendor_exit_code} "
                f"elapsed={result.elapsed_s:.1f}s"
            )
        return result

    if cli == "gemini" and getattr(pydantic_cls, "_binding", None) is not None:
        if result.exit_code == EXIT_OK and result.extraction_error is None:
            return _run_gemini_native_structured_once(result, pydantic_cls)

    if cli in ("claude", "gemini") and pydantic_cls is not None:
        try:
            _check_original_json(strip_markdown_fences(result.stdout), pydantic_cls)
        except ValueError as error:
            result.validation_error = str(error)
            return promote_schema_fail(result)

    result.final_answer = answer

    if pydantic_cls is None:
        return result
    ok, validated_or_err = validate_response(answer, pydantic_cls)
    if ok:
        result.validated = validated_or_err
        return result
    result.validation_error = str(validated_or_err)
    log(f"schema validation failed: {validated_or_err}")
    return promote_schema_fail(result)


# ─── Audit log ────────────────────────────────────────────────────────────

# TRIAD_DISPATCH_LOG_DIR overrides the log root (audit + run-logs; the --debug
# markdown dir is separate). Default = wrapper-adjacent _logs/. Consumers/tests point it
# at a temp dir so an installed plugin dir is never mutated (plugin roots are
# ephemeral per the Claude Code plugin docs).
_LOG_DIR_CONFIGURED = bool(os.environ.get("TRIAD_DISPATCH_LOG_DIR"))
_LOG_DIR = Path(os.environ.get("TRIAD_DISPATCH_LOG_DIR")
                or Path(__file__).resolve().parent / "_logs")
_DEBUG_DIR = Path(__file__).resolve().parent / "_debug"
_AUDIT_LOCK_TIMEOUT_S = 0.10
_AUDIT_LOCK_POLL_S = 0.01
_AUDIT_COPY_CHUNK_BYTES = 64 * 1024


def _audit_open_flags(*names: str) -> int:
    """Return required portable POSIX flags or fail the advisory audit."""
    flags = 0
    for name in names:
        value = getattr(os, name, None)
        if value is None:
            raise OSError(errno.ENOTSUP, f"audit requires os.{name}")
        flags |= value
    return flags


def _open_directory_nofollow(path: Path, *, create: bool = True) -> int:
    """Open a directory by walking every component without following links."""
    flags = os.O_RDONLY | _audit_open_flags(
        "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"
    )
    parts = Path(path).parts
    if any(part == ".." or os.sep in part for part in parts if part != os.sep):
        raise OSError(errno.EINVAL, "unsafe audit directory component")
    start = os.sep if Path(path).is_absolute() else "."
    fd = os.open(start, flags)
    try:
        for part in parts:
            if part in ("", ".", os.sep):
                continue
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    # A cooperative first-use writer won the create race.
                    # Reopen below with the identical no-follow flags; a
                    # symlink, non-directory, or later replacement still fails
                    # the descriptor attestation.
                    pass
                next_fd = os.open(part, flags, dir_fd=fd)
            next_st = os.fstat(next_fd)
            if not stat.S_ISDIR(next_st.st_mode):
                os.close(next_fd)
                raise OSError(errno.ENOTDIR, "audit component is not a directory")
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_regular_nofollow(
    dir_fd: int,
    name: str,
    *,
    flags: int,
    mode: int = 0o600,
    normalize_mode: bool = True,
) -> int:
    """Open one private regular, single-link leaf relative to ``dir_fd``."""
    if not name or name in (".", "..") or os.sep in name:
        raise OSError(errno.EINVAL, "unsafe audit leaf name")
    safe_flags = flags | _audit_open_flags("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")
    fd = os.open(name, safe_flags, mode, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(errno.EPERM, "audit leaf must be regular and single-link")
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or not os.path.samestat(st, current)
        ):
            raise OSError(errno.ESTALE, "audit leaf changed during open")
        if normalize_mode:
            # Creation mode is already 0600; normalize a legacy safe leaf only
            # after proving the pathname and descriptor still name one inode.
            os.fchmod(fd, mode)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _regular_fd_matches_name(dir_fd: int, name: str, fd: int) -> bool:
    """Check that ``name`` still denotes the exact safe inode held by ``fd``."""
    try:
        held = os.fstat(fd)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(current.st_mode)
        and held.st_nlink == 1
        and current.st_nlink == 1
        and os.path.samestat(held, current)
    )


def _bounded_flock(fd: int, timeout_s: float = _AUDIT_LOCK_TIMEOUT_S) -> bool:
    """Acquire an exclusive lock without ever waiting beyond a short deadline."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_AUDIT_LOCK_POLL_S, remaining))


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte, handling partial and interrupted POSIX writes."""
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            count = os.write(fd, view[written:])
        except InterruptedError:
            continue
        if count <= 0:
            raise OSError(errno.EIO, "short audit write")
        written += count


def audit(cli: str, cmd: list[str], prompt: str, result: RunResult) -> bool | None:
    """Append one JSONL record per invocation to _logs/<cli>/audit.jsonl.

    A per-CLI lock file serializes append + rotation across processes.
    `final_answer_head` caps at 500 chars; full answer flows to caller via
    `result.final_answer`.

    Writers with namespace authority over the audit directory are assumed to
    be trusted and cooperative. Descriptor attestation rejects observed swaps,
    but portable Python/POSIX has no atomic unlink-if-same-inode operation.
    Persistence is best-effort audit evidence, not a power-loss durability
    protocol; audit failure never changes the provider result. Return True when
    the record was appended, None for advisory contention/attestation skips, and
    False for an actual storage exception.
    """
    ok = result.exit_code == EXIT_OK
    redact = _audit_redact_enabled()
    # Custody taxonomy (P4.b, spec 3-way 2026-07-11; extends the 2026-07-05
    # prompt-custody adjudication): in redact mode, MODEL-OUTPUT fields
    # (final_answer_head, extraction_error) are allowed at a 500 cap, but
    # STREAMS that can carry PROMPT content (stdout, stdout_head, stderr —
    # vendor UIs/JSON envelopes may reflect the input) are fully "<redacted>"
    # (+ lengths): a partial cap cannot guarantee prompt custody because a
    # prompt echo rides the stream HEAD. Applied to this record only — the
    # RunResult is never mutated (emit_run_log() runs AFTER audit() and must
    # keep the full copies in the transient, pruned run-log).
    def _redact_cap(text: Optional[str]) -> Optional[str]:
        """Model-output field custody: the adjudicated 500 cap in redact mode."""
        if redact and text and len(text) > 500:
            return text[:500] + " …[redact-cap]"
        return text

    rec: dict = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cli": cli,
        # Prompt custody (adjudication 2026-07-05): lab default = full evidence;
        # hardened/redact mode strips prompt-bearing argv + prompt text (length
        # only) so a public install's durable audit never MECHANICALLY stores
        # prompts. Explicit allowance (re-confirm 2026-07-11): the 500-capped
        # model-output fields (final_answer_head / extraction_error /
        # validation_error) may incidentally contain prompt text the MODEL
        # chose to echo into its answer — the guarantee covers mechanical
        # storage of the input, not model-echoed content.
        "cmd": _redact_prompt_args(cmd) if redact else cmd,
        "prompt_head": "<redacted>" if redact else prompt[:200],
        "prompt_len": len(prompt),
        "vendor_exit_code": result.vendor_exit_code,
        "exit_code": result.exit_code,
        "elapsed_s": round(result.elapsed_s, 2),
        "classification": result.classification,
        "dispatch_phase": result.dispatch_phase,
        "runtime_identity": result.runtime_identity,
        "transport": transport_receipt(cli, result),
        "mode": result.mode,
        "repair_attempt": result.repair_attempt,
        "schema_repair_attempt": result.schema_repair_attempt,
        "stderr": "<redacted>" if redact else result.stderr,
        "final_answer_head": (result.final_answer or "")[:500],
        "final_answer_len": len(result.final_answer or ""),
        # validated (the full pydantic dict) and validation_error (pydantic's
        # message embeds the model's failing input) are the SAME model-output
        # class as final_answer_head — the taxonomy bounds them too (panel
        # custody-lens finding 2026-07-11; schema-fail empties final_answer
        # but validation_error would otherwise carry the answer uncapped).
        "validated": ("<redacted>" if (redact and result.validated is not None)
                      else result.validated),
        "extraction_error": _redact_cap(result.extraction_error),
        "validation_error": _redact_cap(result.validation_error),
    }
    if result.review_binding is not None:
        rec["review_binding"] = result.review_binding
    if result._effective_cwd is not None:
        rec["effective_cwd"] = "<redacted:cwd-path>" if redact else result._effective_cwd
    rec["resolved_prompt_file"] = (
        "<redacted:prompt-file-path>" if redact and result._resolved_prompt_file is not None
        else result._resolved_prompt_file
    )
    if cli == "antigravity" and result._agy_read_telemetry is not None:
        telemetry = result._agy_read_telemetry
        rec["agy_read_telemetry"] = (
            {"done_view_file_event_count": telemetry["done_view_file_event_count"]}
            if redact else telemetry
        )
    if cli == "claude" and result._claude_receipt:
        receipt = result._claude_receipt
        if redact:
            # Do not reintroduce stream-derived identifiers in redacted audits.
            receipt = {
                key: receipt[key]
                for key in ("usage", "estimated_cost_usd", "permission_denial_count")
                if key in receipt
            }
        if receipt:
            rec["claude_receipt"] = receipt
    if redact:
        rec["stderr_len"] = len(result.stderr or "")
    if ok:
        rec["stdout_head"] = "<redacted>" if redact else result.stdout[:500]
        rec["stdout_len"] = len(result.stdout)
    elif redact:
        rec["stdout"] = "<redacted>"
        rec["stdout_len"] = len(result.stdout or "")
    else:
        rec["stdout"] = result.stdout
    try:
        payload = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        return _persist_audit_record(cli, payload)
    except Exception:
        # Audit evidence is advisory. Provider output, normalized exit, and
        # classification are already authoritative and must remain untouched.
        return False


def _persist_audit_record(cli: str, payload: bytes) -> bool | None:
    """Persist one record through descriptor-bound, no-follow operations."""
    if not cli or cli in (".", "..") or os.sep in cli:
        raise OSError(errno.EINVAL, "unsafe audit CLI name")
    log_dir_fd = _open_directory_nofollow(_LOG_DIR / cli)
    lock_fd = -1
    locked = False
    try:
        lock_fd = _open_regular_nofollow(
            log_dir_fd,
            ".audit.lock",
            flags=os.O_RDWR | os.O_CREAT,
        )
        locked = _bounded_flock(lock_fd)
        if not locked or not _regular_fd_matches_name(
            log_dir_fd, ".audit.lock", lock_fd
        ):
            return None
        audit_fd = _open_regular_nofollow(
            log_dir_fd,
            "audit.jsonl",
            flags=os.O_RDWR | os.O_APPEND | os.O_CREAT,
        )
        try:
            _write_all(audit_fd, payload)
            try:
                _rotate_audit_fd_if_needed(log_dir_fd, audit_fd, cli)
            except Exception:
                # The record is already complete in the active audit file.
                # Rotation cleanup is advisory and cannot undo that append.
                pass
            return True
        finally:
            os.close(audit_fd)
    finally:
        if locked:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            if lock_fd >= 0:
                os.close(lock_fd)
        finally:
            os.close(log_dir_fd)


def _unlink_regular_if_same(
    dir_fd: int,
    name: str,
    expected: os.stat_result,
    *,
    older_than: Optional[float] = None,
) -> bool:
    """Unlink only the same held regular entry, optionally still stale."""
    try:
        check_fd = _open_regular_nofollow(
            dir_fd,
            name,
            flags=os.O_RDONLY,
            normalize_mode=False,
        )
    except OSError:
        return False
    try:
        current = os.fstat(check_fd)
        if not os.path.samestat(current, expected):
            return False
        if older_than is not None and current.st_mtime >= older_than:
            return False
        if not _regular_fd_matches_name(dir_fd, name, check_fd):
            return False
        # A cooperating consumer can refresh an IPC leaf while the pathname
        # attestation runs. Recheck the held descriptor immediately before the
        # unlink and keep it open through the operation.
        final = os.fstat(check_fd)
        if older_than is not None and final.st_mtime >= older_than:
            return False
        try:
            os.unlink(name, dir_fd=dir_fd)
            return True
        except OSError:
            return False
    finally:
        os.close(check_fd)


def _rotate_audit_fd_if_needed(log_dir_fd: int, audit_fd: int, cli: str) -> None:
    """Copy the held audit inode exclusively, then truncate that same inode."""
    source = os.fstat(audit_fd)
    if (
        source.st_size <= AUDIT_ROTATE_BYTES
        or not stat.S_ISREG(source.st_mode)
        or source.st_nlink != 1
    ):
        return
    pread = getattr(os, "pread", None)
    if pread is None:
        raise OSError(errno.ENOTSUP, "audit rotation requires os.pread")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = (
        f"audit.{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl"
    )
    archive_fd = _open_regular_nofollow(
        log_dir_fd,
        archive_name,
        flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    )
    archive_identity = os.fstat(archive_fd)
    complete = False
    try:
        offset = 0
        while offset < source.st_size:
            chunk = pread(
                audit_fd,
                min(_AUDIT_COPY_CHUNK_BYTES, source.st_size - offset),
                offset,
            )
            if not chunk:
                raise OSError(errno.EIO, "audit source changed during rotation")
            _write_all(archive_fd, chunk)
            offset += len(chunk)
        source_after = os.fstat(audit_fd)
        archive_after = os.fstat(archive_fd)
        if (
            not os.path.samestat(source, source_after)
            or source_after.st_size != source.st_size
            or not stat.S_ISREG(archive_after.st_mode)
            or archive_after.st_nlink != 1
            or archive_after.st_size != source.st_size
            or not _regular_fd_matches_name(
                log_dir_fd, archive_name, archive_fd
            )
        ):
            raise OSError(errno.ESTALE, "audit changed during rotation")
        os.ftruncate(audit_fd, 0)
        complete = True
    finally:
        os.close(archive_fd)
        if not complete:
            _unlink_regular_if_same(log_dir_fd, archive_name, archive_identity)
    log(
        f"WARN: rotated {cli}/audit.jsonl to {archive_name} "
        f"(>{AUDIT_ROTATE_BYTES // (1024*1024)} MB)"
    )
    _prune_audit_archives_fd(log_dir_fd)


def _prune_audit_archives_fd(log_dir_fd: int) -> None:
    """Prune no-follow regular archives from a trusted/cooperative directory."""
    rows: list[tuple[str, float, int, os.stat_result]] = []
    for name in os.listdir(log_dir_fd):
        if name == "audit.jsonl" or not (
            name.startswith("audit.") and name.endswith(".jsonl")
        ):
            continue
        try:
            fd = _open_regular_nofollow(log_dir_fd, name, flags=os.O_RDONLY)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            rows.append((name, st.st_mtime, st.st_size, st))
        finally:
            os.close(fd)
    rows.sort(key=lambda row: row[1])
    total_bytes = sum(row[2] for row in rows)
    over_count = max(0, len(rows) - AUDIT_MAX_ARCHIVES)
    over_bytes = total_bytes - AUDIT_ARCHIVE_MAX_BYTES
    for name, _mtime, size, identity in rows:
        if over_count <= 0 and over_bytes <= 0:
            break
        if _unlink_regular_if_same(log_dir_fd, name, identity):
            over_count -= 1
            over_bytes -= size


def _prune_audit_archives(log_dir: Path) -> None:
    """Compatibility wrapper around descriptor-relative archive pruning."""
    try:
        log_dir_fd = _open_directory_nofollow(log_dir, create=False)
    except OSError:
        return
    try:
        _prune_audit_archives_fd(log_dir_fd)
    except OSError:
        return
    finally:
        os.close(log_dir_fd)


# ─── Deterministic classifier-patch applier (native proposal redesign) ─────
# The fresh native child returns only a structured proposal. This function is
# the single owner-controlled local apply path to the classifier extension JSON:
# validate against the enum +
# pattern-name SoT + literal bounds, then flock + atomic-write. No LLM in the
# write path; safe-by-construction against classifier-poisoning.
#
#   CLASSIFICATION_TOKENS = the classify() result enum and patch eligibility.
#     The exit map also includes transport tokens and compatibility aliases;
#     those do not become classifier-patch targets. Contract membership and
#     exit-code parity are checked in tests/test_exit_token_contract.py.
#   PATTERN_LIST_NAMES    = the built-in pattern-list constant names an
#     extension may extend (a proposal's pattern_list must be one of these).

CLASSIFICATION_TOKENS: frozenset[str] = frozenset(
    (
        "ok",
        "server-capacity",
        "cli-subscription-cap",
        "token-limit",
        "oauth-env",
        "timeout",
        "extraction-error",
        "schema-fail",
        "schema-rejected",
        "config-conflict",
        "task-blocked",
        "unknown",
    )
)

PATTERN_LIST_NAMES: frozenset[str] = frozenset(
    (
        "SERVER_CAPACITY_PATTERNS",
        "CLI_SUB_CAP_PATTERNS",
        "TOKEN_LIMIT_PATTERNS",
        "SCHEMA_REJECTED_PATTERNS",
        "CONFIG_CONFLICT_PATTERNS",
        "AGY_AUTH_BANNER_PATTERNS",
    )
)

# The meaningful-failure subset a repair PROPOSAL may target. `ok` = success →
# mapping a real failure to it SUPPRESSES failures; `unknown` = the default/meta
# bucket, never a useful patch target. (fix1 round, review BLOCKER.)
REPAIR_CLASSIFICATION_TOKENS: frozenset[str] = frozenset(
    CLASSIFICATION_TOKENS - {"ok", "unknown"}
)

# Each pattern-list name → the canonical classification classify() returns when
# that list matches. AGY_AUTH_BANNER entries are carrier line prefixes on own
# stderr or reflected-report lines; the other lists are L2 substrings:
#   AGY_AUTH_BANNER → oauth-env, CLI_SUB_CAP → cli-subscription-cap,
#   SERVER_CAPACITY → server-capacity, TOKEN_LIMIT → token-limit,
#   SCHEMA_REJECTED → schema-rejected,
#   CONFIG_CONFLICT → config-conflict.
# A pattern proposal's `classification` must equal PATTERN_LIST_CLASS[pattern_list]
# (else appending the substring would make classify() return a DIFFERENT class
# than the proposal claims). Locked in step with PATTERN_LIST_NAMES at import.
PATTERN_LIST_CLASS: dict[str, str] = {
    "SERVER_CAPACITY_PATTERNS": "server-capacity",
    "CLI_SUB_CAP_PATTERNS": "cli-subscription-cap",
    "TOKEN_LIMIT_PATTERNS": "token-limit",
    "SCHEMA_REJECTED_PATTERNS": "schema-rejected",
    "CONFIG_CONFLICT_PATTERNS": "config-conflict",
    "AGY_AUTH_BANNER_PATTERNS": "oauth-env",
}
assert (
    set(PATTERN_LIST_CLASS.keys()) == set(PATTERN_LIST_NAMES)
), "PATTERN_LIST_CLASS drifted from PATTERN_LIST_NAMES (classify() mirror broke)"
assert all(
    _c in CLASSIFICATION_TOKENS for _c in PATTERN_LIST_CLASS.values()
), "PATTERN_LIST_CLASS maps to a class not in CLASSIFICATION_TOKENS"

# Bound on a proposed substring literal — long enough for real vendor phrases,
# short enough that a poisoned proposal cannot smuggle a huge blob into the
# classifier or bloat the extension file.
_MAX_SUBSTRING_LEN = 200
# Floor on a proposed substring length (after lowercase-normalize). A defensible
# floor that rejects the pathological "e"/"the" while allowing real short
# signatures ("oauth", "quota"). NOT a claim of full semantic specificity — that
# is the analyzer's + owner's job (see SECURITY.md), only a coarse over-broad guard.
_MIN_SUBSTRING_LEN = 4

# ── fix2/fix3: L1 vendor_exit_map symmetric guard (round-2 + round-3 re-confirm
# BLOCKERs) ────────────────────────────────────────────────────────────────
# classify() consults the (extension-merged) vmap BEFORE the L2 substrings and
# returns immediately, so a poisoned vmap entry has HIGHER blast radius than a
# poisoned substring — round-1 floored L2, round-2 made L1 symmetric via a
# hardcoded enumeration (`_GENERIC_EXIT_CODES`). Round-3 found that enumeration
# LEAKY: it listed 130/137/143 (128+SIGINT/SIGKILL/SIGTERM) but missed the other
# 128+N signal-death codes — e.g. 139 (SIGSEGV), 141 (SIGPIPE), 134 (SIGABRT) —
# so a proposal like {"vendor_exit_code": 139, ...} still passed and could
# poison every future segfault's routing. An enumeration of "the other 128+N
# codes" can never be complete (any signum 1-31 not yet listed is a fresh gap).
#
# Fix: a SOUND RANGE, not an enumeration. A legitimate vendor application-
# specific exit code lives in [3, 125]. Outside that range is either too
# generic or reserved/signal-death, and too broad to safely auto-route:
#   - {0, 1, 2}     = generic (success / general error / misuse-or-EXIT_TIMEOUT)
#   - {126, 127}    = shell (not-executable / not-found)
#   - [128, 255]    = signal-death (128+signum, e.g. 130=SIGINT, 137=SIGKILL,
#                     139=SIGSEGV, 141=SIGPIPE, 143=SIGTERM) or reserved/OOR
# A vmap PROPOSAL outside [3, 125] is refused — the analyzer must propose a
# specific stderr `substring` (L2) or escalate. This is the L1 analog of the
# L2 `_MIN_SUBSTRING_LEN` over-broad floor. (The built-in `<CLI>_VENDOR_EXIT_MAP`
# dicts are trusted hardcoded maps, NOT proposals — this bounds PROPOSALS only.)
_VENDOR_EXIT_CODE_MIN = 3
_VENDOR_EXIT_CODE_MAX = 125

# A vendor's own EXIT CODE cannot mean a WRAPPER-determined status, so a vmap
# PROPOSAL is restricted to the vendor-exit-DERIVABLE classes. Kept: the vendor-
# error classes (server-capacity, cli-subscription-cap, token-limit, oauth-env,
# schema-rejected) + extraction-error (the built-in ANTIGRAVITY_VENDOR_EXIT_MAP[0]
# weak no-answer fallback legitimately uses it, so it stays a valid vmap class).
# Excluded (the wrapper/status classes the WRAPPER decides, never a raw vendor
# exit): timeout (wrapper kills the vendor on its own timeout — exit_code==
# EXIT_TIMEOUT in classify(), not a vmap code); schema-fail (wrapper pydantic
# JSON validation — EXIT_SCHEMA_FAIL, not in classify()); task-blocked (Claude
# permission-denial envelope at EXIT_TERMINAL); config-conflict
# (wrapper/config condition via CONFIG_CONFLICT_PATTERNS).
# Verified against classify() + the wrapper exit-code semantics (2026-07-06).
# This applies ONLY to the vendor_exit_map path — the PATTERN path already
# enforces classification == PATTERN_LIST_CLASS[pattern_list].
VENDOR_EXIT_PROPOSAL_CLASSES: frozenset[str] = frozenset(
    REPAIR_CLASSIFICATION_TOKENS
    - {"timeout", "schema-fail", "task-blocked", "config-conflict"}
)
assert (
    VENDOR_EXIT_PROPOSAL_CLASSES <= REPAIR_CLASSIFICATION_TOKENS
), "VENDOR_EXIT_PROPOSAL_CLASSES must be a subset of REPAIR_CLASSIFICATION_TOKENS"


def apply_classifier_patch(
    cli: str,
    proposal: dict,
    extension_path: Path | None = None,
) -> str:
    """Validate + atomically merge a repair proposal into the classifier JSON.

    ``extension_path`` is the owner CLI's already validated absolute target.
    Existing direct compatibility callers may omit it and retain the ambient
    extension-path fallback.

    proposal = {
        "classification": <one of REPAIR_CLASSIFICATION_TOKENS>,  # required (NOT ok/unknown)
        "reason":         <non-empty str>,                      # required
        # exactly one target:
        "vendor_exit_code": <int > 0>,    # append {code: classification} to vendor_exit_map
        "pattern_list":     <one of PATTERN_LIST_NAMES>,  # + "substring": <bounded str>
        "substring":        <non-empty bounded literal str, stored LOWERCASED>,
    }

    Semantic validation (all BEFORE any file write; ValueError on violation):
      - classification ∈ REPAIR_CLASSIFICATION_TOKENS (ok/unknown rejected — ok
        would suppress real failures, unknown is the default bucket).
      - vendor_exit_code must be an int bounded to the application-specific
        range [3, 125] ({0,1,2}=generic, {126,127}=shell, >=128=signal-death/
        reserved are too broad to auto-route — the L1 analog of the L2 length
        floor; a sound range, not an enumeration [fix3]), AND its classification
        must be vendor-exit-derivable (∈ VENDOR_EXIT_PROPOSAL_CLASSES — a wrapper/
        status class cannot be inferred from a raw vendor exit code). [fix2]
      - substring is lowercased (classify() lowercases the blob), then floored at
        _MIN_SUBSTRING_LEN and required to carry alphanumeric signal — rejects the
        over-broad "e"/whitespace-only case. (Fine-grained SPECIFICITY rests on the
        analyzer + owner review, not this coarse floor — see SECURITY.md.)
      - pattern proposals require classification == PATTERN_LIST_CLASS[pattern_list]
        (the class that list actually yields in classify()).
      - authentication proposals are refused; existing curated entries remain.
      - reason must be a non-empty string; no reason-length or entry-count cap.

    Returns "applied" on success. Raises ValueError on ANY invalid field and
    leaves the extension file UNTOUCHED. A transient read OSError (EACCES/EMFILE/
    EISDIR — NOT FileNotFoundError) PROPAGATES and preserves the existing file
    (never laundered into a `{}` reset that os.replace would clobber). Holds
    fcntl.flock(LOCK_EX) on the `<ext>.lock` sibling for the whole
    read->validate->merge->write cycle (mirrors audit()); writes atomically via a
    temp file + os.replace().
    """
    # ── Validate the proposal shape BEFORE touching any file ────────────────
    if not isinstance(cli, str) or not cli.strip():
        raise ValueError("apply_classifier_patch: cli must be a non-empty str")
    if not isinstance(proposal, dict):
        raise ValueError("apply_classifier_patch: proposal must be a dict")

    classification = proposal.get("classification")
    if classification == "oauth-env":
        raise ValueError(
            "apply_classifier_patch: oauth-env is not a repair proposal; "
            "escalate authentication recovery to the user"
        )
    # SEMANTIC: only a meaningful-failure class is a valid patch target. Reject
    # `ok` (would suppress real failures) and `unknown` (meta/default bucket).
    if classification not in REPAIR_CLASSIFICATION_TOKENS:
        raise ValueError(
            f"apply_classifier_patch: invalid classification "
            f"{classification!r} (not in REPAIR_CLASSIFICATION_TOKENS; "
            f"ok/unknown are not valid patch targets)"
        )

    vendor_exit_code = proposal.get("vendor_exit_code")
    pattern_list = proposal.get("pattern_list")
    substring = proposal.get("substring")

    has_exit = vendor_exit_code is not None
    has_pattern = pattern_list is not None or substring is not None

    if not has_exit and not has_pattern:
        raise ValueError(
            "apply_classifier_patch: proposal has no target "
            "(need vendor_exit_code or pattern_list+substring)"
        )
    if has_exit and has_pattern:
        raise ValueError(
            "apply_classifier_patch: proposal targets both vendor_exit_map and "
            "patterns — supply exactly one"
        )

    if has_exit:
        # bool is an int subclass — reject it explicitly (a poisoned True/False)
        if isinstance(vendor_exit_code, bool) or not isinstance(vendor_exit_code, int):
            raise ValueError(
                f"apply_classifier_patch: vendor_exit_code must be an int, "
                f"got {type(vendor_exit_code).__name__}"
            )
        # SEMANTIC: 0 = success (a failure class for it is nonsensical); a vendor
        # exit code is never negative on a real process.
        if vendor_exit_code <= 0:
            raise ValueError(
                f"apply_classifier_patch: vendor_exit_code must be > 0 "
                f"(0 = success; got {vendor_exit_code})"
            )
        # fix2/fix3 (L1 analog of the L2 _MIN_SUBSTRING_LEN floor): bound the
        # vendor_exit_code to the application-specific SOUND RANGE [3, 125] —
        # not an enumeration (fix2's `_GENERIC_EXIT_CODES` listed 130/137/143 but
        # missed other 128+N signal-death codes like 139/141/134, a structural
        # leak any enumeration is prone to repeat). {0,1,2}=generic, {126,127}=
        # shell, >=128=signal-death/reserved are all too broad to safely auto-
        # route — each would misroute EVERY unrelated future failure carrying
        # that code (e.g. rc=1, or rc=139 on any future segfault). classify()
        # consults the vmap BEFORE the L2 substrings and returns immediately, so
        # a poisoned vmap entry outweighs a poisoned substring. Propose a
        # specific stderr `substring` (L2) or escalate instead.
        if not (_VENDOR_EXIT_CODE_MIN <= vendor_exit_code <= _VENDOR_EXIT_CODE_MAX):
            raise ValueError(
                f"apply_classifier_patch: vendor_exit_code {vendor_exit_code} is "
                f"outside the application-specific range "
                f"[{_VENDOR_EXIT_CODE_MIN}, {_VENDOR_EXIT_CODE_MAX}] — "
                f"{{0,1,2}}=generic, {{126,127}}=shell, >=128=signal-death/reserved "
                f"are too broad to safely auto-route (each would misroute unrelated "
                f"future failures carrying that code). Propose a specific stderr "
                f"substring instead, or escalate."
            )
        # fix2: a vendor's own EXIT CODE cannot mean a WRAPPER-determined status —
        # restrict a vmap PROPOSAL to the vendor-exit-derivable classes (the PATTERN
        # path already enforces classification == PATTERN_LIST_CLASS[pattern_list],
        # so this check applies ONLY here).
        if classification not in VENDOR_EXIT_PROPOSAL_CLASSES:
            raise ValueError(
                f"apply_classifier_patch: classification {classification!r} is not "
                f"vendor-exit-derivable (a wrapper/status class cannot be inferred "
                f"from a raw vendor exit code); vmap proposals must be one of "
                f"{sorted(VENDOR_EXIT_PROPOSAL_CLASSES)}"
            )
    else:  # pattern branch
        if pattern_list not in PATTERN_LIST_NAMES:
            raise ValueError(
                f"apply_classifier_patch: invalid pattern_list {pattern_list!r} "
                f"(not a built-in pattern-list name)"
            )
        if not isinstance(substring, str):
            raise ValueError(
                f"apply_classifier_patch: substring must be a str, "
                f"got {type(substring).__name__}"
            )
        if not substring:
            raise ValueError("apply_classifier_patch: substring must be non-empty")
        if len(substring) > _MAX_SUBSTRING_LEN:
            raise ValueError(
                f"apply_classifier_patch: substring exceeds "
                f"{_MAX_SUBSTRING_LEN} chars ({len(substring)})"
            )
        # SEMANTIC: classify() lowercases the blob before substring matching
        # (see the L2 block). Store the substring lowercased so a mixed-case
        # proposal actually matches; normalize BEFORE the length floor.
        substring = substring.lower()
        # Over-broad guard: reject sub-floor length and all-whitespace/punct
        # (no alphanumeric signal → would smear across unrelated blobs).
        if len(substring) < _MIN_SUBSTRING_LEN:
            raise ValueError(
                f"apply_classifier_patch: substring too short "
                f"(< {_MIN_SUBSTRING_LEN} chars after normalize): {substring!r}"
            )
        if not any(ch.isalnum() for ch in substring):
            raise ValueError(
                f"apply_classifier_patch: substring has no alphanumeric signal "
                f"(whitespace/punctuation only): {substring!r}"
            )
        # SEMANTIC: classification must be the class this list actually yields —
        # appending to a list whose classify() class differs from the proposal's
        # `classification` would silently route to a DIFFERENT class than claimed.
        expected_class = PATTERN_LIST_CLASS[pattern_list]
        if classification != expected_class:
            raise ValueError(
                f"apply_classifier_patch: classification {classification!r} does "
                f"not match pattern_list {pattern_list!r} "
                f"(that list classifies as {expected_class!r})"
            )

    reason = proposal.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("apply_classifier_patch: reason must be a non-empty str")

    ext_path = extension_path if extension_path is not None else _classifier_extension_path()
    ext_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ext_path.parent / (ext_path.name + ".lock")

    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)

            # Read-or-{} defensively. Order matters (Q6a fix):
            #   FileNotFoundError → {}   (first patch — no file yet, fine)
            #   ValueError (corrupt JSON) → {} + a stderr warning (reset)
            #   any OTHER OSError (EACCES/EMFILE/EISDIR — transient) → PROPAGATE.
            # A transient OSError must NOT be laundered into `data = {}`: that
            # would let the os.replace below OVERWRITE a healthy existing file
            # with a single-entry {}, destroying all prior rules. Propagating
            # aborts the patch and leaves the file intact.
            data: dict = {}
            try:
                raw = ext_path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except FileNotFoundError:
                data = {}
            except ValueError:
                # Valid-file-but-corrupt-JSON → reset (mirrors _load_classifier_extension).
                sys.stderr.write(
                    f"[apply] {cli}: corrupt classifier extension JSON at "
                    f"{ext_path} — resetting to a fresh entry\n"
                )
                data = {}

            # Merge into the per-cli entry (create intermediate keys).
            entry = data.get(cli)
            if not isinstance(entry, dict):
                entry = {}

            if has_exit:
                vmap = entry.get("vendor_exit_map")
                if not isinstance(vmap, dict):
                    vmap = {}
                vmap[str(vendor_exit_code)] = classification
                entry["vendor_exit_map"] = vmap
            else:
                pats = entry.get("patterns")
                if not isinstance(pats, dict):
                    pats = {}
                lst = pats.get(pattern_list)
                if not isinstance(lst, list):
                    lst = []
                is_new = substring not in lst
                if is_new:
                    lst.append(substring)
                pats[pattern_list] = lst
                entry["patterns"] = pats
            data[cli] = entry

            # Atomic write: temp file in the SAME dir, JSON-serialize (which is
            # itself a validation of the merged shape), flush+fsync, os.replace.
            serialized = json.dumps(data, ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(
                dir=str(ext_path.parent), prefix=ext_path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    tf.write(serialized)
                    tf.flush()
                    os.fsync(tf.fileno())
                os.replace(tmp, ext_path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except Exception:
                pass

    log(f"[apply] {cli} {classification} — {reason}")
    return "applied"


# ─── Per-execution run-log (dispatch SKILL input) ─────────────────────────

def _fallback_json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fallback_identity(entry: os.stat_result) -> dict[str, int]:
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid():
        raise OSError(errno.EPERM, "fallback directory is not owned")
    return {"uid": entry.st_uid, "device": entry.st_dev, "inode": entry.st_ino}


def _record_fallback(path: Path) -> None:
    """Publish allocation proof after the failure IPC itself is preserved."""
    root = path.parent
    base_fd = _open_directory_nofollow(root.parent, create=False)
    proof_fd = -1
    try:
        record = {"version": 1, "role": "fallback-ipc", "root": str(root),
                  "identity": _fallback_identity(root.lstat()), "run_log": path.name}
        proof_fd = _open_regular_nofollow(base_fd, f".{root.name}.allocation.json",
            flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        _write_all(proof_fd, _fallback_json(record))
        os.fsync(proof_fd)
    finally:
        if proof_fd >= 0:
            os.close(proof_fd)
        os.close(base_fd)


def _fallback_inventory(root_fd: int, run_log: str) -> dict[str, list[int]]:
    """Fallback IPC is flat: its original log and optional repair companion."""
    inventory = {}
    for name in sorted(os.listdir(root_fd)):
        entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (name not in (run_log, run_log + ".repair.json")
                or not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode))
                or (stat.S_ISREG(entry.st_mode) and entry.st_nlink != 1)):
            raise OSError(errno.EPERM, "unexpected fallback contents")
        inventory[name] = [entry.st_dev, entry.st_ino, entry.st_mode,
                           entry.st_size, entry.st_mtime_ns]
    return inventory


def _prune_fallback(base_fd: int, root: Path, cutoff: float) -> None:
    """Keep the allocation record until the exact proven tree is gone."""
    proof_name = f".{root.name}.allocation.json"
    proof_fd = _open_regular_nofollow(base_fd, proof_name,
        flags=os.O_RDWR, normalize_mode=False)
    root_fd = -1
    try:
        # Different dispatches may sweep the same machine-level allocation.
        if not _bounded_flock(proof_fd, timeout_s=0):
            return
        if not _regular_fd_matches_name(base_fd, proof_name, proof_fd):
            raise OSError(errno.ESTALE, "fallback allocation changed before lock")
        proof_identity = os.fstat(proof_fd)
        if proof_identity.st_uid != os.getuid() or proof_identity.st_size > 65536:
            raise OSError(errno.EPERM, "invalid fallback proof owner or size")
        raw = os.read(proof_fd, 65537)
        first, separator, tail = raw.partition(b"\n")
        record = json.loads(first)
        if (not separator or not isinstance(record, dict)
                or first + separator != _fallback_json(record)
                or set(record) != {"version", "role", "root", "identity", "run_log"}
                or type(record["version"]) is not int or record["version"] != 1
                or record["role"] != "fallback-ipc" or record["root"] != str(root)
                or not isinstance(record["identity"], dict)
                or set(record["identity"]) != {"uid", "device", "inode"}
                or any(type(v) is not int for v in record["identity"].values())
                or record["identity"]["uid"] != os.getuid()
                or not isinstance(record["run_log"], str)
                or not record["run_log"].endswith(".json")
                or os.sep in record["run_log"]):
            raise OSError(errno.EPERM, "invalid fallback allocation")
        # The first line remains allocation proof if a stop interrupts the
        # appended deletion-start line. No deletion begins before a full line.
        started = json.loads(tail) if tail.endswith(b"\n") else None
        if tail.endswith(b"\n") and (not isinstance(started, dict)
                or set(started) != {"deleting"} or not isinstance(started["deleting"], dict)
                or tail != _fallback_json(started)):
            raise OSError(errno.EPERM, "invalid fallback deletion record")
        if started is None and proof_identity.st_mtime >= cutoff:
            return
        try:
            entry = os.stat(root.name, dir_fd=base_fd, follow_symlinks=False)
        except FileNotFoundError:
            _unlink_regular_if_same(base_fd, proof_name, proof_identity)
            return
        if _fallback_identity(entry) != record["identity"]:
            raise OSError(errno.ESTALE, "fallback allocation identity changed")
        root_fd = os.open(root.name, os.O_RDONLY | _audit_open_flags(
            "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"), dir_fd=base_fd)
        if _fallback_identity(os.fstat(root_fd)) != record["identity"]:
            raise OSError(errno.ESTALE, "fallback directory changed during open")
        actual = _fallback_inventory(root_fd, record["run_log"])
        if started is None:
            os.lseek(proof_fd, len(first) + 1, os.SEEK_SET)
            os.ftruncate(proof_fd, len(first) + 1)
            _write_all(proof_fd, _fallback_json({"deleting": actual}))
            os.fsync(proof_fd)
        elif any(started["deleting"].get(name) != facts for name, facts in actual.items()):
            raise OSError(errno.ESTALE, "fallback contents changed after cleanup started")
        if not _regular_fd_matches_name(base_fd, proof_name, proof_fd):
            raise OSError(errno.ESTALE, "fallback allocation record changed")
        for name, facts in actual.items():
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if [current.st_dev, current.st_ino, current.st_mode,
                    current.st_size, current.st_mtime_ns] != facts:
                raise OSError(errno.ESTALE, "fallback leaf changed")
            if stat.S_ISLNK(current.st_mode):
                os.unlink(name, dir_fd=root_fd)
            elif not _unlink_regular_if_same(root_fd, name, current):
                raise OSError(errno.EIO, "fallback leaf was not removed")
        if _fallback_identity(os.stat(root.name, dir_fd=base_fd,
                follow_symlinks=False)) != record["identity"]:
            raise OSError(errno.ESTALE, "fallback directory replaced during cleanup")
        os.rmdir(root.name, dir_fd=base_fd)
        _unlink_regular_if_same(base_fd, proof_name, proof_identity)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(proof_fd)


def emit_run_log(
    cli: str,
    wrapper_cmd: list[str],
    vendor_cmd: list[str],
    prompt: str,
    result: RunResult,
) -> Optional[Path]:
    """Write failure IPC or an explicitly bound v2 review's original evidence.

    Run-logs live at `_logs/<cli>/runs/<UTC-ts>-<pid>-<uuid8>.json`. The
    dispatch skill passes the opaque path to the fresh native proposal-only
    child without inline embedding (escape-safe and parallel-safe).

    Ordinary success writes nothing. V2 review success retains the same raw
    record in its caller-configured per-attempt namespace for collection/export;
    this is review evidence, not a repair request or permanent investigation log.

    Self-prunes after write: if dir exceeds `_RUN_LOG_MAX_FILES` or
    `_RUN_LOG_MAX_BYTES`, eligible stale files are unlinked oldest-first.
    Fresh sibling IPC is retained even if that leaves a temporary overflow
    (best-effort, race-tolerant for parallel writes).
    """
    if result.exit_code == EXIT_OK and result.review_binding is None:
        return None
    if not cli or cli in (".", "..") or os.sep in cli:
        raise OSError(errno.EINVAL, "unsafe run-log CLI name")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    fname = f"{ts}-{pid}-{suffix}.json"
    rec: dict = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cli": cli,
        "wrapper_cmd": wrapper_cmd,
        "vendor_cmd": vendor_cmd,
        "prompt_head": prompt[:200],
        "prompt_len": len(prompt),
        "exit_code": result.exit_code,
        "vendor_exit_code": result.vendor_exit_code,
        "classification": result.classification,
        "dispatch_phase": result.dispatch_phase,
        "runtime_identity": result.runtime_identity,
        "transport": transport_receipt(cli, result),
        "mode": result.mode,
        "elapsed_s": round(result.elapsed_s, 2),
        "stderr": result.stderr,
        "stdout": result.stdout,
        "final_answer": result.final_answer,
        "validated": result.validated,
        "extraction_error": result.extraction_error,
        "validation_error": result.validation_error,
    }

    if result.review_binding is not None:
        rec["review_binding"] = result.review_binding
    payload = json.dumps(rec, ensure_ascii=False, indent=2).encode("utf-8")

    def write_under(runs_dir: Path) -> Path:
        runs_fd = _open_directory_nofollow(runs_dir)
        leaf_fd = -1
        identity: Optional[os.stat_result] = None
        complete = False
        try:
            leaf_fd = _open_regular_nofollow(
                runs_fd,
                fname,
                flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            identity = os.fstat(leaf_fd)
            _write_all(leaf_fd, payload)
            if not _regular_fd_matches_name(runs_fd, fname, leaf_fd):
                raise OSError(errno.ESTALE, "run-log leaf changed during write")
            complete = True
            try:
                _prune_run_logs_fd(runs_fd, preserve_name=fname)
            except Exception:
                # The IPC file is already complete. Retention cleanup must not
                # hide it or change the provider's authoritative result.
                pass
            return runs_dir / fname
        finally:
            if leaf_fd >= 0:
                os.close(leaf_fd)
            if not complete and identity is not None:
                _unlink_regular_if_same(runs_fd, fname, identity)
            os.close(runs_fd)

    runs_dir = _LOG_DIR / cli / "runs"
    try:
        path = write_under(runs_dir)
    except Exception:
        if _LOG_DIR_CONFIGURED:
            return None
        # File IPC is the repair handoff. Only an unconfigured primary log root
        # may retain the normalized provider result in a unique stdlib-created
        # private temp directory. An explicit configured-root failure returns
        # None above without fallback.
        # macOS may spell its trusted temp root through `/var`, a system
        # symlink. Canonicalize only this stdlib-created private directory.
        fallback_root = Path(
            tempfile.mkdtemp(prefix=f"triad-{cli}-run-log-")
        ).resolve(strict=True)
        try:
            path = write_under(fallback_root)
        except Exception:
            # Only this call's allocation; rmdir preserves any remaining data.
            try:
                fallback_root.rmdir()
            except OSError:
                pass
            raise
        try:
            _record_fallback(path)
        except Exception:
            # Keep usable repair IPC even when cleanup proof cannot be stored.
            try:
                log("cleanup-proof-unavailable: fallback IPC retained")
            except OSError:
                pass

    return path


def persist_result_artifacts(
    cli: str,
    wrapper_cmd: list[str] | RunResult,
    vendor_cmd: list[str] | None = None,
    prompt: str = "",
    result: RunResult | None = None,
    *,
    debug: bool = False,
) -> Optional[Path]:
    """Persist best-effort evidence without changing the provider result.

    Audit/debug storage is advisory. A failure run-log is repair IPC.
    `emit_run_log` uses a unique private temporary fallback only when the log
    root was unconfigured at import time; an explicit configured-root failure
    returns None without fallback. This helper emits a fixed run-log diagnostic
    when a failure run-log cannot be stored, or when a successful call cannot
    store audit evidence under an explicit configured root. Callers still write
    the provider answer and return its normalized exit code.
    """
    if isinstance(wrapper_cmd, RunResult):
        result = wrapper_cmd
        wrapper_cmd = sys.argv
        vendor_cmd = vendor_cmd or []
        prompt = prompt or ""
    elif result is None:
        raise ValueError("result is required")
    if vendor_cmd is None:
        vendor_cmd = []
    audit_exception = False
    try:
        audit_persisted = audit(cli, vendor_cmd, prompt, result)
    except Exception:
        audit_persisted = False
        audit_exception = True
        log("audit-unavailable: storage-failure")
    if debug:
        try:
            debug_log(cli, prompt, result)
        except Exception:
            log("debug-log-unavailable: storage-failure")
    try:
        path = emit_run_log(cli, wrapper_cmd, vendor_cmd, prompt, result)
    except Exception:
        path = None
    if path is not None:
        log(f"run-log: {path}")
        if (
            _LOG_DIR_CONFIGURED
            and audit_persisted is False
            and not audit_exception
        ):
            log("audit-unavailable: storage-failure")
    elif result.exit_code != EXIT_OK or (
        _LOG_DIR_CONFIGURED and audit_persisted is False
    ):
        log("run-log-unavailable: storage-failure")
    return path


def _prune_run_logs_fd(runs_fd: int, preserve_name: Optional[str] = None) -> None:
    """Prune only descriptor-bound regular, single-link run-log leaves."""
    rows: list[tuple[str, float, int, os.stat_result]] = []
    for name in os.listdir(runs_fd):
        if not (name.endswith(".json") or name.endswith(".prompt.tmp")):
            continue
        try:
            leaf_fd = _open_regular_nofollow(
                runs_fd, name, flags=os.O_RDONLY, normalize_mode=False
            )
        except OSError:
            continue
        try:
            st = os.fstat(leaf_fd)
            rows.append((name, st.st_mtime, st.st_size, st))
        finally:
            os.close(leaf_fd)
    rows.sort(key=lambda row: row[1])
    over_count = max(0, len(rows) - _RUN_LOG_MAX_FILES)
    over_bytes = sum(row[2] for row in rows) - _RUN_LOG_MAX_BYTES
    cutoff = time.time() - _STALE_IPC_AGE_FLOOR_S

    for name, _listed_mtime, _listed_size, listed in rows:
        if over_count <= 0 and over_bytes <= 0:
            break
        if name == preserve_name:
            continue
        try:
            leaf_fd = _open_regular_nofollow(
                runs_fd, name, flags=os.O_RDONLY, normalize_mode=False
            )
        except OSError:
            continue
        try:
            current = os.fstat(leaf_fd)
            if (
                not os.path.samestat(current, listed)
                or current.st_mtime >= cutoff
                or not _regular_fd_matches_name(runs_fd, name, leaf_fd)
            ):
                continue
        finally:
            os.close(leaf_fd)
        if _unlink_regular_if_same(
            runs_fd, name, current, older_than=cutoff
        ):
            over_count -= 1
            over_bytes -= current.st_size


def _prune_run_logs(runs_dir: Path, preserve: Optional[Path] = None) -> None:
    """Compatibility wrapper for descriptor-bound best-effort cap pruning."""
    preserve_name = (
        preserve.name
        if preserve is not None and preserve.parent == runs_dir
        else None
    )
    try:
        runs_fd = _open_directory_nofollow(runs_dir, create=False)
    except OSError:
        return
    try:
        _prune_run_logs_fd(runs_fd, preserve_name=preserve_name)
    except OSError:
        return
    finally:
        os.close(runs_fd)


# Default one-hour age floor for best-effort next-run IPC cleanup. Repair mode
# skips the sweep, while the cap-based `_prune_run_logs` still bounds overflow.
_STALE_IPC_AGE_FLOOR_S = 3600


def prune_stale_run_logs(cli: str, age_floor_s: int = _STALE_IPC_AGE_FLOOR_S) -> None:
    """Next-run cleanup of stale run-logs (owner contract: "clean up on the
    NEXT run", not at exit — a crashed call must leave its evidence).

    Removes `_logs/<cli>/runs/*.json` (run-logs AND their `.repair.json` pairs)
    whose mtime is older than `age_floor_s`. Called at the START of every normal
    (non-repair-mode) dispatch, so a SUBSEQUENT run cleans up the residue a
    prior run left on failure — including failure classes (terminal / server-cap
    / schema-rejected / task-blocked) whose evidence remains for
    analyzer and owner handling. The cap-based `_prune_run_logs` remains the
    over-cap failsafe; this is the time-based next-run sweep.

    The age floor is what makes this concurrency-safe under 4-way parallel
    dispatch: a live sibling's run-log is freshly written (< floor) so it is
    never deleted while still awaiting consumption. Best-effort + per-file
    tolerant — a vanishing entry never aborts the sweep.
    """
    # `emit_run_log` uses a unique private temp directory only when the
    # import-time log root is unconfigured and its primary root is unusable.
    # Explicit configured-root failure has no fallback. Sweep prior unconfigured
    # fallback IPC only with allocation proof aged by at least one hour;
    # unmarked residue is preserved. Normal logs below use each file's mtime.
    prune_stale_tmp_dirs(f"triad-{cli}-run-log-", age_floor_s=age_floor_s)
    runs_dir = _LOG_DIR / cli / "runs"
    cutoff = time.time() - max(0, age_floor_s)
    try:
        runs_fd = _open_directory_nofollow(runs_dir, create=False)
    except OSError:
        return
    try:
        try:
            names = os.listdir(runs_fd)
        except OSError:
            return
        for name in names:
            if not (name.endswith(".json") or name.endswith(".prompt.tmp")):
                continue
            try:
                leaf_fd = _open_regular_nofollow(
                    runs_fd, name, flags=os.O_RDONLY, normalize_mode=False
                )
            except OSError:
                continue
            try:
                listed = os.fstat(leaf_fd)
            except OSError:
                continue
            finally:
                try:
                    os.close(leaf_fd)
                except OSError:
                    pass
            _unlink_regular_if_same(
                runs_fd, name, listed, older_than=cutoff
            )
    finally:
        try:
            os.close(runs_fd)
        except OSError:
            pass


def prune_stale_tmp_dirs(
    prefix: str,
    age_floor_s: int = _STALE_IPC_AGE_FLOOR_S,
    base: Optional[str] = None,
) -> None:
    """Sweep proven fallback IPC allocations; preserve unmarked old residue.

    Prefixes select candidates, never ownership. Allocation proof supplies the
    age and exact directory identity, survives interruption and is removed last.
    `base` is an isolated stand-in; otherwise use the system temporary directory.
    """
    # The system temp root may be exposed through macOS's `/var` symlink; it is
    # trusted only when selected internally. Caller-provided bases remain
    # literal so a symlinked managed root is skipped by the no-follow open.
    base_dir = Path(base) if base else Path(tempfile.gettempdir()).resolve()
    cutoff = time.time() - max(_STALE_IPC_AGE_FLOOR_S, age_floor_s)
    try:
        base_fd = _open_directory_nofollow(base_dir, create=False)
    except OSError:
        return
    try:
        try:
            names = os.listdir(base_fd)
        except OSError:
            return
        suffix = ".allocation.json"
        candidates = {name for name in names if name.startswith(prefix)}
        candidates.update(name[1:-len(suffix)] for name in names
                          if name.startswith("." + prefix) and name.endswith(suffix))
        skipped = False
        for name in sorted(candidates):
            try:
                _prune_fallback(base_fd, base_dir / name, cutoff)
            except (OSError, ValueError, TypeError):
                skipped = True
        if skipped:
            try:
                log("cleanup-skipped: unproven or changed fallback IPC retained")
            except OSError:
                pass
    finally:
        try:
            os.close(base_fd)
        except OSError:
            pass


# ─── Debug log (human-readable per-call markdown table) ───────────────────
# Opt-in via wrapper's `--debug` flag. Append-only markdown table at
# `_debug/<UTC-YYYY-MM-DD>/<cli>.md`. Header is written exactly once per
# file (race-free under flock). Cell content is truncated to 200 chars and
# escaped (`|` → `\|`, newlines → `<br>`) for markdown table safety.
# Audit.jsonl remains the SoT for full data; debug.md is a sample-grade
# human aid for live triage (cat / glow / bat).
_DEBUG_CELL_LIMIT = 200


def _debug_cell(s: str, n: int = _DEBUG_CELL_LIMIT) -> str:
    s = s or ""
    truncated = len(s) > n
    s = s[:n].replace("\r", "").replace("|", "\\|").replace("\n", "<br>")
    return s + ("…" if truncated else "")


def debug_log(cli: str, prompt: str, result: RunResult) -> None:
    """Append one human-readable markdown row per call. Opt-in only.

    Path: `_debug/<UTC-YYYY-MM-DD>/<cli>.md`. Header (table head) written
    exactly once on first append per file, race-free under fcntl lock.
    """
    if _audit_redact_enabled():
        # Redact-mode custody (panel custody-lens, 2026-07-11): the debug dump
        # stores the FULL prompt + streams in a durable-ish per-day file. A
        # hardened install must not get a prompt-custody bypass via --debug.
        log("debug dump skipped: redact mode (prompt/stream custody)")
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir = _DEBUG_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{cli}.md"

    with path.open("a", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            # Race-free header — under lock, fstat().st_size==0 means new
            # file. f.tell() in append mode is undefined per Python docs;
            # fstat() reads the actual on-disk size while we hold flock.
            # 2026-05-03 fault test exposed: parallel writers all saw
            # tell()==0 and emitted duplicate headers.
            if os.fstat(f.fileno()).st_size == 0:
                f.write(f"# {cli} debug log — {today} (UTC)\n\n")
                f.write("| time | request | exitcode | stderr | stdout |\n")
                f.write("|---|---|---|---|---|\n")
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            f.write(
                f"| {ts} | {_debug_cell(prompt)} | {result.exit_code} "
                f"| {_debug_cell(result.stderr)} "
                f"| {_debug_cell(result.stdout)} |\n"
            )
            # Flush the buffered header/row BEFORE releasing the lock. The
            # header check (`fstat().st_size == 0`) reads the kernel inode
            # size, but Python block-buffers the writes until close — which the
            # `with` block performs AFTER this `finally` releases the lock.
            # Without this flush a concurrent writer can acquire the lock in the
            # release→close window, still observe size 0, and emit a duplicate
            # header (a latent TOCTOU that surfaces only under heavy scheduling
            # load). flush() issues the write() syscall, so the new size is
            # immediately visible to any subsequent fstat; no fsync needed —
            # debug.md is a sample-grade aid (audit.jsonl is the durable SoT).
            f.flush()
        finally:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass
