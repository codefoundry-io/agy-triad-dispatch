"""Resolve project review entries without starting a provider or claiming support."""
from __future__ import annotations

import stat
import json
from pathlib import Path

from validate_v2 import _json, _library, load_contracts
from verdict_schema import _read_canonical_regular_file


DEFAULTS = Path(__file__).resolve().parents[1] / "contracts/review-legs.default.json"


def _check_placeholders(document: dict) -> None:
    for entry in document["legs"]:
        for provider in ("claude", "codex", "agy", "gemini"):
            for value in entry.get(provider, {}).values():
                if isinstance(value, str) and any(token in value for token in ("<", ">", "${", "{{", "}}")):
                    raise ValueError("unresolved adapter placeholder")


def _validate(document: object, schema: dict, *, resolved: bool) -> None:
    """Retain the adapter validation interface using the canonical shared API."""
    shared = _library()
    load_contracts()
    if resolved:
        shared.resolve_roster(document)
    else:
        defaults = _json(_read_canonical_regular_file(DEFAULTS))
        shared.resolve_roster(defaults, json.dumps(document, allow_nan=False))
    _check_placeholders(document)


def resolve_roster(project_root: Path) -> dict:
    """Return resolved data and its display metadata; execution validates capabilities."""
    try:
        root = project_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("project root must be an existing directory")
        shared = _library()
        load_contracts()
        defaults = _json(_read_canonical_regular_file(DEFAULTS))
        config = root / ".agents" / "triad-review-legs.json"
        configured = False
        try:
            directory = config.parent.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(directory.st_mode):
                raise ValueError("project .agents must be a real directory")
            try:
                config.lstat()
            except FileNotFoundError:
                pass
            else:
                configured = True
        override = _read_canonical_regular_file(config) if configured else None
        roster = shared.resolve_roster(defaults, override)
        _check_placeholders(defaults)
        if (len(defaults["legs"]) != 3
                or {leg["vendor"] for leg in defaults["legs"]} != {"claude", "codex", "google"}
                or not all(leg["enabled"] for leg in defaults["legs"])):
            raise ValueError("shipped defaults must enable exactly one entry per family")
        _check_placeholders(roster)
        enabled = [entry for entry in roster["legs"] if entry["enabled"]]
        return {**roster, "config_path": str(config) if configured else None,
                "enabled": [entry["name"] for entry in enabled],
                "families": sorted({entry["vendor"] for entry in enabled}),
                "capabilities_checked": False}
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(f"invalid roster: {error}") from None
