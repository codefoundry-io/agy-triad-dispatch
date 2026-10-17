# agy-triad-dispatch Implementation Plan

> **Amendment Note (2026-10-17)**: The library pin in this plan (`0.1.0.dev3`) is superseded by `0.1.0.dev4` via the dev4 adoption amendment. Task 2 and Task 3 sample verdict fixtures in this document are non-canonical; tests use the canonical v2 shape.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify `codefoundry-io/agy-triad-dispatch`, the Google Antigravity (`agy`) host distribution of TRIAD cross-family dispatch and review, backed by pinned `triad-dispatch-lib 0.1.0.dev3` (`2d1e62e7de6f33fa13a918a39579c6b5e74def00`) and full original-carrier `admit_output` enforcement across `claude`, `codex`, `agy`, and `gemini`.

**Architecture:** `agy-triad-dispatch` packages an Antigravity-native plugin (`plugin.json` + `skills/`) with four single-shot provider wrappers (`claude_wrapper.py`, `codex_wrapper.py`, `antigravity_wrapper.py`, `gemini_wrapper.py`), the v2 review lifecycle (`review_round_v2.py`, `review_adapters_v2.py`, `review_prompts_v2.py`, `review_roster.py`, `validate_v2.py`, `verdict_v2.py`), and the immutable vendored `bin/triad_dispatch_lib` (`0.1.0.dev3`) verified offline by `bin/library_adoption.py` against `contracts/library-adoption.json`. Every v2 bound wrapper admits verdicts directly from the original provider carrier (`claude-json`, `native-final`, `agy-stream-json`, `gemini-json`) via `triad_dispatch_lib.output.admit_output(..., expected=binding)` without pre-decoding or re-serializing JSON before duplicate-key and six-binding validation.

**Tech Stack:** Python 3.12+, `pydantic>=2,<3`, `jsonschema>=4.26,<5`, `referencing`, bundled `triad-dispatch-lib==0.1.0.dev3`, Antigravity CLI (`agy >= 1.2.1`), Claude Code CLI (`claude`), Codex CLI (`codex`), Gemini CLI (`gemini >= 0.63.0`).

**Spec:** `/Users/chaniri/codex_workspace/workspace/triad-codex-dispatch-reliability/docs/handoffs/gemini-argon-library-adoption.md` and `/Users/chaniri/codex_workspace/workspace/triad-codex-dispatch-reliability/docs/handoffs/claude-library-adoption.md`

## Global Constraints

- Pinned shared library source commit: `2d1e62e7de6f33fa13a918a39579c6b5e74def00` (`codefoundry-io/triad-dispatch-lib` `0.1.0.dev3`, merged to `main` at `9489543ee24f338e9c6d0e3b032a1aa389986429`).
- Pinned wheel SHA-256: `df1e84b56a7238bab2a019064c850c30bde5b6b15268a81a1e74d76df0c187b8`.
- Pinned `APPROVED_CONTENT_SHA256` in `bin/library_adoption.py`: `ccc38ebfa4f7af4151da8fa51ddd1e60d9af01e4b25c98f8742af919ce224846`.
- Pinned bundle content IDs:
  - `prompt`: `c332d95688b9db3dac187a0bebac6ddc0eddfdbdc5401a649697d0601455e843`
  - `roster`: `a99fb58dbd920dfed0d88d8c8b9117bd07633b76b5d2565e05f50a5b5c423ed8`
  - `verdict`: `455dfd8a7d586d04eedb60ee492cf421e076a77bafcccbba12de37cc12bf0340`
- Vendored `bin/triad_dispatch_lib` and `vendor/triad-dispatch-lib.LICENSE` must NEVER be locally modified; any common library change (such as adding a dedicated `host="AGY"` prompt profile) must be proposed upstream via a GitHub Issue or PR on `codefoundry-io/triad-dispatch-lib`.
- All six v2 binding fields (`review_id`, `family`, `content_digest`, `leg_name`, `attempt`, `route`) are mandatory on every v2 review leg: `route="agy"` on the AGY route, `route="gemini"` on the Gemini CLI route, and `route=None` (`--expected-route null`) on `claude` and `codex`.
- Original provider output (`claude-json`, `native-final`, `agy-stream-json`, `gemini-json`) must pass through `triad_dispatch_lib.output.admit_output` before any `json.loads` / `json.dumps` round-trip so duplicate JSON object keys inside nested carrier payloads (`structured_output`, `response`, or `-o` last-message files) are rejected fail-closed.
- Host-owned failure precedence (authentication/login `oauth-env` exit 65, timeout exit 2, process group cleanup, read-only tool/profile audit, C23 recovery preconditions) must execute before or around verdict admission and must never be downgraded to `schema-fail`.

## Review Focus

