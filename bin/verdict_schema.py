#!/usr/bin/env python3
"""Compact structured verdict shared by every TRIAD review family."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_REVIEW_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DOT_PATH_COMPONENT_PATTERN = (
    r"(?:\.[^./\\\x00-\x1f\x7f][^/\\\x00-\x1f\x7f]*|"
    r"\.\.[^/\\\x00-\x1f\x7f]+)"
)
_PATH_COMPONENT_PATTERN = (
    rf"(?:[^./\\\x00-\x1f\x7f][^/\\\x00-\x1f\x7f]*|{_DOT_PATH_COMPONENT_PATTERN})"
)
# A slash makes a multi-component path nonblank. A single component must
# contain a non-whitespace character without using Rust-unsupported lookaround.
_NATIVE_REVIEW_PATH_PATTERN = (
    rf"^(?:{_PATH_COMPONENT_PATTERN}(?:/{_PATH_COMPONENT_PATTERN})+|"
    r"[^./\\\s\x00-\x1f\x7f][^/\\\x00-\x1f\x7f]*|"
    r"[^\S\x00-\x1f\x7f]+[^/\\\s\x00-\x1f\x7f][^/\\\x00-\x1f\x7f]*|"
    rf"{_DOT_PATH_COMPONENT_PATTERN})$"
)
ReviewRelativePath = Annotated[
    str,
    StringConstraints(pattern=_NATIVE_REVIEW_PATH_PATTERN),
]


def _nonempty(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be whitespace-only")
    return value


def _review_relative_path(value: str) -> str:
    _nonempty(value, "path")
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("path must be a clean POSIX review-relative path")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("path must be a clean POSIX review-relative path")
    return value


class LegFinding(BaseModel):
    """One source-grounded reviewer finding."""

    model_config = ConfigDict(extra="forbid", strict=True)

    severity: Literal["Critical", "Major", "Minor"]
    path: ReviewRelativePath
    line: int | None = Field(default=None, ge=1)
    trigger: str
    evidence: str
    correction: str

    @field_validator("path")
    @classmethod
    def _path_is_review_relative(cls, value: str) -> str:
        return _review_relative_path(value)

    @field_validator("trigger", "evidence", "correction")
    @classmethod
    def _text_is_nonempty(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)


class LegVerdict(BaseModel):
    """One complete terminal verdict for one family in one review round."""

    model_config = ConfigDict(extra="forbid", strict=True)

    review_id: str
    family: Literal["claude", "google", "codex"]
    content_digest: str
    verdict: Literal["SAFE", "NOT-SAFE"]
    criteria_checked: list[str] = Field(min_length=1)
    findings: list[LegFinding]
    affected_surfaces_inspected: list[ReviewRelativePath] = Field(min_length=1)
    open_questions: list[str]

    @staticmethod
    def _triad_check_original_json(raw: str | bytes) -> None:
        """Reject ambiguity before an envelope or verdict loses its members."""
        try:
            json.loads(raw, object_pairs_hook=_reject_duplicate_members)
        except RecursionError:
            raise ValueError("JSON nesting exceeds decoder limit") from None

    @field_validator("review_id")
    @classmethod
    def _valid_review_id(cls, value: str) -> str:
        if not _REVIEW_ID_RE.fullmatch(value):
            raise ValueError("review_id must match [A-Za-z0-9][A-Za-z0-9._-]*")
        return value

    @field_validator("content_digest")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError(
                "content_digest must be 64 lowercase hexadecimal characters"
            )
        return value

    @field_validator("criteria_checked", "open_questions")
    @classmethod
    def _nonempty_text_entries(cls, values: list[str], info) -> list[str]:
        for value in values:
            _nonempty(value, f"{info.field_name} entry")
        if len(values) != len(set(values)):
            raise ValueError(f"{info.field_name} entries must be unique")
        return values

    @field_validator("affected_surfaces_inspected")
    @classmethod
    def _valid_surfaces(cls, values: list[str]) -> list[str]:
        normalized = [_review_relative_path(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("affected surfaces must be unique")
        return normalized

    @model_validator(mode="after")
    def _verdict_matches_evidence(self) -> "LegVerdict":
        blocking = any(
            finding.severity in ("Critical", "Major") for finding in self.findings
        )
        if self.verdict == "SAFE":
            if blocking:
                raise ValueError("SAFE requires no Critical or Major finding")
            if self.open_questions:
                raise ValueError("SAFE requires no open questions")
        elif not blocking and not self.open_questions:
            raise ValueError(
                "NOT-SAFE requires a Critical/Major finding or open question"
            )
        return self


def _read_canonical_regular_file(path: Path) -> bytes:
    try:
        resolved = path.resolve(strict=True)
        before = path.lstat()
    except (OSError, RuntimeError):
        raise ValueError(
            "result file must be a canonical existing regular file"
        ) from None
    if (
        not path.is_absolute()
        or path != resolved
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError("result file must be a canonical existing regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError(
            "result file must be a canonical existing regular file"
        ) from None
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(opened, before):
            raise ValueError("result file changed while opening")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("result file changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> None:
    names = set()
    for name, _ in pairs:
        if name in names:
            raise ValueError("duplicate JSON member")
        names.add(name)


def validate_verdict_file(
    result_file: Path,
    expected_review_id: str,
    expected_family: Literal["claude", "google", "codex"],
    expected_content_digest: str,
) -> LegVerdict:
    raw = _read_canonical_regular_file(result_file)
    # Discard decoded values; semantic validation consumes the original bytes.
    LegVerdict._triad_check_original_json(raw)
    verdict = LegVerdict.model_validate_json(raw, strict=True)
    if verdict.review_id != expected_review_id:
        raise ValueError("review ID mismatch")
    if verdict.family != expected_family:
        raise ValueError("family mismatch")
    if verdict.content_digest != expected_content_digest:
        raise ValueError("content digest mismatch")
    return verdict


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verdict_schema.py")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema")
    validate = commands.add_parser("validate")
    validate.add_argument("--result-file", type=Path, required=True)
    validate.add_argument("--expected-review-id", required=True)
    validate.add_argument(
        "--expected-family", choices=("claude", "google", "codex"), required=True
    )
    validate.add_argument("--expected-content-digest", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "schema":
            print(
                json.dumps(
                    LegVerdict.model_json_schema(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            verdict = validate_verdict_file(
                arguments.result_file,
                arguments.expected_review_id,
                arguments.expected_family,
                arguments.expected_content_digest,
            )
            print(verdict.model_dump_json())
    except (OSError, ValueError) as error:
        print(f"invalid LegVerdict: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
