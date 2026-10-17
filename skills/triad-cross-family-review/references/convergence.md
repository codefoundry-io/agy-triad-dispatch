# Finding convergence and round recovery

This file is the single normative contract for finding classification,
zero-start failure, partial-start failure, reruns, and owner decisions. Other
TRIAD instructions link here instead of restating these rules.

The round/retry procedure below is the legacy workflow. An explicit public v2
round uses v2 collection and recovery,
while retaining this file's finding-classification and terminal-observation rules.
For v2, its linked procedure additionally defines all-selected explicit approval,
compact current residuals, evidence-based reopening and stopping exhausted items
without approval. Its phase/count/outcome rules do not change the legacy gate below.

## Finding classification

The leader reproduces every finding against the exact reviewed bytes and the
canonical worktree. Reviewer labels are claims, not repair instructions.

| Reproduced state | Action |
|---|---|
| Defect or underspecification inside approved design | Apply the smallest bounded correction, run project verification, and start a fresh complete round |
| Contradicted by reviewed source | Record the refutation and preserve the source unchanged |
| Design/specification change | `OWNER_DECISION_REQUIRED` |
| Generalization, new capability, or scope expansion | `OWNER_DECISION_REQUIRED` |
| Contradictory verified findings | `CONFLICTED`; owner adjudication |
| Alternating advice without changed candidate/evidence | `OSCILLATING`; stop unchanged redispatch and ask the owner |

When owner input is required, report:

```text
Proposed delta: <what would change beyond the approved design>
Evidence: <reproduced file:line or runtime evidence>
Impact: <behavior, compatibility, cost, or scope consequence>
Decision needed: <one concrete owner choice>
```

Preserve the affected source while awaiting that decision. Continue unrelated
bounded work only when it cannot pre-decide or conflict with the owner's choice.

## Observation waits and terminal outcome ownership

<!-- TERMINAL_OUTCOME_CONTRACT_START -->
A poll, snapshot, or wait timeout is only a nonterminal wake-up boundary. It is
not evidence of leg failure and does not authorize the leader to interrupt a
still-running leg. Only the route-owning wrapper's full provider-process
deadline or another observable terminal result may classify a provider leg
failure. Leader elapsed time, poll count, and observation timeout never do. For
all provider legs, repeat observation waits until their terminal results arrive.
<!-- TERMINAL_OUTCOME_CONTRACT_END -->

## Failure before any provider starts

Before any cleanup in this protocol, finish all TRIAD-owned writers, export and verify the
round's evidence at its approved durable destination. Managed prepared roots use
packaged `export` then `cleanup`; unproven or unexported residue is preserved and
reported. This also applies to failed and stopped rounds.

A packet, selector, preflight, or launch-setup defect with zero started provider
legs invalidates the attempt. Record the exact non-secret failure evidence, use
supported cleanup for an exact managed root when one exists, correct the defect
or verify recovery from a transient vendor incident, and restart with a fresh
review ID.

After a second comparable zero-provider failure, stop allocating review IDs.
Compare the retained receipts and command context, identify and verify the shared
cause, then run one controlled setup-only probe. A changed command without cause
evidence is not recovery, and the probe is not a formal review round.

## Failure after a provider starts

<!-- PARTIAL_START_CONTRACT_START -->
The partial-start contract applies once any provider leg has started. A later leg
start or result failure that is missing, refused, malformed, route-mismatched, or
incomplete invalidates admission. Close further launch, record each required leg
that did not start, and wait for every already-started sibling to terminate.
Strictly validate every structurally available terminal result. Confirm every
started direct invocation and TRIAD-owned custody/result writer is terminal before
integrity verification. Vendor descendants and remote work remain vendor-owned;
do not require their termination or add a process-tree sweep.

Structurally valid results remain provisional until post-review integrity succeeds.
After a matching integrity check, preserve and reproduce every valid sibling
finding as advisory only while classifying the failure as a workflow, skill, tool,
instruction, operator, or vendor problem. A valid `NOT-SAFE` result is not a
failed leg and receives the same complete sibling collection. No sibling result
admits the failed round or supplies admission credit to a later round.

If integrity fails, use outputs only as untrusted leads and independently
reproduce any claim; diagnose and correct the integrity mismatch before a fresh
round. In either case, reproduce every collected finding before the next round,
correct and verify every reproduced in-scope defect, and correct the classified
leg failure or verify recovery from a transient vendor incident. A finding that
expands design still requires owner approval. Clean the exact round, prepare a
fresh ID, and rerun all three required families over one current basis.
<!-- PARTIAL_START_CONTRACT_END -->

## Convergence

A bounded fix, owner-approved design decision, corrected route, or material new
evidence creates a new review basis and requires a fresh complete round. There is
no fixed round cap. Stop on unanimous admissible `SAFE`, an owner decision,
`CONFLICTED`, `OSCILLATING`, or a required-leg failure that cannot be corrected
without new authority or external-state change.

The convergence unit is one complete focused source view reviewed once by each
required family. It is never divided into batches or satisfied by receipts from
different rounds.
