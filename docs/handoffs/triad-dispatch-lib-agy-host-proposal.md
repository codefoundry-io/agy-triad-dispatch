# Upstream Proposal: First-Class Antigravity Host Profile (`host="C"` / `host="AGY"`) in `triad-dispatch-lib`

- **Target Repository**: `codefoundry-io/triad-dispatch-lib`
- **Target Release**: `0.1.0.dev4`
- **Originating Host Project**: `agy-triad-dispatch` (Antigravity-native TRIAD host toolkit)
- **Vendored Reference Release**: `0.1.0.dev3` (commit `2d1e62e7de6f33fa13a918a39579c6b5e74def00`)
- **Document Status**: Ready to File
- **Date**: 2026-10-15

---

## 1. Executive Summary

In `triad-dispatch-lib 0.1.0.dev3`, prompt rendering via `render_review_clauses` strictly allows only two host profiles: `host="A"` (Claude-led) and `host="B"` (Codex-led). The Antigravity host toolkit (`agy-triad-dispatch`) currently operates by supplying `host="B"` as an operational workaround when dispatching review legs via CLI wrappers (`claude_wrapper.py`, `codex_wrapper.py`, `antigravity_wrapper.py`, `gemini_wrapper.py`). This allows `agy-triad-dispatch` to preserve byte-for-byte fidelity with the frozen upstream `0.1.0.dev3` vendored library without modifying cryptographic package pins.

However, treating an Antigravity host as `host="B"` introduces structural limitations:
1. **Semantic Inaccuracy**: Prompts describe Codex host deployment constraints (`Host B (Codex-led)`) rather than Antigravity host mechanics.
2. **Tool-Instruction Mismatch**: When review legs (especially `google` via `route="agy"` or `codex`) are dispatched natively through Antigravity's `invoke_subagent` interface rather than external CLI subprocesses, the host requires instructions reflecting native Antigravity file tools (`view_file`, `grep_search`, `list_dir`, `find_by_name`) and web tools (`read_url_content`, `search_web`), rather than shell commands (`cat`, `sed`, `rg`, `git diff`) or external CLI wrapper constraints.
3. **Audit and Carrier Asymmetry**: Host C has its own distinct runtime guarantees and carrier admission boundaries (`native-final` for in-session/subagent reviews vs `agy-stream-json` for streaming CLI subprocesses).

This document details the exact architectural rationale for the `0.1.0.dev3` workaround, specifies the formal contract enhancement for `0.1.0.dev4` to introduce `host="C"` (with `host="AGY"` accepted as a canonical alias), and provides a ready-to-file GitHub Issue body for `codefoundry-io/triad-dispatch-lib`.

---

## 2. Analysis of Current State in `triad-dispatch-lib 0.1.0.dev3`

### 2.1 Upstream Code Inspection: `src/triad_dispatch_lib/prompts.py`

In `triad-dispatch-lib 0.1.0.dev3` (`src/triad_dispatch_lib/prompts.py`, mirrored in `bin/triad_dispatch_lib/prompts.py`), lines 65–69 define the strict host validation gate:

```python
    _validate_profile(family, route)
    if host not in ("A", "B"):
        raise PromptError("Unsupported prompt profile.")
    if type(agy_hook_active) is not bool or (agy_hook_active and (family, host, route) != ("google", "A", "agy")):
        raise PromptError("Invalid active hook profile.")
```

Furthermore, clause filtering inside `parse_clauses()` (lines 50–51) relies on a binary assumption between Host A and Host B:

```python
    if host is not None and re.search(r"\b" + ("A" if host == "B" else "B") + r"-only\b", heading):
        continue
```

In the conceptual architecture of `triad-dispatch-lib`:
- `host="A"` represents **"Host A (Claude-led CLI wrapper)"**:
  - The Claude leg is an in-session agent admitted from a raw response containing an `<END-VERDICT>` marker line and starting with a raw `{` (`claude-output-shape-notice`, `claude-output-integrity`).
  - The Google leg incorporates `google-a-hook-audit` when `agy_hook_active=True`, which audits Claude PreToolUse hook enforcement.
