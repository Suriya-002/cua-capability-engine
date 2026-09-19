# Design write-up — CUA Capability Engine

The through-line: **the model discovers once; the artifact is the reusable capability; deterministic replay is how an
agent invokes it.** Everything below is built around keeping those three things separate.

## 1. Architecture

**Shape.** One Python process hosts four things behind clean seams: a `Surface` (perceive/act on an app), a
`DiscoveryAgent` (LLM loop that drives a Surface and records what it did), a `Capability` artifact (the recorded
contract), and a `ReplayEngine` (executes an artifact with no model, gated by a `Policy` and guarded by a
`SessionController` for human handoff). A FastAPI layer exposes the catalog, `invoke`, and a minimal operator inbox.
The target app is a local, deliberately hostile mock (framesets, table layouts, no ids, cookie sessions, fault
injection). Single process, single replica by design (ADR-0004): the live browser *is* the state, and a queue/worker
split buys nothing until there is a browser broker.

**Key decisions and trade-offs.**

- *Pixels first for discovery, structure opportunistically for replay* (ADR-0001). Discovery uses Anthropic's GA
  `computer_toolset_20260801`: screenshots in, coordinates out. That makes the loop identical on a clean web app, a
  frameset, or a desktop app. At record time we *probe* whatever structure the surface can expose under the clicked
  point (DOM today, UIA on desktop) and store a **ranked list of locator candidates**; replay resolves them in order
  and falls back to coordinates last. Cost: discovery is slower and pricier than the DOM-based browser toolset on
  clean apps. Benefit: one agent loop for every surface, and replay that is robust wherever structure exists.
- *Model.* Discovery ran on `claude-fable-5` with adaptive thinking: 3 turns, 3 API calls, 11,395 input / 531 output
  tokens, 15.2 s, roughly $0.14 at list price. The model batched login + search + read into three turns. Sonnet 5
  would be the production default at ~5× lower cost; the artifact and replay are identical either way, because no
  model is involved after discovery.
- *Local hostile mock instead of a public site* (ADR-0003). Public demos cannot reproduce session expiry, permission
  denial, interstitials, or transient 500s on demand, and the brief grades exactly those. Every fault is one
  query parameter away, and the "once per session" faults (expiry, notice, 500) behave like their real
  counterparts rather than firing on every request.
- *Discovery is token-gated; replay is the public path.* Replay never spends tokens, so it is safe to expose; the
  LLM path costs money and has more attack surface, so it runs behind a token with a daily cap, or in CI.

## 2. Artifact schema

A `Capability` (Pydantic v2, JSON on disk, JSON Schema exported) is a **contract plus a procedure plus runtime
conditions plus governance**:

```
lookup_member_balance@1.0.0  [approved, risk=safe]
  goal:     Look up member {member_id} and read their savings balance
  inputs:   member_id: string   (pattern \d{5}, sensitivity=pii)
  secrets:  username, password  (from env at replay; never stored)
  outputs:  savings_balance: decimal  (transform=currency, sensitivity=pii)
  outcomes: MEMBER_NOT_FOUND, PERMISSION_DENIED
  steps:    6   interrupts: 3   success: url ~ /bank/member/ AND text "Current Balance"
```

- **Steps** carry a `Locator` with ranked candidates (`role_name 0.95 → label_relative 0.9 → [name=] 0.85 →
  css_structural 0.5 → coords 0.2`), a value that is either a literal or `${inputs.x}` / `${secrets.x}`, and a
  **postcondition** derived from the state delta the action caused — in a frameset that means the URL that newly
  appeared in *any* frame, canonicalised (`/bank/member/(?P<member_id>[^/?&]+)`). Failure is attributed to the step
  that caused it, not five steps later.
- **Outcomes are not failures.** `MEMBER_NOT_FOUND` is a legitimate answer the caller needs; the engine returns it as
  `business_outcome`, never as a crash.
