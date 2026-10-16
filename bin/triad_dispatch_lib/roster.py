"""Offline named-roster resolution without provider defaults or host I/O."""

from copy import deepcopy

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from .contracts import load_roster_contract
from .errors import AdmissionError, ContractError, RosterError
from .json_input import parse_json


def _validate(value: dict, schema: dict, *, resolved: bool) -> None:
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
    target = {"$ref": schema["$id"] + "#/$defs/resolvedRoster"} if resolved else schema
    try:
        if not Draft202012Validator(target, registry=registry).is_valid(value):
            raise RosterError("schema_violation")
        names = [leg["name"] for leg in value["legs"]]
        if len(names) != len(set(names)):
            raise RosterError("duplicate_name")
    except Unresolvable:
        raise ContractError("External or unresolved roster schema reference.") from None
    except RecursionError:
        raise RosterError("nesting_limit") from None


def _merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        result = deepcopy(base)
        for key, value in override.items():
            result[key] = _merge(result.get(key), value)
        return result
    return deepcopy(override)


def resolve_roster(defaults: dict, overrides: str | bytes | None = None) -> dict:
    """Validate defaults, merge strict named overrides, then validate all entries."""
    schema = load_roster_contract()
    _validate(defaults, schema, resolved=True)
    result = deepcopy(defaults)
    if overrides is not None:
        try:
            override = parse_json(overrides)
        except AdmissionError:
            raise RosterError("invalid_json") from None
        _validate(override, schema, resolved=False)
        entries = {leg["name"]: index for index, leg in enumerate(result["legs"])}
        for leg in override["legs"]:
            if leg["name"] in entries:
                index = entries[leg["name"]]
                result["legs"][index] = _merge(result["legs"][index], leg)
            else:
                result["legs"].append(deepcopy(leg))
        for key, value in override.items():
            if key != "legs":
                result[key] = deepcopy(value)
    _validate(result, schema, resolved=True)
    return result


def select_entries(roster: dict) -> list[dict]:
    """Return independent enabled entries in canonical roster order."""
    _validate(roster, load_roster_contract(), resolved=True)
    selected = [deepcopy(leg) for leg in roster["legs"] if leg["enabled"]]
    if not selected:
        raise RosterError("empty_selection")
    return selected