- Duplicate JSON keys inside nested carrier envelopes (`{"type":"result","status":"SUCCESS","result":{"structured_output":{"review_id":"r1","review_id":"r2",...}}}` in `agy-stream-json`, `.response` in `gemini-json`, `.structured_output` in `claude-json`, or `-o` file in `codex` `native-final`) must fail closed as `schema-fail` instead of being silently collapsed by `json.loads` before validation (pinned in Task 2 and Task 3 tests).
- AGY C23 plan-mode recovery (`status == "ERROR"`, `vendor_exit_code == 0`, one errored `finish` followed by one successful `finish` after an allowed read) is rejected by `admit_output(carrier="agy-stream-json")` directly because `output.py` requires `status == "SUCCESS"`; C23 must extract the raw `structured_output` byte/character span via `json_member_span` only after host C23 recovery preconditions pass, then call `admit_output(ProviderOutput(carrier="native-final", ...), expected=binding)` while still rejecting runs with no allowed read or with forbidden tool execution (pinned in Task 2 tests).
- Cross-route and cross-leg binding substitution (`route="gemini"` verdict returned to an `agy` invocation, `route=null` returned to a Google leg, `route="agy"` returned to `codex` or `claude`, or swapped `leg_name` / `attempt`) must be rejected even when the JSON is otherwise schema-valid (pinned in Task 2 and Task 3 tests).
- Codex CLI v2 formal review route (`codex_wrapper.py` invoked by `review_round_v2.py` with `--pydantic verdict_v2:LegVerdict` and all six `--expected-*` flags) must enforce single-shot execution (no automatic schema-repair retry on v2 bound calls), `-c approval_policy=never`, `--ignore-rules`, `--sandbox read-only`, and `-c web_search="disabled"` when `--web` is not passed (pinned in Task 3 tests).
- Ambient `PYTHONPATH` or preloaded `sys.modules["triad_dispatch_lib"]` pointing outside `<repo>/bin/triad_dispatch_lib`, or a missing/corrupted file in `bin/triad_dispatch_lib`, must fail closed before any provider binary starts (pinned in Task 1 and Task 5 tests).

---

### Task 1: Repository Foundation, Vendored `triad-dispatch-lib 0.1.0.dev3`, and Core V2 Contracts/Roster/Prompts

**Files:**
- Create: `vendor/triad-dispatch-lib.LICENSE`
- Create: `bin/triad_dispatch_lib/` (exact vendored tree from commit `2d1e62e7de6f33fa13a918a39579c6b5e74def00`)
- Create: `contracts/library-adoption.json`
- Create: `contracts/leg-verdict.schema.json`
- Create: `contracts/review-legs.schema.json`
- Create: `contracts/review-legs.default.json`
- Create: `contracts/receipt-fields.json`
- Create: `contracts/review-kind.schema.json`
- Create: `contracts/source-manifest.json`
- Create: `prompts/review-v2/common-clauses.md`
- Create: `prompts/review-v2/leg-claude.md`
- Create: `prompts/review-v2/leg-codex.md`
- Create: `prompts/review-v2/leg-google.md`
- Create: `prompts/review-v2/source-manifest.json`
- Create: `policies/gemini-readonly.toml`
- Create: `bin/library_adoption.py`
- Create: `bin/validate_v2.py`
- Create: `bin/verdict_v2.py`
- Create: `bin/verdict_schema.py`
- Create: `bin/review_roster.py`
- Create: `bin/review_prompts_v2.py`
- Create: `bin/data/claude-capabilities.json`
- Create: `bin/cleanup-roots.default.json`
- Create: `bin/cleanup.py`
- Create: `bin/apply_patch.py`
- Create: `requirements.txt`
- Create: `.gitignore`
- Test: `tests/test_library_adoption_v2.py`

**Interfaces:**
- Consumes: `triad_dispatch_lib` (`0.1.0.dev3` at `bin/triad_dispatch_lib`)
- Produces:
  - `library_adoption.verify_library(root: Path = ROOT) -> dict`
  - `validate_v2._library() -> module`
  - `validate_v2.validate_verdict(raw: str | bytes, *, expected: dict) -> dict`
  - `verdict_v2.bound_verdict(expected: dict) -> type[LegVerdict]`
  - `verdict_v2.bound_wrapper(args, *, family: str, route: str | None) -> type[LegVerdict]`
  - `review_roster.resolve_roster(project_root: Path) -> dict`
  - `review_prompts_v2.render_prompt(*, expected: dict, worktree: Path, brief_file: Path, packet_files: list[Path], diff_file: Path, objective: str, criteria: list[str], approved_boundary: list[str], review_date: str, residual: str = "", google_preflight_sha256: str | None = None, review_web_authorized: bool = False, review_kind: str = "pre-merge") -> str`

- [ ] **Step 1: Write the failing test in `tests/test_library_adoption_v2.py`**

```python
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


class TestLibraryAdoptionV2(unittest.TestCase):
    def test_verify_library_pins_exact_commit_and_bundle_ids(self):
        import library_adoption
        meta = library_adoption.verify_library(ROOT)
        self.assertEqual(meta["version"], "0.1.0.dev3")
        self.assertEqual(meta["source_commit"], "2d1e62e7de6f33fa13a918a39579c6b5e74def00")
        self.assertEqual(
            meta["wheel_sha256"],
            "df1e84b56a7238bab2a019064c850c30bde5b6b15268a81a1e74d76df0c187b8",
        )
        self.assertEqual(
            meta["bundle_ids"],
            {
                "prompt": "c332d95688b9db3dac187a0bebac6ddc0eddfdbdc5401a649697d0601455e843",
                "roster": "a99fb58dbd920dfed0d88d8c8b9117bd07633b76b5d2565e05f50a5b5c423ed8",
                "verdict": "455dfd8a7d586d04eedb60ee492cf421e076a77bafcccbba12de37cc12bf0340",
            },
        )

    def test_ambient_library_override_is_rejected(self):
        import types
        import validate_v2
        fake = types.ModuleType("triad_dispatch_lib")
        fake.__file__ = "/tmp/fake_triad_dispatch_lib/__init__.py"
        saved = sys.modules.get("triad_dispatch_lib")
        sys.modules["triad_dispatch_lib"] = fake
        try:
            with self.assertRaisesRegex(RuntimeError, "ambient shared library"):
                validate_v2._library()
        finally:
            if saved is None:
                sys.modules.pop("triad_dispatch_lib", None)
            else:
                sys.modules["triad_dispatch_lib"] = saved

    def test_roster_resolution_merges_agents_override_and_preserves_defaults(self):
        import review_roster
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            roster = review_roster.resolve_roster(project)
            self.assertEqual(roster["enabled"], ["claude", "codex", "google"])
            self.assertEqual(roster["families"], ["claude", "codex", "google"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_library_adoption_v2.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'library_adoption'`

