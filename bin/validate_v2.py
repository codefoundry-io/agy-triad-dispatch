#!/usr/bin/env python3
"""Explicit offline v2 validation; existing dispatch/gate routes remain legacy."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

import library_adoption
from verdict_schema import _read_canonical_regular_file


CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "contracts"
SOURCE_REPOSITORY = "https://github.com/codefoundry-io/triad-dispatch-spec"
SOURCE_COMMIT = "fb7c962c83273c8b9c48fd11c482d1f5d50ef24d"
PAYLOADS = ("leg-verdict.schema.json", "review-legs.schema.json", "receipt-fields.json", "review-kind.schema.json")
BINDING_FIELDS = ("review_id", "family", "content_digest", "leg_name", "attempt", "route")


def _library():
    """Verify the shipped tree before importing or using any shared API."""
    try:
        library_adoption.verify_library()
    except (OSError, ValueError) as error:
        raise RuntimeError("invalid bundled shared library") from error
    package = Path(__file__).resolve().parent / "triad_dispatch_lib"
    for name, module in list(sys.modules.items()):
        if name == "triad_dispatch_lib" or name.startswith("triad_dispatch_lib."):
            origin = getattr(module, "__file__", None)
            if origin is None or not Path(origin).resolve().is_relative_to(package):
                raise RuntimeError("ambient shared library is not the adopted package")
    spec = importlib.util.find_spec("triad_dispatch_lib")
    if spec is None or spec.origin is None or Path(spec.origin).resolve() != package / "__init__.py":
        raise RuntimeError("ambient shared library is not the adopted package")
    import triad_dispatch_lib
    if Path(triad_dispatch_lib.__file__).resolve() != package / "__init__.py":
        raise RuntimeError("ambient shared library is not the adopted package")
    return triad_dispatch_lib


def _json(raw: str | bytes):
    return _library().parse_json(raw)


def load_contracts() -> dict:
    """Read the fixed candidate bundle; hashes establish integrity, not a signature."""
    try:
        manifest = _json(_read_canonical_regular_file(CONTRACT_ROOT / "source-manifest.json"))
    except (OSError, ValueError):
        raise ValueError("invalid candidate contract: source-manifest.json") from None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"source_repository", "source_commit", "status", "sha256"}
        or manifest["source_repository"] != SOURCE_REPOSITORY
        or manifest["source_commit"] != SOURCE_COMMIT
        or manifest["status"] != "candidate"
        or not isinstance(manifest["sha256"], dict)
        or set(manifest["sha256"]) != set(PAYLOADS)
    ):
        raise ValueError("invalid candidate source manifest")
    try:
        metadata = library_adoption.verify_library()
    except (OSError, ValueError) as error:
        raise RuntimeError("invalid bundled shared library") from error
    compatibility = {
        "leg-verdict.schema.json": "bin/triad_dispatch_lib/data/leg-verdict.schema.json",
        "review-legs.schema.json": "bin/triad_dispatch_lib/data/roster-bundle/contracts/review-legs.schema.json",
    }
    schemas = {}
    for name in PAYLOADS:
        try:
            raw = _read_canonical_regular_file(CONTRACT_ROOT / name)
        except (OSError, ValueError):
            raise ValueError(f"unreadable candidate contract: {name}") from None
        if hashlib.sha256(raw).hexdigest() != manifest["sha256"][name]:
            raise ValueError(f"contract digest mismatch: {name}")
        try:
            schema = _json(raw)
            if not isinstance(schema, dict) or schema.get("$schema") != Draft202012Validator.META_SCHEMA["$id"]:
                raise ValueError("unsupported schema dialect")
            Draft202012Validator.check_schema(schema)
        except (ValueError, SchemaError):
            raise ValueError(f"invalid candidate contract: {name}") from None
        if name in compatibility and hashlib.sha256(raw).hexdigest() != metadata["sha256"][compatibility[name]]:
            raise ValueError(f"candidate compatibility contract drift: {name}")
        schemas[name] = schema
    return schemas


def validate_review_kind(value: object) -> str:
    """Validate an explicit phase offline; callers own omission defaults."""
    try:
        schema = load_contracts()["review-kind.schema.json"]
        Draft202012Validator(schema, registry=Registry()).validate(value)
        return value
    except ValidationError as error:
        raise ValueError(f"schema violation at {error.json_path}: {error.validator}") from None
    except Unresolvable:
        raise ValueError("external or unresolved schema reference") from None
    except RecursionError:
        raise ValueError("JSON/schema nesting exceeds decoder limit") from None


def validate_verdict(raw: str | bytes, *, expected: dict) -> dict:
    """Validate original JSON and all invocation bindings without rewriting values."""
    shared = _library()
    load_contracts()
    return shared.validate_verdict(raw, expected=expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--result-file", type=Path, required=True)
    validate.add_argument("--expected-review-id", required=True)
    validate.add_argument("--expected-family", choices=("claude", "codex", "google"), required=True)
    validate.add_argument("--expected-content-digest", required=True)
    validate.add_argument("--expected-leg-name", required=True)
    validate.add_argument("--expected-attempt", type=int, required=True)
    validate.add_argument("--expected-route", choices=("null", "agy", "gemini"), required=True)
    args = parser.parse_args(argv)
    expected = {name: getattr(args, "expected_" + name) for name in BINDING_FIELDS}
    if expected["route"] == "null":
        expected["route"] = None
    try:
        result = validate_verdict(_read_canonical_regular_file(args.result_file), expected=expected)
    except (OSError, ValueError) as error:
        print(f"invalid v2 LegVerdict: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