- **Interrupts** are recoverable conditions that can appear at any point, each with a detector and a handler
  (`dismiss`, `retry`, `relogin`, `escalate`).
- **Secrets are declared, never valued.** The recorder replaces typed credentials with `${secrets.username}`;
  referencing an undeclared secret fails validation. Inputs tagged `pii` are masked everywhere in evidence.
- **Governance:** semver + `schema_version`, `approval` (draft → needs_review → approved; unattended replay requires
  approved), `stability` (runs/successes), `provenance`, `idempotent` (irreversible capabilities must be
  non-idempotent, which forces an idempotency key at invocation), and `tenant_overrides` (§4).

Rejected: storing the model transcript (unreviewable, leaks PII); free-form step lists (no contract for a calling
agent); CSS-only locators (brittle on legacy markup). Three recorder bugs were caught by the recorder's unit test
before any browser run — the schema is where correctness is cheapest to enforce.

## 3. Determinism & error handling

**Determinism.** Replay never calls a model (`llm_calls` is asserted 0). Waits are state-based (poll for the
checkpoint, abort early if an outcome or interrupt condition becomes visible); there are no sleeps. Each step:
snapshot → interrupt detectors → outcome detectors → precondition → resolve locator cascade → policy gate → act →
postcondition. Ten consecutive replays of the recorded artifact succeeded (stability 10/10, ~4 s each).

**Taxonomy, as returned to the caller.**

| Kind | Meaning | Evidence |
|---|---|---|
| `success` | checkpoint held; typed outputs returned | `replay-…103c62`, 3.9 s |
| `business_outcome` | expected result the caller must handle | `replay-…0efca1` → `MEMBER_NOT_FOUND`, 3.7 s |
| recoverable (inside a success) | handled by the engine, listed in `recoveries` | `replay-…aa2bd3` interstitial dismissed; `replay-…392b82` session expired → re-login, flow restarted once |
| `failure` | step, expected, observed, category, evidence paths | fault-injection unit tests: `locator_not_found`, `checkpoint_failed`, `policy`, `app_error` |
| `escalated` | paused for a human; operator aborted or timed out | operator **Abort** path |

Interrupts are checked *before* each step and *again when a postcondition fails*, because an interstitial or expiry
typically appears as the result of the click you just made. Session expiry restarts the flow from step 1 (bounded to
once) — the artifact's own login steps are the recovery, so nothing app-specific lives in the engine.

**Drift (secondary).** A non-primary locator candidate winning, or the entry-page fingerprint changing, produces a
`drift_proposal` and would move the artifact to `needs_review`. No silent self-healing: a human approves the
re-ranking.

## 4. Heterogeneity & multi-tenant

**Surface seam.** `Surface` is a Protocol with `observe / act / probe / resolve / read_text / all_urls`. The
`Capability` never references Playwright. `LocatorStrategy` already carries what a desktop surface needs
(`aria_path`, `image_anchor`); `DesktopSurface` is a typed stub whose docstring maps each method to UI Automation
(`ElementFromPoint` → candidates: AutomationId, Name+ControlType, tree path, template-matched image anchor, coords).
The engine is provably surface-agnostic: its control-flow tests run against a scripted fake surface.

**Multi-tenant.** Hundreds of institutions on the same vendor product differ in labels, branding, and the odd extra
field — not in flow. So an artifact is a **base per vendor product plus a thin per-tenant overlay**
(`tenant_overrides[tenant] = {entry_url?, locators{step→Locator}, values{step→value}, extra_steps[]}`), applied at
replay time (`apply_tenant`). The mock ships a second tenant (`?tenant=b`: "Member Number", "Find Member", extra
Branch Code field) as the stand-in; the base artifact's `label_relative` and `role_name` candidates are exactly the
ones an overlay would swap. Drift is detected two ways: the entry-page `version_fingerprint` (title + field names +
frame names) is compared at replay start, and per-step drift proposals accumulate per tenant, which would roll up
into a vendor-level review queue in production.

## 5. Escalation & handoff

