"""Read-only checks for existing AGY project permissions and requested web.

Never writes, locks, restores or heals machine settings or legacy transaction
residue. AGY_SETTINGS_PATH is the host's test seam, not a vendor override.
"""
from __future__ import annotations
import contextlib
import json
import os
import uuid
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

# Existing optional project records must retain their declared deny contract.
_READ_ONLY_DENY = [
    "write_file(*)",
    "command(*)",
    "unsandboxed(*)",
    "execute_url(*)",
    "mcp(*)",
]
_FORMAL_REVIEW_DENY = [*_READ_ONLY_DENY, "read_url(*)"]


def build_deny_rules(mode: str, *, formal_review: bool = False) -> list:
    """Return the permissions.deny list for an agy sandbox mode."""
    if mode == "read-only":
        return list(_FORMAL_REVIEW_DENY if formal_review else _READ_ONLY_DENY)
    raise ValueError(f"unknown sandbox mode: {mode!r}")


def validate_project_id(project: str) -> None:
    """Accept only the canonical UUID spelling used by an explicit AGY project."""
    if str(uuid.UUID(project)) != project:
        raise ValueError("--project must be a canonical lowercase UUID")


@contextlib.contextmanager
def agy_project_guard(project: str, cwd: str, *, formal_review: bool = False,
                      require_web: bool = False):
    """Check the owner's project configuration without acquiring a global lease.

    The owner keeps this configuration stable during the call. This local check
    is not runtime policy attestation or a lock against external configuration edits.
    """
    validate_project_id(project)
    path = Path.home() / ".gemini" / "config" / "projects" / f"{project}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    try:
        if not isinstance(record, dict) or record.get("id") != project:
            raise ValueError("AGY project ID mismatch")
        resources = record["projectResources"]["resources"]
        if (
            not isinstance(resources, list)
            or len(resources) != 1
            or not isinstance(resources[0], dict)
            or resources[0].get("folderUri") != Path(cwd).resolve(strict=True).as_uri()
        ):
            raise ValueError("AGY project must contain only the current --cwd resource")
        deny = record["permissionGrants"]["permissionGrants"]["deny"]
        if (
            not isinstance(deny, list)
            or not all(isinstance(rule, str) for rule in deny)
            or not set(build_deny_rules("read-only", formal_review=formal_review)).issubset(deny)
        ):
            raise ValueError("AGY project is missing the read-only deny rules")
    except (KeyError, TypeError) as exc:
        raise ValueError("AGY project permission configuration is invalid") from exc
    if require_web:
        _reject_web_url_deny(deny)
    yield


def _reject_web_url_deny(deny: list) -> None:
    if not isinstance(deny, list) or any(not isinstance(rule, str) for rule in deny):
        raise ValueError("AGY review web requires a valid local deny list")
    if "read_url(*)" in deny:
        raise ValueError("requested review web conflicts with owner read_url(*) deny")


def _settings_path() -> Path:
    env = os.environ.get("AGY_SETTINGS_PATH")
    return Path(env) if env else _DEFAULT_PATH


def check_web_settings() -> None:
    """Reject a known local URL-wide deny without changing owner state.

    Absence means no locally observed deny, not permission-layer attestation.
    Malformed existing settings fail closed only on requested web paths.
    """
    path = _settings_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    try:
        data = json.loads(text)
        permissions = data.get("permissions", {})
        if not isinstance(permissions, dict):
            raise ValueError("permissions must be an object")
        _reject_web_url_deny(permissions.get("deny", []))
    except (ValueError, AttributeError, UnicodeError) as exc:
        raise ValueError(f"AGY requested web settings conflict at {path}: {exc}") from exc
