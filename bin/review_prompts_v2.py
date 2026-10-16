"""Render the four exact shared clause files for B's explicit v2 review route."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
from pathlib import Path

import review_round
from validate_v2 import _json, _library, validate_review_kind
from verdict_v2 import bound_verdict

BUNDLE_ROOT = Path(__file__).resolve().parents[1] / "prompts/review-v2"
SOURCE_COMMIT = "9bca1493ddf9a4893733404cd11dff300b89f038"
PAYLOADS = ("common-clauses.md", "leg-claude.md", "leg-codex.md", "leg-google.md")


def load_bundle() -> dict[str, str]:
    read = review_round._canonical_regular_file_bytes
    try:
        manifest = _json(read(BUNDLE_ROOT / "source-manifest.json", "v2 prompt manifest"))
        if (not isinstance(manifest, dict)
                or set(manifest) != {"source_repository", "source_commit", "status", "sha256"}
                or manifest["source_repository"] != "https://github.com/codefoundry-io/triad-dispatch-spec"
                or manifest["source_commit"] != SOURCE_COMMIT or manifest["status"] != "candidate"
                or not isinstance(manifest["sha256"], dict) or set(manifest["sha256"]) != set(PAYLOADS)):
            raise ValueError("invalid v2 prompt provenance")
        output = {}
        for name in PAYLOADS:
            raw = read(BUNDLE_ROOT / name, "v2 shared prompt")
            if hashlib.sha256(raw).hexdigest() != manifest["sha256"][name]:
                raise ValueError("v2 prompt digest mismatch: " + name)
            output[name] = raw.decode("utf-8")
        return output
    except (OSError, UnicodeError) as error:
        raise ValueError("unreadable v2 prompt bundle") from error


def _clauses(text: str) -> dict[str, str]:
    # This is the fixed published Markdown clause format, not a Markdown engine.
    result = {}
    for section in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        name = section.split(None, 1)[0]
        blocks = re.findall(r"^```text\n(.*?)^```$", section, re.MULTILINE | re.DOTALL)
        if blocks:
            if len(blocks) != 1 or name in result:
                raise ValueError("ambiguous shared clause: " + name)
            result[name] = blocks[0].rstrip("\n")
    return result


def review_web_clause(authorized: bool) -> str:
    if type(authorized) is not bool:
        raise ValueError("review_web_authorized must be a boolean")
    name = "review-web-permission" if authorized else "review-no-web"
    return _clauses(load_bundle()["common-clauses.md"])[name]


def render_prompt(*, expected: dict, worktree: Path, brief_file: Path,
                  packet_files: list[Path], diff_file: Path, objective: str,
                  criteria: list[str], approved_boundary: list[str], review_date: str, residual: str = "",
                  google_preflight_sha256: str | None = None, review_web_authorized: bool = False,
                  review_kind: str = "pre-merge") -> str:
    review_kind = validate_review_kind(review_kind)
    if (not isinstance(review_date, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", review_date) is None):
        raise ValueError("review_date must be the bound UTC preparation date")
    date.fromisoformat(review_date)
    model = bound_verdict(expected)
    shared = _library()
    load_bundle()
    root = review_round._canonical_directory(worktree, "v2 review worktree")
    for path in packet_files:
        review_round._canonical_regular_file_bytes(path, "bound v2 packet file")
    if (brief_file not in packet_files or diff_file not in packet_files
            or len(set(packet_files)) != len(packet_files)):
        raise ValueError("brief and diff must be unique bound packet inputs")
    if (not isinstance(objective, str) or not objective.strip() or not isinstance(residual, str)
            or any(not isinstance(values, list) or not values
                   or any(not isinstance(value, str) or not value.strip() for value in values)
                   for values in (criteria, approved_boundary))):
        raise ValueError("v2 objective, criteria and approved boundary are required")
    if type(review_web_authorized) is not bool:
        raise ValueError("review_web_authorized must be a boolean")
    metadata = dict(expected, review_kind=review_kind)
    if review_web_authorized:
        metadata["review_web_authorized"] = True
    if expected["family"] == "google":
        if not isinstance(google_preflight_sha256, str) or re.fullmatch(r"[a-f0-9]{64}", google_preflight_sha256) is None:
            raise ValueError("Google v2 prompt requires its preflight digest")
        metadata["google_preflight_receipt_sha256"] = google_preflight_sha256
    elif google_preflight_sha256 is not None:
        raise ValueError("non-Google prompt cannot own a Google preflight")
    replacements = {
        "worktree": json.dumps(str(root), ensure_ascii=True),
        "brief-file": json.dumps(str(brief_file), ensure_ascii=True),
        "packet-files": json.dumps([str(path) for path in packet_files]),
        "review-id": expected["review_id"], "content-digest": expected["content_digest"],
        "leg-name": expected["leg_name"], "attempt": str(expected["attempt"]),
    }
    if expected["family"] == "google":
        replacements["gated-patch-file"] = json.dumps(str(diff_file), ensure_ascii=True)
    clauses = shared.render_review_clauses(
        family=expected["family"], host="B", review_date=review_date,
        review_web_authorized=review_web_authorized, replacements=replacements,
        review_kind=review_kind, route=expected["route"])
    # Preserve the established heading before the fence; shared code owns its
    # caveat and data encoding. The final three lines are the fenced JSON block.
    caveat, *fence = shared.render_data(residual).rsplit("\n", 3)
    data_prefix = "" if caveat in clauses else caveat + "\n"
    framing = {"objective": objective, "criteria": criteria, "approved_boundary": approved_boundary}
    return ("Review v2 metadata: " + json.dumps(metadata, sort_keys=True, separators=(",", ":"))
            + "\n\n" + clauses + "\n\nBound review scope:\n" + json.dumps(framing, ensure_ascii=True)
            + "\n\n" + data_prefix + "Prior findings and rebuttal evidence:\n"
            + "\n".join(fence) + "\n\nComplete authoritative output schema:\n"
            + json.dumps(model.model_json_schema(), sort_keys=True) + "\n")
