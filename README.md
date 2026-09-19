# CUA Capability Engine

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the agent invokes it.

**Live demo:** https://cua-demo.greenground-c99e6748.eastus.azurecontainerapps.io — landing page with links to the mock bank (`/bank/login`), catalog (`/capabilities`),
and operator inbox + noVNC (`/operator`) (cold start ≈ 30–60 s; scale-to-zero on Azure Container Apps' free grant)

**Discovery:** 3 turns · 3 API calls · 11,395 / 531 tokens · 15 s · ≈ $0.14 &nbsp;|&nbsp; **Replay:** ~4 s · $0.00 · 0 LLM calls · 10/10 stable &nbsp;|&nbsp; **Escalation:** human took the live session, 4 recorded actions, run completed

An LLM drives a legacy back-office app once (screenshots + coordinates, no DOM assumed), the run is recorded as a
typed, versioned **capability artifact**, and that artifact replays **without a model**, returning a three-way
result (`success` / `business_outcome` / `failure`), escalating to a human who takes over the **same live browser**
when it can't proceed safely.

## Setup

```bash
git clone <repo> && cd cua-capability-engine
python -m venv .venv && . .venv/bin/activate          # PowerShell: .venv\Scripts\Activate.ps1
make install                                          # pip install -e ".[dev]", playwright chromium, pre-commit
cp .env.example .env                                  # set ANTHROPIC_API_KEY (discovery only); CUA_SECRET_* are the mock's login
make serve                                            # http://localhost:7860/bank/login (mock app), /operator, /capabilities
```

Windows/PowerShell: `.\.venv\Scripts\Activate.ps1`, then the same `pip`/`playwright` commands; `make` targets are one-liners
you can run directly (`ruff check . ; mypy ; pytest -m "not live" -q`). The CLI starts the local server itself when nothing
is listening on :7860, so no second window is needed.

Run **without live services**: everything except `cua discover` works with no API key. The mock bank is local.
Replay, the operator console, tests, and the API need no network.

## Demo path

```bash
# 1. One real LLM-driven discovery run → evidence/discovery-<id>/ + evidence/capabilities/lookup_member_balance@1.0.0.json
cua discover --goal "Look up member 10023 and read their savings balance" \
             --entry http://localhost:7860/bank/login --param member_id=10023 --name lookup_member_balance

# 2. Review + approve the artifact (unattended replay is gated on approval)
cua describe evidence/capabilities/lookup_member_balance@1.0.0.json
cua approve  evidence/capabilities/lookup_member_balance@1.0.0.json

# 3. Deterministic replay — no LLM
cua replay evidence/capabilities/lookup_member_balance@1.0.0.json --param member_id=10041
#    → {"kind":"success","outputs":{"savings_balance":"..."},"llm_calls":0,...}

# 4. Replay into an exceptional state (business outcome, not a crash)
cua replay evidence/capabilities/lookup_member_balance@1.0.0.json --param member_id=99999 --fault not_found
#    → {"kind":"business_outcome","outcome_code":"MEMBER_NOT_FOUND",...}

# 5. Recoverable interrupts handled in-engine (no human, no LLM)
cua replay ... --fault interstitial        # success, recoveries=[notice_interstitial]
cua replay ... --fault session_expired     # success, flow restarted once after re-login

# 6. Escalation: the member screen returns a 500, the engine pauses and routes an intervention request.
#    Open http://localhost:7860/operator -> Take control -> do the search yourself in the live browser
#    -> Hand back. The engine re-verifies, records your clicks as human_step evidence, and completes
#    (or, if the screen was left elsewhere, re-runs the flow once from the login).
cua replay ... --fault error_500 --attended

# 6b. Ten replays in a row -> stability score written into the artifact
cua stability evidence/capabilities/lookup_member_balance@1.0.0.json --n 10 --param member_id=10041

# 7. Evidence integrity and leak check
cua evidence verify evidence/
cua evidence leak-check evidence/   # secrets from .env are checked automatically
```

Via HTTP (what an AI agent would call): `GET /capabilities`, `GET /capabilities/lookup_member_balance`,
`POST /capabilities/lookup_member_balance/invoke {"inputs":{"member_id":"10023"}}`.

## Architecture

```
goal ─► agent/loop.py (Claude, computer_toolset_20260801) ─► surface/ (Playwright; pixels in, DOM probe at record)
                    │ every action gated by policy/ and logged to evidence/
                    ▼
        agent/recorder.py ─► artifact/schema.py (Capability: inputs, outputs, steps+ranked locators,
                                                 interrupts, outcomes, success, approval, tenant overrides)
                    ▼
        replay/engine.py (NO LLM) ─► result contract: success | business_outcome | failure
                    │ interrupts → recover; irreversible/unresolvable → escalation/session.py
                    ▼
        operator console + noVNC on the same browser ─► human acts ─► hand back ─► engine re-verifies, resumes
```

| Module | Responsibility |
|---|---|
| `artifact/` | schema (the contract), store, JSON Schema export |
| `surface/` | `Surface` protocol; `PlaywrightSurface` built; `DesktopSurface` designed stub |
| `agent/` | discovery loop (GA toolset shape, batch actions, pruning), prompts, recorder |
| `replay/` | deterministic engine, error taxonomy, idempotency, drift proposals, dry-run |
| `policy/` | allowlist + risk classes (`policies/default.yaml`), redaction |
| `escalation/` | control-transfer state machine, intervention requests |
| `evidence/` | hash-chained JSONL, screenshots, failure DOM/a11y dumps |
| `api/` | catalog, invoke, gated discovery, operator inbox |
| `mockbank/` | hostile legacy target with fault injection and tenant B |

## Evidence layout

`evidence/discovery-<id>/`, `evidence/replay-<id>/`: `events.jsonl` (hash chain), `shots/`, `failure/`, `summary.json`.
`evidence/capabilities/`: saved artifacts. See `REPORT.md` for the design write-up.

## Deploy (free)

`docker build -t cua .` runs everything (Xvfb + Chromium + noVNC + API) on port 7860. `deploy/azure/deploy.ps1`
puts it on Azure Container Apps within the monthly free grant (`min_replicas=0`); `.github/workflows/deploy.yml`
builds to ghcr.io and updates the app. Hugging Face Spaces (Docker, CPU Basic) works with the same image.

## Evidence in this repo

| Run | Shows |
|---|---|
| `evidence/discovery-…1b4d25` | the real LLM-driven run that produced the artifact |
| `evidence/replay-…103c62` | deterministic success, typed output, 0 LLM calls |
| `evidence/replay-…0efca1` | `MEMBER_NOT_FOUND` returned as a business outcome |
| `evidence/replay-…aa2bd3` | interstitial notice dismissed by the engine (`recoveries`) |
| `evidence/replay-…392b82` | session expiry → re-login → flow restarted once |
| `evidence/replay-…7ada1c` | transient 500 → escalation → human steps recorded → resumed → success |

Every `events.jsonl` is hash-chained (`cua evidence verify`) and passes the leak check with the real secret values.

## What is mocked, deliberately

Operator console (HTML inbox + noVNC — the handoff mechanism is real), desktop surface (typed stub with the UIA
mapping), multi-tenant storage (overlay in the artifact, not a service). See `REPORT.md → Cuts`.
