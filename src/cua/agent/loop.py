"""Goal-driven discovery: observe -> decide (LLM) -> act, until done or a stopping condition.

Implements the `computer_toolset_20260801` contract exactly:
- one toolset entry in `tools`, no beta header, no display dims (we resize + scale ourselves)
- Claude may return several member `tool_use` blocks per turn (a batch); run them in order,
  stop at the first failure, answer the rest with the halt text, echo `toolset_name` on results
- a custom `finish` tool lets the model declare completion with extracted outputs as JSON
- screenshots are pruned in batches (keep last 3, every 25 turns) to keep the prompt cache warm

Every action passes the policy gate BEFORE it runs. Every action is recorded (with a DOM probe of
the clicked point) so the Recorder can emit an artifact without re-reading the transcript.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ThinkingConfigParam, ToolUnionParam

from cua.agent.prompts import SYSTEM_PROMPT, user_prompt
from cua.artifact.schema import ActionType
from cua.evidence.writer import EvidenceWriter
from cua.policy.engine import Policy, PolicyViolation
from cua.surface.base import Action, Surface

HALT = "Not executed: an earlier computer action in this turn failed."
TOOLSET = "computer"
FINISH_TOOL = {
    "name": "finish",
    "description": "Call when the goal is achieved or provably impossible; report extracted values.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["success", "business_outcome", "stuck"]},
            "outcome_code": {
                "type": "string",
                "description": "e.g. MEMBER_NOT_FOUND when status=business_outcome",
            },
            "outputs": {"type": "object", "additionalProperties": {"type": "string"}},
            "summary": {"type": "string"},
        },
        "required": ["status", "summary"],
    },
}


@dataclass
class RecordedAction:
    turn: int
    name: str
    input: dict[str, Any]
    url_before: str
    url_after: str | None = None
    urls_before: list[str] = field(default_factory=list)
    urls_after: list[str] = field(default_factory=list)
    probe: Any = None  # ProbeResult for click/type targets
    rationale: str | None = None
    risk: str = "safe"
    ok: bool = True
    error: str | None = None
    screenshot: str | None = None


@dataclass
class DiscoveryOutcome:
    status: str
    summary: str
    outputs: dict[str, str] = field(default_factory=dict)
    outcome_code: str | None = None
    turns: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    actions: list[RecordedAction] = field(default_factory=list)
    stop_reason: str = "finished"


_MEMBER_TO_ACTIONTYPE = {
    "left_click": ActionType.CLICK,
    "double_click": ActionType.CLICK,
    "right_click": ActionType.CLICK,
    "triple_click": ActionType.CLICK,
    "type": ActionType.TYPE,
    "key": ActionType.KEY,
    "scroll": ActionType.SCROLL,
    "wait": ActionType.WAIT_FOR,
}


class DiscoveryAgent:
    def __init__(
        self,
        client: AsyncAnthropic,
        surface: Surface,
        policy: Policy,
        evidence: EvidenceWriter,
        *,
        model: str = "claude-sonnet-5",
        effort: str = "medium",
        max_steps: int = 40,
        timeout_s: int = 300,
        keep_screens: int = 3,
        prune_every: int = 25,
    ) -> None:
        self.client, self.surface, self.policy, self.ev = client, surface, policy, evidence
        self.model, self.effort = model, effort
        self.max_steps, self.timeout_s = max_steps, timeout_s
        self.keep_screens, self.prune_every = keep_screens, prune_every
        self.actions: list[RecordedAction] = []
        self._pending_rationale: str | None = None

    async def run(
        self,
        goal: str,
        entry_url: str,
        params: dict[str, str],
        param_sensitivity: dict[str, str],
        secrets: dict[str, str] | None = None,
    ) -> DiscoveryOutcome:
        t0 = time.monotonic()
        await self.surface.start(entry_url)
        self.ev.event(
            "discovery_start", goal=goal, entry_url=entry_url, model=self.model, params=list(params)
        )
        obs = await self.surface.observe()
        self.ev.screenshot(obs.png, "initial")
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt(goal, params, param_sensitivity, secrets or {})},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(obs.png).decode(),
                        },
                    },
                ],
            }
        ]
        tools: list[dict[str, Any]] = [
            {"type": "computer_toolset_20260801", "cache_control": {"type": "ephemeral"}},
            FINISH_TOOL,
        ]
        out = DiscoveryOutcome(status="stuck", summary="")
        for turn in range(1, self.max_steps + 1):
            if time.monotonic() - t0 > self.timeout_s:
                out.stop_reason = "timeout"
                break
            thinking = cast(ThinkingConfigParam, {"type": "adaptive", "display": "summarized"})
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=cast(list[ToolUnionParam], tools),
                messages=cast(list[MessageParam], messages),
                thinking=thinking,
            )
            out.llm_calls += 1
            out.input_tokens += resp.usage.input_tokens
            out.output_tokens += resp.usage.output_tokens
            messages.append(
                {
                    "role": "assistant",
                    "content": [b.model_dump(exclude_none=True) for b in resp.content],
                }
            )

            self._pending_rationale = next((b.text for b in resp.content if b.type == "text"), None)
            results, finished = await self._process(resp, turn)
            if finished is not None:
                out.status, out.summary = (
                    finished.get("status", "stuck"),
                    finished.get("summary", ""),
                )
                out.outputs = dict(finished.get("outputs") or {})
                out.outcome_code = finished.get("outcome_code")
                out.turns = turn
                break
            if not results:
                out.stop_reason = "no_tool_use"
                out.turns = turn
                break
            messages.append({"role": "user", "content": results})
            if turn % self.prune_every == 0:
                self._prune(messages)
        else:
            out.stop_reason = "max_steps"
            out.turns = self.max_steps

        out.actions = self.actions
        out.duration_ms = int((time.monotonic() - t0) * 1000)
        self.ev.event(
            "discovery_end",
            status=out.status,
            stop_reason=out.stop_reason,
            turns=out.turns,
            llm_calls=out.llm_calls,
            input_tokens=out.input_tokens,
            output_tokens=out.output_tokens,
            duration_ms=out.duration_ms,
            outcome_code=out.outcome_code,
        )
        return out

    # --- batch handling ----------------------------------------------------------------------
    async def _process(self, resp: Any, turn: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        results: list[dict[str, Any]] = []
        failed = False
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "finish":
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": "OK"})
                return results, dict(block.input)
            if getattr(block, "toolset_name", None) != TOOLSET:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "is_error": True,
                        "content": "unknown tool",
                    }
                )
                continue
            res: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "toolset_name": TOOLSET,
            }
            if failed:
                res.update(is_error=True, content=HALT)
                results.append(res)
                continue
            content, err = await self._execute(block.name, dict(block.input), turn)
            if err:
                failed = True
                res.update(is_error=True, content=err)
            else:
                res["content"] = content
            results.append(res)
        # Always end a batch with a fresh screenshot so the model sees the outcome without a round trip.
        if (
            results
            and not failed
            and not any(b.type == "tool_use" and b.name == "screenshot" for b in resp.content)
        ):
            obs = await self.surface.observe()
            last = results[-1]
            img = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(obs.png).decode(),
                },
            }
            last["content"] = [{"type": "text", "text": str(last.get("content", "OK"))}, img]
        return results, None

    async def _execute(self, name: str, inp: dict[str, Any], turn: int) -> tuple[Any, str | None]:
        url = await self.surface.current_url()
        rec = RecordedAction(
            turn=turn, name=name, input=inp, url_before=url, rationale=self._pending_rationale
        )
        # 1) probe what's under the pointer BEFORE acting (record-time locator derivation)
        coord = inp.get("coordinate")
        target_text = None
        if coord and name in _MEMBER_TO_ACTIONTYPE:
            rec.probe = await self.surface.probe(int(coord[0]), int(coord[1]))
            target_text = rec.probe.text
        # 2) policy gate
        atype = _MEMBER_TO_ACTIONTYPE.get(name)
        if atype is not None:
            try:
                d = self.policy.enforce(atype, url, target_text)
                rec.risk = d.risk.value
            except PolicyViolation as pv:
                rec.ok, rec.error = False, f"policy: {pv}"
                self.actions.append(rec)
                self.ev.event("policy_block", action=name, url=url, target=target_text, reason=str(pv))
                return (
                    None,
                    f"Blocked by policy: {pv}. Choose a different action within the allowlist.",
                )
        # 3) act
        a = Action(
            name=name,
            coordinate=tuple(coord) if coord else None,
            start_coordinate=tuple(inp["start_coordinate"]) if inp.get("start_coordinate") else None,
            region=tuple(inp["region"]) if inp.get("region") else None,
            text=inp.get("text"),
            repeat=int(inp.get("repeat", 1)),
            scroll_direction=inp.get("scroll_direction"),
            scroll_amount=int(inp.get("scroll_amount", 0)),
            duration=float(inp.get("duration", 0)),
        )
        r = await self.surface.act(a)
        if name in _MEMBER_TO_ACTIONTYPE:
            await asyncio.sleep(0.6)  # let a click-triggered navigation settle before reading state
        rec.url_after = await self.surface.current_url()
        rec.urls_after = await self.surface.all_urls()
        rec.ok, rec.error = r.ok, r.error
        if name in {"screenshot", "zoom"} and r.png:
            p = self.ev.screenshot(r.png, name)
            rec.screenshot = str(p)
        self.actions.append(rec)
        self.ev.event(
            "agent_action",
            turn=turn,
            action=name,
            input={**inp, "text": "[typed]"} if name == "type" else inp,
            ok=r.ok,
            error=r.error,
            risk=rec.risk,
            target=(rec.probe.description if rec.probe else None),
            rationale=rec.rationale,
        )
        if not r.ok:
            return None, r.error or "action failed"
        if r.png is not None:
            return [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(r.png).decode(),
                    },
                }
            ], None
        return r.message, None

    def _prune(self, messages: list[dict[str, Any]]) -> None:
        """Drop older screenshot images in one batch to keep the cached prefix stable between prunes."""
        seen = 0
        for m in reversed(messages):
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for part in m["content"]:
                if part.get("type") != "tool_result" or not isinstance(part.get("content"), list):
                    continue
                for c in part["content"]:
                    if c.get("type") == "image":
                        seen += 1
                        if seen > self.keep_screens:
                            c.clear()
                            c.update({"type": "text", "text": "[screenshot pruned]"})
        self.ev.event("prune_screenshots", kept=self.keep_screens)

    @staticmethod
    def dump_transcript(messages: list[dict[str, Any]]) -> str:
        return json.dumps(messages, default=str)[:200_000]
