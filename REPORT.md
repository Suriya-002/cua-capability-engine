# Design write-up

<!-- ~1–3 pages. Keep the seven headings verbatim; each section = decision + trade-off + what it cost. -->

## 1. Architecture
- Single process, single replica; live browser + control state in memory (ADR-0004). Why: the live session is
  the state; a queue/worker split buys nothing until there is a browser broker.
- Pixel-first discovery on `computer_toolset_20260801`; structure probed opportunistically at record time; replay
  is locator-first with coordinates as last resort (ADR-0001).
- Model: `claude-sonnet-5`, adaptive thinking (summarized). Trade-off vs Opus 5 / browser toolset: cost and
  click precision vs token overhead; numbers from `evidence/discovery-*/summary.json`: _fill in_.
- Boundaries: `Surface` (perceive/act) ⟂ `Capability` (recorded flow) ⟂ `ReplayEngine` (execution) ⟂ `Policy`
  (gate) ⟂ `SessionController` (who is in control). Each is independently testable without a browser.

## 2. Artifact schema
- Contract + procedure + runtime conditions + governance (ADR-0002). Paste the `describe()` output of the real
  artifact here and walk through one step's ranked locators.
- Why outcomes ≠ failures; why postconditions are per step; why `idempotent=False` is enforced by the engine.
- Reviewability: JSON on disk, JSON Schema exported, `approval` state gates unattended replay.

## 3. Determinism & error handling
- No model in replay (`llm_calls == 0` asserted). State-based waits only; checkpoint after every step.
- Taxonomy: **business outcome** (`MEMBER_NOT_FOUND`, `PERMISSION_DENIED`, `ALREADY_APPLIED`) vs **recoverable**
  (`notice_interstitial` → dismiss, `slow_load` → retry, `session_expired` → relogin) vs **hard failure**
  (`locator_not_found`, `checkpoint_failed`, `policy`, `timeout`, `app_error`) with step / expected / observed /
  evidence paths. Point at the three evidence runs that show each.
- Drift (secondary): fingerprint mismatch and non-primary locator wins become `drift_proposals`; artifact →
  `needs_review`. No silent self-healing.

## 4. Heterogeneity & multi-tenant
- Surface seam: `Surface.probe()/resolve()`; `LocatorStrategy` already carries desktop strategies
  (`image_anchor`, UIA AutomationId → `role_name`); `DesktopSurface` docstring gives the mapping.
- Multi-tenant: base artifact per vendor product + `tenant_overrides` overlay (entry URL, per-step locator/value,
  extra steps). Demonstrated: tenant B (`?tenant=b`) — _fill in whether the base replays with a 2-line overlay_.
- Drift detection: `version_fingerprint` (title + field names + frames) checked at replay start; stability score
  per artifact; per-tenant proposals would roll up into a vendor-level review queue.

## 5. Escalation & handoff
- Stuck = max steps / unresolved interrupt / exhausted locator cascade / irreversible step without approval.
- `AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION` (ADR-0006). Same browser via noVNC on the engine's
  display; human clicks recorded as `human_step`; precondition re-verified on hand-back, request re-opens if false.
- Mocked: the console UI. Real: the state machine, the request payload, the recording, the resume check.

## 6. Safety
- Allowlist (hosts, paths, action types) enforced in discovery **and** replay; risk classes; irreversible needs
  approval + explicit caller flag + idempotency key (ADR-0005).
- Redaction before hashing; leak test in CI with a canary; credentials env-only; hash-chained evidence.
- Prompt-injection canary in the mock app: cite the discovery evidence line where the agent ignored it, and note
  the allowlist would have blocked `/bank/wire` regardless.
- Limits: text-based risk rules; no per-operator authn on the mock console; screenshots may contain PII and are
  kept only in evidence you control.

## 7. Cuts
- Not built: desktop surface, browser-toolset hybrid, multi-replica session broker, real operator console,
  assisted LLM fallback on replay failure, screenshot PII blurring.
- Next, in order: (1) hybrid discovery using both toolsets, (2) Redis-backed session state + CDP browser pool,
  (3) bounded single-step assisted fallback recorded as evidence, (4) vendor-level drift review queue.
