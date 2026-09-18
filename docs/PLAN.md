# Build plan (time-boxed)

| Phase | Deliverable | Done when |
|---|---|---|
| 0 Scaffold | repo, CI green, schema drafted, ADR 1–3 | `make lint type test` green |
| 1 Mock app | `mockbank/` + faults + tenant B + canary | every fault reachable in a browser |
| 2 Surface | `PlaywrightSurface` observe/act/probe/resolve | scripted happy path via `Surface` only |
| 3 Discovery | first real run, evidence committed | `evidence/discovery-*/summary.json` shows success |
| 4 Artifact | recorder → artifact, validate/describe | artifact reviewable by a stranger |
| 5 Replay | engine + taxonomy + idempotency + drift + dry-run | ok / not-found / hard-failure evidence |
| 6 Safety | policy hardening, leak test, injection evidence | CI leak test green |
| 7 Handoff | escalation runs end-to-end via operator page + noVNC | GIF recorded |
| 8 Stretch | stability score, tenant B overlay, catalog invoke | optional |
| 9 Deploy | container → ACA/HF, evidence viewer | stranger can run a replay |
| 10 Write-up | README numbers, REPORT sections, fresh discovery run | submitted |