- [ ] **Step 3: Populate the vendored `triad_dispatch_lib`, `contracts/`, `prompts/`, `policies/`, and foundation `bin/` modules**

Copy the exact verified files from `/Users/chaniri/codex_workspace/workspace/triad-codex-dispatch-reliability`:
- `vendor/triad-dispatch-lib.LICENSE`
- `bin/triad_dispatch_lib/` (all 11 modules + `data/` bundles)
- `contracts/library-adoption.json`, `contracts/leg-verdict.schema.json`, `contracts/review-legs.schema.json`, `contracts/review-legs.default.json`, `contracts/receipt-fields.json`, `contracts/review-kind.schema.json`, `contracts/source-manifest.json`
- `prompts/review-v2/*`
- `policies/gemini-readonly.toml`
- `bin/library_adoption.py`, `bin/validate_v2.py`, `bin/verdict_v2.py`, `bin/verdict_schema.py`, `bin/review_roster.py`, `bin/review_prompts_v2.py`, `bin/data/claude-capabilities.json`, `bin/cleanup-roots.default.json`, `bin/cleanup.py`, `bin/apply_patch.py`, `bin/review_round.py`, `bin/google_preflight_v2.py`
- `requirements.txt` and `.gitignore`

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_library_adoption_v2.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add .gitignore requirements.txt vendor/ contracts/ prompts/ policies/ bin/library_adoption.py bin/validate_v2.py bin/verdict_v2.py bin/verdict_schema.py bin/review_roster.py bin/review_prompts_v2.py bin/review_round.py bin/google_preflight_v2.py bin/data/ bin/cleanup-roots.default.json bin/cleanup.py bin/apply_patch.py bin/triad_dispatch_lib/ tests/test_library_adoption_v2.py
git commit -m "feat: seed vendored triad-dispatch-lib 0.1.0.dev3 and v2 contracts foundation"
```

---

### Task 2: Original-Carrier `admit_output` Enforcement in Google (`agy`, `gemini`) and Claude Wrappers

**Files:**
- Create: `bin/_common.py`
- Create: `bin/antigravity_wrapper.py`
- Create: `bin/gemini_wrapper.py`
- Create: `bin/claude_wrapper.py`
- Modify: `bin/verdict_v2.py`
- Test: `tests/test_carrier_admission_v2.py`

**Interfaces:**
- Consumes:
  - `triad_dispatch_lib.output.ProviderOutput(carrier: str, stdout: str | bytes, stderr: str | bytes = "", exit_code: int = 0, completed: bool = True)`
  - `triad_dispatch_lib.output.admit_output(output: ProviderOutput, *, expected: Mapping[str, Any]) -> dict[str, Any]`
  - `triad_dispatch_lib.json_input.json_member_span(raw: str, *path: str) -> str`
- Produces:
  - `verdict_v2.admit_carrier_output(pydantic_cls: type[LegVerdict], *, carrier: str, stdout: str | bytes, stderr: str | bytes = "", exit_code: int = 0, completed: bool = True) -> dict[str, Any]`
  - In `bin/antigravity_wrapper.py`:
    - When `pydantic_cls` has `_binding is not None` and `status == "SUCCESS"` (and every non-empty stdout line is a JSON object record): validates original `run.stdout` via `admit_carrier_output(pydantic_cls, carrier="agy-stream-json", stdout=run.stdout, stderr=run.stderr, exit_code=run.vendor_exit_code, completed=True)`.
    - When `pydantic_cls` has `_binding is not None` and C23 recovery (`denied_post_completion` or `_agy_finish_resubmitted`) is proven (`status == "ERROR"`, `vendor_exit_code == 0`, allowed read observed, no forbidden tool execution): locates the single terminal `"type": "result"` line in `run.stdout`, extracts the raw `structured_output` JSON text span using `json_member_span(terminal_line, "result", "structured_output")`, and validates it via `admit_carrier_output(pydantic_cls, carrier="native-final", stdout=raw_span, stderr=run.stderr, exit_code=0, completed=True)` so original duplicate keys inside `structured_output` are still caught without bypassing `admit_output`.
  - In `bin/_common.py` (`_run_gemini_native_structured_once`):
    - When `pydantic_cls` has `_binding is not None` and `result.exit_code == EXIT_OK` with `extraction_error is None`: validates original `result.stdout` via `admit_carrier_output(pydantic_cls, carrier="gemini-json", stdout=result.stdout, stderr=result.stderr, exit_code=result.vendor_exit_code, completed=True)` before setting `result.validated`.
  - In `bin/claude_wrapper.py` (`_run_native_structured_once`):
    - When `pydantic_cls` has `_binding is not None` and `result.exit_code == EXIT_OK` with `extraction_error is None` and `has_native_structured_output`: validates original `_common.strip_markdown_fences(result.stdout)` via `admit_carrier_output(pydantic_cls, carrier="claude-json", stdout=_common.strip_markdown_fences(result.stdout), stderr=result.stderr, exit_code=result.vendor_exit_code, completed=True)`.

- [ ] **Step 1: Write the failing tests in `tests/test_carrier_admission_v2.py`**

```python
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

