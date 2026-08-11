"""Generic reader for an ANTLR4-Python context's accessors.

By signature introspection (not via generated/ast-accessors.yml, found
incomplete for some rules - e.g. attributeDef only has a placeholder
`'...': null`). ANTLR4-Python convention, confirmed against the real
generated code (src/interlis/antlr/InterlisParser.py): an accessor that
can match multiple times (repeated rule, repeated token) takes an
optional `i: int = None` parameter (None -> full list via
getTypedRuleContexts/getTokens, an int -> the element at that position);
an accessor guaranteed unique takes NO parameter. Introspecting the bound
signature (len(parameters) == 0 vs 1) reliably distinguishes the two
cases, without depending on an external cache.
"""
import inspect
from typing import Any


def has_accessor(ctx: Any, field: str) -> bool:
    return callable(getattr(ctx, field, None))


def call(ctx: Any, field: str, index: int | None = None) -> Any:
    """Call ctx.<field>(...), respecting its real signature.

    Raises AttributeError if the accessor doesn't exist on this ctx,
    TypeError if a nonzero index is requested on a "single" accessor
    (guaranteed unique) - index=0 is tolerated in that case (equivalent to
    "the only/first match"), since the spec doesn't always explicitly
    distinguish single/multi.
    """
    method = getattr(ctx, field, None)
    if method is None or not callable(method):
        raise AttributeError(f"{type(ctx).__name__} has no accessor {field!r}")
    accepts_index = len(inspect.signature(method).parameters) > 0
    if index is not None:
        if not accepts_index:
            if index == 0:
                return method()
            raise TypeError(f"{field} on {type(ctx).__name__} does not accept an index (single accessor)")
        if index < 0:
            # Python-style index (-1 = last element): the generated ANTLR
            # accessor (getTypedRuleContext(s)/getToken(s)) only understands
            # positive 0-based indices (returns None without error for a
            # negative index, never translated to "from the end") - go
            # through the full list to benefit from normal Python slicing.
            # Without this, index: -1 always returns None instead of the
            # real last expression() (see setConstraint.Constraint, which
            # relies on this for `expression(index=-1)`).
            values = method()
            values = list(values) if values is not None else []
            return values[index] if -len(values) <= index < len(values) else None
        return method(index)
    return method()


def call_list(ctx: Any, field: str) -> list:
    """Return a uniform list regardless of accessor arity.

    A "multi" accessor with no index already returns its full list (0..N
    elements); a "single" accessor returns 0 or 1 element - both are
    normalized to a list.
    """
    method = getattr(ctx, field, None)
    if method is None or not callable(method):
        raise AttributeError(f"{type(ctx).__name__} n'a pas d'accesseur {field!r}")
    accepts_index = len(inspect.signature(method).parameters) > 0
    if accepts_index:
        result = method()
        return list(result) if result is not None else []
    result = method()
    return [] if result is None else [result]


def is_present(ctx: Any, field: str, index: int | None = None) -> bool:
    """Check whether the accessor matched anything.

    On a "multi" accessor (one that accepts `i: int = None`), calling
    `call(ctx, field, None)` with no explicit index returns its full list
    (`getTokens`/`getTypedRuleContexts`) - always a list, never `None`,
    even when it's empty (no occurrence). A plain `is not None` test would
    wrongly treat that as "present" unconditionally as soon as an accessor
    becomes multi: this matters because the grammar encodes `Properties<>`
    as comma-separated lists (e.g. `CLASS X (ABSTRACT,FINAL)`), which makes
    ABSTRACT/FINAL/TRANSIENT/... multi (repeatable inside the `(COMMA ...)*`
    group) - `presence: true` bindings without an index on those tokens
    (03_classes_and_structures.yml/04_attributes.yml/etc.) would then
    evaluate to `True` unconditionally.
    """
    result = call(ctx, field, index)
    if isinstance(result, list):
        return len(result) > 0
    return result is not None


def text(ctx: Any, field: str, index: int | None = None) -> str | None:
    """Return an accessor's raw text (token or rule), or None if absent.

    Calls .getText() on the returned node. INTERLIS STRING literals keep
    their quotes in getText() - unquoting is the caller's responsibility
    (source_resolver), not this generic layer's.
    """
    node = call(ctx, field, index)
    return None if node is None else node.getText()
