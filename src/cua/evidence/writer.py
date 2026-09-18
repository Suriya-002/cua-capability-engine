"""Tamper-evident evidence for every run.

Layout: evidence/<kind>-<run_id>/
    events.jsonl          hash-chained structured log (what happened, why, who was in control)
    shots/NNN-<name>.png  screenshot per step
    failure/dom.html      DOM dump on failure (redacted)
    failure/aria.txt      accessibility snapshot on failure
    summary.json          run header + result

Every event is redacted before it is written. Each line carries sha256(prev_hash + canonical(event)).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.policy.redaction import Redactor

GENESIS = "0" * 64


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


class EvidenceWriter:
    def __init__(self, root: Path, kind: str, redactor: Redactor, run_id: str | None = None) -> None:
        self.run_id = run_id or new_run_id()
        self.kind = kind
        self.dir = root / f"{kind}-{self.run_id}"
        (self.dir / "shots").mkdir(parents=True, exist_ok=True)
        self._events = self.dir / "events.jsonl"
        self._redactor = redactor
        self._seq = 0
        self._prev = GENESIS
        self._t0 = datetime.now(UTC)

    # --- events ---------------------------------------------------------------------------
    def event(self, type_: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        body: dict[str, Any] = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "type": type_,
            **fields,
        }
        body = self._redactor.redact_obj(body)
        digest = hashlib.sha256((self._prev + _canonical(body)).encode()).hexdigest()
        line = {**body, "prev_hash": self._prev, "hash": digest}
        with self._events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._prev = digest
        return line

    # --- artefacts ------------------------------------------------------------------------
    def screenshot(self, png: bytes, name: str) -> Path:
        p = self.dir / "shots" / f"{self._seq:03d}-{name}.png"
        p.write_bytes(png)
        return p

    def failure_snapshot(self, dom_html: str | None, aria: str | None) -> list[Path]:
        out: list[Path] = []
        fdir = self.dir / "failure"
        fdir.mkdir(exist_ok=True)
        if dom_html is not None:
            p = fdir / "dom.html"
            p.write_text(self._redactor.redact(dom_html), encoding="utf-8")
            out.append(p)
        if aria is not None:
            p = fdir / "aria.txt"
            p.write_text(self._redactor.redact(aria), encoding="utf-8")
            out.append(p)
        return out

    def summary(self, **fields: Any) -> Path:
        body = {
            "run_id": self.run_id,
            "kind": self.kind,
            "started_at": self._t0.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "duration_ms": int((datetime.now(UTC) - self._t0).total_seconds() * 1000),
            "events": self._seq,
            "final_hash": self._prev,
            **fields,
        }
        p = self.dir / "summary.json"
        p.write_text(
            json.dumps(self._redactor.redact_obj(body), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return p


def verify_chain(events_path: Path) -> tuple[bool, int]:
    """Return (ok, checked_count). Recomputes every hash; any edit breaks the chain."""
    prev = GENESIS
    n = 0
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        line = json.loads(raw)
        stored_hash = line.pop("hash")
        stored_prev = line.pop("prev_hash")
        if stored_prev != prev:
            return False, n
        if hashlib.sha256((prev + _canonical(line)).encode()).hexdigest() != stored_hash:
            return False, n
        prev = stored_hash
        n += 1
    return True, n
