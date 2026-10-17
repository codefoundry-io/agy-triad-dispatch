---
name: triad-codex-dispatch
description: 격리된 작업에 대해 승인된 OpenAI Codex CLI 답변을 구하거나 교차 패밀리 리뷰 스킬에서 Codex 레그를 실행할 때 사용합니다.
---

# Codex Dispatch

Use the packaged `bin/codex_wrapper.py`; do not invoke Codex CLI internals directly
or modify Codex authentication or user configuration.

## Ordinary bounded dispatch

Invoke `bin/codex_wrapper.py` with `--prompt` or `--prompt-file`, `--sandbox {read-only,workspace-write}`, `--cwd`, optional `--model`, `--effort` (or `--reasoning`), `--web` (or `--search`), and optional `--pydantic`:

```bash
python3 bin/codex_wrapper.py \
  --prompt-file "$prompt_file" \
  --cwd "$workdir" \
  --sandbox read-only \
  --model gpt-5.6-terra \
  --effort high \
  --timeout 900
```

- When structured output is requested, pass `--pydantic module:Class`. The wrapper derives an OpenAI-compatible strict schema (`additionalProperties: false` and all keys listed in `required`).
- For authorized web research, specify `--web` (or `--search`); the wrapper configures `-c web_search=live` and appends the shared investigation clauses. When web is not authorized, `-c web_search="disabled"` is enforced.
- Long prompts must be stored in a UTF-8 file and passed via `--prompt-file` to keep sensitive content off command argv.

## Cross-family review leg

Under the Antigravity host leader, the formal Codex review leg executes `bin/codex_wrapper.py` with full v2 review bindings:

```bash
python3 bin/codex_wrapper.py \
  --prompt-file "$prompt_file" \
  --cwd "$review_root" \
  --sandbox read-only \
  --model gpt-5.6-terra \
  --effort xhigh \
  --timeout 900 \
  --pydantic verdict_v2:LegVerdict \
  --expected-review-id "$review_id" \
  --expected-family codex \
  --expected-content-digest "$review_digest" \
  --expected-leg-name codex \
  --expected-attempt 1 \
  --expected-route null
```

On formal v2 bound calls, the wrapper strictly enforces:
- `--sandbox read-only`
- `--ignore-rules` and `-c approval_policy=never`
- Single-shot execution without automatic schema-repair retry
- Direct validation of the raw last-message output (`-o <last_msg_path>`) using `admit_carrier_output(carrier="native-final")` via `triad_dispatch_lib.output.admit_output`.

Codex reviews only; it does not edit or execute candidate code.

## Result handling

The wrapper reads the raw `-o <last_msg_path>` output, admits the verdict against all six binding criteria, and outputs the result JSON on stdout.
- Exit code `0`: Valid admitted `LegVerdict`.
- Exit code `66`: Schema validation or carrier admission failure (including duplicate JSON keys).
- Exit code `2`: Timeout.
- Exit code `65`: Authentication or environment refusal.

Keep run logs and result files outside immutable reviewed evidence. Provider findings never authorize a code or design change; the leader reproduces and classifies them.
