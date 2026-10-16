# agy-triad-dispatch — triad-dispatch-lib dev4 Update (Plan Amendment)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bundled `triad-dispatch-lib 0.1.0.dev3` with the published `0.1.0.dev4`, keep all host behavior unchanged, then finish the branch with a whole-branch review.

**Architecture:** This amends [2026-10-15-agy-triad-dispatch.md](2026-10-15-agy-triad-dispatch.md) (Tasks 1–6, implemented). It mirrors host B's own dev4 update (`triad-codex-dispatch` 0.2.562, plan `docs/superpowers/plans/2026-10-17-library-dev4-update.md` in that repo): a verified package refresh plus the new receipt bundle/module in the adoption inventory. New dev4 APIs are available in the bundle but adopting them into host orchestration is out of scope here (see Follow-ups).

**Tech Stack:** Python 3.12+, `jsonschema>=4.26,<5`, `referencing>=0.37,<1`, pytest, `gh` (read-only release download).

**Spec:** triad-dispatch-lib `v0.1.0.dev4` — `docs/installing.md`, `docs/host-adoption.md`, `spec/reference/library-api.md` at commit `087af94a7078de0ae9701fb057840b5492f51f66`.

## What changed upstream (dev3 → dev4), verified

- Public exports 27 → 31, none removed: `ReceiptError`, `validate_transport_receipt(doc) -> None`, `merge_roster(defaults, overrides=None) -> dict`, `validate_resolved(roster) -> None`.
- `render_review_clauses(..., with_manifest=False)`: default return unchanged (`str`); `True` returns `(text, manifest)`.
- `AdmissionError` gains `.field/.path/.missing`; `RosterError` gains `.path/.missing/.document` (additive).
- `output.py`, `agreement.py`, `dispatch.py` byte-identical; all 8 existing contract/prompt payloads byte-identical; bundle IDs for verdict/roster/prompt unchanged.
- New bundle: `receipt` (`data/receipt-bundle/`, contract ID `26bf981f9fe1b3798508908fe648806a33db365d308c8424a11d2ed06c96d9e8`, payload `contracts/receipt-fields.json` sha256 `4108a1501bc1c5077c30fe5cc9e030ae4fb90c7b09c5b1137dbfdaaeec4214c4`) and module `receipt.py`.
- Prompt profiles still accept only hosts `A`/`B`. The existing `host="B"` decision (all foreign legs are CLI legs, as on host B) remains valid; no upstream report is needed.

## Global Constraints

- Library pin: version `0.1.0.dev4`, tag `v0.1.0.dev4`, source commit `087af94a7078de0ae9701fb057840b5492f51f66`, wheel `triad_dispatch_lib-0.1.0.dev4-py3-none-any.whl` sha256 `c5bfdf10c4ce81f431ec7f77106e4d77b1556ac9c7b6b01bdb6616f988e8b2cc` (same as host B 0.2.562).
- Obtain artifacts only from `https://github.com/codefoundry-io/triad-dispatch-lib/releases/download/v0.1.0.dev4/` (`triad-release.json` first; verify wheel byte size and sha256 against it before unpacking).
- Never hand-edit `bin/triad_dispatch_lib/**`; it is generated from the verified wheel.
- Host behavior unchanged: wrappers, adapters, round lifecycle, `host="B"` prompt profile, model/effort defaults.
- Never write `~/.gemini/antigravity-cli/settings.json`; no API keys (R-NOCOST/R-AUTH).
- Tests run on macOS with resolved temp paths (`Path(tmp).resolve()`); the suite must stay green (baseline: 19 passed at `9ba08a3`).

## Review Focus

1. Stale dev3 bundle files left behind after replacement (e.g. old manifests) → `verify_library` must refuse unexpected files; test it.
2. Missing or tampered `receipt.py` / `data/receipt-bundle/*` → adoption verification fails closed; test both.
3. Installed copy in a neutral directory (bootstrap path) still verifies dev4 → covered by `test_neutral_directory_installed_copy_verifies_library_and_contracts`.
4. Private import `triad_dispatch_lib.json_input.json_member_span` in `bin/antigravity_wrapper.py` (C23 bridge) still resolves under dev4 → keep a test that exercises the C23 bridge (`test_agy_c23_bridge_extracts_original_span_and_preserves_duplicate_key_rejection`).
5. Leftover host-B "native Codex spawn" path in `bin/review_adapters_v2.py` (`native-spawn-tool`) is unreachable on an agy host → flag in final review; do not change in this amendment.

---

### Task 7: Bundle published dev4 and extend the adoption inventory

