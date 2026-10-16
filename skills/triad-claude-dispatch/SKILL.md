---
name: triad-claude-dispatch
description: 격리된 작업에 대해 승인된 Claude 패밀리 단일 답변을 구하거나 교차 패밀리 리뷰 스킬에서 Claude 레그를 실행할 때 사용합니다.
---

# Claude Dispatch

Use the packaged `bin/claude_wrapper.py`; do not call provider internals or
modify Claude authentication or user configuration. Ordinary calls leave Claude
permission selection native. The fully bound formal `LegVerdict` route adds
native per-call Plan Mode without changing persistent user configuration.
Explicitly requested `--web` preapproves native `WebSearch`/`WebFetch`; review calls
must also match the [bound round condition](../triad-cross-family-review/references/review-web.md).
The fully bound formal route rejects any model other than `opus`, any effort
other than `xhigh`, any timeout other than `1200`, and every `--fallback-model`
before provider resolution.

## Ordinary bounded dispatch

1. Confirm the exact objective, working directory, and authorized external
   data boundary.
2. Put long prompts in a leader-owned UTF-8 file outside reviewed evidence.
3. Invoke the wrapper with `--prompt-file`, `--cwd`, and the requested
   model/effort. Add `--pydantic module:Class` only when the caller supplies a
   real structured-output contract.
4. Treat nonzero exit, malformed result, refusal, timeout, or capacity failure
   as a failed dispatch. Do not reinterpret it as task evidence.

## Cross-family review leg

Use `triad-cross-family-review` and read its
`references/leg-contracts.md`. The formal Claude route is:

```bash
python3 bin/claude_wrapper.py \
  --prompt-file "$prompt_file" \
  --cwd "$review_root" \
  --sandbox read-only \
  --model opus \
  --effort xhigh \
  --timeout 1200 \
  --pydantic verdict_v2:LegVerdict \
  --expected-review-id "$review_id" \
  --expected-family claude \
  --expected-content-digest "$review_digest" \
  --expected-leg-name claude \
  --expected-attempt 1 \
  --expected-route null
```

Claude reviews only; it does not edit or execute candidate code. One validated
result belongs to one review ID and content digest. The original carrier output
(`claude-json`) is admitted directly through `triad_dispatch_lib.output.admit_output`
to enforce strict schema adherence and reject nested duplicate JSON keys fail-closed.

## Result handling

Return the wrapper's terminal stdout to the leader. Keep run logs and result
files outside immutable reviewed evidence. Provider findings never authorize a
code or design change; the leader reproduces and classifies them.

Each wrapper invocation runs inference once; capacity or schema failure does not
automatically replay the CLI. Diagnose the returned failure before any explicit
later invocation. Vendor-internal retries remain vendor-owned.

For terminal `unknown` or `extraction-error`, follow the repair protocol
with the last emitted `run-log:` path as opaque data. Its fresh native child
proposes; the owner applies and verifies against that stored record without
another vendor call. A timeout is surfaced without phrase repair; `oauth-env`
requires user authentication recovery. A repaired classifier does not make the
failed review valid.
