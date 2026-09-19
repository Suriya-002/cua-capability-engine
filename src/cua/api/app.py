"""HTTP surface for the engine.

  GET  /health
  GET  /capabilities                      catalog (what an AI agent would discover)
  GET  /capabilities/{id}                 describe (inputs/outputs/outcomes) + JSON schema for args
  POST /capabilities/{id}/invoke          deterministic replay — the production path, no LLM
  POST /discover                          LLM discovery; token-gated + daily cap (costs money)
  GET  /operator                          intervention inbox (mock operator console)
  POST /operator/{req}/acquire|release    control transfer
  GET  /session/                          noVNC (proxied by nginx in the container)

Single-replica by design: the live session and the controller live in this process (see REPORT.md).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from cua.artifact.store import ArtifactStore
from cua.config import settings
from cua.escalation.session import SessionController, SessionState
from cua.evidence.writer import EvidenceWriter
from cua.logging import configure, get_logger, install_redactor
from cua.policy.engine import Policy
from cua.policy.redaction import Redactor
from cua.replay.engine import IdempotencyStore, ReplayEngine
from cua.replay.results import ReplayResult
from cua.surface.playwright_surface import PlaywrightSurface

configure()
log = get_logger("api")
redactor = Redactor(settings.redact_values)
install_redactor(redactor)

app = FastAPI(title="CUA Capability Engine", version="0.1.0")
store = ArtifactStore(settings.evidence_dir / "capabilities")
policy = Policy.load(settings.policy_path)
idem = IdempotencyStore(settings.idempotency_store)

# --- in-process live-session registry (single replica) ---------------------------------------
_active: dict[str, SessionController] = {}  # request_id -> controller
_discovery_calls: list[datetime] = []
_run_lock = asyncio.Lock()  # one live browser session at a time
_tasks: set[asyncio.Task[Any]] = set()


class InvokeBody(BaseModel):
    inputs: dict[str, str] = Field(default_factory=dict)
    tenant: str | None = None
    idempotency_key: str | None = None
    approved_irreversible: bool = False
    dry_run: bool = False
    attended: bool = Field(False, description="If true, the engine may pause for a human instead of failing")
    fault: str | None = Field(None, description="Mock-bank fault to inject via the entry URL (demo only)")


@app.get("/", include_in_schema=False)
def root(request: Request) -> HTMLResponse:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    base = f"{proto}://{host}"
    return HTMLResponse(HOME_HTML.replace("{{base}}", base))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


@app.get("/capabilities")
def catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": c.id,
            "version": c.version,
            "name": c.name,
            "risk": c.risk_class.value,
            "approval": c.approval.value,
            "inputs": {k: v.type.value for k, v in c.inputs.items()},
            "outputs": {k: v.type.value for k, v in c.outputs.items()},
            "outcomes": [o.code for o in c.outcomes],
            "stability": c.stability.score,
        }
        for c in store.list()
    ]


@app.get("/capabilities/{cap_id}")
def describe(cap_id: str) -> dict[str, Any]:
    cap = store.latest(cap_id)
    if cap is None:
        raise HTTPException(404, "unknown capability")
    args_schema = {
        "type": "object",
        "required": [k for k, v in cap.inputs.items() if v.required],
        "properties": {
            k: {
                "type": "string",
                "description": v.description,
                **({"pattern": v.pattern} if v.pattern else {}),
            }
            for k, v in cap.inputs.items()
        },
    }
    return {
        "ref": cap.ref,
        "description": cap.describe(),
        "args_schema": args_schema,
        "returns": {k: {"type": v.type.value, "description": v.description} for k, v in cap.outputs.items()},
        "outcomes": [{"code": o.code, "description": o.description} for o in cap.outcomes],
    }


@app.post("/capabilities/{cap_id}/invoke")
async def invoke(cap_id: str, body: InvokeBody) -> JSONResponse:
    cap = store.latest(cap_id)
    if cap is None:
        raise HTTPException(404, "unknown capability")
    if body.fault:
        sep = "&" if "?" in cap.entry_url else "?"
        cap = cap.model_copy(update={"entry_url": f"{cap.entry_url}{sep}fault={body.fault}"})
    async with _run_lock:
        ev = EvidenceWriter(settings.evidence_dir, "replay", redactor)
        # An attended run needs a browser a human can see: a real window locally, Xvfb+noVNC in the container.
        surface = PlaywrightSurface(
            headless=settings.headless and not body.attended,
            viewport=(settings.viewport_width, settings.viewport_height),
        )
        session = SessionController(ev, cap.ref) if body.attended else None
        if session:
            surface.install_human_recorder(lambda p: session.record_human_step(**p))
        log.info(
            "invoke",
            capability=cap.ref,
            attended=body.attended,
            headed=not surface.headless,
            run_id=ev.run_id,
        )
        print(
            f"[engine] invoke {cap.ref} attended={body.attended} browser={'headed' if not surface.headless else 'headless'}",
            flush=True,
        )
        engine = ReplayEngine(
            surface,
            policy,
            ev,
            redactor,
            session=session,
            idempotency=idem,
            unattended=not body.attended,
            escalation_wait_s=600,
            secrets=settings.secrets,
        )
        task = asyncio.create_task(
            engine.run(
                cap,
                body.inputs,
                tenant=body.tenant,
                idempotency_key=body.idempotency_key,
                approved_irreversible=body.approved_irreversible,
                dry_run=body.dry_run,
            )
        )
        # Attended runs may park on a human; expose the controller so the operator UI can act on it.
        if session:
            await asyncio.sleep(0)
            _watch(session, task)
        result: ReplayResult = await task
        assert result.llm_calls == 0
        return JSONResponse(result.model_dump(mode="json"))


def _watch(session: SessionController, task: asyncio.Task[Any]) -> None:
    async def poll() -> None:
        while not task.done():
            if session.request and session.request.id not in _active:
                _active[session.request.id] = session
            await asyncio.sleep(0.5)
        for rid, s in list(_active.items()):
            if s is session:
                _active.pop(rid, None)

    _bg = asyncio.create_task(poll())
    _tasks.add(_bg)
    _bg.add_done_callback(_tasks.discard)


class DiscoverBody(BaseModel):
    goal: str
    entry_url: str
    name: str
    params: dict[str, str] = Field(default_factory=dict)


@app.post("/discover")
async def discover(
    body: DiscoverBody, x_discovery_token: str | None = Header(default=None)
) -> dict[str, Any]:
    if not settings.discovery_token or x_discovery_token != settings.discovery_token:
        raise HTTPException(403, "discovery is disabled on this deployment (replay is the public path)")
    today = [t for t in _discovery_calls if t.date() == datetime.now(UTC).date()]
    if len(today) >= settings.discovery_daily_cap:
        raise HTTPException(429, "daily discovery cap reached")
    _discovery_calls.append(datetime.now(UTC))
    from cua.cli import run_discovery  # lazy: keeps the API importable without an API key

    async with _run_lock:
        cap_path, outcome = await run_discovery(body.goal, body.entry_url, body.name, body.params)
    return {
        "artifact": str(cap_path),
        "status": outcome.status,
        "turns": outcome.turns,
        "llm_calls": outcome.llm_calls,
    }


# --- operator console (mock; the handoff mechanism is real) ----------------------------------
@app.get("/operator", response_class=HTMLResponse)
def operator(request: Request) -> HTMLResponse:
    rows = []
    for rid, s in _active.items():
        r = s.request
        if not r:
            continue
        rows.append(
            f"<tr><td>{rid}</td><td>{r.capability_ref}</td><td>{r.step_n}</td><td>{r.reason}</td>"
            f"<td>{s.state.value}</td><td>{r.acquired_by or ''}</td>"
            f"<td><form method=post action='/operator/{rid}/acquire'><input name=operator placeholder='your name'>"
            f"<button>Take control</button></form>"
            f"<form method=post action='/operator/{rid}/release'><input name=resolution placeholder='what you did'>"
            f"<button>Hand back</button></form></td></tr>"
        )
    body = "".join(rows) or "<tr><td colspan=7>No open intervention requests.</td></tr>"
    return HTMLResponse(
        OPERATOR_HTML.replace("{{rows}}", body).replace(
            "{{novnc}}",
            os.getenv("NOVNC_PATH", "/session/vnc.html?autoconnect=1&path=session/websockify"),
        )
    )


@app.post("/operator/{rid}/acquire")
async def acquire(rid: str, request: Request) -> dict[str, str]:
    form = await request.form()
    s = _active.get(rid)
    if not s or s.state != SessionState.PAUSED:
        raise HTTPException(409, "request not in PAUSED state")
    s.acquire(str(form.get("operator") or "operator"))
    return {"state": s.state.value, "controller": s.controller.value}


@app.post("/operator/{rid}/release")
async def release(rid: str, request: Request) -> dict[str, str]:
    form = await request.form()
    s = _active.get(rid)
    if not s or s.state != SessionState.HUMAN:
        raise HTTPException(409, "request not in HUMAN state")
    s.release(str(form.get("resolution") or "resolved by operator"))
    return {"state": s.state.value, "controller": s.controller.value}


@app.post("/operator/{rid}/abort")
async def abort(rid: str, request: Request) -> dict[str, str]:
    form = await request.form()
    s = _active.get(rid)
    if not s or s.state not in {SessionState.PAUSED, SessionState.HUMAN}:
        raise HTTPException(409, "request not in PAUSED/HUMAN state")
    s.abort(f"aborted by {form.get('operator') or 'operator'}: {form.get('resolution') or 'no reason given'}")
    return {"state": s.state.value, "controller": s.controller.value}


@app.get("/operator/requests")
def requests_json() -> list[dict[str, Any]]:
    return [
        {
            "id": rid,
            "state": s.state.value,
            "controller": s.controller.value,
            "capability": s.request.capability_ref if s.request else None,
            "operator": s.request.acquired_by if s.request else None,
            "reason": s.request.reason if s.request else None,
            "browser": s.request.context.get("browser") if s.request else None,
            "url": s.request.context.get("url") if s.request else None,
            "step": s.request.step_n if s.request else None,
            "screenshot": s.request.screenshot_path if s.request else None,
            "human_steps": len(s.request.human_steps) if s.request else 0,
        }
        for rid, s in _active.items()
        if s.request is not None and s.request.id == rid  # a session keeps only its current request visible
    ]


OPERATOR_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Operator console — CUA</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:#0f1115;--panel:#161a22;--line:#242a35;--fg:#e6e9ef;--muted:#8b93a3;--accent:#5eead4;--warn:#f59e0b;--ok:#34d399;--bad:#f87171;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ea;--fg:#14171d;--muted:#5d6472;--accent:#0f766e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 64px}
header.top{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:28px}
header.top h1{font-size:22px;margin:0;letter-spacing:.2px}header.top nav a{color:var(--muted);text-decoration:none;margin-left:18px}header.top nav a:hover{color:var(--fg)}
.tag{display:inline-block;font:12px/1 var(--mono);padding:4px 8px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.hero{font-size:20px;max-width:760px;margin:0 0 8px}.lead{color:var(--muted);max-width:760px;margin:0 0 28px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:0 0 32px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}.stat b{display:block;font-size:22px}.stat span{color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;display:flex;flex-direction:column;gap:8px}
.card h3{margin:0;font-size:16px}.card p{margin:0;color:var(--muted);font-size:14px;flex:1}
.card a.btn{align-self:flex-start;margin-top:8px;padding:8px 14px;border-radius:8px;background:var(--accent);color:#06221f;text-decoration:none;font-weight:600;font-size:14px}
code,pre{font-family:var(--mono);font-size:13px}pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
tr:last-child td{border-bottom:0}.muted{color:var(--muted)}
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font:600 12px/1.4 var(--mono)}
.badge.paused{background:rgba(245,158,11,.15);color:var(--warn)}.badge.human{background:rgba(52,211,153,.15);color:var(--ok)}.badge.automation,.badge.resuming{background:rgba(94,234,212,.12);color:var(--accent)}.badge.aborted,.badge.completed{background:rgba(139,147,163,.15);color:var(--muted)}
input[type=text],input:not([type]){background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:14px}
button{background:var(--accent);color:#06221f;border:0;border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;font-size:13px}button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
section{margin-top:36px}section h2{font-size:17px;margin:0 0 12px}
iframe{width:100%;height:640px;border:1px solid var(--line);border-radius:12px;background:#000}
footer{margin-top:48px;color:var(--muted);font-size:13px}

.toolbar{display:flex;align-items:center;gap:12px;margin-bottom:14px}.toolbar label{color:var(--muted);font-size:14px}
</style></head><body><div class="wrap">
<header class="top"><h1>Operator console</h1><nav><a href="/">Home</a><a href="/bank/login">Mock bank</a><a href="/capabilities">Catalog</a></nav></header>
<section style="margin-top:0"><h2>Intervention inbox</h2>
<div class="toolbar"><label>Operator name</label><input id="op" placeholder="your name" size="24"><span class="tag">updates every 3 s · input is never reset</span></div>
<table><thead><tr><th>Request</th><th>Capability</th><th>Step</th><th>Reason</th><th>Engine browser</th><th>State</th><th>Operator</th><th>Action</th></tr></thead>
<tbody id="rows"><tr><td colspan="8" class="muted">Loading…</td></tr></tbody></table></section>
<section><h2>Live session</h2>
<p id="vnc-note" class="muted">No noVNC on this deployment: use the automation Chromium window on your desktop.</p>
<iframe id="vnc" src="{{novnc}}" style="display:none"></iframe>
<p class="muted">Flow: <b>Take control</b> → do the manual steps in the live browser → <b>Hand back</b>. The engine re-verifies the step, records
your actions as <code>human_step</code> evidence, and continues — or re-runs the flow once if the screen was left elsewhere.</p></section>
<script>
const rows = document.getElementById('rows');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load() {
  let reqs = [];
  try { reqs = await (await fetch('/operator/requests')).json(); } catch (e) { return; }
  if (!reqs.length) { rows.innerHTML = '<tr><td colspan="8" class="muted">No open intervention requests.</td></tr>'; return; }
  rows.innerHTML = reqs.map(q => {
    const abort = ` <button class="ghost" onclick="act('${q.id}','abort')" title="End this run as failed">Abort</button>`;
    const btn = q.state === 'paused' ? `<button onclick="act('${q.id}','acquire')">Take control</button>` + abort
              : q.state === 'human' ? `<button onclick="act('${q.id}','release')">Hand back</button>` + abort
              : '<span class="muted">engine running</span>';
    return `<tr><td><code>${esc(q.id)}</code></td><td>${esc(q.capability)}</td><td>${esc(q.step)}</td><td>${esc(q.reason)}</td>
            <td>${esc(q.browser ?? '')}<br><small class="muted">${esc(q.url ?? '')}</small></td>
            <td><span class="badge ${esc(q.state)}">${esc(q.state)}</span></td><td>${esc(q.operator ?? '')}</td><td>${btn}</td></tr>`;
  }).join('');
}
async function act(id, verb) {
  const operator = document.getElementById('op').value || 'operator';
  const resolution = verb === 'release' ? (prompt('What did you do in the live session?') || 'resolved by operator')
                   : verb === 'abort' ? (prompt('Why abort?') || 'no reason given') : '';
  const r = await fetch(`/operator/${id}/${verb}`, { method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({ operator, resolution }) });
  if (!r.ok) alert(`${verb} failed: ${r.status} ${await r.text()}`);
  load();
}
setInterval(load, 3000); load();
fetch('/session/', {method: 'HEAD'}).then(r => { if (r.ok) { document.getElementById('vnc').style.display = 'block';
  document.getElementById('vnc-note').style.display = 'none'; } }).catch(() => {});
</script></div></body></html>"""


