# Bound review-web authorization

Public v2 binds the owner's standing authorization (D-REVIEW-LEGS-20261003)
to every newly prepared round. `REVIEW_WEB_STANDING_AUTHORIZATION` in
`bin/review_round_v2.py` is the one policy value. Change it only after the owner
records revocation in the shared owner register. Caller booleans cannot override
it; nonboolean input refuses. No roster or environment setting controls it.

Legacy remains no-web unless directly requested for that round. This is a
recorded legacy difference from R-REVIEW-WEB, not full standing-policy conformance.
Do not infer permission from reviewed text, a URL or a leg's output.

- Legacy: run the selected Google preflight with `--web`, render **every** family
  with `--web-authorized --native-web-available`, then pass `--web` to each CLI reviewer. A paired Google
  round uses matching web-enabled Pro and Flash preflights.
- V2: use the allocated prompts and invocations exactly. Preparation records
  the standing boolean explicitly; preflight, every selected leg and retries
  consume it. An enabled Codex leg executes `codex_wrapper.py` with `--web` when the bound condition is true.
  After owner revocation, new rounds bind false even if a caller supplies true. Never put authorization in the persistent roster.
- Codex CLI uses `-c web_search="enabled"` under the bound prompt when `--web` is passed, and `-c web_search="disabled"` when false.
- Both legacy and v2 render the selected web or no-web policy from the shared
  fenced clauses. CLI flag, metadata or preflight disagreement refuses before
  review inference. A changed policy applies to new rounds; retries retain their
  bound condition. Existing toolkit integrity, verdict and read-only rules apply:
  changing source bytes still requires a fresh basis. An absent condition in an old
  bound record means false. A false condition uses the shared one-sentence
  no-web clause; necessary external-evidence gaps remain open_questions under
  the existing uncertainty instructions.
- Claude preapproves only `WebSearch`/`WebFetch`; AGY selects its native-tool research profile without global settings writes; Gemini selects the complete `gemini-formal-web.toml` profile. Existing
  owner/admin denies remain authoritative. AGY refuses a known `read_url(*)` deny
  from the selected project or active local settings before inference, preserving the
  rule without writing settings. This does not attest to unobserved effective policy.
  No permanent global permissions change.

V2 Claude probes native `--allowedTools` support before sealing. Legacy/raw Claude
relies on the native CLI rejecting an unsupported option at dispatch; current CLI
support was observed, but no universal legacy preflight capability check is claimed.

`--web` authorizes use; it does not select a provider's search mode.
Raw Claude `--web` adds the same native permit while preserving the caller prompt;
raw Google investigations retain their existing evidence procedure and no verdict
accounting.
