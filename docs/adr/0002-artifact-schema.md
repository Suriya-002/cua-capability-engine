# ADR-0002: Capability artifact as a typed contract
Date: 2026-09-18 · Status: accepted

## Decision
`Capability` = contract (typed `inputs`, `outputs`, `success`, `outcomes`) + procedure (`steps` with ranked
`Locator`s and per-step `postcondition`) + runtime conditions (`interrupts`) + governance (`approval`, `stability`,
`provenance`, `tenant_overrides`, `idempotent`). Semver + `schema_version`. Pydantic v2, JSON on disk, JSON Schema
exported for reviewers and agents.

## Why these shapes
- **Outcomes are not failures.** `MEMBER_NOT_FOUND` is data the caller needs; conflating it with a crash is the
  mistake the brief calls out explicitly.
- **Postcondition per step** attributes failure to the step that caused it, not five steps later.
- **Ranked locators** make drift observable (non-primary win ⇒ proposal) rather than silent.
- **`idempotent=False` forces an idempotency key** for irreversible capabilities at the engine level.
- **Tenant overrides are a thin overlay**, not a copy: a second institution on the same vendor product needs a
  handful of locator/value swaps, and `version_fingerprint` tells us when the base no longer matches.

## Rejected
Storing the model transcript (not reviewable, leaks PII); free-form step lists (no contract for a calling agent);
CSS-only locators (brittle on legacy markup).
