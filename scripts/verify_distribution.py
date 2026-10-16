#!/usr/bin/env python3
"""Stage and test exact plugin archive bytes from a clean Git HEAD."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


HASH_TARGETS = (
    "plugin.json",
    ".antigravity-plugin/plugin.json",
    "skills/triad-claude-dispatch/SKILL.md",
    "skills/triad-codex-dispatch/SKILL.md",
    "skills/triad-antigravity-dispatch/SKILL.md",
    "skills/triad-gemini-dispatch/SKILL.md",
    "skills/triad-cross-family-review/SKILL.md",
    "skills/triad-cross-family-review/references/convergence.md",
    "skills/triad-cross-family-review/references/review-web.md",
    "bin/_common.py",
    "bin/antigravity_wrapper.py",
    "bin/claude_wrapper.py",
    "bin/codex_wrapper.py",
    "bin/gemini_wrapper.py",
    "bin/review_round.py",
    "bin/review_round_v2.py",
    "bin/review_adapters_v2.py",
    "bin/review_roster.py",
    "bin/review_prompts_v2.py",
    "bin/verdict_v2.py",
    "bin/google_preflight_v2.py",
    "bin/verdict_schema.py",
    "bin/validate_v2.py",
    "contracts/leg-verdict.schema.json",
    "contracts/review-legs.schema.json",
    "contracts/review-legs.default.json",
    "contracts/receipt-fields.json",
    "contracts/review-kind.schema.json",
    "contracts/source-manifest.json",
    "prompts/review-v2/common-clauses.md",
    "prompts/review-v2/leg-claude.md",
    "prompts/review-v2/leg-codex.md",
    "prompts/review-v2/leg-google.md",
    "prompts/review-v2/source-manifest.json",
    "requirements.txt",
    "scripts/bootstrap.sh",
    "bin/bootstrap_repair.py",
    "bin/library_adoption.py",
    "contracts/library-adoption.json",
    "bin/triad_dispatch_lib/__init__.py",
    "bin/triad_dispatch_lib/agreement.py",
    "bin/triad_dispatch_lib/bundle_manifest.py",
    "bin/triad_dispatch_lib/contracts.py",
    "bin/triad_dispatch_lib/data/leg-verdict.schema.json",
    "bin/triad_dispatch_lib/data/prompt-bundle/contracts/review-kind.schema.json",
    "bin/triad_dispatch_lib/data/prompt-bundle/prompts/common-clauses.md",
    "bin/triad_dispatch_lib/data/prompt-bundle/prompts/investigation.md",
    "bin/triad_dispatch_lib/data/prompt-bundle/prompts/leg-claude.md",
    "bin/triad_dispatch_lib/data/prompt-bundle/prompts/leg-codex.md",
    "bin/triad_dispatch_lib/data/prompt-bundle/prompts/leg-google.md",
    "bin/triad_dispatch_lib/data/prompt-bundle/source-manifest.json",
    "bin/triad_dispatch_lib/data/roster-bundle/contracts/review-legs.schema.json",
    "bin/triad_dispatch_lib/data/roster-bundle/source-manifest.json",
    "bin/triad_dispatch_lib/data/source-manifest.json",
    "bin/triad_dispatch_lib/dispatch.py",
    "bin/triad_dispatch_lib/errors.py",
    "bin/triad_dispatch_lib/json_input.py",
    "bin/triad_dispatch_lib/output.py",
    "bin/triad_dispatch_lib/prompts.py",
    "bin/triad_dispatch_lib/roster.py",
    "bin/triad_dispatch_lib/verdict.py",
    "vendor/triad-dispatch-lib.LICENSE",
)
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(
    argv: list[str],
    *,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise RuntimeError(f"command failed ({result.returncode}): {argv!r}: {detail}")
    return result


def _git(source_root: Path, *args: str) -> str:
    result = _run(["git", "-C", str(source_root), *args], cwd=source_root)
    return result.stdout.strip()


def _validate_source(source_root: Path) -> str:
    top_level = Path(
        _git(source_root, "rev-parse", "--show-toplevel")
    ).resolve(strict=True)
    if top_level != source_root:
        raise ValueError(f"source root is not the Git top level: {source_root}")

    dirty = _git(
        source_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if dirty:
        raise ValueError(f"source worktree must be clean:\n{dirty}")
    return _git(source_root, "rev-parse", "HEAD")


def _absolute_without_symlink_resolution(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(expanded))


def _validate_output(source_root: Path, output_dir: Path) -> Path:
    output = _absolute_without_symlink_resolution(output_dir)
    try:
        relative = output.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(
            f"output directory must be below {source_root / '_runs'}"
        ) from exc
    if len(relative.parts) < 2 or relative.parts[0] != "_runs":
        raise ValueError(f"output directory must be below {source_root / '_runs'}")

    if output.exists() or output.is_symlink():
        raise ValueError(f"output directory must not already exist: {output}")

    current = source_root / "_runs"
    for part in relative.parts[1:-1]:
        if current.is_symlink():
            raise ValueError(f"output path must not traverse a symlink: {current}")
        current = current / part
    if current.is_symlink():
        raise ValueError(f"output path must not traverse a symlink: {current}")

    resolved = output.resolve(strict=False)
    try:
        resolved.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(
            f"output directory must resolve below {source_root / '_runs'}"
        ) from exc
    return output


def _manifest(source_root: Path) -> dict[str, Any]:
    path = source_root / "plugin.json"
    if not path.is_file():
        path = source_root / ".antigravity-plugin" / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid plugin manifest: {path}: {exc}") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise ValueError("plugin manifest needs a valid version")
    if data.get("name") != "agy-triad-dispatch":
        raise ValueError("plugin manifest has an unexpected name")
    return data


def _validate_archive_members(
    members: Iterable[tarfile.TarInfo],
) -> tuple[list[tarfile.TarInfo], str]:
    admitted: list[tarfile.TarInfo] = []
    roots: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError(f"archive has an unsafe member path: {member.name!r}")
        roots.add(path.parts[0])
        if not (member.isdir() or member.isfile()):
            raise ValueError(
                f"archive has a link or special member: {member.name!r}"
            )
        admitted.append(member)
    if len(roots) != 1:
        raise ValueError("archive must contain exactly one top-level directory")
    return admitted, next(iter(roots))


def safe_extract(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:") as bundle:
        members, root_name = _validate_archive_members(bundle.getmembers())
        bundle.extractall(destination, members=members, filter="data")
    extracted_root = destination / root_name
    if not extracted_root.is_dir() or extracted_root.is_symlink():
        raise ValueError("archive top-level directory was not extracted safely")
    return extracted_root


def _verify_library(root: Path) -> dict:
    spec = importlib.util.spec_from_file_location(
        "distribution_library_adoption", root / "bin/library_adoption.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_library(root=root)


def verify_distribution(
    source_root: Path, output_dir: Path
) -> dict[str, object]:
    source = source_root.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"source root is not a directory: {source}")
    output = _validate_output(source, output_dir)
    head = _validate_source(source)
    adoption = _verify_library(source)
    manifest = _manifest(source)
    version = manifest["version"]
    prefix = f"agy-triad-dispatch-{version}"

    output.mkdir(parents=True, exist_ok=False)
    archive = output / f"{prefix}.tar"
    _run(
        [
            "git",
            "-C",
            str(source),
            "archive",
            "--format=tar",
            f"--prefix={prefix}/",
            f"--output={archive}",
            "HEAD",
        ],
        cwd=source,
    )
    extracted_root = safe_extract(archive, output / "unpacked")

    if _verify_library(extracted_root) != adoption:
        raise RuntimeError("source/archive adoption mismatch")

    hashes: dict[str, dict[str, object]] = {}
    for relative in HASH_TARGETS:
        source_path = source / relative
        archive_path = extracted_root / relative
        if not source_path.exists():
            continue
        source_hash = sha256_file(source_path)
        archive_hash = sha256_file(archive_path)
        matches = source_hash == archive_hash
        hashes[relative] = {
            "source": source_hash,
            "archive": archive_hash,
            "match": matches,
        }
        if not matches:
            raise RuntimeError(f"source/archive hash mismatch: {relative}")

    test_argv = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-v",
    ]
    test_env = os.environ.copy()
    test_result = _run(
        test_argv,
        cwd=extracted_root,
        check=False,
        env=test_env,
    )
    if test_result.returncode != 0:
        detail = test_result.stdout + test_result.stderr
        raise RuntimeError(
            f"archive package tests failed ({test_result.returncode}):\n{detail}"
        )

    report: dict[str, object] = {
        "archive": str(archive),
        "archive_sha256": sha256_file(archive),
        "commit": head,
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "extracted_root": str(extracted_root),
        "hashes": hashes,
        "tests": {
            "argv": test_argv,
            "returncode": test_result.returncode,
            "stdout": test_result.stdout,
            "stderr": test_result.stderr,
        },
        "version": version,
        "library_adoption": adoption,
    }
    report_path = output / "verification.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive a clean plugin HEAD and test the exact extracted bytes"
    )
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = verify_distribution(args.source_root, args.output_dir)
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as exc:
        print(f"distribution verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
