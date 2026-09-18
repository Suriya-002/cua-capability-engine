# ADR-0003: A local, intentionally hostile mock app as the target
Date: 2026-09-18 · Status: accepted

## Decision
`mockbank/` is a server-rendered FastAPI app with framesets, nested tables, no ids/ARIA/test-ids, cookie sessions,
Faker-generated synthetic members, fault injection (`?fault=`), a second tenant (`?tenant=b`), and a hidden
prompt-injection canary.

## Why
Public demo sites can't reproduce session expiry, permission denial, or interstitials on demand, and their terms
often forbid automation. The brief grades error handling; we need to *cause* errors deterministically.

## Consequences
+ Every error class in §3.3 is demonstrable in `/evidence/`.
+ Zero ToS/PII risk; the whole thing ships in one container.
− The reviewer must trust that the mock is representative; the README lists which legacy properties it reproduces.
