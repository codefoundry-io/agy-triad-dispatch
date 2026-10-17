# Security Model

`agy-triad-dispatch` coordinates external model families from the **Google Antigravity (`agy`)** host leader; it does not turn their output into a trusted control plane. Its durable boundaries are explicit data authorization, pinned executable paths, immutable-directory digests, mutation detection, strict result custody, and deterministic owner apply.

## Bundled Shared Library (`triad-dispatch-lib 0.1.0.dev4`)

Explicit v2 review and carrier admission use the pinned, generated `triad-dispatch-lib` package (version `0.1.0.dev4`, commit `087af94a7078de0ae9701fb057840b5492f51f66`, wheel SHA-256 `c5bfdf10c4ce81f431ec7f77106e4d77b1556ac9c7b6b01bdb6616f988e8b2cc`).
- Toolkit custody includes package code, resource manifests, and the adoption record (`contracts/library-adoption.json`).
- Verified offline via `bin/library_adoption.py`. Any ambient `PYTHONPATH` or pre-loaded module override outside `<repo>/bin/triad_dispatch_lib` is rejected fail-closed before inference.
- The adoption hashes establish consistency with the reviewed build. Runtime imports never download dependencies.

## Native Permission Boundaries

### Google Antigravity (`agy`)
- Read-only calls require AGY >=1.2.1 and select native read/search tools with `excludeDefaultComponents: true`.
- Native plan mode (`--mode plan`) and read-only sandbox (`--sandbox read-only`) are enforced.
- Headless autoapproval is strictly omitted on read-only review calls.
- Dispatch never installs or mutates global AGY settings, leases, or lock files. Old residue is preserved without deletion or healing.
- With `--project <UUID>`, the wrapper validates the project record's ID, single cwd resource, and required deny entries without editing configuration.
- The formal Google prompt authorizes only AGY native file-read/search tools (`grep_search`, `list_dir`, `find_by_name`, `view_file`) and prohibits command, terminal, file-write, notebook, subagent, browser, and MCP tools.

### Google Gemini CLI (`gemini`)
- When AGY is absent, Gemini Enterprise OAuth may be selected as a Google leg fallback.
- The wrapper enforces CLI Auto (`-m auto`) and native Plan Mode, with the mode-independent packaged read/search policy (`policies/gemini-readonly.toml`) as the enforcement boundary.
- Effective approval mode is recorded as `unexposed`. The wrapper records literal `runtime_identity: "unexposed"` and never infers model identity from `stats.models`.

### Anthropic Claude Code CLI (`claude`)
- Dispatches Claude with UTF-8 stdin prompt delivery, avoiding command-line argv exposure.
- Enforces `--sandbox read-only`, `--model opus`, `--effort xhigh`, and `--timeout 1200` on the formal review leg.
- Does not edit or execute candidate code.

### OpenAI Codex CLI (`codex`)
- Formal review leg enforces `--sandbox read-only`, `--ignore-rules`, `-c approval_policy=never`, and `-c web_search="disabled"` (unless web is bound).
- Uses strict object schemas derived from Pydantic (`additionalProperties: false` and all properties in `required`).
- Single-shot execution without automatic schema-repair retry on bound v2 calls.

## Original-Carrier `admit_output` Enforcement

Every formal v2 review leg admits verdicts directly from the original provider carrier payload via `triad_dispatch_lib.output.admit_output`:
- `claude-json`: Validates markdown-fenced or raw JSON from Claude stdout.
- `native-final`: Validates raw `-o <last_msg_path>` bytes from Codex.
- `agy-stream-json`: Validates streaming event lines from AGY plan mode stdout.
- `gemini-json`: Validates raw response JSON from Gemini stdout.

### Nested Duplicate Key Rejection
Nested duplicate JSON keys (e.g., duplicate `review_id` keys inside `structured_output` or `response`) are detected and rejected fail-closed with exit code 66 (`schema-fail`). JSON parser implementations that silently collapse duplicate keys are bypassed by carrier-level syntax inspection.

### Six Mandatory Review Bindings
Every formal review verdict strictly binds all six fields:
1. `review_id`: Matches the allocated round review ID.
2. `family`: One of `"claude"`, `"google"`, or `"codex"`.
3. `content_digest`: SHA-256 digest of the prepared review directory.
4. `leg_name`: Canonical leg identifier (`"claude"`, `"google"`, `"codex"`).
5. `attempt`: Attempt index integer (starts at 1).
6. `route`: Explicit route string (`"agy"` for Antigravity, `"gemini"` for Gemini fallback, `null` for Claude and Codex).

Cross-route or cross-leg substitutions fail closed even if the payload is otherwise schema-valid.

## C23 Plan-Mode Recovery

Under AGY plan mode, benign post-completion terminal events (`denied_post_completion` or `_agy_finish_resubmitted`) with `status == "ERROR"` and `vendor_exit_code == 0` are recovered safely:
1. The wrapper checks host C23 recovery preconditions: `status == "ERROR"`, `vendor_exit_code == 0`, at least one allowed read observed, and zero forbidden tool executions.
2. It extracts the raw `structured_output` byte/character span via `json_member_span`.
3. It validates the span via `admit_carrier_output(carrier="native-final")` using `admit_output`.
4. If no allowed read occurred or forbidden tools were executed, C23 recovery is refused fail-closed.

## Review Directory and Round Integrity

- Formal three-family gates review one leader-prepared directory under a mode-0700 root in the system temporary directory (`triad-review-*`).
- The prepared directory contains approved production source, configuration, documentation, current `TASK.md`, and `REVIEW.diff`.
- `manifest` computes the SHA-256 digest of the directory, and `capture` records the baseline.
- Reviewers inspect with read/search tools only; they never execute candidate code, tests, or build scripts.
- After all legs terminate, `verify` confirms that zero bytes in the prepared directory were modified (`ROUND_INTEGRITY_OK`). Any mutation invalidates the round.

## Bootstrap Installer and Trust Boundaries

- Bootstrap (`scripts/bootstrap.sh`) installs 5 managed command launchers (`claude_wrapper.py`, `codex_wrapper.py`, `antigravity_wrapper.py`, `gemini_wrapper.py`, `review_round.py`) into `~/.local/bin`.
- Launchers use isolated Python execution (`-E`) and scrub code-injection loader variables (`LD_*`, `DYLD_*`, `PYTHON*`, `NODE_*`) before invoking wrappers.
- All-or-nothing transactional staging: no partial launcher group is exposed on failure.
- Preflight verifies `library_adoption.verify_library()` before any install mutation.
- Learned classifier patches persist at `~/.config/agy-triad-dispatch/classifier-patches.json`.
- Repair analysis uses a fresh native proposal-only child; applying repairs remains an explicit owner action via `apply_patch.py`.
