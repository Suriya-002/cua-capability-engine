from __future__ import annotations

import json

from cua.evidence.writer import EvidenceWriter, verify_chain


def test_chain_verifies_and_detects_tamper(evidence: EvidenceWriter) -> None:
    evidence.event("a", x=1)
    evidence.event("b", secret="canary-secret-XYZ")
    evidence.event("c")
    events = evidence.dir / "events.jsonl"
    ok, n = verify_chain(events)
    assert ok and n == 3
    # redaction happened before hashing
    assert "canary-secret-XYZ" not in events.read_text()
    # tamper with the middle line
    lines = events.read_text().splitlines()
    mid = json.loads(lines[1])
    mid["x"] = "edited"
    lines[1] = json.dumps(mid)
    events.write_text("\n".join(lines) + "\n")
    ok, n = verify_chain(events)
    assert not ok and n == 1


def test_summary_written(evidence: EvidenceWriter) -> None:
    evidence.event("x")
    p = evidence.summary(status="ok")
    data = json.loads(p.read_text())
    assert data["events"] == 1 and data["status"] == "ok" and len(data["final_hash"]) == 64
