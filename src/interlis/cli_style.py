"""CLI presentation: process exit codes and colored stderr messages.

Kept separate from `interlis.cli` so the two exit-code axes never blur
together at the call site: `interlis.diagnostics.DiagnosticBag.exit_code()`
(0 clean / 2 degraded / 1 failed, see docs/diagnostics.md) already owns
0/1/2 for every command that produces a `DiagnosticBag` - `ExitCode`
mirrors those three values under a name rather than redefining them, and
only ADDS 3/5 for the hard stops that happen before any bag exists (bad
invocation, a named file/resource not found, an input file that fails to
parse) - previously all collapsed into a bare `return 1`, indistinguishable
from a real diagnostics failure.
"""

from __future__ import annotations

import os
import sys
from enum import IntEnum
from typing import TextIO


class ExitCode(IntEnum):
    """Process exit codes for `interlis.cli.main`.

    0/1/2 are exactly `DiagnosticBag.exit_code()`'s values (never
    repurpose them - see docs/diagnostics.md). 3-5 classify a hard stop
    that happens before any `DiagnosticBag` is even built.
    """

    OK = 0
    ERROR = 1  # DiagnosticBag: an `error` diagnostic, or --strict with any diagnostic
    DEGRADED = 2  # DiagnosticBag: completed, only note/warning diagnostics
    USAGE = 3  # invocation is missing what it needs (e.g. no --model and no --repo to auto-detect one)
    NOT_FOUND = 4  # a named file, or a resource looked up in --repo/--catalog/TRANSLATION, doesn't exist
    INVALID = 5  # an input file exists but fails to parse (.ili/.xtf syntax errors)


_PALETTE = {"error": "31", "warning": "33"}


def use_color(stream: TextIO) -> bool:
    """`NO_COLOR` (https://no-color.org) always wins; otherwise color only when `stream` is a real terminal.

    Also used by `interlis.cli._finish` to decide `diagnostics.render_text`'s `color=`, so a `DiagnosticBag`
    dump and this module's own `error`/`warn` lines honor `NO_COLOR` the same way.
    """
    return "NO_COLOR" not in os.environ and stream.isatty()


def _paint(text: str, code: str, stream: TextIO) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color(stream) else text


def error(message: str, *, stream: TextIO | None = None) -> None:
    """Print `message` to `stream` (default `sys.stderr`) prefixed `error:` (red on a real terminal).

    `stream` defaults to `None`, resolved to `sys.stderr` INSIDE the body rather than as the parameter's
    default value - a default value is bound once at import time, so it would miss a test's `capsys`/
    `monkeypatch` swap of `sys.stderr` done later.
    """
    stream = stream if stream is not None else sys.stderr
    print(f"{_paint('error', _PALETTE['error'], stream)}: {message}", file=stream)


def warn(message: str, *, stream: TextIO | None = None) -> None:
    """Print `message` to `stream` (default `sys.stderr`) prefixed `warning:` (yellow on a real terminal).

    See `error`'s docstring for why `stream` defaults to `None` rather than `sys.stderr` directly.
    """
    stream = stream if stream is not None else sys.stderr
    print(f"{_paint('warning', _PALETTE['warning'], stream)}: {message}", file=stream)
