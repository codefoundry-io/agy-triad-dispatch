"""Pydantic producer boundary backed by the unchanged canonical v2 contract."""
from __future__ import annotations

import copy
import json
from typing import Any, ClassVar

from pydantic import RootModel, model_validator

from validate_v2 import BINDING_FIELDS, _json, _library, load_contracts, validate_verdict


class LegVerdict(RootModel[dict[str, Any]]):
    """Wrapper-compatible model; field definitions remain in the shared schema."""

    _binding: ClassVar[dict | None] = None

    @staticmethod
    def _triad_check_original_json(raw: str | bytes) -> None:
        _json(raw)

    @classmethod
    def model_validate_json(cls, json_data, **kwargs):
        # Validate original members even when called outside the wrapper helper.
        return cls.model_validate(_json(json_data), **kwargs)

    @model_validator(mode="before")
    @classmethod
    def validate_canonical(cls, value):
        if not isinstance(value, dict):
            raise ValueError("v2 verdict must be an object")
        expected = cls._binding
        if expected is None:
            expected = {field: value.get(field) for field in BINDING_FIELDS}
        return validate_verdict(json.dumps(value, allow_nan=False), expected=expected)

    @classmethod
    def model_json_schema(cls, **kwargs):
        _library()
        load_contracts()
        from triad_dispatch_lib.contracts import load_contract
        schema = load_contract()
        if cls._binding is not None:
            for field, value in cls._binding.items():
                schema["properties"][field]["const"] = value
                if field == "route":
                    schema["properties"][field]["enum"] = [value]
        return schema


def bound_verdict(expected: dict) -> type[LegVerdict]:
    """Bind one invocation without modifying another model or the shared schema."""
    shared = _library()
    load_contracts()
    shared.validate_binding(expected)

    class BoundLegVerdict(LegVerdict):
        _binding: ClassVar[dict | None] = copy.deepcopy(expected)

    return BoundLegVerdict


def bound_wrapper(args, *, family: str, route: str | None) -> type[LegVerdict]:
    """Require one complete invocation tuple, including an explicit null route."""
    import _common
    if not _common._LOG_DIR_CONFIGURED:
        raise ValueError("v2 review requires a configured per-attempt TRIAD_DISPATCH_LOG_DIR")
    expected = {name: getattr(args, "expected_" + name) for name in BINDING_FIELDS}
    if any(value is None for value in expected.values()):
        raise ValueError("v2 review requires all six expected bindings")
    if expected["route"] == "null":
        expected["route"] = None
    if expected["family"] != family or expected["route"] != route:
        raise ValueError("v2 binding does not match this wrapper route")
    return bound_verdict(expected)


def producer_schema(cls: type[LegVerdict]) -> dict:
    """CLI generation aid only; local validation retains every full constraint.

    Claude 2.1.271 rejects the draft declaration and top-level allOf. The actual
    CLI accepted this exact projection. No field/type/reference is translated.
    """
    schema = cls.model_json_schema()
    for field in ("$schema", "$id", "allOf"):
        schema.pop(field)
    return schema


def admit_carrier_output(
    cls: type[LegVerdict],
    *,
    carrier: str,
    stdout: str | bytes,
    exit_code: int = 0,
    completed: bool = True,
) -> dict[str, Any]:
    """Validate original provider output through triad_dispatch_lib.output.admit_output."""
    if cls._binding is None:
        raise ValueError("admit_carrier_output requires a bound v2 LegVerdict class")
    _library()
    load_contracts()
    from triad_dispatch_lib.output import ProviderOutput, admit_output
    provider_output = ProviderOutput(
        carrier=carrier,
        stdout=stdout,
        exit_code=exit_code,
        completed=completed,
    )
    return admit_output(provider_output, expected=cls._binding)

