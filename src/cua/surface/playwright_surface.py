"""Web surface on Playwright.

Perception is pixel-first (screenshot + coordinates) so the discovery loop would behave the same on
a surface with no usable DOM. The DOM is used opportunistically at *record* time to derive robust
locators, and at *replay* time to resolve them. Framesets are handled by probing every frame.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
from typing import Any

from PIL import Image
from playwright.async_api import Browser, BrowserContext, Frame, Page, async_playwright

from cua.artifact.schema import Locator, LocatorCandidate, LocatorStrategy
from cua.surface.base import Action, ActResult, Observation, ProbeResult, Resolution

# Image limit for current toolset models (long edge 2576px, ~3.75MP). We stay well inside it.
MAX_LONG_EDGE = 1568
MAX_PIXELS = 1_150_000

# Runs inside the page: describe the element at a point as locator candidates.
_PROBE_JS = r"""
([x, y]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const tag = el.tagName.toLowerCase();
  const role = el.getAttribute('role') || ({a:'link', button:'button', input:(el.type==='submit'||el.type==='button')?'button':'textbox',
                 select:'combobox', textarea:'textbox'}[tag] || null);
  const txt = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 80);
  // nearest label: <label for>, wrapping label, or preceding cell text in table layouts
  let label = null;
  if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) label = l.innerText.trim(); }
  if (!label) { const l = el.closest('label'); if (l) label = l.innerText.replace(txt,'').trim(); }
  if (!label) { const td = el.closest('td'); const prev = td && td.previousElementSibling; if (prev) label = prev.innerText.trim(); }
  if (!label) { let p = el.previousElementSibling; if (p && p.innerText) label = p.innerText.trim(); }
  // structural css path
  const path = []; let n = el;
  while (n && n.nodeType === 1 && path.length < 8) {
    let s = n.tagName.toLowerCase();
    const sib = Array.from(n.parentNode ? n.parentNode.children : []).filter(c => c.tagName === n.tagName);
    if (sib.length > 1) s += `:nth-of-type(${sib.indexOf(n)+1})`;
    path.unshift(s); n = n.parentElement;
  }
  const name = el.getAttribute('name');
  return { tag, role, text: txt, label: label ? label.slice(0,80) : null, name, css: path.join(' > '),
           placeholder: el.getAttribute('placeholder') };
}
"""

_HUMAN_RECORDER_JS = r"""
() => {
  if (window.__cuaRec) return;
  window.__cuaRec = true;
  window.__cuaQueue = [];
  const push = (kind, e) => {
    const t = e.target; if (!t || !t.tagName) return;
    const text = (t.innerText || t.value || t.getAttribute('name') || '').toString().slice(0, 60);
    window.__cuaQueue.push({kind, tag: t.tagName.toLowerCase(), text, x: e.clientX|0, y: e.clientY|0,
                            url: location.href, ts: Date.now()});
  };
  document.addEventListener('click', e => push('click', e), true);
  document.addEventListener('change', e => push('change', e), true);
  document.addEventListener('submit', e => push('submit', e), true);
}
"""


def _fit(width: int, height: int) -> float:
    long_edge = max(width, height)
    return min(1.0, MAX_LONG_EDGE / long_edge, math.sqrt(MAX_PIXELS / (width * height)))


class PlaywrightSurface:
    def __init__(
        self, *, headless: bool = True, viewport: tuple[int, int] = (1280, 800), slow_mo: int = 0
    ) -> None:
        self._headless = headless
        self._viewport = viewport
        self._slow_mo = slow_mo
        self._pw: Any = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self.page: Page | None = None
        self._scale = 1.0
        self._human_cb: Any = None

    @property
    def headless(self) -> bool:
        return self._headless

    # --- lifecycle --------------------------------------------------------------------------
    async def start(self, entry_url: str | None = None) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless, slow_mo=self._slow_mo)
        self._ctx = await self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]}
        )
        self.page = await self._ctx.new_page()
        await self.page.add_init_script(_HUMAN_RECORDER_JS)
        await self.page.expose_function("__cuaHuman", self._on_human)
        if entry_url:
            await self.page.goto(entry_url, wait_until="domcontentloaded")

    async def stop(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    def install_human_recorder(self, callback: Any) -> None:
        self._human_cb = callback

    async def _on_human(self, payload: dict[str, Any]) -> None:
        if self._human_cb:
            self._human_cb(payload)

    # --- perception -------------------------------------------------------------------------
    async def observe(self, *, with_aria: bool = False) -> Observation:
        assert self.page
        raw = await self.page.screenshot(type="png", full_page=False)
        img: Image.Image = Image.open(io.BytesIO(raw))
        scale = _fit(img.width, img.height)
        if scale < 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        self._scale = scale
        aria = None
        if with_aria:
            try:
                aria = await self.page.locator("body").aria_snapshot()
            except Exception:
                aria = None
        return Observation(
            png=buf.getvalue(),
            scale=scale,
            width=img.width,
            height=img.height,
            url=self.page.url,
            title=await self.page.title(),
            aria=aria,
        )

    def _real(self, xy: tuple[int, int]) -> tuple[float, float]:
        return xy[0] / self._scale, xy[1] / self._scale

    async def read_text(self) -> str:
        """Visible text of every frame. Uses evaluate() so a frameset document (no <body>) costs nothing."""
        assert self.page
        parts: list[str] = []
        for fr in self.page.frames:
            try:
                txt = await fr.evaluate("() => document.body ? document.body.innerText : ''")
                if txt:
                    parts.append(str(txt))
            except Exception:  # detached or navigating frame
                continue
        return "\n".join(parts)

    async def current_url(self) -> str:
        assert self.page
        return self.page.url

    async def all_urls(self) -> list[str]:
        """Main URL first, then every frame (framesets navigate frames, not the page)."""
        assert self.page
        return [self.page.url, *[f.url for f in self.page.frames if f != self.page.main_frame]]

    async def drain_human_events(self) -> list[dict[str, Any]]:
        """Collect actions a human performed in any frame since the last drain (used during handoff)."""
        assert self.page
        out: list[dict[str, Any]] = []
        for fr in self.page.frames:
            try:
                got = await fr.evaluate(
                    "() => { const q = window.__cuaQueue || []; window.__cuaQueue = []; return q; }"
                )
                out.extend(dict(x) for x in got)
            except Exception:  # detached or navigating frame
                continue
        out.sort(key=lambda e: e.get("ts", 0))
        return out

    async def dom_snapshot(self) -> str | None:
        assert self.page
        try:
            return await self.page.content()
        except Exception:
            return None

    async def fingerprint(self) -> str:
        """Stable landmarks only: title + form field names + frame names. Drift signal for tenants."""
        assert self.page
        title = await self.page.title()
        names: list[str] = []
        for fr in self.page.frames:
            try:
                names += await fr.evaluate(
                    "() => Array.from(document.querySelectorAll('input,select')).map(e=>e.name||e.type)"
                )
            except Exception:
                continue
        frames = [fr.name for fr in self.page.frames]
        return hashlib.sha256("|".join([title, *sorted(names), *frames]).encode()).hexdigest()[:16]

    # --- record-time probe -------------------------------------------------------------------
    async def probe(self, x: int, y: int) -> ProbeResult:
        assert self.page
        rx, ry = self._real((x, y))
        for fr in self._frames_topdown():
            off = await self._frame_offset(fr)
            info = await fr.evaluate(_PROBE_JS, [rx - off[0], ry - off[1]])
            if not info or info["tag"] in {"html", "body", "frameset", "frame", "iframe"}:
                continue
            return self._candidates(info, fr.name or None, (x, y))
        return ProbeResult(
            candidates=[LocatorCandidate(strategy=LocatorStrategy.COORDS, value=f"{x},{y}", confidence=0.2)],
            description="unknown control",
            text=None,
        )

    def _frames_topdown(self) -> list[Frame]:
        assert self.page
        return [f for f in self.page.frames if f != self.page.main_frame] + [self.page.main_frame]

    async def _frame_offset(self, fr: Frame) -> tuple[float, float]:
        if fr == fr.page.main_frame:
            return 0.0, 0.0
        el = await fr.frame_element()
        box = await el.bounding_box()
        return (box["x"], box["y"]) if box else (0.0, 0.0)

    @staticmethod
    def _candidates(info: dict[str, Any], frame: str | None, xy: tuple[int, int]) -> ProbeResult:
        cands: list[LocatorCandidate] = []
        role, text, label, name = (
            info.get("role"),
            info.get("text"),
            info.get("label"),
            info.get("name"),
        )
        if role and text and role in {"button", "link"}:
            cands.append(
                LocatorCandidate(
                    strategy=LocatorStrategy.ROLE_NAME,
                    value=f"{role}|{text}",
                    confidence=0.95,
                    frame=frame,
                )
            )
        if label and role in {"textbox", "combobox"}:
            cands.append(
                LocatorCandidate(
                    strategy=LocatorStrategy.LABEL_RELATIVE,
                    value=label,
                    confidence=0.9,
                    frame=frame,
                )
            )
        if name:
            cands.append(
                LocatorCandidate(
                    strategy=LocatorStrategy.CSS_STRUCTURAL,
                    value=f'[name="{name}"]',
                    confidence=0.85,
                    frame=frame,
                    note="form field name - stable in server-rendered apps",
                )
            )
        if text and role not in {"textbox", "combobox"}:
            cands.append(
                LocatorCandidate(strategy=LocatorStrategy.TEXT, value=text, confidence=0.75, frame=frame)
            )
        if info.get("placeholder"):
            cands.append(
                LocatorCandidate(
                    strategy=LocatorStrategy.LABEL_RELATIVE,
                    value=f"placeholder:{info['placeholder']}",
                    confidence=0.7,
                    frame=frame,
                )
            )
        cands.append(
            LocatorCandidate(
                strategy=LocatorStrategy.CSS_STRUCTURAL,
                value=info["css"],
                confidence=0.5,
                frame=frame,
            )
        )
        cands.append(
            LocatorCandidate(
                strategy=LocatorStrategy.COORDS,
                value=f"{xy[0]},{xy[1]}",
                confidence=0.2,
                frame=frame,
                note="viewport-dependent fallback",
            )
        )
        desc = f"{role or info['tag']} '{text or label or name or info['css']}'"
        return ProbeResult(candidates=cands, description=desc, text=text, tag=info["tag"], frame=frame)

    # --- replay-time resolve -----------------------------------------------------------------
    async def resolve(self, locator: Locator, timeout_ms: int) -> Resolution | None:
        assert self.page
        per = max(500, timeout_ms // max(1, len(locator.candidates)))
        for idx, cand in enumerate(locator.candidates):
            fr = self._frame_by_name(cand.frame)
            if fr is None:
                continue
            loc = self._to_playwright(fr, cand)
            if loc is None:
                continue
            try:
                await loc.first.wait_for(state="visible", timeout=per)
                if await loc.count() != 1 and cand.strategy != LocatorStrategy.COORDS:
                    continue  # ambiguity is a resolution failure, not a guess
                txt = (
                    (await loc.first.inner_text(timeout=500))
                    if cand.strategy != LocatorStrategy.COORDS
                    else None
                )
                return Resolution(handle=loc.first, candidate=cand, candidate_index=idx, text=txt)
            except Exception:
                continue
        return None

    def _frame_by_name(self, name: str | None) -> Frame | None:
        assert self.page
        if not name:
            return self.page.main_frame
        return next((f for f in self.page.frames if f.name == name), None)

    def _to_playwright(self, fr: Frame, c: LocatorCandidate) -> Any:
        s = c.strategy
        if s == LocatorStrategy.ROLE_NAME:
            role, _, nm = c.value.partition("|")
            return fr.get_by_role(role, name=nm, exact=True)  # type: ignore[arg-type]
        if s == LocatorStrategy.LABEL_RELATIVE:
            if c.value.startswith(("xpath=", "css=")):
                return fr.locator(c.value)
            if c.value.startswith("placeholder:"):
                return fr.get_by_placeholder(c.value.split(":", 1)[1])
            # table-layout legacy apps: label is the previous cell → walk to the next control
            return fr.locator(
                f"xpath=//*[normalize-space(text())={_xq(c.value)}]/following::*[self::input or self::select or self::textarea][1]"
            )
        if s == LocatorStrategy.TEXT:
            return fr.get_by_text(c.value, exact=True)
        if s == LocatorStrategy.CSS_STRUCTURAL:
            return fr.locator(c.value)
        if s == LocatorStrategy.COORDS:
            return None  # handled by act() with coordinates
        return None

    # --- acting -----------------------------------------------------------------------------
    async def act(self, a: Action) -> ActResult:
        assert self.page
        p = self.page
        try:
            match a.name:
                case "screenshot":
                    return ActResult(True, png=(await self.observe()).png)
                case "zoom":
                    assert a.region
                    x0, y0, x1, y1 = (v / self._scale for v in a.region)
                    raw = await p.screenshot(clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
                    return ActResult(True, png=raw)
                case "left_click" | "double_click" | "right_click" | "triple_click":
                    if a.handle is not None:
                        await a.handle.click(
                            click_count={"double_click": 2, "triple_click": 3}.get(a.name, 1),
                            button="right" if a.name == "right_click" else "left",
                        )
                    else:
                        assert a.coordinate
                        x, y = self._real(a.coordinate)
                        await p.mouse.click(
                            x,
                            y,
                            click_count={"double_click": 2, "triple_click": 3}.get(a.name, 1),
                            button="right" if a.name == "right_click" else "left",
                        )
                    return ActResult(True)
                case "mouse_move":
                    assert a.coordinate
                    await p.mouse.move(*self._real(a.coordinate))
                    return ActResult(True)
                case "left_click_drag":
                    assert a.start_coordinate and a.coordinate
                    await p.mouse.move(*self._real(a.start_coordinate))
                    await p.mouse.down()
                    await p.mouse.move(*self._real(a.coordinate))
                    await p.mouse.up()
                    return ActResult(True)
                case "type":
                    assert a.text is not None
                    if a.handle is not None:
                        await a.handle.fill(a.text)
                    else:
                        await p.keyboard.type(a.text, delay=10)
                    return ActResult(True)
                case "select":
                    assert a.handle is not None and a.text is not None
                    await a.handle.select_option(label=a.text)
                    return ActResult(True)
                case "key":
                    assert a.text
                    for _ in range(max(1, a.repeat)):
                        await p.keyboard.press(_map_key(a.text))
                    return ActResult(True)
                case "scroll":
                    if a.coordinate:
                        await p.mouse.move(*self._real(a.coordinate))
                    dx, dy = {"down": (0, 1), "up": (0, -1), "right": (1, 0), "left": (-1, 0)}[
                        a.scroll_direction or "down"
                    ]
                    await p.mouse.wheel(dx * 100 * a.scroll_amount, dy * 100 * a.scroll_amount)
                    return ActResult(True)
                case "wait":
                    await asyncio.sleep(min(a.duration, 30))
                    return ActResult(True)
                case "navigate":
                    assert a.url
                    await p.goto(a.url, wait_until="domcontentloaded")
                    return ActResult(True)
                case "extract":
                    assert a.handle is not None
                    return ActResult(True, extracted=(await a.handle.inner_text()).strip())
                case "cursor_position":
                    return ActResult(True, message="X=0, Y=0")
                case _:
                    return ActResult(False, error=f"unsupported action {a.name}")
        except Exception as e:
            return ActResult(False, error=f"{type(e).__name__}: {e}")


def _xq(s: str) -> str:
    """Quote a string for XPath."""
    if "'" not in s:
        return f"'{s}'"
    parts = s.split("'")
    return "concat(" + ', "\'", '.join(f"'{p}'" for p in parts) + ")"


def _map_key(k: str) -> str:
    table = {
        "Return": "Enter",
        "ctrl": "Control",
        "alt": "Alt",
        "shift": "Shift",
        "super": "Meta",
        "space": " ",
    }
    return "+".join(table.get(part, part) for part in k.split("+"))
