---
name: triad-cross-family-review
description: 아키텍처, 호환성, 배포, 보안, 데이터 손실 위험 또는 머지 전 결정에 대해 Claude, Google (agy/gemini), Codex 3대 모델 패밀리의 독립적인 교차 검증 리뷰를 수행할 때 사용합니다.
---

# Triad Cross-Family Review

Explicit owner/project selection of **public v2** uses `bin/review_round.py`
(backed by `bin/review_round_v2.py`) for named rosters (`resolve-roster`),
producer/render/collector commands (`v2-create`, `v2-allocate`, `v2-record-cli`,
`v2-record-start-failure`, `v2-collect`), host evidence, and same-basis retry.
Every enabled entry participates, including informational entries; never
convert v2 results to legacy.
Public v2 binds the owner's standing [review-web authorization](references/review-web.md) to every new round. Legacy retains explicit per-round authorization.

The Antigravity host leader coordinates three independent provider families:
1. **Claude**: via `bin/claude_wrapper.py` (`opus`, `xhigh`, `timeout 1200`).
2. **Google**: via `bin/antigravity_wrapper.py` (preferred, `gemini-3.1-pro-high`, `timeout 600`) or `bin/gemini_wrapper.py` (Enterprise OAuth fallback when AGY is absent).
3. **Codex**: via `bin/codex_wrapper.py` (`gpt-5.6-terra`, `xhigh`, `timeout 900`).

All families review the same complete prepared directory. Batches, shards, and mixed-round
evidence are not supported.

## Choose the review basis

- **Prepared directory (default):** copy the exact approved members, add current `TASK.md`
  and `REVIEW.diff`, manifest the final bytes, and capture their SHA-256 digest.
  Keep selector receipts, prompts, logs, and results under the managed root.

Read [convergence](references/convergence.md) for all zero-start, partial-start, finding,
and rerun handling.

## Leader workflow

1. **Authorize and bound.** The current owner-supplied task or explicitly designated executable
   plan is the execution authority for the round. Record the objective, criteria, approved paths,
   exclusions, test-source rule, and Google authentication class. Exclude credentials, tokens,
   cookies, environment dumps, and unrelated data.

2. **Resolve toolkit.** Resolve the repository root and use only its packaged lifecycle
   and wrapper code (`bin/*_wrapper.py`, `bin/review_round.py`, `bin/review_round_v2.py`, `bin/library_adoption.py`).
   Verify `library_adoption.verify_library()` before running reviews.

3. **Create the basis.** Run `python3 bin/review_round.py prepare` with canonical source root, member-list,
   and fresh review ID. Add `TASK.md`, `REVIEW.diff`, optional `EVIDENCE.md`, then run `python3 bin/review_round.py manifest`.
   For public v2 rounds, use `python3 bin/review_round.py resolve-roster`, `v2-create`, `v2-allocate`,
   `v2-record-cli`, `v2-record-start-failure`, and `v2-collect` (backed by `bin/review_round_v2.py`).

4. **Capture integrity.** Run `python3 bin/review_round.py capture` against the prepared directory and record its SHA-256
   prepared digest. Keep all mutable round artifacts outside the guarded review basis.

5. **Freeze the Google route.** Select and record the Google route (`personal-google` -> `agy`,
   `gemini-enterprise` -> `agy` or `gemini`). The preflight receipt binds the review ID, route,
   executable, model, and effort into every family digest.

6. **Render from code.** Render the prompts using `review_prompts_v2.py` (or via `python3 bin/review_round.py v2-allocate`). The renderer binds
   all six v2 fields: `review_id`, `family`, `content_digest`, `leg_name`, `attempt`, `route`.

7. **Start independent legs.** Start Claude, Google, and Codex concurrently or sequentially
   before admitting any verdict. Reviewers inspect with native read/search capabilities only;
   they do not edit state or execute candidate code, tests, builds, hooks, or scripts.

8. **Collect and validate.** Collect outputs directly from original carriers:
   - Claude: `claude-json`
   - Google: `agy-stream-json` (or `gemini-json`)
   - Codex: `native-final`
   Validate every result with `triad_dispatch_lib.output.admit_output` using all six binding fields.
   Duplicate JSON keys inside carrier envelopes fail closed with exit 66.

9. **Verify before admission.** Wait for all started legs to terminate. Run `python3 bin/review_round.py verify` to confirm
   the prepared review directory was not modified during inspection (`ROUND_INTEGRITY_OK`).
   Admit evidence only when directory integrity matches and all required families return structurally
   valid results for the same content digest.

10. **Reproduce and converge.** The leader reproduces every finding against the canonical worktree.
    Apply the smallest verified correction inside the approved design, run project verification,
    and start a fresh complete round under a new review ID.

11. **Clean the exact round.** Run packaged `python3 bin/review_round.py export` to a durable location, followed by `python3 bin/review_round.py cleanup`.

## Gate Decision

- `SAFE`: Allows Minor findings, but zero Critical or Major findings and zero open questions.
- `NOT-SAFE`: Requires at least one Critical or Major finding or open question.
- Gate passes only when all required admitted results are unanimous `SAFE` for the same verified digest.
