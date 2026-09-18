"""`cua` command line.

cua discover --goal "..." --entry URL --param member_id=10023 --name lookup_member_balance
cua replay evidence/capabilities/lookup_member_balance@1.0.0.json --param member_id=10023
cua replay ... --fault not_found          (injects a fault via the entry URL for the demo)
cua validate PATH | cua describe PATH | cua schema
cua stability PATH --n 10 --param ...
cua evidence verify evidence/
cua approve PATH
cua serve
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from cua.api.localserver import ensure_server
from cua.artifact.schema import (
    AppRef,
    ApprovalState,
    Capability,
    Checkpoint,
    ParamSpec,
    ParamType,
    RiskClass,
    Sensitivity,
)
from cua.artifact.store import ArtifactStore
from cua.config import settings
from cua.evidence.writer import EvidenceWriter, verify_chain
from cua.logging import configure, get_logger, install_redactor
from cua.policy.engine import Policy
from cua.policy.redaction import Redactor

app = typer.Typer(no_args_is_help=True, add_completion=False)
evidence_app = typer.Typer(no_args_is_help=True)
app.add_typer(evidence_app, name="evidence")
log = get_logger("cli")


def _redactor() -> Redactor:
    r = Redactor(settings.redact_values)
    install_redactor(r)
    return r


def _params(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = v.strip()
    return out


def _with_fault(url: str, fault: str | None, tenant: str | None) -> str:
    q = []
    if fault:
        q.append(f"fault={fault}")
    if tenant:
        q.append(f"tenant={tenant}")
    return url + (("&" if "?" in url else "?") + "&".join(q) if q else "")


# ------------------------------------------------------------------------------- discover
async def run_discovery(
    goal: str,
    entry: str,
    name: str,
    params: dict[str, str],
    *,
    outputs: dict[str, str] | None = None,
) -> tuple[Path, Any]:
    from anthropic import AsyncAnthropic

    from cua.agent.loop import DiscoveryAgent
    from cua.agent.recorder import DEFAULT_INTERRUPTS, DEFAULT_OUTCOMES, Recorder, savings_locator
    from cua.surface.playwright_surface import PlaywrightSurface

    if not ensure_server(entry):
        raise typer.Exit(code=2)
    red = _redactor()
    secrets = settings.secrets
    red.register(*[v for k, v in params.items() if k == "member_id"])  # PII never lands in evidence
    ev = EvidenceWriter(settings.evidence_dir, "discovery", red)
    policy = Policy.load(settings.policy_path)
    surface = PlaywrightSurface(
        headless=settings.headless, viewport=(settings.viewport_width, settings.viewport_height)
    )
    client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    agent = DiscoveryAgent(
        client,
        surface,
        policy,
        ev,
        model=settings.model,
        effort=settings.effort,
        max_steps=settings.max_discovery_steps,
        timeout_s=settings.discovery_timeout_s,
    )
    sens = {k: ("pii" if k == "member_id" else "none") for k in params}
    try:
        outcome = await agent.run(goal, entry, params, sens, secrets)
        fp = outcome.entry_fingerprint
    finally:
        await surface.stop()

    if outcome.status != "success":
        ev.summary(status=outcome.status, summary=outcome.summary, outcome_code=outcome.outcome_code)
        typer.echo(f"discovery ended with status={outcome.status}: {outcome.summary}")
        return Path(ev.dir), outcome

    # Declared outputs -> extraction locators (label-anchored; never literal values).
    outputs = outputs or {"savings_balance": "decimal"}
    out_locs = {oname: savings_locator() for oname in outputs}
    rec = Recorder(
        cap_id=name,
        name=name.replace("_", " ").title(),
        version="1.0.0",
        app=AppRef(vendor="LegacyCU", product="Core Servicing", version_fingerprint=fp),
        run_id=ev.run_id,
        model=settings.model,
    )
    cap = rec.build(
        outcome,
        goal=goal,
        entry_url=entry,
        params=params,
        param_specs={
            k: ParamSpec(
                type=ParamType.STRING,
                description=k.replace("_", " "),
                pattern=r"\d{5}" if k == "member_id" else None,
                sensitivity=Sensitivity.PII if k == "member_id" else Sensitivity.NONE,
                example="10023",
            )
            for k in params
        },
        output_specs={
            k: (ParamType(v), f"{k.replace('_', ' ')} as shown on the member detail screen")
            for k, v in outputs.items()
        },
        output_locators=out_locs,
        secrets=secrets,
        interrupts=DEFAULT_INTERRUPTS,
        outcomes=DEFAULT_OUTCOMES,
        success=Checkpoint(text_contains="Current Balance", url_pattern=r"/bank/member/"),
    )
    path = ArtifactStore(settings.evidence_dir / "capabilities").save(cap)
    ev.summary(
        status="success",
        artifact=str(path),
        turns=outcome.turns,
        llm_calls=outcome.llm_calls,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        duration_ms=outcome.duration_ms,
    )
    return path, outcome


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural-language goal")],
    entry: Annotated[str, typer.Option(help="Entry URL of the target app")],
    name: Annotated[str, typer.Option(help="capability id, snake_case")],
    param: Annotated[list[str] | None, typer.Option(help="k=v, repeatable")] = None,
    fault: Annotated[str | None, typer.Option(help="mockbank fault to inject")] = None,
) -> None:
    """Run one real LLM-driven discovery and emit a capability artifact."""
    configure()
    path, outcome = asyncio.run(run_discovery(goal, _with_fault(entry, fault, None), name, _params(param)))
    typer.echo(
        f"status={outcome.status} turns={outcome.turns} llm_calls={outcome.llm_calls} "
        f"tokens={outcome.input_tokens}/{outcome.output_tokens} duration_ms={outcome.duration_ms}"
    )
    typer.echo(f"artifact: {path}")


# ------------------------------------------------------------------------------- replay
async def run_replay(
    cap: Capability,
    inputs: dict[str, str],
    *,
    fault: str | None,
    tenant: str | None,
    attended: bool,
    idempotency_key: str | None,
    approved: bool,
    dry_run: bool,
) -> Any:
    from cua.escalation.session import SessionController
    from cua.replay.engine import IdempotencyStore, ReplayEngine
    from cua.surface.playwright_surface import PlaywrightSurface

    if not ensure_server(cap.entry_url):
        raise typer.Exit(code=2)
    red = _redactor()
    ev = EvidenceWriter(settings.evidence_dir, "replay", red)
    policy = Policy.load(settings.policy_path)
    surface = PlaywrightSurface(
        headless=settings.headless and not attended,
        viewport=(settings.viewport_width, settings.viewport_height),
    )
    session = SessionController(ev, cap.ref) if attended else None
    if session:
        surface.install_human_recorder(lambda p: session.record_human_step(**p))
    if fault or tenant:
        cap = cap.model_copy(update={"entry_url": _with_fault(cap.entry_url, fault, tenant)})
    engine = ReplayEngine(
        surface,
        policy,
        ev,
        red,
        session=session,
        idempotency=IdempotencyStore(settings.idempotency_store),
        unattended=not attended,
        relogin=_relogin,
        secrets=settings.secrets,
    )
    return await engine.run(
        cap,
        inputs,
        tenant=tenant,
        idempotency_key=idempotency_key,
        approved_irreversible=approved,
        dry_run=dry_run,
    )


async def _relogin(surface: Any) -> None:
    """Sub-flow used by the RELOGIN interrupt handler. Credentials come from env, never the artifact."""
    from cua.surface.base import Action

    page = surface.page
    sec = settings.secrets
    await page.goto(settings.mockbank_url + "/login", wait_until="domcontentloaded")
    await page.fill("input[name=username]", sec.get("username", ""))
    await page.fill("input[name=password]", sec.get("password", ""))
    await surface.act(Action(name="key", text="Enter"))


@app.command()
def replay(
    artifact: Path,
    param: Annotated[list[str] | None, typer.Option()] = None,
    fault: Annotated[str | None, typer.Option()] = None,
    tenant: Annotated[str | None, typer.Option()] = None,
    attended: Annotated[bool, typer.Option(help="Allow escalation to a human (headed browser)")] = False,
    idempotency_key: Annotated[str | None, typer.Option()] = None,
    approve_irreversible: Annotated[bool, typer.Option()] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Resolve locators and check preconditions; do not act")
    ] = False,
) -> None:
    """Deterministically replay an artifact. No LLM."""
    configure()
    cap = ArtifactStore.load(artifact)
    res = asyncio.run(
        run_replay(
            cap,
            _params(param),
            fault=fault,
            tenant=tenant,
            attended=attended,
            idempotency_key=idempotency_key,
            approved=approve_irreversible,
            dry_run=dry_run,
        )
    )
    typer.echo(res.model_dump_json(indent=2))
    raise typer.Exit(code=0 if res.kind.value in {"success", "business_outcome"} else 1)


@app.command()
def stability(artifact: Path, n: int = 10, param: Annotated[list[str] | None, typer.Option()] = None) -> None:
    """Replay N times; write the flakiness score back into the artifact."""
    configure()
    cap = ArtifactStore.load(artifact)
    ok = 0
    for i in range(n):
        res = asyncio.run(
            run_replay(
                cap,
                _params(param),
                fault=None,
                tenant=None,
                attended=False,
                idempotency_key=None,
                approved=False,
                dry_run=False,
            )
        )
        ok += int(res.ok())
        typer.echo(f"run {i + 1}/{n}: {res.kind.value} {res.duration_ms}ms drift={len(res.drift_proposals)}")
    cap.stability.runs += n
    cap.stability.successes += ok
    cap.stability.last_run_at = datetime.now(UTC)
    ArtifactStore(artifact.parent).save(cap)
    typer.echo(f"stability: {ok}/{n} = {ok / n:.2f}")


# ------------------------------------------------------------------------------- artifact tools
@app.command()
def validate(artifact: Path) -> None:
    cap = ArtifactStore.load(artifact)
    typer.echo(f"OK {cap.ref}: {len(cap.steps)} steps, {len(cap.inputs)} inputs, {len(cap.outputs)} outputs")


@app.command()
def describe(artifact: Path) -> None:
    typer.echo(ArtifactStore.load(artifact).describe())


@app.command()
def schema() -> None:
    typer.echo(ArtifactStore.json_schema())


@app.command()
def approve(artifact: Path, reviewer: str = "reviewer") -> None:
    """Move draft/needs_review -> approved. Unattended replay requires this."""
    cap = ArtifactStore.load(artifact)
    cap = cap.model_copy(update={"approval": ApprovalState.APPROVED})
    ArtifactStore(artifact.parent).save(cap)
    typer.echo(f"{cap.ref} approved by {reviewer}")


@evidence_app.command("verify")
def evidence_verify(root: Path) -> None:
    """Recompute every hash chain under root; exit 1 if any run has been tampered with."""
    bad = 0
    for p in sorted(root.rglob("events.jsonl")):
        ok, n = verify_chain(p)
        typer.echo(f"{'OK ' if ok else 'BAD'} {p.parent.name} ({n} events)")
        bad += int(not ok)
    raise typer.Exit(code=1 if bad else 0)


@evidence_app.command("leak-check")
def evidence_leak(root: Path, literal: Annotated[list[str] | None, typer.Option()] = None) -> None:
    """Assert none of the given literals (or configured secrets) appear anywhere in evidence."""
    red = Redactor([*settings.redact_values, *(literal or [])])
    red.assert_clean_dir(root)
    typer.echo("clean")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 7860) -> None:
    import uvicorn

    uvicorn.run("cua.api.app:app", host=host, port=port)


if __name__ == "__main__":
    app()

_ = (json, RiskClass)