- `host="B"` represents **"Host B (Codex-led)"**:
  - Claude and Google review legs are dispatched as external CLI subprocess children where the child process strictly enforces JSON schema output (`--json-schema` on Claude, structured JSON on Gemini/AGY) and exits cleanly.
  - Raw reply markers (`<END-VERDICT>`) and Host A in-session hooks are skipped.

### 2.2 Why `agy-triad-dispatch` Uses `host="B"` in `0.1.0.dev3`

In `agy-triad-dispatch`, the vendored library integrity is rigorously verified by `bin/library_adoption.py`:
- Target commit: `2d1e62e7de6f33fa13a918a39579c6b5e74def00`
- Target version: `0.1.0.dev3`
- Approved package hash: `ccc38ebfa4f7af4151da8fa51ddd1e60d9af01e4b25c98f8742af919ce224846`

Because modifying `bin/triad_dispatch_lib/` would immediately invalidate the package digest and fail `library_adoption.verify_library()`, callers must supply a valid `host` argument from `("A", "B")`.

In `bin/review_prompts_v2.py` (lines 103–106), `agy-triad-dispatch` invokes:

```python
    clauses = shared.render_review_clauses(
        family=expected["family"], host="B", review_date=review_date,
        review_web_authorized=review_web_authorized, replacements=replacements,
        review_kind=review_kind, route=expected["route"])
```

The reasons `host="B"` was chosen over `host="A"` are:

1. **Strict CLI Child Wrapper Architecture**:
   In `agy-triad-dispatch`, the review orchestrator (`bin/review_round_v2.py`) dispatches external review legs via CLI wrappers:
   - Claude leg: `bin/claude_wrapper.py` invokes `claude` CLI with `--json-schema` and `--output-format json`.
   - Codex leg: `bin/codex_wrapper.py` invokes `codex` CLI with `--output-schema` and `--sandbox read-only`.
   - Google leg: `bin/antigravity_wrapper.py` or `bin/gemini_wrapper.py` invokes provider CLIs with structured JSON carriers (`agy-stream-json` or `gemini-json`).

2. **Incompatibility with Host A Clauses**:
   - If `host="A"` were passed to `render_review_clauses()` for `family="claude"`, `prompts/leg-claude.md` would include `claude-output-shape-notice` (requiring the output to start with `{` without surrounding JSON envelope) and `claude-output-integrity` (requiring a non-JSON trailing marker line `<END-VERDICT>`). When passed to `claude_wrapper.py`, the Claude CLI's `--json-schema` validation fails if trailing non-JSON markers are emitted.
   - If `host="A"` were passed for `family="google"`, the profile validation at line 68 restricts `agy_hook_active` strictly to `(family, host, route) == ("google", "A", "agy")`. Host A's `google-a-hook-audit` assumes Claude's PreToolUse hook mechanism, which does not exist in the same shape under Antigravity.

3. **Byte-for-Byte Output Stability**:
   Passing `host="B"` filters out the `A-only` clauses via line 50 (`re.search(r"\bA-only\b", heading)`), producing a clean, schema-enforced prompt bundle suitable for isolated CLI wrapper execution across all review legs.

Consequently, `host="B"` was the only mathematically and architecturally viable host profile in `0.1.0.dev3` that allowed `agy-triad-dispatch` to preserve byte-for-byte compatibility with `triad-dispatch-lib 0.1.0.dev3`.

---

## 3. Motivations for First-Class AGY Host (`host="C"` / `host="AGY"`)

While `host="B"` provides operational stability for CLI wrapper execution, it falls short when Antigravity acts as a first-class native host in the following scenarios:

### 3.1 Host Identity and Deployment Context
Under `host="B"`, `prompts/common-clauses.md` (`deployment-context`) states:
> "Inside one working folder no second operation runs while one runs... operations started from different folders, and the claude-host and codex-host toolkits, do run at the same time on one machine and meet at machine-level state..."

