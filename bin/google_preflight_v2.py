"""Public v2 Google binding; legacy fixed-model receipt validation stays intact."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry

import review_round
import _agy_agent
from validate_v2 import _json, load_contracts, validate_review_kind


def fields(args, cwd: str | None, selector) -> dict:
    """Freeze entry/attempt and requested arguments before content is rendered."""
    identities = {
        "review_id": args.expected_review_id, "leg_name": args.expected_leg_name,
        "attempt": args.expected_attempt, "route": args.expected_route,
    }
    properties = load_contracts()["leg-verdict.schema.json"]["properties"]
    shape = {"type": "object", "required": list(identities),
             "properties": {key: properties[key] for key in identities}}
    if not Draft202012Validator(shape, registry=Registry()).is_valid(identities):
        raise ValueError("v2 Google preflight requires complete entry/attempt binding")
    if args.expected_route != selector.route or args.expected_review_id != selector.review_id:
        raise ValueError("v2 Google selector binding mismatch")
    if cwd is None or args.timeout <= 0 or not isinstance(args.model, str) or not args.model.strip():
        raise ValueError("v2 Google review requires cwd, positive timeout and explicit model")
    effort = getattr(args, "effort", None)
    if selector.route == "agy":
        if effort not in ("low", "medium", "high"):
            raise ValueError("v2 AGY review requires an explicit supported effort")
        route_args = (["--model=" + args.model] if args.model.startswith("-") else ["--model", args.model])
        route_args += ["--effort", effort]
        if getattr(args, "project", None) is not None:
            route_args += ["--project", args.project]
    else:
        if effort is not None:
            raise ValueError("Gemini has no supported effort flag")
        route_args = ["--model=" + args.model]
    packet_fields = {}
    packet = getattr(args, "review_packet_dir", None)
    if packet is not None:
        if selector.route != "gemini" or not isinstance(packet, str):
            raise ValueError("review packet directory is a Gemini v2 launch control")
        path = review_round._canonical_directory(Path(packet), "review packet directory")
        root = review_round._lifecycle_root(path)
        if (packet != str(path) or "," in packet or root is None
                or root.name != "triad-review-" + selector.review_id):
            raise ValueError("review packet directory must be the exact canonical shared directory for this round")
        packet_fields["review_packet_dir"] = packet
        route_args += ["--include-directories", packet]
    return {**packet_fields, **({"review_web_authorized": True} if getattr(args, "web", False) else {}), "schema_version": 2, "leg_name": args.expected_leg_name,
            "attempt": args.expected_attempt, "cwd": cwd, "timeout_s": args.timeout,
            "model": args.model, "effort": effort, "route_args": route_args}


def load_preflight(path: Path, selector, args, cwd: str | None) -> dict:
    """Validate provider-free evidence before a prompt can carry its digest."""
    payload = review_round._canonical_regular_file_bytes(path, "v2 Google preflight")
    record = _json(payload)
    expected = {
        **fields(args, cwd, selector), "review_id": selector.review_id,
        "route": selector.route, "executable": str(selector.executable),
        "google_selector_receipt_sha256": selector.receipt_sha256,
        "provider_started": False,
    }
    extra = ({"agy_version"} | _agy_agent.PROFILE_KEYS if selector.route == "agy" else {
        "gemini_version", "effective_approval_mode", "requested_approval_mode",
        "policy", "policy_sha256", "read_only_enforcement",
    })
    if (not isinstance(record, dict) or set(record) != set(expected) | extra
            or payload != review_round._canonical_json_bytes(record)
            or any(type(record[key]) is not type(value) or record[key] != value
                   for key, value in expected.items())):
        raise ValueError("v2 Google preflight schema or invocation binding mismatch")
    if selector.route == "agy":
        _agy_agent.validate_binding(record, getattr(args, "web", False), source_bin=selector.wrapper.parent)
        version = record["agy_version"]
        if (not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None
                or tuple(map(int, version.split("."))) < (1, 2, 1)):
            raise ValueError("v2 AGY preflight version is invalid")
    else:
        review_round.validate_formal_gemini_version(record["gemini_version"])
        policy = selector.wrapper.parent / "policies" / ("gemini-formal-web.toml" if getattr(args, "web", False) else "gemini-formal-readonly.toml")
        expected_policy = {
            "effective_approval_mode": "unexposed", "requested_approval_mode": "plan",
            "read_only_enforcement": "packaged-mode-independent-policy",
            "policy": str(policy),
            "policy_sha256": hashlib.sha256(review_round._canonical_regular_file_bytes(
                policy, "Gemini policy")).hexdigest(),
        }
        if any(record[key] != value for key, value in expected_policy.items()):
            raise ValueError("v2 Gemini policy binding mismatch")
    return record


def load_receipt(path: Path, selector, args, cwd: str | None, prompt: str) -> dict:
    """Refuse sibling receipts, changed controls and an unbound prompt pre-start."""
    record = load_preflight(path, selector, args, cwd)
    payload = review_round._canonical_regular_file_bytes(path, "v2 Google preflight")
    if _json(payload) != record:
        raise ValueError("v2 Google preflight changed during validation")
    metadata = {
        "review_id": args.expected_review_id, "family": "google",
        "content_digest": args.expected_content_digest, "leg_name": args.expected_leg_name,
        "attempt": args.expected_attempt, "route": selector.route,
        "google_preflight_receipt_sha256": hashlib.sha256(payload).hexdigest(),
    }
    if getattr(args, "web", False):
        metadata["review_web_authorized"] = True
    prefix = "Review v2 metadata: "
    lines = [line[len(prefix):] for line in prompt.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise ValueError("v2 Google prompt binding mismatch")
    actual = _json(lines[0])
    if isinstance(actual, dict) and "review_kind" in actual:
        metadata["review_kind"] = validate_review_kind(actual["review_kind"])
    if actual != metadata:
        raise ValueError("v2 Google prompt binding mismatch")
    return record
