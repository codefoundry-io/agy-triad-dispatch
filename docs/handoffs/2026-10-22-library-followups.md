# AGY library follow-ups: verify current boundaries first

Status: proposal only, 2026-10-22. Target: `codefoundry-io/agy-triad-dispatch`,
default branch `feat/agy-triad-dispatch`, inspected at
[`aa4b8db07881cf299ec174e2f23c310196975600`](https://github.com/codefoundry-io/agy-triad-dispatch/tree/aa4b8db07881cf299ec174e2f23c310196975600). This document proposes one bounded
verification unit; it does not implement a library issue or authorize installation.

## Confirmed baseline

The host already consumes `triad-dispatch-lib 0.1.0.dev4`. Its [adoption manifest](https://github.com/codefoundry-io/agy-triad-dispatch/blob/aa4b8db07881cf299ec174e2f23c310196975600/contracts/library-adoption.json)
pins source `087af94a7078de0ae9701fb057840b5492f51f66` and wheel SHA-256
`c5bfdf10c4ce81f431ec7f77106e4d77b1556ac9c7b6b01bdb6616f988e8b2cc`.
`bin/library_adoption.py` verifies the complete bundled inventory and bundle IDs;
`bin/validate_v2.py::_library` rejects ambient package overrides before use.
Existing host calls include strict JSON parsing, verdict validation, roster
resolution, prompt rendering, and original-carrier output admission. Adoption is
implemented; the next task should measure the remaining host boundaries.

`bin/review_roster.py::resolve_roster` calls the shared resolver, then applies
host filesystem/default/placeholder checks and returns display metadata. Its
exception wrapper currently emits `invalid roster: {error}`. The bundled
`RosterError` exposes a stable code and schema `path`, `missing`, and `document`;
it does not expose an instance path or offending array index. Schema paths must
not be presented as an exact failing leg in a roster containing repeated families.

`bin/review_round_v2.py::_observations` validates the nested transport shape with
the pinned receipt schema, then checks invocation binding, source, delivery,
terminal observations, and custody. The shared `validate_transport_receipt`
validates shape only. `collect` walks every enabled leg and returns INCOMPLETE,
BLOCKED, or AGREED. Shared `evaluate_agreement` likewise requires all selected
invocations, including informational entries and repeated families; the host also
owns findings/open-question and lifecycle semantics.

## Selected next unit: offline roster diagnostic evidence

Add focused fixtures to `tests/test_library_adoption_v2.py` and
`tests/test_v2_lifecycle_e2e.py`, using the existing local fake-provider approach.
First reuse existing coverage, then record current behavior for a valid roster
with two differently named legs from one family, a string `timeout_s` in a later
override entry, and a resolved Google entry selecting `agy` with only a `gemini` block.
Capture the host error and available structured library metadata. Require refusal
without interpolating arbitrary roster values; explicitly record when the current
API cannot identify an instance index. This produces a concrete multi-leg case
for upstream diagnostic discussion without fabricating new error fields.

Retain existing lifecycle coverage for all-selected agreement, failed integrity,
and receipt custody while adding the diagnostic cases. Receipt validation and
agreement reducers are outside this selected unit; the observations above do not
establish a defect or require a refactor. Provider calls must not begin after an
invalid roster, and existing refusal/host-fault exit behavior must remain unchanged.

Expected production delta: zero additions/deletions; novel core: zero. Expected
changes: up to two test files plus a short evidence record, approximately 40–100 test
lines. Any production refactor is a later separately reviewed unit. Preserve the
generated `bin/triad_dispatch_lib/**` tree and existing package pins throughout.

## Verification and completion evidence

From the selected checkout, record interpreter path/version and dependencies,
then run the existing README command `python3 -m unittest discover -s tests -v`.
Follow the selected checkout's execution policy. Retain the baseline commit,
fixture inputs, observed error fields,
per-case outcomes, command exit status, and final diff. These commands have not
been run for this proposal. Offline results establish adapter behavior only;
they do not establish live provider success or platform coverage.

## Later decisions and preserved boundaries

[Upstream #9](https://github.com/codefoundry-io/triad-dispatch-lib/issues/9) remains partially addressed/open: the concrete missing capability is
instance-level multi-leg location, beyond existing schema diagnostics. Consider
host consumption only after an approved upstream contract, tests, and published
pin are available. Do not implement #9 here. [Library PR #20](https://github.com/codefoundry-io/triad-dispatch-lib/pull/20) is an open wording-only
change at `13bee1422032984be42c7bb24ef74ac5001b5a7c`; it implies no runtime release.
Keep [#21](https://github.com/codefoundry-io/triad-dispatch-lib/issues/21) acceptance metadata as required data, with no inferred optional-leg or
exclusion rule. Keep [#17](https://github.com/codefoundry-io/triad-dispatch-lib/issues/17) legacy schema identifiers in intentional pin checks. Leave [#5](https://github.com/codefoundry-io/triad-dispatch-lib/issues/5)
additional extractions held. The [historical AGY host-profile proposal](triad-dispatch-lib-agy-host-proposal.md) was not
filed by owner decision: retain `host="B"` and do not revive `host="C"`/`"AGY"`.
