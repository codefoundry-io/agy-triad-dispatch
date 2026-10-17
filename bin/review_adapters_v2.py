"""Resolve installed interfaces before preparing a native or CLI review call.

Discovery never submits a review prompt. Selection/capability evidence is not
account entitlement or proof of the eventual inference's effective identity.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import _common
import google_preflight_v2
import review_round
from claude_wrapper import EFFORT_CHOICES as CLAUDE_EFFORT_CHOICES
from review_roster import DEFAULTS, _validate
from validate_v2 import _json, load_contracts


ROOT = Path(__file__).resolve().parents[1]


def resolve_binary(name: str) -> str | None:
    # Same install pins and no-PATH-fallback rule as the legacy selector.
    found = review_round._selectable_google_binary(name)
    if found is None:
        return None
    path = Path(found).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("selected CLI is not an executable file")
    return str(path)


def probe(argv: list[str], *, cwd: Path, env: dict | None = None, cli: str = "preflight") -> str:
    command = argv if not env else ["/usr/bin/env", *(f"{key}={value}" for key, value in env.items()), *argv]
    # Reuse signal handling, direct-child termination and finite output capture. No second
    # subprocess engine or global environment mutation for adapter probes.
    result = _common._run_once(cli, command, str(cwd), 60, classify_and_log=False)
    if result.exit_code != 0 or result.vendor_exit_code != 0:
        raise ValueError("capability probe failed: " + (result.stderr or result.stdout)[-4000:])
    return result.stdout


def _directory(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("v2 capability custody must be absolute")
    try:
        fd = _common._open_directory_nofollow(path)
        os.close(fd)
    except OSError as error:
        raise ValueError("invalid v2 capability custody directory") from error
    review_round._canonical_directory(path, "v2 capability custody")


def _new(path: Path, data: dict) -> None:
    review_round._write_new(path, review_round._canonical_json_bytes(data))


def _codex(
    block: dict,
    *,
    cwd: Path,
    folder: Path,
    web: bool = False,
    native_capabilities: dict | None = None,
) -> dict:
    del web
    if isinstance(native_capabilities, dict) and native_capabilities.get("source") == "native-spawn-tool":
        raise ValueError("native-spawn-tool is not supported on the Antigravity host; use CLI wrapper route")

    binary = resolve_binary("codex")
    if binary is None:
        raise ValueError("Codex reviewer executable is unavailable")

    version_text = probe([binary, "--version"], cwd=cwd)
    found = re.search(r"\b(\d+\.\d+\.\d+)\b", version_text)
    version = found[1] if found else version_text.strip()

    help_text = probe([binary, "exec", "--help"], cwd=cwd)
    required = ("--json", "--output-schema", "--sandbox", "--ephemeral", "--skip-git-repo-check")
    if any(flag not in help_text for flag in required):
        raise ValueError("installed Codex interface lacks required review controls")

    model = block.get("model")
    effort = block.get("reasoning") if block.get("reasoning") is not None else block.get("effort")

    capability = {
        "model": model,
        "effort": effort,
        "binary": binary,
        "cli_version": version,
        "transport_route": "codex",
        "capability_source": "cli-selection",
    }
    path = folder / "codex-capability.json"
    _new(path, {**capability, "version_output": version_text, "help_output": help_text})
    return {**capability, "capability_file": str(path)}


def _claude_selection_conflicts(requested: str | None, selected: str) -> bool:
    """Compare exposed identity components, without an availability catalog.

    Display labels do not expose every full-ID suffix. Unknown labels/aliases
    remain observations, not evidence from which to manufacture a model ID.
    """
    if requested is None:
        return False
    pin = requested.removesuffix("[1m]")
    observed = selected.removesuffix("[1m]")
    tokens = observed.split(maxsplit=1)
    observed_token = tokens[0].removesuffix("[1m]") if tokens else ""
    if pin.startswith("claude-") and observed_token.startswith("claude-"):
        return pin != observed_token
    id_pattern = r"claude-([a-z]+)-(\d{1,3}(?:-\d{1,3})*)(?:-\d{8})?"
    full_id = re.fullmatch(id_pattern, pin)
    selected_id = re.fullmatch(id_pattern, observed_token)
    label = re.match(r"(?:Claude )?([A-Za-z]+) (\d+(?:\.\d+)*)(?=$|[ \t`])", observed)
    if selected_id is not None:
        family, version = selected_id[1], selected_id[2].replace("-", ".")
    elif label is not None:
        family, version = label[1].lower(), label[2]
    else:
        return False
    if full_id is not None:
        return (full_id[1], full_id[2].replace("-", ".")) != (family, version)
    return pin in ("opus", "sonnet", "haiku", "fable") and pin != family


def _claude(block: dict, *, cwd: Path, folder: Path, web: bool = False) -> dict:
    agent, model, effort = (block.get(key) for key in ("agent", "model", "effort"))
    if agent is not None and (
            not agent.strip() or any(char in agent for char in ("\x00", "\r", "\n"))):
        raise ValueError("Claude agent must be a nonblank native name without control separators")
    if effort is not None and effort not in CLAUDE_EFFORT_CHOICES:
        raise ValueError("Claude wrapper does not support the requested effort")
    binary = resolve_binary("claude")
    if binary is None:
        raise ValueError("Claude reviewer executable is unavailable")
    data = _json(review_round._canonical_regular_file_bytes(ROOT / "bin/data/claude-capabilities.json", "Claude capabilities"))
    if (not isinstance(data, dict) or not isinstance(data.get("minimum_preflight_version"), str)
            or re.fullmatch(r"\d+\.\d+\.\d+", data["minimum_preflight_version"]) is None):
        raise ValueError("invalid packaged Claude capability document")
    version_text = probe([binary, "--version"], cwd=cwd)
    found = re.search(r"\b(\d+\.\d+\.\d+)\b", version_text)
    if found is None or tuple(map(int, found[1].split("."))) < tuple(map(int, data["minimum_preflight_version"].split("."))):
        raise ValueError("Claude session-only model preflight requires the supported CLI version")
    help_text = probe([binary, "--help"], cwd=cwd)
    required = ("--print", "--model", "--effort", "--no-session-persistence", "--permission-mode", "--output-format")
    if any(flag not in help_text for flag in required) or (agent is not None and "--agent" not in help_text):
        raise ValueError("installed Claude interface lacks required review controls")
    if web and "--allowedTools" not in help_text:
        raise ValueError("Claude web authorization requires native --allowedTools support")
    command = [binary, "--print", "--no-session-persistence", "--output-format", "text", "--permission-mode", "plan"]
    if agent is not None:
        command += ["--agent", agent]
    if model is not None:
        command += ["--model", model]
    if effort is not None:
        command += ["--effort", effort]
    selection = probe(command + (["/model " + model] if model is not None else ["/model"]), cwd=cwd)
    prefix = "Set model to " if model is not None else "Current model: "
    if not selection.startswith(prefix):
        raise ValueError("Claude refused or did not expose the requested model selection")
    selected = selection[len(prefix):].strip()
    if not selected:
        raise ValueError("Claude did not expose a model selection")
    # Model and effort must come from the same selection line. Later prose is
    # retained in selection_output, but cannot hide or replace these controls.
    selected = selected.splitlines()[0].removesuffix(" for this session only")
    observed_effort = re.search(r"\s+\(effort: ([a-z]+)\)\s*$", selected)
    if observed_effort is not None:
        selected = selected[:observed_effort.start()].rstrip()
        if effort is not None and observed_effort[1] != effort:
            raise ValueError("Claude substituted a different requested effort during preflight")
    if selected.startswith("`"):
        selected = selected[1:].split("`", 1)[0]
    else:
        selected = selected.removesuffix(" for this session only")
    if not selected.strip():
        raise ValueError("Claude did not expose a model selection")
    if _claude_selection_conflicts(model, selected):
        raise ValueError("Claude substituted a different requested model during preflight")
    # Optional catalog aliases retain existing receipt display compatibility.
    # Membership and per-model effort lists never decide admission.
    models = data.get("models")
    models = models if isinstance(models, dict) else {}
    matches = [label for label, row in models.items() if isinstance(label, str) and label
               and isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
               and re.fullmatch(re.escape(label) + r"(?: \([^()]* context\))?", selected)]
    support = models[max(matches, key=len)] if matches else None
    requested = model
    capability = {"binary": binary, "cli_version": found[1], "model": requested,
                  "effort": effort, "agent": agent,
                  "selected_model": support["id"] if support else selected,
                  "capability_scope": "cli-selection", "provider_started": False}
    path = folder / "claude-capability.json"
    _new(path, {**capability, "selection_output": selection})
    return {**capability, "transport_route": "claude", "capability_file": str(path)}


def _google(entry: dict, *, defaults: dict, review_id: str, cwd: Path,
            authentication_class: str, folder: Path, attempt: int, web: bool = False,
            review_packet_dir: Path | None = None) -> dict:
    pin = entry.get("google", {}).get("route")
    if authentication_class not in ("personal-google", "gemini-enterprise"):
        raise ValueError("unsupported Google authentication class")
    if pin == "gemini" and authentication_class == "personal-google":
        raise ValueError("personal Google authentication requires AGY")
    route = pin or ("agy" if resolve_binary("agy") is not None else "gemini")
    if route == "gemini" and authentication_class == "personal-google":
        raise ValueError("personal Google authentication requires an available AGY")
    binary = resolve_binary(route)
    if binary is None:
        raise ValueError("selected Google route is unavailable; no route substitution")
    block = entry.get(route)
    if block is None:
        raise ValueError("selected Google route has no adapter configuration")
    model = block.get("model") if block.get("model") is not None else defaults[route]["model"]
    effort = block.get("effort") if block.get("effort") is not None else defaults[route]["effort"]
    if route == "gemini" and effort is not None:
        raise ValueError("Gemini CLI does not support an effort flag")
    wrapper = ROOT / "bin" / ("antigravity_wrapper.py" if route == "agy" else "gemini_wrapper.py")
    selector_file = folder / "selector.json"
    _new(selector_file, {"review_id": review_id, "authentication_class": authentication_class,
                         "route": route, "executable": binary, "wrapper": str(wrapper), "provider_started": False})
    selector = review_round.load_google_selector_receipt(selector_file, expected_review_id=review_id,
                                                        expected_route=route, expected_wrapper=wrapper)
    args = SimpleNamespace(expected_review_id=review_id, expected_leg_name=entry["name"],
                           expected_attempt=attempt, expected_route=route,
                           model=model, effort=effort, timeout=entry["timeout_s"], project=None, web=web)
    packet_control = ({"review_packet_dir": str(review_packet_dir)}
                      if route == "gemini" and review_packet_dir is not None else {})
    args.review_packet_dir = packet_control.get("review_packet_dir")
    # The wrapper's preflight is the source of route/version/containment/policy
    # evidence. It receives no review content and performs no review inference.
    argv = [sys.executable, str(wrapper), "--preflight-only", "--prompt", "v2 capability check",
            "--cwd", str(cwd), "--pydantic", "verdict_v2:LegVerdict", "--model=" + model,
            "--timeout", str(entry["timeout_s"]), "--expected-review-id", review_id,
            "--expected-leg-name", entry["name"],
            "--expected-attempt", str(attempt), "--expected-route", route,
            "--google-selector-receipt", str(selector_file)]
    if packet_control:
        argv += ["--review-packet-dir", packet_control["review_packet_dir"]]
    if web:
        argv += ["--web"]
    if effort is not None:
        argv += ["--effort", effort]
    if route == "agy":
        argv += ["--sandbox", "read-only"]
    env = {"TRIAD_REQUIRE_PINNED_VENDOR": "1", "TRIAD_" + route.upper() + "_BIN": binary,
           "TRIAD_DISPATCH_LOG_DIR": str(folder / "logs")}
    raw = probe(argv, cwd=cwd, env=env,
                cli="gemini" if route == "gemini" else "antigravity").encode("utf-8")
    preflight_file = folder / "preflight.json"
    review_round._write_new(preflight_file, raw)
    receipt = google_preflight_v2.load_preflight(preflight_file, selector, args, str(cwd))
    return {**packet_control, "route": route, "transport_route": route, "binary": binary,
            "cli_version": receipt["agy_version" if route == "agy" else "gemini_version"],
            "model": model, "effort": effort, "preflight_file": str(preflight_file),
            "preflight_sha256": hashlib.sha256(raw).hexdigest(), "selector_file": str(selector_file)}


def prepare_adapters(roster: dict, *, review_id: str, cwd: Path, authentication_class: str,
                     receipt_root: Path, native_capabilities: dict | None = None, attempt: int = 1,
                     review_web_authorized: bool = False, review_packet_dir: Path | None = None) -> dict:
    if type(review_web_authorized) is not bool:
        raise ValueError("review_web_authorized must be a boolean")
    review_round._validate_review_id(review_id)
    review_round._canonical_directory(cwd, "v2 child cwd")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("v2 attempt must be a positive integer")
    _validate({"schema": roster["schema"], "legs": roster["legs"]}, load_contracts()["review-legs.schema.json"], resolved=True)
    defaults = {entry["vendor"]: entry for entry in _json(review_round._canonical_regular_file_bytes(DEFAULTS, "host defaults"))["legs"]}
    _directory(receipt_root)
    output = {}
    for entry in roster["legs"]:
        if not entry["enabled"]:
            continue
        name, family = entry["name"], entry["vendor"]
        parent = receipt_root / name
        _directory(parent)
        folder = parent / f"attempt-{attempt}"
        try:
            folder.mkdir()
        except FileExistsError:
            raise ValueError("capability attempt already recorded") from None
        base = {"review_web_authorized": review_web_authorized, "family": family, "route": None, "agent": None,
                "timeout_s": entry["timeout_s"], "cwd": str(cwd), "capabilities_checked": True,
                "preflight_sha256": None, "preflight_file": None, "selector_file": None}
        try:
            if family == "codex":
                extra = _codex(entry.get("codex", {}), cwd=cwd, folder=folder, web=review_web_authorized,
                               native_capabilities=native_capabilities)
            elif family == "claude":
                extra = _claude(entry["claude"], cwd=cwd, folder=folder, web=review_web_authorized)
            else:
                extra = _google(entry, defaults=defaults["google"], review_id=review_id,
                                cwd=cwd, authentication_class=authentication_class, folder=folder, attempt=attempt,
                                web=review_web_authorized, review_packet_dir=review_packet_dir)
            output[name] = {**base, **extra}
            _new(folder / "adapter.json", output[name])
        except (ValueError, OSError) as error:
            _new(folder / "preparation-failure.json", {"leg_name": name, "attempt": attempt,
                                                      "provider_started": False, "error": str(error)})
            raise ValueError(f"v2 capability preparation failed for {name}: {error}") from error
    return output


def preflight(
    roster: dict,
    *,
    review_id: str,
    cwd: Path,
    authentication_class: str,
    receipt_root: Path,
    native_capabilities: dict | None = None,
    attempt: int = 1,
    review_web_authorized: bool = False,
    review_packet_dir: Path | None = None,
) -> dict:
    return prepare_adapters(
        roster,
        review_id=review_id,
        cwd=cwd,
        authentication_class=authentication_class,
        receipt_root=receipt_root,
        native_capabilities=native_capabilities,
        attempt=attempt,
        review_web_authorized=review_web_authorized,
        review_packet_dir=review_packet_dir,
    )
