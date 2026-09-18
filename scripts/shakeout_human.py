"""Verify the human-action recorder without a human: drive real mouse/keyboard input through
Playwright (which dispatches genuine DOM events) and check drain_human_events() sees it in frames."""

from __future__ import annotations

import asyncio

from cua.api.localserver import ensure_server
from cua.surface.playwright_surface import PlaywrightSurface

BASE = "http://localhost:7860/bank"


async def main() -> None:
    if not ensure_server(BASE):
        raise SystemExit("could not start the local server")
    s = PlaywrightSurface(headless=True, viewport=(1280, 800))
    await s.start(f"{BASE}/login")
    try:
        assert s.page
        await s.page.fill("input[name=username]", "svc_automation_7f3")
        await s.page.fill("input[name=password]", "Lcu-demo-9x2Q")
        await s.page.click("input[type=submit]")
        await s.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.8)
        leftover = await s.drain_human_events()
        print(f"login-page events captured: {len(leftover)}")
        nav = s._frame_by_name("nav")
        main = s._frame_by_name("main")
        assert nav and main
        await nav.click("text=Member Search")  # click inside the nav frame
        await asyncio.sleep(0.6)
        main = s._frame_by_name("main")
        assert main
        await main.fill("input[name=member_id]", "10041")  # change event on blur/submit
        await main.click("input[type=submit]")  # click + submit in the main frame
        await asyncio.sleep(0.8)
        events = await s.drain_human_events()
        for e in events:
            print(f"  {e['kind']:<7} {e['tag']:<6} {e.get('text', '')!r:<20} {e['url']}")
        kinds = [e["kind"] for e in events]
        assert "click" in kinds and ("change" in kinds or "submit" in kinds), kinds
        print("\nHUMAN RECORDER OK")
    finally:
        await s.stop()


if __name__ == "__main__":
    asyncio.run(main())
