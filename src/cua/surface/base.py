"""Surface = "how we perceive and act on an application". The recorded flow never depends on it.

A surface must be able to:
- observe(): screenshot (scaled for the model) + optional structure (a11y snapshot, url)
- act(): execute one primitive action expressed in screenshot pixel space or against a resolved handle
- probe(x, y): describe what is under a point as ranked locator candidates (used at record time)
- resolve(locator): turn a locator into a handle (used at replay time), reporting which candidate won
- read_text(): visible text for checkpoint/outcome detection

Implementations: PlaywrightSurface (web, built), DesktopSurface (UIA/pywinauto, designed stub).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from cua.artifact.schema import Locator, LocatorCandidate


@dataclass
class Observation:
    png: bytes  # already scaled to fit the model's image limits
    scale: float  # screenshot px -> real px multiply factor is 1/scale
    width: int
    height: int
    url: str | None = None
    title: str | None = None
    aria: str | None = None  # accessibility snapshot, when cheap to get


@dataclass
class Action:
    """One primitive, mirroring the computer-use member tools plus locator-based variants."""

    name: str  # screenshot|zoom|left_click|double_click|right_click|type|key|scroll|wait|
    # navigate|click_handle|type_handle|select_handle|extract_handle
    coordinate: tuple[int, int] | None = None  # in SCREENSHOT pixel space
    start_coordinate: tuple[int, int] | None = None
    region: tuple[int, int, int, int] | None = None
    text: str | None = None
    repeat: int = 1
    scroll_direction: str | None = None
    scroll_amount: int = 0
    duration: float = 0.0
    url: str | None = None
    handle: Any = None  # resolved element handle (replay path)
    modifiers: str | None = None


@dataclass
class Resolution:
    handle: Any
    candidate: LocatorCandidate
    candidate_index: int  # 0 == primary; >0 means drift
    text: str | None = None


@dataclass
class ActResult:
    ok: bool
    message: str = "OK"
    png: bytes | None = None  # for screenshot/zoom
    extracted: str | None = None  # for extract_handle
    error: str | None = None


@dataclass
class ProbeResult:
    candidates: list[LocatorCandidate] = field(default_factory=list)
    description: str = ""
    text: str | None = None
    tag: str | None = None
    frame: str | None = None

    def to_locator(self) -> Locator:
        return Locator(description=self.description or "control", candidates=self.candidates)


@runtime_checkable
class Surface(Protocol):
    async def start(self, entry_url: str | None = None) -> None: ...
    async def stop(self) -> None: ...
    async def observe(self, *, with_aria: bool = False) -> Observation: ...
    async def act(self, action: Action) -> ActResult: ...
    async def probe(self, x: int, y: int) -> ProbeResult: ...
    async def resolve(self, locator: Locator, timeout_ms: int) -> Resolution | None: ...
    async def read_text(self) -> str: ...
    async def current_url(self) -> str: ...
    async def all_urls(self) -> list[str]: ...
    async def dom_snapshot(self) -> str | None: ...
    async def drain_human_events(self) -> list[dict[str, Any]]: ...
    async def fingerprint(self) -> str: ...
    def install_human_recorder(self, callback: Any) -> None: ...
