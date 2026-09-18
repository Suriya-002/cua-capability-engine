# ADR-0006: Explicit control-transfer state machine
Date: 2026-09-18 · Status: accepted

## Decision
`AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION`, single owner, every transition an evidence event. The
human operates the **same** browser (noVNC on the engine's Xvfb display). Human clicks are captured by an injected
page script as `human_step` events. On hand-back the engine re-verifies the current step's precondition; if false,
the request re-opens instead of the engine guessing.

## Why
"Who is in control" must be unambiguous for an audit and for safety (the engine must not click while a human is).
A fresh session would lose cookies/state and make the handoff meaningless.

## Mocked
The operator UI is a minimal HTML inbox + noVNC iframe. Real product: auth, assignment, chat, redaction of the
live view. The mechanism underneath is what the brief asks for and is real.