DIGEST = "a" * 64


def _valid_verdict_dict(*, family: str, leg_name: str, route: str | None, attempt: int = 1) -> dict:
    return {
        "schema": "triad-leg-verdict.v2",
        "review_id": "rev-20261015-01",
        "family": family,
        "leg_name": leg_name,
        "route": route,
        "attempt": attempt,
        "content_digest": DIGEST,
        "verdict": "SAFE TO MERGE",
        "ApproveForMerge": True,
        "inspected_surfaces": ["bin/validate_v2.py"],
        "findings": [],
        "strengths": ["Clean boundaries"],
        "open_questions": [],
    }


class TestCarrierAdmissionV2(unittest.TestCase):
    def test_admit_carrier_output_rejects_nested_duplicate_keys_across_all_carriers(self):
        import verdict_v2

        agy_binding = {
            "review_id": "rev-20261015-01",
            "family": "google",
            "content_digest": DIGEST,
            "leg_name": "google",
            "attempt": 1,
            "route": "agy",
        }
        bound_agy = verdict_v2.bound_verdict(agy_binding)
        body = json.dumps(_valid_verdict_dict(family="google", leg_name="google", route="agy"))
        dup_body = '{"review_id":"other",' + body[1:]

        # 1. agy-stream-json duplicate key inside structured_output
        agy_stream = (
            '{"type":"step_update","step":1}\n'
            + '{"type":"result","status":"SUCCESS","result":{"structured_output":'
            + dup_body
            + "}}\n"
        )
        with self.assertRaises(ValueError):
            verdict_v2.admit_carrier_output(
                bound_agy, carrier="agy-stream-json", stdout=agy_stream, exit_code=0, completed=True
            )

        # 2. gemini-json duplicate key inside response string
        gemini_binding = {**agy_binding, "route": "gemini"}
        bound_gemini = verdict_v2.bound_verdict(gemini_binding)
        gemini_body = json.dumps(_valid_verdict_dict(family="google", leg_name="google", route="gemini"))
        dup_gemini_body = '{"review_id":"other",' + gemini_body[1:]
        gemini_envelope = json.dumps({"response": dup_gemini_body})
        with self.assertRaises(ValueError):
            verdict_v2.admit_carrier_output(
                bound_gemini, carrier="gemini-json", stdout=gemini_envelope, exit_code=0, completed=True
            )

        # 3. claude-json duplicate key inside structured_output
        claude_binding = {**agy_binding, "family": "claude", "leg_name": "claude", "route": None}
        bound_claude = verdict_v2.bound_verdict(claude_binding)
        claude_body = json.dumps(_valid_verdict_dict(family="claude", leg_name="claude", route=None))
        dup_claude_body = '{"review_id":"other",' + claude_body[1:]
        claude_envelope = (
            '{"type":"result","subtype":"success","is_error":false,"result":"ok","structured_output":'
            + dup_claude_body
            + "}"
        )
        with self.assertRaises(ValueError):
            verdict_v2.admit_carrier_output(
                bound_claude, carrier="claude-json", stdout=claude_envelope, exit_code=0, completed=True
            )

    def test_admit_carrier_output_rejects_route_and_binding_swaps(self):
        import verdict_v2

        agy_binding = {
            "review_id": "rev-20261015-01",
            "family": "google",
            "content_digest": DIGEST,
            "leg_name": "google",
            "attempt": 1,
            "route": "agy",
        }
        bound_agy = verdict_v2.bound_verdict(agy_binding)
        wrong_route_verdict = _valid_verdict_dict(family="google", leg_name="google", route="gemini")
        agy_stream = json.dumps(
            {"type": "result", "status": "SUCCESS", "result": {"structured_output": wrong_route_verdict}}
        )
        with self.assertRaises(ValueError):
            verdict_v2.admit_carrier_output(
                bound_agy, carrier="agy-stream-json", stdout=agy_stream, exit_code=0, completed=True
            )

    def test_agy_c23_bridge_extracts_original_span_and_preserves_duplicate_key_rejection(self):
        import antigravity_wrapper
        import verdict_v2

        agy_binding = {
            "review_id": "rev-20261015-01",
            "family": "google",
            "content_digest": DIGEST,
            "leg_name": "google",
            "attempt": 1,
            "route": "agy",
        }
        bound_agy = verdict_v2.bound_verdict(agy_binding)
        valid_body = json.dumps(_valid_verdict_dict(family="google", leg_name="google", route="agy"))
        c23_stream_valid = (
            '{"type":"result","status":"ERROR","result":{"structured_output":' + valid_body + "}}\n"
        )
        admitted = antigravity_wrapper._admit_v2_agy_output(
            bound_agy,
            stdout=c23_stream_valid,
            stderr="",
            vendor_exit_code=0,
            c23_recovered=True,
        )
        self.assertEqual(admitted["route"], "agy")

        dup_body = '{"review_id":"other",' + valid_body[1:]
        c23_stream_dup = (
            '{"type":"result","status":"ERROR","result":{"structured_output":' + dup_body + "}}\n"
        )
        with self.assertRaises(ValueError):
            antigravity_wrapper._admit_v2_agy_output(
                bound_agy,
                stdout=c23_stream_dup,
                stderr="",
                vendor_exit_code=0,
                c23_recovered=True,
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_carrier_admission_v2.py -v`
Expected: FAIL with `AttributeError: module 'verdict_v2' has no attribute 'admit_carrier_output'`

- [ ] **Step 3: Implement `admit_carrier_output` in `bin/verdict_v2.py` and wire `bin/_common.py`, `bin/antigravity_wrapper.py`, `bin/gemini_wrapper.py`, and `bin/claude_wrapper.py`**

In `bin/verdict_v2.py`, add:
```python
def admit_carrier_output(
    cls: type[LegVerdict],
    *,
    carrier: str,
    stdout: str | bytes,
    stderr: str | bytes = "",
    exit_code: int = 0,
    completed: bool = True,
) -> dict[str, Any]:
    """Validate original provider output through triad_dispatch_lib.output.admit_output."""
    if cls._binding is None:
        raise ValueError("admit_carrier_output requires a bound v2 LegVerdict class")
    shared = _library()
    load_contracts()
    from triad_dispatch_lib.output import ProviderOutput, admit_output
    provider_output = ProviderOutput(
        carrier=carrier,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        completed=completed,
    )
    return admit_output(provider_output, expected=cls._binding)
```
In `bin/antigravity_wrapper.py`, add `_admit_v2_agy_output(pydantic_cls, *, stdout: str, stderr: str, vendor_exit_code: int, c23_recovered: bool) -> dict` which:
- For `c23_recovered=False`: checks if every non-empty line in `stdout` is a JSON object; if so, calls `verdict_v2.admit_carrier_output(pydantic_cls, carrier="agy-stream-json", stdout=stdout, stderr=stderr, exit_code=vendor_exit_code, completed=True)`. If non-JSON preamble/noise lines exist (which host `parse_agy_stream` tolerates when a single terminal `result` line succeeded), extracts the single terminal `"type": "result"` line and passes that line with `carrier="agy-stream-json"`.
- For `c23_recovered=True`: locates the single terminal `"type": "result"` line in `stdout`, extracts the raw `structured_output` span via `from triad_dispatch_lib.json_input import json_member_span; span = json_member_span(terminal_line, "result", "structured_output")`, and calls `verdict_v2.admit_carrier_output(pydantic_cls, carrier="native-final", stdout=span, stderr=stderr, exit_code=0, completed=True)`.
In `bin/_common.py` (`_run_gemini_native_structured_once`), when `getattr(pydantic_cls, "_binding", None) is not None`, call `verdict_v2.admit_carrier_output(pydantic_cls, carrier="gemini-json", stdout=result.stdout, stderr=result.stderr, exit_code=result.vendor_exit_code, completed=True)` and handle `ValueError` / `SharedError` as `schema-fail` (`EXIT_SCHEMA_FAIL`).
In `bin/claude_wrapper.py` (`_run_native_structured_once`), when `getattr(pydantic_cls, "_binding", None) is not None`, call `verdict_v2.admit_carrier_output(pydantic_cls, carrier="claude-json", stdout=_common.strip_markdown_fences(result.stdout), stderr=result.stderr, exit_code=result.vendor_exit_code, completed=True)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_carrier_admission_v2.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add bin/_common.py bin/verdict_v2.py bin/antigravity_wrapper.py bin/gemini_wrapper.py bin/claude_wrapper.py tests/test_carrier_admission_v2.py
git commit -m "feat: enforce original-carrier admit_output across agy, gemini, and claude wrappers"
```

---

### Task 3: Port `bin/codex_wrapper.py` with V2 6-Binding & `native-final` Admission and Wire CLI/Native Routes in `review_adapters_v2.py` & `review_round_v2.py`

**Files:**
- Create: `bin/codex_wrapper.py`
- Modify: `bin/_common.py`
- Modify: `bin/review_adapters_v2.py`
- Modify: `bin/review_round_v2.py`
- Test: `tests/test_codex_wrapper_and_adapters_v2.py`

**Interfaces:**
- Consumes:
  - `verdict_v2.bound_wrapper(args, *, family: str, route: str | None) -> type[LegVerdict]`
  - `verdict_v2.admit_carrier_output(cls, *, carrier="native-final", stdout=..., stderr=..., exit_code=..., completed=True)`
- Produces:
  - `bin/codex_wrapper.py`:
    - Supports both standalone consults (`--prompt` / `--prompt-file`, `--sandbox {read-only,workspace-write}`, `--model`, `--reasoning` / `--effort`, `--search` / `--web`, `--pydantic`, `--output-schema-file`, `--image`, `--debug`) and formal v2 bound review calls (`--pydantic verdict_v2:LegVerdict` with `--expected-review-id`, `--expected-family codex`, `--expected-content-digest`, `--expected-leg-name`, `--expected-attempt`, `--expected-route null`).
    - On formal v2 bound calls: enforces `--sandbox read-only`, `--ignore-rules`, `-c approval_policy=never`, `-c web_search="disabled"` (unless `--web` / `--search` is authorized and matches the prompt's web clause via `_common.validate_review_web`), generates strict Codex `--output-schema` JSON (`additionalProperties: false` and all properties listed in `required` at every object level), executes a single vendor attempt (no schema-repair retry on bound v2 calls), reads the raw `-o <last_msg_path>` UTF-8 bytes, and validates them via `verdict_v2.admit_carrier_output(pydantic_cls, carrier="native-final", stdout=raw_last_msg, stderr=result.stderr, exit_code=result.vendor_exit_code, completed=True)`.
    - Emits `result.review_binding`, `result.transport` (`route: "codex"`, `stdin_delivery: "complete"`, `binary: codex_bin`), and `persist_result_artifacts("codex", ...)`.
  - `bin/review_adapters_v2.py`:
    - `_codex(block: dict, *, cwd: Path, folder: Path, web: bool = False, native_capabilities: dict | None = None) -> dict`:
      - When `native_capabilities` has `source == "native-spawn-tool"` (legacy Host B compatibility), retains `_native(...)`.
      - When `native_capabilities` is omitted/empty or `source == "cli"` (the default on the `agy` host where Codex is an external CLI leg), resolves `codex` via `resolve_binary("codex")`, probes `[binary, "--version"]` and `[binary, "exec", "--help"]` to verify required flags (`--json`, `--output-schema`, `--sandbox`, `--ephemeral`, `--skip-git-repo-check`), writes `codex-capability.json` into `folder`, and returns `{"model": model, "effort": effort, "binary": binary, "cli_version": version, "transport_route": "codex", "capability_source": "cli-selection", "capability_file": str(path)}`.
  - `bin/review_round_v2.py`:
    - `_invocation(binding, adapter, folder, prompt)`: dispatches via `collaboration.spawn_agent` ONLY when `adapter["transport_route"] == "native"`; when `adapter["transport_route"] == "codex"`, builds the CLI wrapper invocation for `bin/codex_wrapper.py` (with `--sandbox read-only`, `--model`, `--effort`, `--expected-*`, `TRIAD_CODEX_BIN`, `TRIAD_REQUIRE_PINNED_VENDOR=1`, `TRIAD_DISPATCH_LOG_DIR`).
    - `_observations(...)` and `record_cli_attempt(...)`: accepts `transport_route == "codex"` as a `cli-run-log` route with `stdin_delivery == "complete"`.

- [ ] **Step 1: Write the failing test in `tests/test_codex_wrapper_and_adapters_v2.py`**

```python
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

DIGEST = "b" * 64


class TestCodexWrapperAndAdaptersV2(unittest.TestCase):
    def test_codex_wrapper_v2_admits_native_final_and_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "codex"
            verdict = {
                "schema": "triad-leg-verdict.v2",
                "review_id": "rev-codex-01",
                "family": "codex",
                "leg_name": "codex",
                "route": None,
                "attempt": 1,
                "content_digest": DIGEST,
                "verdict": "SAFE TO MERGE",
                "ApproveForMerge": True,
                "inspected_surfaces": ["bin/codex_wrapper.py"],
                "findings": [],
                "strengths": ["Solid"],
                "open_questions": [],
            }
            fake_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "args = sys.argv[1:]\n"
                "out_idx = args.index('-o') + 1\n"
                "out_file = args[out_idx]\n"
                "payload = os.environ['FAKE_CODEX_PAYLOAD']\n"
                "with open(out_file, 'w', encoding='utf-8') as f:\n"
                "    f.write(payload)\n"
                "print('{\"type\":\"turn.completed\"}')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

            import review_prompts_v2
            no_web_clause = review_prompts_v2.review_web_clause(False)
            prompt_file = tmp_path / "prompt.md"
            prompt_file.write_text(f"Header\n\n{no_web_clause}\n", encoding="utf-8")

            log_dir = tmp_path / "logs"
            env = {
                **os.environ,
                "TRIAD_REQUIRE_PINNED_VENDOR": "1",
                "TRIAD_CODEX_BIN": str(fake_bin),
                "TRIAD_DISPATCH_LOG_DIR": str(log_dir),
                "FAKE_CODEX_PAYLOAD": json.dumps(verdict),
            }
            cmd = [
                sys.executable,
                str(ROOT / "bin" / "codex_wrapper.py"),
                "--prompt-file",
                str(prompt_file),
                "--cwd",
                str(tmp_path),
                "--sandbox",
                "read-only",
                "--pydantic",
                "verdict_v2:LegVerdict",
                "--model",
                "gpt-5.6-terra",
                "--effort",
                "xhigh",
                "--timeout",
                "900",
                "--expected-review-id",
                "rev-codex-01",
                "--expected-family",
                "codex",
                "--expected-content-digest",
                DIGEST,
                "--expected-leg-name",
                "codex",
                "--expected-attempt",
                "1",
                "--expected-route",
                "null",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            parsed = json.loads(proc.stdout)
            self.assertEqual(parsed["family"], "codex")

            # Duplicate key in -o file must fail closed with exit 66 (schema-fail)
            body = json.dumps(verdict)
            dup_body = '{"review_id":"wrong",' + body[1:]
            env["FAKE_CODEX_PAYLOAD"] = dup_body
            proc_dup = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(proc_dup.returncode, 66, proc_dup.stderr)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_codex_wrapper_and_adapters_v2.py -v`
Expected: FAIL (missing `bin/codex_wrapper.py`)

- [ ] **Step 3: Implement `bin/codex_wrapper.py` and update `bin/_common.py`, `bin/review_adapters_v2.py`, and `bin/review_round_v2.py`**

- In `bin/_common.py`:
  - Ensure `PINNED_ENV_VARS` maps `"codex": "TRIAD_CODEX_BIN"` alongside `"claude"`, `"gemini"`, and `"antigravity"`.
  - Add `pydantic_to_codex_schema(pydantic_cls)` helper (adapted from Host A `bin/_common.py`) that enforces OpenAI structured-output strict object rules (`additionalProperties: false` and all property keys in `required`) while preserving v2 `const` and `enum` bindings.
- Create `bin/codex_wrapper.py`:
  - Accept `--reasoning` and `--effort` as aliases (`dest="effort"`, choices `("low", "medium", "high", "xhigh", "max")`) and `--search` / `--web` as aliases so `review_round_v2.py` can invoke `codex_wrapper.py` with the same flag convention (`--model=...`, `--effort`, `--web`, `--sandbox read-only`, `--expected-*`) as other CLI wrappers while preserving backward compatibility with Host A `--reasoning` and `--search`.
  - For v2 bound verdicts (`args.pydantic in _common.PACKAGED_V2_VERDICT_SPECS`), call `bound_wrapper(args, family="codex", route=None)`, enforce `args.sandbox == "read-only"`, validate review web clause via `_common.validate_review_web(args, args.prompt)`, run a single vendor call via `_run_codex_v2_structured_once`, read `last_msg_path`, and validate via `verdict_v2.admit_carrier_output(pydantic_cls, carrier="native-final", stdout=raw_last_msg, stderr=result.stderr, exit_code=result.vendor_exit_code, completed=True)`.
- Update `bin/review_adapters_v2.py` and `bin/review_round_v2.py` so `codex` supports CLI adapter preflight (`transport_route: "codex"`, `capability_file: str(folder / "codex-capability.json")`) when `native_capabilities` is not a `native-spawn-tool` dict, while preserving `native-spawn-tool` support when explicitly passed.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_codex_wrapper_and_adapters_v2.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bin/_common.py bin/codex_wrapper.py bin/review_adapters_v2.py bin/review_round_v2.py tests/test_codex_wrapper_and_adapters_v2.py
git commit -m "feat: add codex_wrapper.py with v2 native-final admission and CLI adapter support"
```

---

### Task 4: Antigravity (`agy`) Plugin Manifests, Skills, Bootstrap Installer, and Documentation

**Files:**
- Create: `plugin.json`
- Create: `.antigravity-plugin/plugin.json`
- Create: `skills/triad-claude-dispatch/SKILL.md`
- Create: `skills/triad-codex-dispatch/SKILL.md`
- Create: `skills/triad-antigravity-dispatch/SKILL.md`
- Create: `skills/triad-gemini-dispatch/SKILL.md`
- Create: `skills/triad-cross-family-review/SKILL.md`
- Create: `skills/triad-cross-family-review/references/convergence.md`
- Create: `skills/triad-cross-family-review/references/review-web.md`
- Create: `scripts/bootstrap.sh`
- Create: `bin/bootstrap_repair.py`
- Create: `scripts/verify_distribution.py`
- Create: `README.md`
- Create: `README.ko.md`
- Create: `SECURITY.md`
- Test: `tests/test_agy_plugin_and_bootstrap.py`

**Interfaces:**
- Consumes: `bin/*_wrapper.py`, `bin/review_round_v2.py`, `bin/library_adoption.py`
- Produces:
  - `plugin.json` & `.antigravity-plugin/plugin.json`: Antigravity native plugin manifest (`name: "agy-triad-dispatch"`, `version: "0.1.0"`, `skills: "./skills/"`).
  - `scripts/bootstrap.sh --install | --remove`:
    - Verifies `library_adoption.verify_library()` before any mutation.
    - Installs/removes the 5 managed launchers (`claude_wrapper.py`, `codex_wrapper.py`, `antigravity_wrapper.py`, `gemini_wrapper.py`, `review_round.py`) in `TRIAD_BOOTSTRAP_BIN_DIR` (`~/.local/bin`) with atomic command-group staging and SHA-256 provenance verification via `bin/bootstrap_repair.py`.
    - Records classifier patches under `~/.config/agy-triad-dispatch/classifier-patches.json` (or `TRIAD_CLASSIFIER_EXTENSION`).

- [ ] **Step 1: Write the failing test in `tests/test_agy_plugin_and_bootstrap.py`**

```python
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestAgyPluginAndBootstrap(unittest.TestCase):
    def test_plugin_manifests_and_skills_exist(self):
        plugin_json = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin_json["name"], "agy-triad-dispatch")
        for skill in (
            "triad-claude-dispatch",
            "triad-codex-dispatch",
            "triad-antigravity-dispatch",
            "triad-gemini-dispatch",
            "triad-cross-family-review",
        ):
            skill_md = ROOT / "skills" / skill / "SKILL.md"
            self.assertTrue(skill_md.is_file(), f"missing {skill_md}")

    def test_toolkit_inventory_hashes_cleanly_in_review_round_v2(self):
        sys.path.insert(0, str(ROOT / "bin"))
        import review_round_v2
        toolkit_hashes = review_round_v2._toolkit()
        self.assertIn("bin/codex_wrapper.py", toolkit_hashes)
        self.assertIn("bin/claude_wrapper.py", toolkit_hashes)
        self.assertIn("bin/antigravity_wrapper.py", toolkit_hashes)
        self.assertIn("bin/gemini_wrapper.py", toolkit_hashes)
        self.assertIn("contracts/library-adoption.json", toolkit_hashes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_agy_plugin_and_bootstrap.py -v`
Expected: FAIL with `FileNotFoundError: .../plugin.json`

- [ ] **Step 3: Create `plugin.json`, `.antigravity-plugin/plugin.json`, `skills/*`, `scripts/bootstrap.sh`, `bin/bootstrap_repair.py`, `scripts/verify_distribution.py`, `README.md`, `README.ko.md`, and `SECURITY.md`**

- Write `plugin.json` and `.antigravity-plugin/plugin.json` for Antigravity native plugin discovery.
- Adapt the 5 skills under `skills/` (`triad-claude-dispatch`, `triad-codex-dispatch`, `triad-antigravity-dispatch`, `triad-gemini-dispatch`, `triad-cross-family-review`) for the `agy` host leader, keeping all skill body instructions in English and Korean descriptions where appropriate per `GEMINI.md`.
- Update `bin/bootstrap_repair.py` and `scripts/bootstrap.sh` so `MANAGED_LAUNCHERS` includes `codex_wrapper.py` alongside `claude_wrapper.py`, `gemini_wrapper.py`, `antigravity_wrapper.py`, and `review_round.py`, and verifies `library_adoption.verify_library()` before any install mutation.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_agy_plugin_and_bootstrap.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin.json .antigravity-plugin/ skills/ scripts/ bin/bootstrap_repair.py README.md README.ko.md SECURITY.md tests/test_agy_plugin_and_bootstrap.py
git commit -m "feat: package agy-triad-dispatch plugin manifests, skills, and bootstrap installer"
```

---

### Task 5: End-to-End V2 Lifecycle & Neutral-Directory Installed Package Verification

**Files:**
- Create: `tests/test_v2_lifecycle_e2e.py`

**Interfaces:**
- Consumes: `review_round_v2.create_basis`, `review_round_v2.allocate_attempt`, `review_round_v2.record_cli_attempt`, `review_round_v2.collect`, `library_adoption.verify_library`
- Produces: Complete provider-free verification of the 3-family v2 review round (`claude` + `codex` + `google` via `agy` and `gemini` routes), C23 positive/negative recovery, timeout/cancellation cleanup, and neutral-directory installed package verification (`cd /tmp`).

- [ ] **Step 1: Write `tests/test_v2_lifecycle_e2e.py` covering the full handoff verification matrix**

```python
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


class TestV2LifecycleE2E(unittest.TestCase):
    def test_neutral_directory_installed_copy_verifies_library_and_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "installed-plugin"
            shutil.copytree(
                ROOT,
                installed,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "_runs", "_logs", "_debug"),
            )
            cmd = [
                sys.executable,
                "-c",
                (
                    f"import sys; sys.path.insert(0, {str(installed / 'bin')!r}); "
                    "import library_adoption, validate_v2; "
                    "meta = library_adoption.verify_library(); "
                    "contracts = validate_v2.load_contracts(); "
                    "assert meta['version'] == '0.1.0.dev3'; "
                    "assert 'leg-verdict.schema.json' in contracts"
                ),
            ]
            proc = subprocess.run(cmd, cwd="/tmp", capture_output=True, text=True, check=True)
            self.assertEqual(proc.returncode, 0)
```
Also include tests in `tests/test_v2_lifecycle_e2e.py` for:
- Full 3-family v2 round (`claude`, `codex`, `google` via `agy`) from `create_basis` -> `allocate_attempt` -> wrapper execution -> `record_cli_attempt` -> `collect` returning `status == "AGREED"`.
- Missing or mutated file in `bin/triad_dispatch_lib` failing closed in `library_adoption.verify_library()` with missing/unexpected path diagnostics.
- C23 negative test: `status == "ERROR"` at `vendor_exit_code == 0` without an allowed read or with a forbidden tool call is rejected and never admitted.

- [ ] **Step 2: Run the full test suite from `/tmp` and from the repository root**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Expected: PASS (all tests green)

- [ ] **Step 3: Commit**

```bash
git add tests/test_v2_lifecycle_e2e.py
git commit -m "test: add end-to-end v2 lifecycle and neutral-directory verification suite"
```

---

### Task 6: Upstream `codefoundry-io/triad-dispatch-lib` Issue Proposal for First-Class AGY Host Profile (`host="C"` / `host="AGY"`)

**Files:**
- Create: `docs/handoffs/triad-dispatch-lib-agy-host-proposal.md`

**Interfaces:**
- Consumes: `triad_dispatch_lib/prompts.py` (`0.1.0.dev3`)
- Produces: Upstream GitHub Issue on `codefoundry-io/triad-dispatch-lib` documenting the `host="AGY"` prompt profile enhancement while preserving `host="B"` CLI-wrapper compatibility in `0.1.0.dev3`.

- [ ] **Step 1: Document the upstream library proposal in `docs/handoffs/triad-dispatch-lib-agy-host-proposal.md` and file the GitHub Issue on `codefoundry-io/triad-dispatch-lib` (upon owner confirmation)**

Document the exact lines in `src/triad_dispatch_lib/prompts.py` (lines 66–69) where `host not in ("A", "B")` is enforced, explain why `agy-triad-dispatch` uses `host="B"` for CLI-wrapper structured-output dispatches against `0.1.0.dev3`, and specify the exact contract for a first-class `host="AGY"` (or `host="C"`) profile if native `invoke_subagent` review legs are added in `0.1.0.dev4`.

- [ ] **Step 2: Commit**

```bash
git add docs/handoffs/triad-dispatch-lib-agy-host-proposal.md
git commit -m "docs: add upstream triad-dispatch-lib AGY host profile proposal"
```
