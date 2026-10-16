# Shared contract candidate

These four JSON Schema Draft 2020-12 payloads are byte-identical to
[shared commit fb7c962](https://github.com/codefoundry-io/triad-dispatch-spec/commit/fb7c962c83273c8b9c48fd11c482d1f5d50ef24d).
The scalar `review-kind.schema.json` selects plan or code purpose. The host applies
the omission default; JSON Schema's `default` annotation does not mutate input.
The receipt description now defines complete input availability rather than vendor consumption. Schema fields and constraints, canonical verdict and roster remain unchanged.
`source-manifest.json` is host-owned provenance: source repository, exact commit,
candidate status and per-file SHA-256. It is neither a revision tag nor a signature.
Changes to shared semantics must first be published and reviewed in the shared repo.

`bin/validate_v2.py` checks the bundle and validates original verdict JSON offline
with the maintained [python-jsonschema library](https://python-jsonschema.readthedocs.io/en/stable/validate/).
An explicit empty [reference registry](https://python-jsonschema.readthedocs.io/en/stable/referencing/)
prevents remote/file retrieval. The host also rejects original duplicate members
and compares all six expected invocation bindings. It returns the validated data
without converting legacy labels or filling absent evidence.

The explicit [v2 procedure](../skills/triad-cross-family-review/references/public-v2-review.md)
connects roster resolution, capability checks, the pinned shared clauses,
wrapper/native producers and per-entry collection. `verdict_v2.py` is a thin
Pydantic boundary backed by this same schema, not another field definition.
A Claude generation projection omits only `$schema`, `$id` and top-level
`allOf`; local admission still validates the complete authoritative schema.
Existing custom schemas and the copied standalone `bin/verdict_schema.py`
legacy gate retain their interfaces and do not consume converted v2 results.
Installing this candidate does not change a deployed `SPEC_REVISION`, confer
admission or establish live CLI enforcement. V1–V5 remain separate checks.
