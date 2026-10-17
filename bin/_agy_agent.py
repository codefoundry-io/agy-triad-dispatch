"""Explicit AGY profile provisioning and formal-review definition binding."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile


PROFILE_KEYS = {"agent", "agent_path", "agent_sha256"}


def agents_dir() -> Path:
    return Path.home() / ".gemini" / "config" / "agents"


def profile(web: bool = False, *, source_bin: Path | None = None) -> tuple[str, bytes]:
    name = "triad-codex-readonly-" + ("research" if web else "review")
    source = (source_bin or Path(__file__).resolve().parent) / "agents" / f"{name}.md"
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"AGY profile source is not a regular file: {source}")
    body = source.read_bytes()
    if not body:
        raise ValueError(f"AGY profile source is empty: {source}")
    return name, body


def controls(web: bool = False) -> dict[str, str]:
    name, body = profile(web)
    target = agents_dir() / f"{name}.md"
    if target.is_symlink() or not target.is_file() or target.read_bytes() != body:
        raise ValueError(f"AGY profile missing or changed: {target}; explicitly run bin/_agy_agent.py --install (review changes before --replace)")
    return {"agent": name, "agent_path": str(target),
            "agent_sha256": hashlib.sha256(body).hexdigest()}


def validate_binding(record: dict, web: bool, *, source_bin: Path) -> None:
    """Validate receipt source identity; dispatch checks the live installed copy."""
    name, body = profile(web, source_bin=source_bin)
    path = record.get("agent_path")
    if (record.get("agent") != name
            or record.get("agent_sha256") != hashlib.sha256(body).hexdigest()
            or not isinstance(path, str) or not Path(path).is_absolute()
            or ".." in Path(path).parts or Path(path).name != f"{name}.md"):
        raise ValueError("AGY profile binding mismatch")


def install(directory: Path, *, replace: bool = False) -> list[Path]:
    """Only explicit setup writes; never called by preflight or dispatch."""
    items = [(directory / f"{name}.md", body) for name, body in (profile(), profile(True))]
    for target, body in items:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"Refusing non-regular AGY profile: {target}")
        if target.exists() and target.read_bytes() != body and not replace:
            raise ValueError(f"AGY profile differs: {target}; review before --replace")
    directory.mkdir(parents=True, exist_ok=True)
    for target, body in items:
        if target.exists() and target.read_bytes() == body:
            continue
        if not replace:
            with target.open("xb") as handle:
                handle.write(body)
        else:
            fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(body)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return [target for target, _ in items]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", required=True)
    parser.add_argument("--replace", action="store_true", help="Replace changed B-owned profiles after review")
    args = parser.parse_args()
    try:
        for path in install(agents_dir(), replace=args.replace):
            print(path)
    except (ValueError, OSError) as error:
        parser.exit(65, f"{error}\n")
