"""Compile a built `CONSTRAINT`'s Expression tree to a CQL2-JSON filter (OGC API - Features Part 3: Filtering).

Mirrors `constraint_eval.py`'s exact evaluated subset (relational
operators, `And`/`Or`/`Not`/`Implication`, `DEFINED(...)`, a plain
possibly-multi-hop attribute path, constants) - a structural transform of
the SAME already-built `Expression` AST nodes that module evaluates
against a Feature's `properties` dict, not a second implementation with
its own scope. Anything `constraint_eval.py` itself raises
`UnsupportedExpressionError` on (arithmetic, `THIS`/`PARENT`, aggregate/
function-call context, indexed paths, INSPECTION-based factors) is out of
scope here too, raised the same way - "zero silent loss": never emit a
syntactically valid but semantically wrong/truncated CQL2-JSON filter.

JSON shapes (verified against the OGC CQL2 standard, 21-065r2, not
guessed): a comparison is `{"op": "=", "args": [{"property": "x"}, v]}`;
`and`/`or`/`not` wrap predicates the same way (`{"op": "and", "args":
[...]}`); `isNull` is `{"op": "isNull", "args": [{"property": "x"}]}`,
negated via a `not` wrapper for `DEFINED(x)`; a property reference is
`{"property": "name"}`, a multi-hop STRUCTURE path dotted
(`"struct.sub"`); a named-function extension point (e.g. a hypothetical
`interlis_mod` for `MOD`, not implemented here) uses the SAME
`{"op": "<name>", "args": [...]}` shape as any built-in operator - no
separate `"function"` wrapper.

`UNIQUE`/`SetConstraint`/`ExistenceConstraint` and the percentage-based
plausibility form (`Kind` `LowPercC`/`HighPercC`) are population/
basket-level checks - categorically not a per-Feature CQL2 filter
(`cql2_unsupported_reason`), not a gap to close later.
"""

from typing import Any

from interlis.convert.constraint_eval import (
    UnsupportedExpressionError,
    _evaluate_constant,
)
from interlis.metamodel.instance import MetaInstance

_RELATIONAL_OPS = {
    "Equal": "=",
    "NotEqual": "<>",
    "Less": "<",
    "Greater": ">",
    "LessOrEqual": "<=",
    "GreaterOrEqual": ">=",
}


def _describe_path(path_els: list[MetaInstance]) -> str:
    return "->".join(str(getattr(el, "Ref", None) or "?") for el in path_els)


def _path_property_name(path_els: list[MetaInstance]) -> str:
    """Dotted CQL2 `property` name for a `PathEls` list - same node validation as `constraint_eval._resolve_path`.

    A STATIC check (no `properties` dict to walk against - unlike
    `_resolve_path`, this never learns whether the path actually exists in
    a given Feature, only whether its SHAPE is CQL2-expressible): each hop
    must be a plain `Attribute`/`ReferenceAttr` `PathEl` with no
    `[FIRST]`/`[LAST]`/`[n]` index - the same restriction
    `constraint_eval.py` applies at evaluation time, applied here at
    compile time instead.
    """
    names = []
    for path_el in path_els:
        kind = getattr(path_el, "Kind", None)
        if kind not in ("ReferenceAttr", "Attribute"):
            raise UnsupportedExpressionError(
                f"path element kind {kind!r} needs object-graph context beyond a single Feature's properties",
            )
        if getattr(path_el, "NumIndex", None) is not None or getattr(path_el, "SpecIndex", None) is not None:
            raise UnsupportedExpressionError("indexed path elements ([FIRST]/[LAST]/[n]) are not supported")
        names.append(str(getattr(path_el, "Ref", None)))
    return ".".join(names)


def to_cql2(expr: MetaInstance) -> Any:
    """Compile one `Expression` node to its CQL2-JSON value.

    A predicate (`CompoundExpr`/`UnaryExpr`) or property reference
    (`PathOrInspFactor`) returns a `dict`; a `Constant` returns the bare
    JSON literal (`bool`/`int`/`float`/`str`) - CQL2-JSON's `"args"` array
    is itself a mix of both shapes (e.g. `{"op": "=", "args":
    [{"property": "x"}, 5]}`), so callers assembling a larger filter feed
    this straight into their own `"args"` list without unwrapping.

    Raises `UnsupportedExpressionError` for anything
    `constraint_eval.evaluate_expression` itself couldn't evaluate against
    a single Feature's `properties` - same scope, same exception type.
    """
    qualified = expr._qualified_class
    if qualified.endswith("CompoundExpr"):
        return _compile_compound(expr)
    if qualified.endswith("UnaryExpr"):
        return _compile_unary(expr)
    if qualified.endswith("PathOrInspFactor"):
        return _compile_path_factor(expr)
    if qualified.endswith("Constant"):
        return _compile_constant(expr)
    raise UnsupportedExpressionError(
        f"expression node {qualified} needs THIS/PARENT/aggregate/function-call context "
        f"beyond a single Feature's properties",
    )


