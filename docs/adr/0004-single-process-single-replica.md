# ADR-0004: Single process, single replica, in-memory session ownership
Date: 2026-09-18 · Status: accepted

## Decision
One process hosts the API, the live browser, and the `SessionController`. Deployed as Container Apps
`min_replicas=0, max_replicas=1`. No queue, no worker pool.

## Why
The live session *is* the state; sharing it across replicas would need a browser broker (CDP proxy) and an
externalised control-state machine. The brief explicitly does not reward building that plumbing.

## Next
Session state → Redis/Cosmos keyed by run id; browsers → a pool behind a CDP proxy; invoke → queued job with a
poll/callback contract. The `SessionController` API is already transport-agnostic so this is additive.
