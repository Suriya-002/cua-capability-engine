# ADR-0001: Pixel-first discovery, DOM-probed locators, locator-first replay
Date: 2026-09-18 · Status: accepted

## Context
The brief's common case is a surface with no clean DOM (framesets, table layouts, native desktop). Anthropic ships
two GA toolsets: `computer_toolset_20260801` (screenshots + coordinates) and `browser_toolset_20260801`
(accessibility tree + `ref_N` element handles, ~6.6k tokens of definitions vs ~4.5k).

## Decision
Discovery uses the **computer toolset** so the loop is identical on web, legacy web, and desktop. At record time we
**probe** whatever structure the surface can expose under the clicked point (DOM today, UIA on desktop) and store a
*ranked list* of locator candidates. Replay resolves candidates in order and falls back to coordinates last.

## Consequences
+ One agent loop for every surface; the seam is `Surface.probe()`/`resolve()`.
+ Replay is robust where structure exists and still possible where it doesn't.
− Discovery is slower and costlier than the browser toolset on clean web apps.
− Coordinate fallback is viewport-dependent; we record the viewport and flag COORDS use as drift.

## Alternatives
Browser toolset only (rejected: assumes a DOM); both toolsets in one request (deferred: "next steps").
