# CLAUDE.md — constraints for AI-assisted development on this repo

Read before generating code. These are not preferences; they are correctness constraints.

## Non-negotiables
- **Replay never calls an LLM.** `ReplayResult.llm_calls` must stay 0. If you think replay needs a model, you are
  designing the wrong feature — write a drift proposal instead.
- **Secrets/PII never land in `evidence/` or artifacts.** Everything written goes through `Redactor`. Inputs tagged
  `sensitivity: pii|secret` are masked. Credentials come from env only.
- **Every action passes `Policy.check()` first**, in discovery and replay alike.
- **The artifact schema (`src/cua/artifact/schema.py`) is the contract.** Change it deliberately, bump
  `schema_version`, update `docs/adr/0002`, and keep golden tests green.

## Anthropic API shape (do not regress to 2024-era code)
- Tools entry: `{"type": "computer_toolset_20260801"}`. No beta header. No `name`, `display_width_px`,
  `display_height_px`, `display_number`, `enable_zoom` — the toolset rejects them.
- Claude returns member `tool_use` blocks (`name` = `left_click`/`type`/`screenshot`/…, `toolset_name` = `"computer"`),
  several per turn. Run them **in order**, stop at the first failure, answer the rest with
  `"Not executed: an earlier computer action in this turn failed."` and `is_error: true`.
- Every `tool_result` for a member echoes `"toolset_name": "computer"`. Only `screenshot`/`zoom` return images.
- We resize screenshots ourselves and scale coordinates back (`PlaywrightSurface._scale`).
- Model: `claude-sonnet-5`, adaptive thinking, `display: summarized`.

## Stack
Python 3.11+, Playwright 1.63 (async), Pydantic v2, FastAPI, Typer, structlog. Ruff + mypy --strict are CI gates.

## Layout
`src/cua/{artifact,surface,agent,replay,policy,escalation,evidence,api}` — see README "Architecture".
`mockbank/` is the target app; it is intentionally hostile (framesets, tables, no ids). Keep it that way.

## Testing
`make test` (unit, no browser) must pass before every commit. `make test-live` needs `playwright install chromium`.
Add a fault-injection case to `tests/test_live_e2e.py` whenever you add an interrupt or outcome.