This explicitly names only Claude and Codex hosts. An Antigravity host should be acknowledged as a peer host with its own deployment context and configuration boundaries (e.g. `~/.config/agy-triad-dispatch/`, `TRIAD_CLASSIFIER_EXTENSION`, and Antigravity plugin paths).

### 3.2 Native `invoke_subagent` Dispatch vs. Subprocess CLI Wrappers
In an Antigravity environment, reviews can be conducted in two modes:
1. **External CLI wrapper mode** (`bin/antigravity_wrapper.py`, `bin/codex_wrapper.py`, `bin/claude_wrapper.py`):
   Executed as separate CLI subprocesses. Output arrives via stdout envelopes or last-message files.
2. **Native subagent mode** (`invoke_subagent`):
   Dispatched directly within Antigravity using Antigravity's native agent routing (`Model`, `TypeName: "tier-standard"` / `"tier-cheap"` / `"tier-capable"`).

When a leg is executed as a native Antigravity subagent:
- **Available Tools**:
  - File inspection: `view_file`, `grep_search`, `list_dir`, `find_by_name`.
  - Web inspection (when authorized): `read_url_content`, `search_web`.
- **Forbidden Tools**:
  - Mutation: `write_to_file`, `replace_file_content`.
  - Execution: `run_command` (forbidden to prevent interactive user approval modals).
  - Agent coordination: `invoke_subagent`, `send_message` (except when returning final verdict to caller).
- **Tool Instruction Mismatch**:
  - `prompts/leg-codex.md` tells the reviewer: `"You MAY read authorized files under the working directory with read-only commands (cat, sed -n, rg, ls, git diff, git show, git log)..."`
  - In a native Antigravity subagent environment, shell commands are forbidden; the reviewer must use native file inspection tools (`view_file`, `grep_search`, `list_dir`, `find_by_name`).

### 3.3 Output Carrier Selection
In native subagent execution, output is returned directly in the conversation context or final response, matching `carrier="native-final"`, whereas CLI wrapper execution emits `carrier="agy-stream-json"` or `carrier="gemini-json"`. A first-class host profile allows prompt clauses to align cleanly with the designated carrier.

---

## 4. Proposed Specification for `triad-dispatch-lib 0.1.0.dev4`

### 4.1 Interface Signatures

In `src/triad_dispatch_lib/prompts.py`:

```python
VALID_HOSTS = ("A", "B", "C", "AGY")
CANONICAL_HOST = {"A": "A", "B": "B", "C": "C", "AGY": "C"}

HOST_PROFILES = {
    "A": "Host A (Claude-led CLI wrapper)",
    "B": "Host B (Codex-led)",
    "C": "Host C (Antigravity-led)",
}

def render_review_clauses(*, family: str, host: str, review_date: str,
                          review_web_authorized: bool, replacements: dict,
                          review_kind: str = "pre-merge", route: str | None = None,
                          agy_hook_active: bool = False,
                          transport_mode: str = "cli") -> str:
    """Render a canonical family/host profile with one-pass caller substitution."""
    _validate_profile(family, route)
    if host not in VALID_HOSTS:
        raise PromptError("Unsupported prompt profile.")
    canonical_host = CANONICAL_HOST[host]
    ...
```

### 4.2 Clause Filtering in `parse_clauses`

Update clause filtering to support multi-host selector tags:
- Headings can contain `(A-only)`, `(B-only)`, `(C-only)`, `(CLI-only)`, `(native-only)`, or composite selectors like `(A,B-only)`.
- When `host` is supplied, headings with `([Host]-only)` matching other hosts are filtered out.
- For backward compatibility:
  - If heading has `A-only`, it is included only when `canonical_host == "A"`.
  - If heading has `B-only`, it is included only when `canonical_host == "B"`.
  - If heading has `C-only` or `AGY-only`, it is included only when `canonical_host == "C"`.