HOME_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>CUA Capability Engine</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:#0f1115;--panel:#161a22;--line:#242a35;--fg:#e6e9ef;--muted:#8b93a3;--accent:#5eead4;--warn:#f59e0b;--ok:#34d399;--bad:#f87171;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ea;--fg:#14171d;--muted:#5d6472;--accent:#0f766e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 64px}
header.top{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:28px}
header.top h1{font-size:22px;margin:0;letter-spacing:.2px}header.top nav a{color:var(--muted);text-decoration:none;margin-left:18px}header.top nav a:hover{color:var(--fg)}
.tag{display:inline-block;font:12px/1 var(--mono);padding:4px 8px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.hero{font-size:20px;max-width:760px;margin:0 0 8px}.lead{color:var(--muted);max-width:760px;margin:0 0 28px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:0 0 32px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}.stat b{display:block;font-size:22px}.stat span{color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;display:flex;flex-direction:column;gap:8px}
.card h3{margin:0;font-size:16px}.card p{margin:0;color:var(--muted);font-size:14px;flex:1}
.card a.btn{align-self:flex-start;margin-top:8px;padding:8px 14px;border-radius:8px;background:var(--accent);color:#06221f;text-decoration:none;font-weight:600;font-size:14px}
code,pre{font-family:var(--mono);font-size:13px}pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
tr:last-child td{border-bottom:0}.muted{color:var(--muted)}
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font:600 12px/1.4 var(--mono)}
.badge.paused{background:rgba(245,158,11,.15);color:var(--warn)}.badge.human{background:rgba(52,211,153,.15);color:var(--ok)}.badge.automation,.badge.resuming{background:rgba(94,234,212,.12);color:var(--accent)}.badge.aborted,.badge.completed{background:rgba(139,147,163,.15);color:var(--muted)}
input[type=text],input:not([type]){background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:14px}
button{background:var(--accent);color:#06221f;border:0;border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;font-size:13px}button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
section{margin-top:36px}section h2{font-size:17px;margin:0 0 12px}
iframe{width:100%;height:640px;border:1px solid var(--line);border-radius:12px;background:#000}
footer{margin-top:48px;color:var(--muted);font-size:13px}
</style></head><body><div class="wrap">
<header class="top"><h1>CUA Capability Engine</h1><nav><a href="/bank/login">Mock bank</a><a href="/capabilities">Catalog</a><a href="/operator">Operator</a><a href="https://github.com/Suriya-002/cua-capability-engine">Source</a></nav></header>
<p class="hero">The model discovers once. The artifact becomes a reusable capability. Deterministic replay is how an agent invokes it.</p>
<p class="lead">An LLM drives a legacy back-office app from screenshots, the run is recorded as a typed, versioned capability, and that
capability replays with no model in the loop — returning a typed result, recovering from runtime conditions, and handing the live
browser to a human when it can't proceed safely.</p>
<div class="stats">
<div class="stat"><b>3 turns · 15 s</b><span>one real LLM discovery run</span></div>
<div class="stat"><b>~4 s · $0.00</b><span>deterministic replay, 0 LLM calls</span></div>
<div class="stat"><b>10 / 10</b><span>replay stability</span></div>
<div class="stat"><b>4 + 1</b><span>error classes handled · human handoff</span></div>
</div>
<div class="cards">
<div class="card"><h3>Mock legacy bank</h3><p>The target: framesets, table layouts, no ids, cookie sessions, injectable faults. Deliberately ugly — that's the point.</p><a class="btn" href="/bank/login">Open the app</a></div>
<div class="card"><h3>Capability catalog</h3><p>What an AI agent sees: typed inputs, outputs, business outcomes, approval and stability. Invoke with <code>POST /capabilities/{id}/invoke</code>.</p><a class="btn" href="/capabilities">View catalog</a></div>
<div class="card"><h3>Operator console</h3><p>Intervention inbox and the live session over noVNC. Take control of the engine's browser, fix the screen, hand back.</p><a class="btn" href="/operator">Open console</a></div>
</div>
<section><h2>Try a replay from your terminal</h2>
<pre>curl -s -X POST {{base}}/capabilities/lookup_member_balance/invoke \
  -H "content-type: application/json" -d '{"inputs":{"member_id":"10041"}}'

# a legitimate business outcome, not a crash
curl -s -X POST {{base}}/capabilities/lookup_member_balance/invoke \
  -H "content-type: application/json" -d '{"inputs":{"member_id":"99999"},"fault":"not_found"}'

# escalation: the member screen 500s; finish it from the operator console
curl -s -X POST {{base}}/capabilities/lookup_member_balance/invoke \
  -H "content-type: application/json" -d '{"inputs":{"member_id":"10041"},"attended":true,"fault":"error_500"}'</pre></section>
<footer>Discovery is token-gated on this deployment; replay is the production path and costs nothing. Evidence, schema and the design write-up are in the repository.</footer>
</div></body></html>"""


# Mount the mock bank under /bank so one container serves everything.
try:
    from mockbank.app import app as _bank

    app.mount("/", _bank)  # routes are already prefixed with /bank
except Exception as e:
    log.warning("mockbank not mounted", error=str(e))

_ = Path  # keep import for type checkers when unused in some configs