def _compile_compound(expr: MetaInstance) -> dict[str, Any]:
    op = expr.Operation
    subs = expr.SubExpressions
    if op in _RELATIONAL_OPS:
        if len(subs) != 2:
            raise UnsupportedExpressionError(f"relational operator {op!r} needs exactly 2 operands, got {len(subs)}")
        return {"op": _RELATIONAL_OPS[op], "args": [to_cql2(subs[0]), to_cql2(subs[1])]}
    if op == "And":
        return {"op": "and", "args": [to_cql2(sub) for sub in subs]}
    if op == "Or":
        return {"op": "or", "args": [to_cql2(sub) for sub in subs]}
    if op == "Implication":
        # CQL2 has no direct implication operator - `A => B` == `NOT A OR B`.
        if len(subs) != 2:
            raise UnsupportedExpressionError("implication needs exactly 2 operands")
        return {"op": "or", "args": [{"op": "not", "args": [to_cql2(subs[0])]}, to_cql2(subs[1])]}
    raise UnsupportedExpressionError(
        f"operator {op!r} needs numeric-domain context beyond boolean constraint evaluation",
    )


def _compile_unary(expr: MetaInstance) -> dict[str, Any]:
    op = expr.Operation
    if op == "Not":
        return {"op": "not", "args": [to_cql2(expr.SubExpression)]}
    if op == "Defined":
        sub = expr.SubExpression
        if sub is None or not sub._qualified_class.endswith("PathOrInspFactor"):
            raise UnsupportedExpressionError("DEFINED(...) is only supported for a plain attribute path")
        is_null = {"op": "isNull", "args": [_compile_path_factor(sub)]}
        return {"op": "not", "args": [is_null]}
    raise UnsupportedExpressionError(f"unary operator {op!r} is not supported")


def _compile_path_factor(expr: MetaInstance) -> dict[str, Any]:
    if getattr(expr, "Inspection", None):
        raise UnsupportedExpressionError("INSPECTION-based path factors are not supported")
    return {"property": _path_property_name(expr.PathEls)}


def _compile_constant(expr: MetaInstance) -> Any:
    """`_evaluate_constant`'s value, with `#true`/`#false` coerced to a real JSON boolean.

    `#true`/`#false` build as `Type="Enumeration"` with a literal
    `"true"`/`"false"` string `Value` - `constraint_eval.py`'s own
    `_as_bool` already treats any such string as boolean-coercible
    (deliberately not distinguishing it from a same-spelled domain enum
    value, since a BOOLEAN comparison and a TEXT-domain one look identical
    at this AST level) - mirrored here so `KBfrei == #false` compiles to
    `{"op": "=", "args": [{"property": "KBfrei"}, false]}` (a proper JSON
    boolean), not the string `"false"`.
    """
    value = _evaluate_constant(expr)
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def cql2_unsupported_reason(constraint: MetaInstance) -> str | None:
    """Why `constraint_to_cql2(constraint)` would raise, or `None` if it's in scope.

    Mirrors `check_feature_constraints`'s own `SimpleConstraint` gate
    (`constraint_eval.py`): only a plain `SimpleConstraint` with `Kind`
    `None`/`"MandC"` and no `Percentage` is a per-Feature filter at all -
    `UniqueConstraint`/`SetConstraint`/`ExistenceConstraint` and the
    percentage-based plausibility form are population/basket-level checks,
    categorically not a CQL2 filter (`not_applicable`, not a gap - see
    module docstring).
    """
    qualified = constraint._qualified_class
    if not qualified.endswith("SimpleConstraint"):
        kind = qualified.rsplit(".", 1)[-1]
        return f"{kind} is a population/basket-level check, not a per-Feature CQL2 filter"
    if getattr(constraint, "Kind", None) not in (None, "MandC"):
        return (
            f"plausibility constraint (Kind={constraint.Kind!r}) needs statistical population "
            "context, not a boolean filter"
        )
    if getattr(constraint, "Percentage", None) is not None:
        return "percentage-based plausibility constraint needs population context, not a boolean filter"
    if getattr(constraint, "LogicalExpression", None) is None:
        return "no LogicalExpression to compile"
    return None


def constraint_to_cql2(constraint: MetaInstance) -> dict[str, Any]:
    """CQL2-JSON filter for one per-Feature `SimpleConstraint`'s `LogicalExpression`.

    Raises `ValueError` if `cql2_unsupported_reason(constraint)` isn't
    `None` - callers (e.g. a future CLI subcommand) are expected to check
    that first and skip with a diagnostic, same contract as
    `convert/jsonfg.py`'s `evaluate_view`/`unsupported_view_reason`.
    Raises `UnsupportedExpressionError` for any `Expression` node outside
    `constraint_eval.py`'s own evaluated subset - never a truncated or
    silently-wrong filter.
    """
    reason = cql2_unsupported_reason(constraint)
    if reason is not None:
        raise ValueError(f"cannot compile constraint to CQL2: {reason}")
    return to_cql2(constraint.LogicalExpression)


__all__ = [
    "UnsupportedExpressionError",
    "constraint_to_cql2",
    "cql2_unsupported_reason",
    "to_cql2",
]
