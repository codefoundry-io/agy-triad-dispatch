"""Named-entry accounting inside the existing review workspace lifecycle.

This module prepares immutable calls and collects host observations. The host
still starts and waits for native tools and CLI wrappers; this is not a scheduler.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry

import review_round as lifecycle
from review_prompts_v2 import load_bundle, render_prompt
from review_roster import resolve_roster
from validate_v2 import BINDING_FIELDS, _json, load_contracts, validate_review_kind, validate_verdict
from verdict_v2 import bound_verdict


TOOLKIT = Path(__file__).resolve().parents[1]
# R-REVIEW-WEB / D-REVIEW-LEGS-20261003: only a recorded owner revocation
# changes this policy. New preparations bind it; retries use their bound value.
REVIEW_WEB_STANDING_AUTHORIZATION = True


def prepare_adapters(*args, **kwargs) -> dict:
    # Keep CLI discovery outside lifecycle accounting and native invocation.
    from review_adapters_v2 import prepare_adapters as prepare
    return prepare(*args, **kwargs)


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> bytes:
    return lifecycle._canonical_regular_file_bytes(path, "v2 custody file")


def _seal(path: Path, value: dict) -> None:
    raw = _bytes(value)
    lifecycle._write_new(path, raw)
    lifecycle._write_new(path.with_suffix(path.suffix + ".sha256"), _hash(raw).encode() + b"\n")


def _sealed(path: Path) -> dict:
    raw = _read(path)
    if _read(path.with_suffix(path.suffix + ".sha256")) != _hash(raw).encode() + b"\n":
        raise ValueError("v2 custody digest mismatch")
    data = _json(raw)
    if not isinstance(data, dict) or raw != _bytes(data):
        raise ValueError("invalid canonical v2 custody object")
    return data


def _toolkit() -> dict:
    # Bind consumed implementation, instructions, policy and vendored data. Python
    # Bytecode and wrapper-created logs are not implementation inputs. Existing
    # legacy pins and per-attempt evidence integrity remain intact.
    files = []
    for directory in ("bin", "contracts", "prompts", "skills/triad-cross-family-review"):
        dir_path = TOOLKIT / directory
        if not dir_path.exists():
            continue
        files.extend(path for path in dir_path.rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts
                     and path.relative_to(TOOLKIT).parts[:2] not in (("bin", "_logs"), ("bin", "_debug"))
                     and path.suffix not in (".pyc", ".pyo"))
    return {str(path.relative_to(TOOLKIT)): _hash(_read(path)) for path in sorted(files)}


def _adapter_files(adapters: dict) -> dict:
    paths = {Path(adapter[key]) for adapter in adapters.values()
             for key in ("preflight_file", "selector_file", "capability_file") if adapter.get(key) is not None}
    return {str(path): _hash(_read(path)) for path in sorted(paths)}


def _controls(adapter: dict) -> dict:
    # Receipt hashes include attempt-specific metadata. All actual launch
    # controls and capabilities remain invariant across a same-basis retry.
    return {key: value for key, value in adapter.items()
            if key not in ("preflight_file", "preflight_sha256", "selector_file", "capability_file")}


def _check_files(files: dict) -> None:
    for name, expected in files.items():
        if _hash(_read(Path(name))) != expected:
            raise ValueError("v2 bound artifact changed")


def _preparation(root: Path) -> Path:
    # Preserve failed preparation receipts, including a retry's failed probe.
    preflights = root / "preflight-v2"
    preflights.mkdir(exist_ok=True)
    lifecycle._canonical_directory(preflights, "v2 preparation custody")
    preparation = preflights / f"preparation-{len(list(preflights.iterdir())) + 1}"
    preparation.mkdir()
    return preparation


def _review_cwd(request: dict) -> Path:
    return Path(request["prepared_dir"] if request["mode"] == "prepared-directory" else request["worktree"])


def _load_basis(path: Path) -> dict:
    basis = _sealed(path)
    if (basis.get("schema") != "triad-round-basis.v2" or basis.get("basis_file") != str(path)
            or path.name != "basis-v2.json" or basis.get("root") != str(path.parent)):
        raise ValueError("v2 basis location or version mismatch")
    claimed = basis["content_digest"]
    substantive = {key: value for key, value in basis.items() if key != "content_digest"}
    if _hash(_bytes(substantive)) != claimed:
        raise ValueError("v2 basis content digest mismatch")
    request = basis["request"]
    if lifecycle._lifecycle_root(Path(request["prepared_dir"])) != path.parent:
        raise ValueError("v2 basis must remain in its managed review root")
    snapshot = lifecycle.RoundSnapshot(**basis["snapshot"])
    lifecycle.verify_round(snapshot, Path(request["prepared_dir"]), Path(request["worktree"]))
    if _toolkit() != basis["toolkit"] or resolve_roster(Path(request["project_root"])) != basis["roster"]:
        raise ValueError("v2 toolkit or roster changed; full new review required")
    _check_files(basis["adapter_files"])
    return basis


def create_basis(request: dict, *, root: Path) -> dict:
    required = {"review_id", "project_root", "prepared_dir", "worktree", "mode", "objective",
                "criteria", "approved_boundary", "authentication_class"}
    if not isinstance(request, dict) or not required <= set(request) or set(request) - required - {"prior_residual", "review_web_authorized", "review_kind", "native_capabilities"}:
        raise ValueError("invalid v2 review request fields")
    request = _json(_bytes(request))
    request.setdefault("review_web_authorized", False)
    request.setdefault("native_capabilities", None)
    if type(request["review_web_authorized"]) is not bool:
        raise ValueError("review_web_authorized must be a boolean")
    request["review_web_authorized"] = REVIEW_WEB_STANDING_AUTHORIZATION
    request["review_kind"] = validate_review_kind(request.get("review_kind", "pre-merge"))
    lifecycle._validate_review_id(request["review_id"])
    root = lifecycle._canonical_directory(root, "v2 review root")
    prepared = Path(request["prepared_dir"])
    worktree = Path(request["worktree"])
    if (lifecycle._lifecycle_root(prepared) != root
            or root.name != "triad-review-" + request["review_id"]):
        raise ValueError("v2 basis requires its existing managed review workspace")
    if request["mode"] not in ("guarded-worktree", "prepared-directory"):
        raise ValueError("unsupported v2 review mode")
    roster = resolve_roster(Path(request["project_root"]))
    if not roster["enabled"]:
        raise ValueError("v2 review requires at least one enabled entry")
    load_bundle()
    snapshot = lifecycle.capture_round(prepared, worktree)
    # A preparation failure owns its partial evidence. Start a new numbered
    # preparation without reusing or deleting those receipts.
    preparation = _preparation(root)
    adapters = prepare_adapters(
        roster, review_id=request["review_id"], cwd=_review_cwd(request),
        authentication_class=request["authentication_class"],
        native_capabilities=request["native_capabilities"], receipt_root=preparation,
        review_packet_dir=prepared if request["mode"] == "guarded-worktree" else None,
        **({"review_web_authorized": True} if request["review_web_authorized"] else {}))
    if set(adapters) != set(roster["enabled"]) or any(
            adapter.get("capabilities_checked") is not True for adapter in adapters.values()):
        raise ValueError("every enabled entry requires capability-checked launch controls")
    packet = [prepared / name for name in ("TASK.md", "REVIEW.diff", "SOURCE_SHA256SUMS")]
    if (prepared / "EVIDENCE.md").exists():
        packet.append(prepared / "EVIDENCE.md")
    basis = {"schema": "triad-round-basis.v2", "root": str(root),
             "basis_file": str(root / "basis-v2.json"), "request": request,
             "review_date": datetime.now(timezone.utc).date().isoformat(),
             "preparation_root": str(preparation),
             "roster": roster, "enabled": roster["enabled"], "adapters": adapters,
             "adapter_files": _adapter_files(adapters), "snapshot": dataclasses.asdict(snapshot),
             "packet_files": [str(path) for path in packet], "toolkit": _toolkit()}
    basis["content_digest"] = _hash(_bytes(basis))
    # Exercise all prompt inputs before sealing a usable basis, without dispatch.
    for name in basis["enabled"]:
        _prompt(basis, _binding(basis, name, 1), adapters[name])
    lifecycle.verify_round(snapshot, prepared, worktree)
    _seal(Path(basis["basis_file"]), basis)
    lifecycle._refresh_lifecycle_activity(prepared)
    return basis


def _binding(basis: dict, name: str, attempt: int) -> dict:
    adapter = basis["adapters"][name]
    result = {"review_id": basis["request"]["review_id"], "family": adapter["family"],
              "content_digest": basis["content_digest"], "leg_name": name,
              "attempt": attempt, "route": adapter["route"]}
    bound_verdict(result)
    return result


def _prompt(basis: dict, binding: dict, adapter: dict) -> str:
    request = basis["request"]
    prepared = Path(request["prepared_dir"])
    return render_prompt(
        expected=binding, worktree=_review_cwd(request), brief_file=prepared / "TASK.md",
        diff_file=prepared / "REVIEW.diff", packet_files=[Path(item) for item in basis["packet_files"]],
        objective=request["objective"], criteria=request["criteria"],
        approved_boundary=request["approved_boundary"], residual=request.get("prior_residual", ""),
        google_preflight_sha256=adapter["preflight_sha256"],
        review_web_authorized=request.get("review_web_authorized", False),
        review_kind=request["review_kind"], review_date=basis["review_date"])


def _invocation(binding: dict, adapter: dict, folder: Path, prompt: str) -> dict:
    del prompt
    if adapter["transport_route"] == "native":
        raise ValueError("native transport route is not supported on the Antigravity host; use CLI wrapper route")
    route = adapter["transport_route"]
    wrapper = "antigravity_wrapper.py" if route == "agy" else route + "_wrapper.py"
    argv = ["python3", str(TOOLKIT / "bin" / wrapper), "--prompt-file", str(folder / "prompt.md"),
            "--cwd", adapter["cwd"], "--pydantic", "verdict_v2:LegVerdict",
            "--timeout", str(adapter["timeout_s"])]
    if adapter.get("review_web_authorized", False):
        argv += ["--web"]
    if adapter["model"] is not None:
        argv += ["--model=" + adapter["model"]]
    if adapter["effort"] is not None:
        argv += ["--effort", adapter["effort"]]
    if route in ("agy", "codex"):
        argv += ["--sandbox", "read-only"]
    if adapter.get("review_packet_dir") is not None:
        argv += ["--review-packet-dir", adapter["review_packet_dir"]]
    if adapter.get("agent") is not None:
        argv += ["--agent", adapter["agent"]]
    for field in BINDING_FIELDS:
        argv += ["--expected-" + field.replace("_", "-"),
                 "null" if binding[field] is None else str(binding[field])]
    for key, flag in (("selector_file", "--google-selector-receipt"),
                      ("preflight_file", "--google-preflight-receipt")):
        if adapter.get(key) is not None:
            argv += [flag, adapter[key]]
    if adapter.get("project") is not None:
        argv += ["--project", adapter["project"]]
    binary_name = "AGY" if route == "agy" else route.upper()
    return {"argv": argv, "stdout_file": str(folder / "wrapper.stdout"),
            "stderr_file": str(folder / "wrapper.stderr"),
            "env": {"TRIAD_DISPATCH_LOG_DIR": str(folder / "logs"),
            "TRIAD_REQUIRE_PINNED_VENDOR": "1", "TRIAD_" + binary_name + "_BIN": adapter["binary"]}}


def _attempts(basis: dict, name: str) -> list[Path]:
    if name not in basis["enabled"]:
        raise ValueError("entry is not enabled in this basis")
    parent = Path(basis["root"]) / "results" / name
    if not parent.exists() and not parent.is_symlink():
        return []
    lifecycle._canonical_directory(parent, "v2 entry custody")
    paths = list(parent.iterdir())
    expected = [parent / f"attempt-{number}" for number in range(1, len(paths) + 1)]
    if set(paths) != set(expected):
        raise ValueError("non-contiguous or unknown v2 attempt custody")
    for path in paths:
        lifecycle._canonical_directory(path, "v2 attempt custody")
    return expected


def _allocation(basis: dict, name: str, path: Path) -> dict:
    item = _sealed(path / "allocation.json")
    attempt = int(path.name.removeprefix("attempt-"))
    if (item["binding"] != _binding(basis, name, attempt)
            or item["folder"] != str(path) or _controls(item["adapter"]) != _controls(basis["adapters"][name])):
        raise ValueError("v2 allocation does not match its basis")
    _check_files(item["files"])
    return item


def allocate_attempt(basis_file: Path, name: str, *, diagnosis: str | None = None) -> dict:
    basis = _load_basis(basis_file)
    attempts = _attempts(basis, name)
    adapter = basis["adapters"][name]
    number = len(attempts) + 1
    if attempts:
        previous = _allocation(basis, name, attempts[-1])
        terminal = _terminal(previous)
        if (terminal["state"] != "FAILED_TO_RUN" or not isinstance(diagnosis, str) or not diagnosis.strip()):
            raise ValueError("only diagnosed failed-to-run attempts permit same-basis retry")
        selected = {**basis["roster"], "legs": [entry for entry in basis["roster"]["legs"] if entry["name"] == name]}
        request = basis["request"]
        adapter = prepare_adapters(
            selected, review_id=request["review_id"], cwd=_review_cwd(request),
            authentication_class=request["authentication_class"], native_capabilities=request.get("native_capabilities"),
            receipt_root=_preparation(Path(basis["root"])), attempt=number,
            review_packet_dir=Path(request["prepared_dir"]) if request["mode"] == "guarded-worktree" else None,
            **({"review_web_authorized": True} if request.get("review_web_authorized", False) else {}))[name]
        if _controls(adapter) != _controls(basis["adapters"][name]):
            raise ValueError("launch controls changed; every entry requires a new basis")
    binding = _binding(basis, name, number)
    prompt = _prompt(basis, binding, adapter)
    parent = Path(basis["root"]) / "results" / name
    parent.mkdir(exist_ok=True)
    lifecycle._canonical_directory(parent, "v2 entry custody")
    folder = parent / f"attempt-{number}"
    try:
        folder.mkdir()
    except FileExistsError:
        raise ValueError("v2 attempt already allocated") from None
    lifecycle._write_new(folder / "prompt.md", prompt.encode("utf-8"))
    item = {"binding": binding, "folder": str(folder), "adapter": adapter, "diagnosis": diagnosis,
            "prompt_file": str(folder / "prompt.md"), "result_file": str(folder / "result.json"),
            "read_evidence_file": str(folder / "read-evidence.json"),
            "receipt_file": str(folder / "provider-receipt.json"),
            "invocation": _invocation(binding, adapter, folder, prompt),
            "files": {str(folder / "prompt.md"): _hash(prompt.encode("utf-8")),
                      **_adapter_files({name: adapter})}}
    _load_basis(basis_file)
    _seal(folder / "allocation.json", item)
    lifecycle._refresh_lifecycle_activity(Path(basis["request"]["prepared_dir"]))
    return item


def _observations(item: dict, receipt: dict, reads: dict, raw: bytes) -> tuple[str, dict | None]:
    binding, adapter = item["binding"], item["adapter"]
    if (not isinstance(receipt, dict) or receipt.get("review_binding") != binding
            or not isinstance(reads, dict) or reads.get("review_binding") != binding):
        raise ValueError("provider/read evidence belongs to another invocation")
    bound_verdict(receipt["review_binding"])
    bound_verdict(reads["review_binding"])
    if (set(reads) != {"review_binding", "exposure", "observations"}
            or reads["exposure"] not in ("observed", "unexposed")
            or (reads["exposure"] == "unexposed" and reads["observations"] is not None)
            or (reads["exposure"] == "observed" and not isinstance(reads["observations"], list))):
        raise ValueError("read observations must distinguish unexposed from observed empty")
    transport = receipt.get("transport")
    if not Draft202012Validator(load_contracts()["receipt-fields.json"], registry=Registry()).is_valid(transport):
        raise ValueError("invalid v2 host transport receipt")
    expected = {"attempt": binding["attempt"], "route": adapter["transport_route"],
                "binary": adapter["binary"], "cli_version": adapter["cli_version"]}
    # Preflight's version observation is retained separately. A provider run
    # that does not expose its version must not inherit that observation.
    if transport["cli_version"] is None:
        expected["cli_version"] = None
    prestart = receipt.get("observation_source") == "host-start-failure"
    if prestart:
        if (receipt.get("provider_started") is not False or receipt.get("exit_code") == 0
                or receipt.get("vendor_exit_code") != -1 or receipt.get("classification") != "start-failure"
                or transport["stdin_delivery"] != ("not-used" if adapter["transport_route"] == "native" else "not-started")):
            raise ValueError("invalid pre-start failure observation")
        expected.update(binary=None, cli_version=None)
    if any(transport[key] != value for key, value in expected.items()):
        raise ValueError("host transport does not match the selected invocation")
    source = "host-start-failure" if prestart else "native-host" if adapter["transport_route"] == "native" else "cli-run-log"
    if (receipt.get("observation_source") != source
            or not isinstance(receipt.get("provider_reference"), str) or not receipt["provider_reference"].strip()
            or any(type(receipt.get(key)) is not int for key in ("exit_code", "vendor_exit_code"))
            or any(not isinstance(receipt.get(key), str) for key in ("stdout", "stderr", "classification"))):
        raise ValueError("terminal host observation is required")
    failed = receipt["exit_code"] != 0 or receipt["vendor_exit_code"] != 0 or receipt["classification"] != "ok"
    if failed:
        # A completed invalid answer is not a failed-to-run review. Retain it,
        # but never turn schema/extraction failure into a transport retry.
        state = "INVALID" if receipt["classification"] in ("schema-fail", "extraction-error", "identity-mismatch") else "FAILED_TO_RUN"
        return state, None
    allowed_delivery = (("not-used", "unexposed") if source == "native-host" else
                        ("not-used",) if adapter["transport_route"] in ("agy", "gemini") else
                        ("complete",))
    if transport["stdin_delivery"] not in allowed_delivery:
        raise ValueError("successful review lacks complete transport evidence")
    try:
        verdict = validate_verdict(raw, expected=binding)
    except (ValueError, UnicodeError):
        return "INVALID", None
    if receipt.get("validated") != verdict:
        return "INVALID", None
    return "COMPLETE", verdict


def _terminal(item: dict) -> dict:
    terminal = _sealed(Path(item["folder"]) / "terminal.json")
    if terminal.get("binding") != item["binding"]:
        raise ValueError("terminal binding mismatch")
    _check_files(terminal["files"])
    state, verdict = _observations(item, _json(_read(Path(item["receipt_file"]))),
                                   _json(_read(Path(item["read_evidence_file"]))),
                                   _read(Path(item["result_file"])))
    if state != terminal["state"] or verdict != terminal["verdict"]:
        raise ValueError("terminal record differs from its evidence")
    return terminal


def record_attempt(basis_file: Path, name: str, *, receipt: dict,
                   raw_verdict: bytes, read_evidence: dict, host_files: dict | None = None) -> dict:
    basis = _load_basis(basis_file)
    attempts = _attempts(basis, name)
    if not attempts:
        raise ValueError("entry has no allocated invocation")
    item = _allocation(basis, name, attempts[-1])
    state, verdict = _observations(item, receipt, read_evidence, raw_verdict)
    observed = {str(path): _hash(raw) for path, raw in (host_files or {}).items()}
    _check_files(observed)
    files = {item["result_file"]: raw_verdict, item["read_evidence_file"]: _bytes(read_evidence),
             item["receipt_file"]: _bytes(receipt)}
    for path, raw in files.items():
        lifecycle._write_new(Path(path), raw)
    terminal = {"binding": item["binding"], "state": state, "verdict": verdict,
                "files": {**observed, **{path: _hash(raw) for path, raw in files.items()}}}
    _load_basis(basis_file)
    _seal(attempts[-1] / "terminal.json", terminal)
    lifecycle._refresh_lifecycle_activity(Path(basis["request"]["prepared_dir"]))
    return terminal


def _latest(basis_file: Path, name: str) -> dict:
    basis = _load_basis(basis_file)
    attempts = _attempts(basis, name)
    if not attempts:
        raise ValueError("entry has no allocated invocation")
    return _allocation(basis, name, attempts[-1])


def _host_file(item: dict, path: Path) -> bytes:
    folder = Path(item["folder"])
    if not path.is_relative_to(folder):
        raise ValueError("host evidence must belong to this allocated invocation")
    reserved = {Path(item[key]) for key in ("prompt_file", "result_file", "read_evidence_file", "receipt_file")}
    reserved.update(folder / name for name in ("allocation.json", "allocation.json.sha256",
                                                "terminal.json", "terminal.json.sha256"))
    if path in reserved:
        raise ValueError("host input evidence must not use collector-owned output or metadata paths")
    return _read(path)


def _reads(item: dict, path: Path | None, files: dict) -> dict:
    if path is None:
        return {"review_binding": item["binding"], "exposure": "unexposed", "observations": None}
    files[path] = _host_file(item, path)
    return _json(files[path])


def record_cli_attempt(basis_file: Path, name: str, *, run_log: Path,
                       read_evidence: Path | None = None) -> dict:
    item = _latest(basis_file, name)
    adapter = item["adapter"]
    cli = "antigravity" if adapter["transport_route"] == "agy" else adapter["transport_route"]
    if cli == "native" or run_log.parent != Path(item["folder"]) / "logs" / cli / "runs":
        raise ValueError("run log must belong to the allocated CLI attempt")
    files = {run_log: _host_file(item, run_log)}
    receipt = _json(files[run_log])
    if (not isinstance(receipt, dict) or receipt.get("cli") != cli
            or receipt.get("wrapper_cmd") != item["invocation"]["argv"][1:]):
        raise ValueError("run log does not match the allocated wrapper invocation")
    for key in ("stdout_file", "stderr_file"):
        path = Path(item["invocation"][key])
        files[path] = _host_file(item, path)
    receipt = {**receipt, "observation_source": "cli-run-log", "provider_reference": str(run_log)}
    reads = _reads(item, read_evidence, files)
    return record_attempt(basis_file, name, receipt=receipt,
                          raw_verdict=files[Path(item["invocation"]["stdout_file"])],
                          read_evidence=reads, host_files=files)


def record_native_attempt(basis_file: Path, name: str, *, host_receipt: Path,
                          result_file: Path, read_evidence: Path | None = None) -> dict:
    del basis_file, name, host_receipt, result_file, read_evidence
    raise ValueError("native host observations are not supported on the Antigravity host; use record_cli_attempt")


def record_start_failure(basis_file: Path, name: str, *, host_receipt: Path) -> dict:
    item = _latest(basis_file, name)
    raw = _host_file(item, host_receipt)
    host = _json(raw)
    if (not isinstance(host, dict) or set(host) != {"review_binding", "provider_started", "exit_code", "stderr"}
            or host["provider_started"] is not False or type(host["exit_code"]) is not int
            or host["exit_code"] == 0 or not isinstance(host["stderr"], str) or not host["stderr"].strip()):
        raise ValueError("invalid host start-failure observation")
    receipt = {**host, "observation_source": "host-start-failure", "provider_reference": str(host_receipt),
               "vendor_exit_code": -1, "classification": "start-failure", "stdout": "", "validated": None,
               "runtime_identity": "unexposed", "transport": {"schema_version": 2,
                   "route": item["adapter"]["transport_route"], "binary": None, "cli_version": None,
                   "attempt": item["binding"]["attempt"],
                   "stdin_delivery": "not-used" if item["adapter"]["transport_route"] == "native" else "not-started"}}
    return record_attempt(basis_file, name, receipt=receipt, raw_verdict=b"",
                          read_evidence=_reads(item, None, {}), host_files={host_receipt: raw})


def collect(basis_file: Path) -> dict:
    basis = _load_basis(basis_file)
    legs, missing, deviations, families = {}, [], [], set()
    blocked = False
    for name in basis["enabled"]:
        attempts = _attempts(basis, name)
        terminal = None
        for folder in attempts:
            item = _allocation(basis, name, folder)
            terminal = _terminal(item) if (folder / "terminal.json").exists() else None
            if folder != attempts[-1] and (terminal is None or terminal["state"] != "FAILED_TO_RUN"):
                raise ValueError("retry history requires every prior failed-to-run terminal")
        if terminal is None or terminal["state"] != "COMPLETE":
            missing.append(name)
            legs[name] = {"state": terminal["state"] if terminal else "MISSING"}
            continue
        verdict = terminal["verdict"]
        families.add(verdict["family"])
        blocking = bool(verdict["open_questions"]) or any(
            finding["severity"] in ("Critical", "must-fix") for finding in verdict["findings"])
        blocked |= blocking or verdict["verdict"] != "SAFE TO MERGE"
        if not blocking and verdict["verdict"] != "SAFE TO MERGE":
            deviations.append(name)
        legs[name] = {"state": "COMPLETE", "attempt": verdict["attempt"], "verdict": verdict}
    status = "INCOMPLETE" if missing else "BLOCKED" if blocked else "AGREED"
    _load_basis(basis_file)
    return {"status": status, "content_digest": basis["content_digest"], "legs": legs,
            "families": sorted(families), "missing": missing, "selection_deviations": deviations}