### 4.3 Prompt Clause Enhancements

#### 1. `prompts/common-clauses.md` (`deployment-context`)
Update the deployment context to recognize Host C:
```text
When the reviewed code is a TRIAD dispatch host's own code (its review and dispatch toolkit), its deployment context, evidence R-THREAT / D-THREAT-MODEL-20261003 / D-CONCURRENCY-FACT-20261008, is one operator and no malicious actor. Inside one working folder no second operation runs while one runs (concurrency inside one operation, such as two legs of one round, is real); operations started from different folders, and the claude-host, codex-host, and antigravity-host toolkits, do run at the same time on one machine and meet at machine-level state (the CLIs' own settings and configuration, a classifier extension file, the logs of one installed toolkit).
```

#### 2. `prompts/leg-codex.md`
Support Host C native subagent vs CLI wrapper dispatch:
- For CLI wrapper dispatch (default, `transport_mode="cli"`): retain read-only shell commands (`cat`, `sed -n`, `rg`, `ls`, `git diff`).
- For Host C native subagent dispatch (`transport_mode="native"`, `C-native-only`):
```text
Use the native read/search tools provided by your host (view_file, grep_search, list_dir, find_by_name) to verify claims within <worktree>, including relevant unchanged code. Never execute shell commands, mutation tools, or dispatch nested subagents. Cite file:line for assertions.
```

#### 3. `prompts/leg-google.md`
- Clarify tool instructions for `route="agy"`:
  - Under `host="C"`:
    ```text
    On Antigravity host, inspect authorized files using native tools: view_file, grep_search, list_dir, find_by_name. Open <brief-file> FIRST and ONCE, then open <gated-patch-file>. When web verification is authorized, use read_url_content and search_web. Do not execute run_command or call mutation tools (write_to_file, replace_file_content). Return exactly one LegVerdict JSON object matching the provided schema.
    ```

### 4.4 Backward Compatibility Invariants
1. **Existing Host A and Host B Outputs**:
   For any call with `host="A"` or `host="B"`, `render_review_clauses()` must return byte-for-byte identical output to `0.1.0.dev3`.
2. **Zero Ambient Library Breakage**:
   `triad-dispatch-lib` remains a pure-Python, zero-subprocess, zero-filesystem-mutation library.
3. **Graceful Adoption**:
   `agy-triad-dispatch` will be able to switch its call from `host="B"` to `host="AGY"` (or `host="C"`) in `0.1.0.dev4` with zero schema disruption.

---

## 5. Ready-to-File GitHub Issue Body

```markdown
### Summary

Add first-class Antigravity host profile (`host="C"` / `host="AGY"`) to `render_review_clauses()` and clause bundle definitions in `triad_dispatch_lib.prompts`.

### Background & Current State (`0.1.0.dev3`)

In `triad-dispatch-lib 0.1.0.dev3` (`src/triad_dispatch_lib/prompts.py`, lines 66–69):

```python
    _validate_profile(family, route)
    if host not in ("A", "B"):
        raise PromptError("Unsupported prompt profile.")
    if type(agy_hook_active) is not bool or (agy_hook_active and (family, host, route) != ("google", "A", "agy")):
        raise PromptError("Invalid active hook profile.")
```

Currently, the library formalizes two host profiles:
- `host="A"`: Claude-led CLI wrapper / in-session agent
- `host="B"`: Codex-led host

The emerging Antigravity host toolkit (`codefoundry-io/agy-triad-dispatch`) currently vendors `triad-dispatch-lib 0.1.0.dev3` at commit `2d1e62e7de6f33fa13a918a39579c6b5e74def00`. To maintain byte-for-byte integrity and pass `library_adoption.verify_library()`, `agy-triad-dispatch` dispatches external CLI review legs (`claude_wrapper.py`, `codex_wrapper.py`, `antigravity_wrapper.py`, `gemini_wrapper.py`) using `host="B"`:

```python
    clauses = shared.render_review_clauses(
        family=expected["family"], host="B", review_date=review_date,
        review_web_authorized=review_web_authorized, replacements=replacements,
        review_kind=review_kind, route=expected["route"])
