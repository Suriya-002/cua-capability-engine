"""Designed, not built: a native-desktop surface.

Mapping (Windows UI Automation via `pywinauto`/`uiautomation`, or macOS AX API):
- observe()   -> screenshot of the target window (mss/PIL); optional UIA tree dump as `aria`
- act()       -> pyautogui/SendInput for coordinate actions; UIA Invoke/SetValue for handle actions
- probe(x,y)  -> UIA ElementFromPoint -> candidates: AutomationId (0.95), Name+ControlType (0.9),
                 tree path (0.5), image_anchor of a cropped screenshot (0.4), coords (0.2)
- resolve()   -> walk candidates the same way PlaywrightSurface does; IMAGE_ANCHOR uses template
                 matching (OpenCV) on the live screenshot — the only strategy that works when
                 the app exposes nothing (e.g. terminal emulators)

Nothing in the artifact schema changes: `LocatorStrategy` already carries the desktop strategies,
`frame` is reused for window/pane identity, and the replay engine is unchanged.
"""

from __future__ import annotations

from typing import Any

from cua.artifact.schema import Locator
from cua.surface.base import Action, ActResult, Observation, ProbeResult, Resolution


class DesktopSurface:
    """Raises on use; exists so the seam is a real type and the engine is provably surface-agnostic."""

    async def start(self, entry_url: str | None = None) -> None:
        raise NotImplementedError("DesktopSurface is a documented seam; see module docstring")

    async def stop(self) -> None: ...
    async def observe(self, *, with_aria: bool = False) -> Observation:
        raise NotImplementedError

    async def act(self, action: Action) -> ActResult:
        raise NotImplementedError

    async def probe(self, x: int, y: int) -> ProbeResult:
        raise NotImplementedError

    async def resolve(self, locator: Locator, timeout_ms: int) -> Resolution | None:
        raise NotImplementedError

    async def read_text(self) -> str:
        raise NotImplementedError

    async def current_url(self) -> str:
        return "desktop://"

    async def all_urls(self) -> list[str]:
        return ["desktop://"]

    async def dom_snapshot(self) -> str | None:
        return None

    async def drain_human_events(self) -> list[dict[str, Any]]:
        return []

    async def fingerprint(self) -> str:
        raise NotImplementedError

    def install_human_recorder(self, callback: Any) -> None: ...
