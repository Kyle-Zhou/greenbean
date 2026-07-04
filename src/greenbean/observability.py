"""Structured logging for greenbean.

One place to configure Python's ``logging`` for the CLI, plus a small helper the
generation loop uses to tag every line with the doc it is working on. The goal
(Architecture §Logging) is per-job structured logs — tools called, tokens
consumed, failures — so that when something goes wrong with a doc we can
reconstruct what the agent saw.

Design notes:

- Logs go to **stderr** so they never mix with a command's stdout (progress
  lines, piped doc output, the viewer link).
- Library modules just do ``logging.getLogger("greenbean.<component>")`` and
  stay silent until the CLI attaches a handler via :func:`configure_logging`.
- ``configure_logging`` is idempotent — ``watch`` re-runs the pipeline in-process
  and tests call it repeatedly; neither should stack duplicate handlers.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

ROOT_LOGGER = "greenbean"


class _Formatter(logging.Formatter):
    """``HH:MM:SS LEVEL    message`` — compact and readable in a terminal."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


def configure_logging(verbose: bool = False, *, stream: TextIO | None = None) -> None:
    """Attach greenbean's log handler. Safe to call more than once.

    ``verbose`` selects DEBUG (per-turn detail, full tool inputs/outputs) over
    the default INFO (one line per tool call, plus warnings/errors).
    """
    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers:
        if getattr(handler, "_greenbean", False):
            handler.setLevel(level)
            return
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler._greenbean = True  # type: ignore[attr-defined]
    handler.setLevel(level)
    handler.setFormatter(_Formatter())
    logger.addHandler(handler)


class _DocAdapter(logging.LoggerAdapter[logging.Logger]):
    """Prefixes every record with ``[<doc path>]`` for per-doc lineage."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        doc = self.extra["doc"] if self.extra else "?"
        return f"[{doc}] {msg}", kwargs


def doc_logger(doc_path: str) -> logging.LoggerAdapter[logging.Logger]:
    """A logger whose lines are tagged with the doc currently being generated."""
    return _DocAdapter(logging.getLogger(f"{ROOT_LOGGER}.generator"), {"doc": doc_path})
