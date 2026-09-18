"""Phase-2 shakeout: drive the mock bank through PlaywrightSurface only (no LLM).

Run `uvicorn cua.api.app:app --port 7860` in another window first, then:
    python scripts/shakeout.py
Exercises observe() scaling, resolve() for each locator strategy, framesets, probe(), and extraction.
"""

from __future__ import annotations

import asyncio
import sys

from cua.artifact.schema import Locator, LocatorCandidate, LocatorStrategy
from cua.surface.base import Action
from cua.surface.playwright_surface import PlaywrightSurface

BASE = "http://localhost:7860/bank"


def loc(desc: str, strategy: LocatorStrategy, value: str, frame: str | None = None) -> Locator:
    return Locator(
        description=desc,
        candidates=[LocatorCandidate(strategy=strategy, value=value, confidence=0.9, frame=frame)],
    )


async def step(
    surface: PlaywrightSurface, name: str, locator: Locator, action: str, text: str | None = None
) -> None:
    r = await surface.resolve(locator, 5000)
    if r is None:
        print(f"  FAIL resolve: {name}")
        sys.exit(1)
    res = await surface.act(Action(name=action, handle=r.handle, text=text))
    print(f"  ok   {name:<28} via {r.candidate.strategy.value:<15} {'' if res.ok else res.error}")
    if not res.ok:
        sys.exit(1)


async def main() -> None:
    s = PlaywrightSurface(headless=False, viewport=(1280, 800), slow_mo=150)
    await s.start(f"{BASE}/login")
    try:
        obs = await s.observe()
        print(
            f"observe: {obs.width}x{obs.height} scale={obs.scale:.3f} png={len(obs.png)} bytes title={obs.title!r}"
        )

        print("login page")
        await step(
            s,
            "username field",
            loc("user", LocatorStrategy.LABEL_RELATIVE, "User"),
            "type",
            "svc_automation_7f3",
        )
        await step(
            s,
            "password field",
            loc("pass", LocatorStrategy.LABEL_RELATIVE, "Password"),
            "type",
            "Lcu-demo-9x2Q",
        )
        await step(
            s, "sign in button", loc("sign in", LocatorStrategy.ROLE_NAME, "button|Sign In"), "left_click"
        )
        await asyncio.sleep(0.8)
        print(f"  url now {await s.current_url()}")
        print(f"  fingerprint {await s.fingerprint()}")
        print(f"  frames: {[f.name for f in s.page.frames]}")

        print("search screen (inside frame 'main')")
        await step(
            s,
            "member id field",
            loc("member id", LocatorStrategy.LABEL_RELATIVE, "Member ID", "main"),
            "type",
            "10023",
        )
        # probe what's under the Search button before clicking it: this is what discovery records
        btn = await s.resolve(loc("search", LocatorStrategy.ROLE_NAME, "button|Search", "main"), 3000)
        assert btn is not None
        box = await btn.handle.bounding_box()
        frame_el = await s._frame_by_name("main").frame_element()
        fbox = await frame_el.bounding_box()
        x = int((box["x"] + fbox["x"] + box["width"] / 2) * s._scale)
        y = int((box["y"] + fbox["y"] + box["height"] / 2) * s._scale)
        probe = await s.probe(x, y)
        print(f"  probe({x},{y}) -> {probe.description}")
        for c in probe.candidates:
            print(f"     {c.confidence:.2f} {c.strategy.value:<15} frame={c.frame} {c.value}")
        await step(
            s,
            "search button",
            loc("search", LocatorStrategy.ROLE_NAME, "button|Search", "main"),
            "left_click",
        )
        await asyncio.sleep(0.8)

        print("member detail")
        text = await s.read_text()
        assert "Current Balance" in text, "expected member detail"
        cell = loc(
            "savings",
            LocatorStrategy.CSS_STRUCTURAL,
            "xpath=//tr[td[normalize-space()='Savings']]/td[2]",
            "main",
        )
        r = await s.resolve(cell, 3000)
        assert r is not None
        res = await s.act(Action(name="extract", handle=r.handle))
        print(f"  savings_balance = {res.extracted}")

        print("fault: not_found")
        await s.act(Action(name="navigate", url=f"{BASE}/member/99999?fault=not_found"))
        print(f"  outcome text present: {'No member matches' in await s.read_text()}")
        print("\nSHAKEOUT OK")
    finally:
        await s.stop()


if __name__ == "__main__":
    asyncio.run(main())
