"""Redaction of secrets and PII before anything is persisted or logged.

Two mechanisms:
1. Literal masking — every registered sensitive value (API key, PII inputs) is replaced wherever
   it appears, including inside nested dicts/lists and URLs.
2. Pattern masking — SSN, card numbers, emails, bearer tokens.

`assert_clean` is used by the CI leak test: run a discovery+replay with a canary secret, then assert
it appears in no file under evidence/.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "bearer": re.compile(r"(?i)bearer\s+[a-z0-9._-]{16,}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
}


class Redactor:
    def __init__(
        self, literals: Iterable[str] = (), patterns: dict[str, re.Pattern[str]] | None = None
    ) -> None:
        self._literals = sorted({v for v in literals if v and len(v) >= 3}, key=len, reverse=True)
        self._patterns = patterns if patterns is not None else PATTERNS

    def register(self, *values: str) -> None:
        for v in values:
            if v and len(v) >= 3 and v not in self._literals:
                self._literals.append(v)
        self._literals.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        for lit in self._literals:
            text = text.replace(lit, "[REDACTED]")
        for name, pat in self._patterns.items():
            text = pat.sub(f"[REDACTED:{name}]", text)
        return text

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return type(obj)(self.redact_obj(v) for v in obj)
        return obj

    def leaks(self, text: str) -> list[str]:
        found = [lit for lit in self._literals if lit in text]
        found += [name for name, pat in self._patterns.items() if pat.search(text)]
        return found

    def assert_clean_dir(
        self,
        root: Path,
        suffixes: tuple[str, ...] = (".json", ".jsonl", ".log", ".txt", ".md", ".html"),
    ) -> None:
        offenders: list[tuple[Path, list[str]]] = []
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in suffixes:
                hits = self.leaks(p.read_text(encoding="utf-8", errors="ignore"))
                if hits:
                    offenders.append((p, hits))
        if offenders:
            detail = "; ".join(f"{p}: {h}" for p, h in offenders)
            raise AssertionError(f"sensitive data leaked into evidence: {detail}")