```

### Problem & Motivation

Using `host="B"` as a proxy for Antigravity has significant drawbacks:
1. **Deployment Context Inaccuracy**:
   In `prompts/common-clauses.md` (`deployment-context`), the prompt states that only `claude-host` and `codex-host` toolkits meet at machine-level state. The Antigravity host (`antigravity-host`) is omitted.
2. **Native Subagent (`invoke_subagent`) vs. External CLI Wrapper Dispatch**:
   On Host C (Antigravity), review legs can be dispatched either via external CLI wrappers or directly via native `invoke_subagent`.
   - When dispatched via native `invoke_subagent`, shell commands (`cat`, `sed`, `rg`, `ls`, `git diff`) referenced in `leg-codex.md` are unavailable or inappropriate, as Antigravity subagents operate via native tools (`view_file`, `grep_search`, `list_dir`, `find_by_name`).
   - The reviewer needs clean native tool constraints and explicit prohibition of mutation tools (`write_to_file`, `replace_file_content`) and terminal commands (`run_command`).
3. **Host Profile Nomenclature**:
   Host profiles should formally distinguish `Host C (Antigravity-led)` alongside `Host A` and `Host B`.

### Proposed Solution for `0.1.0.dev4`

1. **Host Profile Validation**:
   Expand `host` validation in `src/triad_dispatch_lib/prompts.py`:
   - Accept `host in ("A", "B", "C", "AGY")`.
   - Map `"AGY"` as an alias to `"C"`.
   - Define `host_profile = "Host C (Antigravity-led)"` for `host in ("C", "AGY")`.

2. **Clause Filtering**:
   Generalize clause filtering in `parse_clauses()` so that `A-only`, `B-only`, and `C-only` clauses are selectively included based on the selected host profile.

3. **Prompt Bundle Updates**:
   - `prompts/common-clauses.md`: Add `antigravity-host` toolkit to `deployment-context`.
   - `prompts/leg-google.md`: Add Host C tool conventions (explicit native tools: `view_file`, `grep_search`, `list_dir`, `find_by_name`, `read_url_content`, `search_web`).
   - `prompts/leg-codex.md`: Provide native subagent clause alternative for Host C.

4. **Backward Compatibility**:
   - Retain 100% byte-for-byte identical prompt rendering for `host="A"` and `host="B"`.
   - Keep `carrier="native-final"` and `carrier="agy-stream-json"` compatibility intact in `output.py`.

### Verification Plan
- Unit tests asserting `render_review_clauses(..., host="C", ...)` and `render_review_clauses(..., host="AGY", ...)` succeed and produce equivalent output.
- Regression tests asserting `host="A"` and `host="B"` outputs match `0.1.0.dev3` character-for-character.
- Integration testing with `agy-triad-dispatch` v2 lifecycle test suite.
```

---

## 6. Adoption Plan for `agy-triad-dispatch`

Once `triad-dispatch-lib 0.1.0.dev4` is released:
1. Vendor `0.1.0.dev4` into `bin/triad_dispatch_lib/` and update `contracts/library-adoption.json` with new content and bundle SHA-256 digests.
2. In `bin/review_prompts_v2.py`, switch `host="B"` to `host="AGY"`.
3. Update `review_adapters_v2.py` and `review_round_v2.py` to route native subagent dispatch (`invoke_subagent`) using Host C native tool clauses and `carrier="native-final"`, while preserving CLI wrapper routes (`antigravity_wrapper.py`, `codex_wrapper.py`) with their respective carriers.
4. Run complete test suites (`tests/test_library_adoption_v2.py`, `tests/test_carrier_admission_v2.py`, `tests/test_v2_lifecycle_e2e.py`) to verify seamless upgrade.
