from __future__ import annotations

from pathlib import Path

import pytest

from cua.policy.redaction import Redactor


def test_literal_and_pattern_masking() -> None:
    r = Redactor(["sk-ant-abcdefghijklmnop", "10023"])
    s = r.redact("key=sk-ant-abcdefghijklmnop member=10023 ssn=123-45-6789 mail=a.b@x.io")
    assert "sk-ant" not in s and "10023" not in s and "123-45-6789" not in s and "a.b@x.io" not in s
    assert "[REDACTED" in s


def test_nested_objects() -> None:
    r = Redactor(["secret-value"])
    out = r.redact_obj({"a": ["secret-value", {"b": "x secret-value y"}], "n": 3})
    assert out == {"a": ["[REDACTED]", {"b": "x [REDACTED] y"}], "n": 3}


def test_leak_check_dir(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text('{"x": "[REDACTED]"}')
    r = Redactor(["canary-XYZ"])
    r.assert_clean_dir(tmp_path)
    (tmp_path / "bad.jsonl").write_text('{"x": "canary-XYZ"}')
    with pytest.raises(AssertionError, match="leaked"):
        r.assert_clean_dir(tmp_path)


def test_short_literals_ignored() -> None:
    r = Redactor(["ab"])  # too short to be meaningful; must not nuke every 'ab'
    assert r.redact("abstract") == "abstract"


def test_capability_ref_is_not_an_email() -> None:
    r = Redactor()
    assert r.redact("capability=lookup_member_balance@1.0.0") == "capability=lookup_member_balance@1.0.0"
    assert "[REDACTED:email]" in r.redact("contact ops@bank.example")
