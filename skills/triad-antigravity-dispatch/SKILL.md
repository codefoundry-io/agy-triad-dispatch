---
name: triad-antigravity-dispatch
description: 격리된 작업에 대해 승인된 Antigravity (agy) Google 패밀리 답변을 구하거나 교차 패밀리 리뷰 스킬에서 Google 레그를 실행할 때 사용합니다.
---

# Antigravity Dispatch

Use the packaged `bin/antigravity_wrapper.py`.
All read-only calls require AGY >=1.2.1 and select an explicitly provisioned
native-tool profile with `excludeDefaultComponents: true`. Formal review selects
the no-web/research twin from its bound web value; raw read-only investigations
use the research twin, retaining their separate prompt authorization. Native
`--mode plan --sandbox read-only` remains; read-only calls never use headless autoapproval.
Permissive raw calls retain version-gated headless compatibility unless
`AGY_NO_HEADLESS_AUTOAPPROVE=1` opts out. Callers do not pass the flag.
No call writes, locks, restores or heals global AGY settings or legacy backup/
lease files. Requested web only reads existing settings to reject a known
`read_url(*)` deny or malformed existing JSON. Absence is not permission proof.
Old residue is preserved; no automatic recovery/deletion is performed.

On AGY 1.2.1 or newer, the formal route passes a review-bound native `--json-schema`
in plan mode and consumes the terminal `structured_output`. It admits the original
carrier output (`agy-stream-json`) via `triad_dispatch_lib.output.admit_output` to
enforce strict schema validation and reject duplicate JSON keys.

## C23 Plan-Mode Recovery

When AGY plan mode encounters a benign post-completion deny event (`denied_post_completion`
or `_agy_finish_resubmitted`) with `status == "ERROR"` and `vendor_exit_code == 0`,
where an allowed read was observed and no forbidden tool execution occurred:
1. The wrapper extracts the raw `structured_output` JSON text span from the terminal result line via `json_member_span`.
2. It validates the extracted span via `admit_carrier_output(carrier="native-final")` using `admit_output`.
3. If no allowed read occurred or forbidden tools were attempted, C23 recovery is refused fail-closed.

## Native tools in static review

The formal Google prompt authorizes only AGY native file-read/search tools for local inspection:
- `grep_search` with required `SearchPath` and `Query`.
- `list_dir`, `find_by_name`, and `view_file` with required `AbsolutePath`.
- For large files, specify explicit positive-integer `StartLine` and `EndLine` ranges.
- Never request `ContentOffset` or `IsSkillFile`.
- Command execution, shell, terminal, file-write/edit, notebook-execution, subagent, and browser tools are strictly prohibited.

## Formal Google leg

Use `triad-cross-family-review` and its `references/leg-contracts.md`. The formal wrapper arguments are:

```bash
python3 bin/antigravity_wrapper.py \
  --prompt-file "$prompt_file" \
  --cwd "$review_root" \
  --sandbox read-only \
  --model gemini-3.1-pro-high \
  --effort high \
  --timeout 600 \
  --pydantic verdict_v2:LegVerdict \
  --expected-review-id "$review_id" \
  --expected-family google \
  --expected-content-digest "$review_digest" \
  --expected-leg-name google \
  --expected-attempt 1 \
  --expected-route agy
```

The wrapper calls AGY print mode with native `--output-format stream-json`, enforces
a 600-second wrapper timeout (`--print-timeout 590s`), and strictly validates all
six v2 binding fields locally.

## Failure handling

A failure before provider submission stops the round with zero provider legs.
A failure after submission invalidates the Google leg and round. Do not switch
providers or authentication classes, drop `--sandbox read-only`, or claim
unmeasured permission guarantees from profile declarations.

For terminal `unknown` or `extraction-error`, follow the repair protocol
with the last emitted `run-log:` path as opaque data. Its fresh native child
proposes; the owner applies and verifies against that stored record without another
vendor call. A timeout is surfaced without phrase repair; `oauth-env` requires user
authentication recovery.