**Stuck** = exhausted locator cascade, unresolved interrupt, max steps, or an irreversible step without approval.

**Control-transfer model** (ADR-0006): `AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION`, exactly one controller at
a time, every transition an evidence event. The engine raises an `InterventionRequest` (capability, step, reason,
screenshot, current URL, whether the browser is headed) and waits — up to 10 minutes — on the **same browser**. The
human takes control from the inbox, acts in that browser (locally a headed window; in the container the same
window over noVNC), and hands back. While the human holds the session, a page script queues every click, change and
submit in every frame (persisted in `sessionStorage` so a submit that navigates is not lost); the engine drains the
queue and records them as `human_step` events.

**Hand-back** re-verifies before continuing, in order: the step's postcondition holds → continue with the next step;
the step's target is resolvable → rerun just that step; otherwise → navigate to the entry page and re-run the flow
once. The last rule is what makes the demo robust: as long as the operator cleared the blocking condition, it does
not matter which screen they left the browser on. Evidence `replay-…7ada1c`: 500 on the member screen → escalated at
step 6 → human took control, searched (four `human_step`s, the typed member ID masked) → handed back → engine
resumed, extracted, succeeded, `llm_calls: 0`.

**Mocked, deliberately:** the operator console is a single HTML inbox with Take control / Hand back / Abort and a
background-polled list. Real product: auth, assignment, a redacted live view. The mechanism underneath — the state
machine, request payload, recording, and re-verification — is real and tested.

## 6. Safety

- **Allowlist** (`policies/default.yaml`): hosts, allowed/denied path patterns (`/bank/admin`, `/bank/wire` denied),
  allowed action types. Enforced before every action in discovery *and* replay; a blocked action during discovery is
  reported back to the model as a tool error so it must choose differently.
- **Risk classes** `safe | risky | irreversible` from regex rules on the control's visible text plus the artifact's
  own `risk_class`. Unattended replay may perform up to `risky`; an irreversible step requires an **approved**
  artifact, an explicit `approved_irreversible=True` from the caller, and an **idempotency key** (replaying with the
  same key returns `ALREADY_APPLIED` instead of acting twice). Otherwise the step escalates to a human.
- **Redaction before persistence.** Secrets (env-only), PII-tagged inputs, and pattern matches (SSN, card, email,
  bearer tokens) are masked before evidence is hashed, so the chain never contains them even transiently. Typed
  text is never logged. `cua evidence leak-check` runs in CI with the real secret values; the committed evidence is
  clean.
- **Tamper-evident evidence.** Every event carries `sha256(prev_hash + event)`; `cua evidence verify` recomputes
  the chain.
- **Prompt injection.** The mock's search page carries a hidden instruction to wire funds. A pixel-based agent
  cannot see it (a property of the pixels-first decision), and `/bank/wire` is on the denied-path list regardless —
  two independent layers.
- **Limits, stated honestly.** Text-based risk rules misclassify a button labelled "OK" that moves money — the
  artifact-level `risk_class` set at review time is the backstop. The operator inbox has no authentication.
  Screenshots may contain on-screen PII and are kept only in evidence the operator controls.

## 7. Cuts

**Left out on purpose:** the desktop surface (typed seam only); a hybrid discovery using the browser toolset's
`ref_N` handles alongside pixels; a multi-replica session broker (Redis-backed control state + CDP browser pool);
a real operator console; bounded single-step LLM recovery on replay failure; screenshot PII blurring; a second,
irreversible capability recorded end-to-end (the idempotency contract is implemented and tested at the engine level
but not demonstrated with a discovered artifact).

**Next, in order:** (1) hybrid discovery — pixels for perception, `ref_N` handles as an additional locator source
where a DOM exists; (2) externalised session state and a browser pool so `invoke` can be queued and attended runs
can outlive a replica; (3) a vendor-level drift review queue fed by `drift_proposals`; (4) assisted fallback:
one policy-checked LLM step on replay failure, recorded as evidence and never open-ended.