**Files:**
- Replace (generated): `bin/triad_dispatch_lib/**`
- Modify: `bin/library_adoption.py` (`COMMIT`, `VERSION`, `MANIFESTS`, `MODULES`, wheel hash constant if present)
- Modify: `contracts/library-adoption.json`
- Modify: `vendor/triad-dispatch-lib.LICENSE` (only if the dev4 wheel's LICENSE differs)
- Modify: `scripts/verify_distribution.py` (receipt inventory, if it enumerates bundle files)
- Test: `tests/test_library_adoption_v2.py`

**Interfaces:**
- Consumes: `library_adoption.verify_library(root: Path)` (existing).
- Produces: `MANIFESTS = {"receipt": "data/receipt-bundle/source-manifest.json", "verdict": "data/source-manifest.json", "roster": "data/roster-bundle/source-manifest.json", "prompt": "data/prompt-bundle/source-manifest.json"}`; `MODULES` = existing set ∪ `{"receipt"}`; `contracts/library-adoption.json` with `version "0.1.0.dev4"`, `source_commit "087af94a7078de0ae9701fb057840b5492f51f66"`, `wheel_sha256 "c5bfdf10c4ce81f431ec7f77106e4d77b1556ac9c7b6b01bdb6616f988e8b2cc"`, `bundle_ids` adding `"receipt": "26bf981f9fe1b3798508908fe648806a33db365d308c8424a11d2ed06c96d9e8"`, and `sha256` entries for every bundled file.

- [ ] **Step 1: Write failing tests** in `tests/test_library_adoption_v2.py`:
  - `test_adoption_pins_dev4`: `json.loads(contracts/library-adoption.json)["version"] == "0.1.0.dev4"`, `source_commit` and `wheel_sha256` equal the Global Constraints values, `bundle_ids["receipt"] == "26bf981f…d9e8"`.
  - `test_receipt_module_and_bundle_required`: in a copied root (resolved tmp), deleting `bin/triad_dispatch_lib/receipt.py` makes `verify_library` raise; restoring it and appending one byte to `data/receipt-bundle/contracts/receipt-fields.json` makes it raise.
  - `test_stale_unexpected_file_refused`: adding `bin/triad_dispatch_lib/data/stale.json` makes `verify_library` raise.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_library_adoption_v2.py` → new tests FAIL (version is dev3, no receipt files).
- [ ] **Step 3: Fetch and verify artifacts** into `_runs/dev4-bundle/` (gitignored): `gh release download v0.1.0.dev4 -R codefoundry-io/triad-dispatch-lib -p triad-release.json -p '*.whl' -D _runs/dev4-bundle`; check `tag`, `source_commit`, wheel `size` and `sha256` from `triad-release.json`, and `shasum -a 256` == `c5bfdf10…2cc`. Stop and report if any value differs.
- [ ] **Step 4: Replace the bundle**: unzip the wheel to a fresh stage dir, then replace `bin/triad_dispatch_lib/` with the stage's `triad_dispatch_lib/` (remove old tree first so no dev3 file survives; drop `__pycache__`). Compare the result with host B's bundled copy at `/Users/chaniri/codex_workspace/workspace/triad-codex-dispatch-reliability/bin/triad_dispatch_lib/` (`diff -r` excluding `__pycache__` must be empty).
- [ ] **Step 5: Update** `bin/library_adoption.py` constants and regenerate `contracts/library-adoption.json` hashes from the new tree (same field set as today plus the receipt entries).
- [ ] **Step 6: Run** `python3 -m pytest -q tests` → all pass (19 existing + 3 new).
- [ ] **Step 7: Commit** `git commit -m "feat: bundle published triad-dispatch-lib 0.1.0.dev4 with receipt inventory"`.

### Task 8: Align version references and record decisions

**Files:**
- Modify: `README.md`, `README.ko.md` (lines naming dev3 / `2d1e62e7` / old wheel hash)
- Modify: `scripts/bootstrap.sh` (lines 14, 389: dev3 → dev4 wording)
- Modify: `docs/handoffs/triad-dispatch-lib-agy-host-proposal.md` (add a header: historical; dev4 still supports only hosts A/B; this host stays on `host="B"`; not filed — owner decision)
- Modify: `docs/superpowers/plans/2026-10-15-agy-triad-dispatch.md` (header note: library pin superseded by this amendment; Task 2/3 sample verdict fixtures are non-canonical — tests use the canonical v2 shape)
- Test: `tests/test_agy_plugin_and_bootstrap.py`

- [ ] **Step 1: Write failing test** `test_no_dev3_references_in_shipped_text`: none of `README.md`, `README.ko.md`, `scripts/bootstrap.sh`, `plugin.json`, `.antigravity-plugin/plugin.json`, `skills/**/SKILL.md` contains `0.1.0.dev3` or `2d1e62e7de6f33fa13a918a39579c6b5e74def00`.
- [ ] **Step 2: Run** it → FAIL (README and bootstrap mention dev3).
- [ ] **Step 3: Edit** the listed files (English and Korean READMEs say the same thing; link the dev4 release and `docs/host-adoption.md` at tag `v0.1.0.dev4`).
- [ ] **Step 4: Run** `python3 -m pytest -q tests` → all pass; `bash scripts/bootstrap.sh 2>&1 | grep -c 0.1.0.dev4` prints `1` (no-arg run prints usage and exits 2).
- [ ] **Step 5: Commit** `git commit -m "docs: point shipped guidance to triad-dispatch-lib dev4"`.

### Task 9: Whole-branch review and finish

- [ ] **Step 1:** Dispatch the final whole-branch review (`tier-max`, `Model: inherit`) over `main..feat/agy-triad-dispatch`, including the Review Focus list above and the fact that earlier task reviews approved tests that never passed.
- [ ] **Step 2:** Fix reproduced defects only (same reviewer re-reviews via `send_message`).
- [ ] **Step 3:** Use superpowers:finishing-a-development-branch (merge/PR/push decision is the owner's).

## Follow-ups (not in this amendment)

- Adopt dev4 APIs where the host hand-rolls equivalents: `validate_transport_receipt` in `bin/review_round_v2.py` (`_observations`), `validate_resolved` in `bin/review_roster.py` / `bin/review_adapters_v2.py`, package-root imports in `bin/verdict_v2.py`. Host B deferred the same; do it as its own reviewed change.
- Remove the private `json_member_span` dependency from the C23 bridge.
- Remove or justify the unreachable native-Codex path in `bin/review_adapters_v2.py`.
