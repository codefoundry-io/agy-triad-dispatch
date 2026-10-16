---
name: triad-gemini-dispatch
description: 격리된 작업에 대해 승인된 독립형 Gemini CLI 호환성 답변을 구하거나 agy 부재 시 Gemini Enterprise OAuth 리뷰 레그를 실행할 때 사용합니다.
---

# Gemini Dispatch

Use the packaged `bin/gemini_wrapper.py`; preserve native authentication,
workspace trust, and permissions.

## Ordinary bounded dispatch

Confirm objective, cwd, model, and external data boundary. Invoke the wrapper
with `--prompt-file` for long input, `--cwd`, optional `--model`, and optional
`--pydantic module:Class`. A nonzero exit or malformed result is a failed
dispatch, not substantive evidence.

For explicitly authorized web research, add `--web`; the wrapper appends the
shared page-fetch evidence procedure last with Gemini tool names. A search
summary or successful exit is not verified source content. Keep incomplete or
unfetched facts UNSURE. Use repeated `--add-dir` only for authorized additional
inputs; existing native permissions remain authoritative. These raw options
are refused on the formal review route.

## Formal ownership boundary

This skill is a standalone compatibility consult only and never leads formal cross-family review.
The `triad-cross-family-review` leader may select this packaged wrapper for an
owner-selected Gemini Enterprise OAuth route only before any family starts and only
when AGY is absent. Personal Google Sign-In requires AGY. If AGY was selected,
started, or later failed, never switch to Gemini.

When the cross-family review skill selects it, the exact formally bound route:

```bash
python3 bin/gemini_wrapper.py \
  --prompt-file "$prompt_file" \
  --cwd "$review_root" \
  --sandbox read-only \
  --model auto \
  --effort high \
  --timeout 600 \
  --pydantic verdict_v2:LegVerdict \
  --expected-review-id "$review_id" \
  --expected-family google \
  --expected-content-digest "$review_digest" \
  --expected-leg-name google \
  --expected-attempt 1 \
  --expected-route gemini
```

- Injects `-m auto` and requests native Plan Mode.
- Uses the mode-independent packaged read/search-only policy as its enforcement boundary.
- Effective approval mode is recorded as `unexposed`.
- Accepts only `--timeout 600` before provider resolution.
- Validates the original carrier output (`gemini-json`) directly through `triad_dispatch_lib.output.admit_output`.
- Records literal `runtime_identity: "unexposed"` in formal audit and never infers model identity from `stats.models`.

Gemini reviews only; it does not edit or execute candidate code.

## Result handling

Each wrapper invocation runs inference once; capacity or schema failure does not
automatically replay the CLI. Vendor-internal retries remain vendor-owned.

For terminal `unknown` or `extraction-error`, follow the repair protocol with
the last emitted `run-log:` path as opaque data. A timeout is surfaced without
phrase repair; `oauth-env` requires user authentication recovery.
