# agy-triad-dispatch

Cross-family Triad dispatch and formal v2 review plugin for **Google Antigravity (`agy`)**, backed by pinned `triad-dispatch-lib 0.1.0.dev4` (`087af94a7078de0ae9701fb057840b5492f51f66`) and original-carrier `admit_output` validation.

## Overview

`agy-triad-dispatch` packages an Antigravity-native plugin manifest (`plugin.json`, `.antigravity-plugin/plugin.json`), 5 managed provider and selector wrappers, formal v2 review coordination, and an offline-verified bundled library.

The Antigravity host leader coordinates three independent model families:
- **Claude**: Anthropic Claude Code CLI (`claude`), via `bin/claude_wrapper.py`.
- **Google**: Google Antigravity CLI (`agy`), via `bin/antigravity_wrapper.py` (preferred; optional Gemini Enterprise OAuth fallback via `bin/gemini_wrapper.py`).
- **Codex**: OpenAI Codex CLI (`codex`), via `bin/codex_wrapper.py`.

## Core Architecture

### 1. Bundled Shared Library (`triad-dispatch-lib 0.1.0.dev4`)
- Bundled at `bin/triad_dispatch_lib/`, pinned to commit `087af94a7078de0ae9701fb057840b5492f51f66` (Wheel SHA-256 `c5bfdf10c4ce81f431ec7f77106e4d77b1556ac9c7b6b01bdb6616f988e8b2cc`).
- Verified offline against `contracts/library-adoption.json` via `bin/library_adoption.py`. Ambient library overrides are rejected fail-closed.
- Release: https://github.com/codefoundry-io/triad-dispatch-lib/releases/tag/v0.1.0.dev4
- Host adoption guide: https://github.com/codefoundry-io/triad-dispatch-lib/blob/v0.1.0.dev4/docs/host-adoption.md

### 2. Managed Wrappers and Launchers
Bootstrap installs 5 managed command wrappers in `~/.local/bin` (`TRIAD_BOOTSTRAP_BIN_DIR`):
- `claude_wrapper.py`: Dispatches Claude with stdin prompt delivery, native Plan Mode, and `claude-json` carrier admission.
- `codex_wrapper.py`: Dispatches Codex CLI with `--sandbox {read-only,workspace-write}`, `--model`, `--effort`, `--web`, strict OpenAI structured schemas, and `native-final` carrier admission.
- `antigravity_wrapper.py`: Dispatches Antigravity with native plan mode, `--sandbox read-only`, exclusion profiles, and `agy-stream-json` carrier admission.
- `gemini_wrapper.py`: Standalone Gemini CLI compatibility wrapper with mode-independent read/search policy and `gemini-json` carrier admission.
- `review_round.py`: Cross-family review orchestrator invoking `bin/review_round_v2.py`.

### 3. Original-Carrier `admit_output` Enforcement
Every formal v2 review leg admits verdicts directly from the original provider carrier payload via `triad_dispatch_lib.output.admit_output`:
- `claude-json`: Validates markdown-fenced or raw JSON from Claude stdout.
- `native-final`: Validates raw `-o <last_msg_path>` bytes from Codex.
- `agy-stream-json`: Validates streaming event lines from AGY plan mode stdout.
- `gemini-json`: Validates raw response JSON from Gemini stdout.

Duplicate JSON object keys inside nested carrier structures (e.g., duplicate `review_id` keys inside `structured_output` or `response`) fail closed immediately with exit code 66 (`schema-fail`), preventing silent key-overwriting bugs.

### 4. Six Mandatory Review Bindings
Every formal review verdict strictly binds all six fields:
1. `review_id`: Matches the allocated round review ID.
2. `family`: One of `"claude"`, `"google"`, or `"codex"`.
3. `content_digest`: SHA-256 digest of the prepared review directory.
4. `leg_name`: Canonical leg identifier (`"claude"`, `"google"`, `"codex"`).
5. `attempt`: Attempt index integer (starts at 1).
6. `route`: Explicit route string (`"agy"` for Antigravity, `"gemini"` for Gemini fallback, `null` for Claude and Codex).

### 5. C23 Plan-Mode Recovery
Under AGY plan mode, benign post-completion terminal events (`denied_post_completion` or `_agy_finish_resubmitted`) with `status == "ERROR"` and `vendor_exit_code == 0` are recovered safely:
- The wrapper extracts the raw `structured_output` byte/character span via `json_member_span`.
- Validates the span via `admit_carrier_output(carrier="native-final")` using `admit_output`.
- Refuses recovery if no allowed read was observed or if forbidden tools were executed.

## Bootstrap Installer

Run bootstrap from your authenticated login environment outside the workspace:

```bash
# Install managed launchers and verify library adoption
./scripts/bootstrap.sh --install

# Remove managed launchers and clean legacy artifacts
./scripts/bootstrap.sh --remove
```

- Preflight verifies `library_adoption.verify_library()` before any install mutation.
- Installs atomic command groups with SHA-256 provenance verification.
- Learned classifier patches persist at `~/.config/agy-triad-dispatch/classifier-patches.json`.

## Packaged Skills

The plugin registers 5 skills under `skills/`:
- `triad-claude-dispatch`: Authorized bounded Claude calls and formal Claude review legs.
- `triad-codex-dispatch`: Authorized bounded Codex calls and formal Codex review legs.
- `triad-antigravity-dispatch`: Authorized Antigravity calls and formal Google review legs.
- `triad-gemini-dispatch`: Standalone Gemini CLI compatibility and Enterprise OAuth fallback.
- `triad-cross-family-review`: Multi-family review orchestrator coordinating Claude, Google, and Codex.

## Verification

Run all test suites offline:

```bash
python3 -m unittest discover -s tests -v
```
