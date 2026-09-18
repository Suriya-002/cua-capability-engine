# ADR-0005: Allowlist + risk classes + redaction + gated discovery
Date: 2026-09-18 · Status: accepted

## Decision
- `policies/*.yaml`: allowed hosts/paths, denied paths, allowed action types, regex risk rules.
- Risk classes `safe | risky | irreversible`. Unattended replay may perform ≤ `risky`; `irreversible` requires
  (a) an **approved** artifact, (b) `approved_irreversible=True` from the caller, (c) an **idempotency key**.
  Otherwise the step escalates to a human rather than failing.
- Redaction is applied before hashing evidence, so the chain never contains a secret even transiently.
- The public deployment exposes **replay only**; discovery (LLM, costs money, more attack surface) is token-gated
  with a daily cap or runs in CI.

## Limits (stated honestly)
Regex risk rules classify by visible text; a button labelled "OK" that wires money is misclassified as safe. The
mitigation is the artifact-level `risk_class` set at review time, plus the denied-path list.
