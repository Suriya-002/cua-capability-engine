"""structlog setup: JSON in containers, pretty in a terminal. Redaction is applied as a processor."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from cua.policy.redaction import Redactor

_redactor: Redactor | None = None


def install_redactor(redactor: Redactor) -> None:
    global _redactor
    _redactor = redactor


def _redact_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    if _redactor is None:
        return event_dict
    out: dict[str, Any] = _redactor.redact_obj(dict(event_dict))
    return out


def configure(json: bool | None = None) -> None:
    use_json = json if json is not None else not sys.stderr.isatty()
    renderer: Any = structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
