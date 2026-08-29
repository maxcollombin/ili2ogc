"""Errors and diagnostic objects for the ModelBuilder."""

from typing import Any


class BuildError(Exception):
    """Unrecoverable build error.

    Raised for a malformed binding, a value not found on the ANTLR ctx, an
    unknown association/role, etc. Carries the grammar rule and, when
    available, the source position (line/column of the ctx's start token)
    for an actionable diagnostic instead of an opaque Python crash.
    """

    def __init__(self, message: str, *, rule: str | None = None, ctx: Any = None):
        self.rule = rule
        self.ctx = ctx
        location = ""
        start = getattr(ctx, "start", None)
        if start is not None:
            location = f" (line {start.line}, column {start.column})"
        prefix = f"[{rule}]" if rule else ""
        super().__init__(f"{prefix}{location} {message}".strip())


class UnresolvedNamedReference:
    """A named reference that couldn't be resolved.

    A reference into an imported model (IMPORTS) becomes this explicit,
    documented object instead of an exception or a silent None. This is
    both the default behavior (no ModelRepository configured) and the
    fallback for any model genuinely not found even with a repository
    (outside the given director(y/ies), file with a syntax error, circular
    import not fully resolved) - see ModelRepository (repository.py) for
    actual multi-file resolution.
    """

    __slots__ = ("name", "reason")

    def __init__(self, name: str, reason: str = "external_import"):
        self.name = name
        self.reason = reason

    def __repr__(self) -> str:
        return f"<UnresolvedNamedReference {self.name!r} reason={self.reason!r}>"

    def __eq__(self, other):
        return isinstance(other, UnresolvedNamedReference) and (self.name, self.reason) == (other.name, other.reason)
