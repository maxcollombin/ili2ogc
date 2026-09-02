"""CONSTRAINT/WHERE `Expression` tree -> SQL boolean text.

`_expression_to_sql` (row-local `MANDATORY CONSTRAINT` -> `CHECK`, used by
`tables.py`) and `views.py`'s own VIEW `WHERE` translator share this
module's path-resolution and literal helpers rather than duplicating them
- a leaf module (only `tables.py`/`views.py` call into it), no other
submodule of this package needed to build it.
"""

from __future__ import annotations

from interlis.convert.constraint_eval import _unquote_text
from interlis.metamodel.instance import MetaInstance

from .identifiers import _quote, _sql_identifier

# STRUCTURE-in-STRUCTURE levels flattened inline as `<a>_<b>_<c>` columns; a STRUCTURE deeper than this is a `-- NOTE`.
_MAX_STRUCT_FLATTEN_DEPTH = 2


class _UnsupportedCheckExpression(Exception):
    """Raised when an `Expression` node needs context a single-row SQL `CHECK` cannot express - caught by the caller,
    never propagated (RULE #5: a `-- NOTE`, not a crash).
    """


_SQL_RELATIONAL_OPERATORS = {
    "Equal": "=",
    "NotEqual": "<>",
    "Less": "<",
    "Greater": ">",
    "LessOrEqual": "<=",
    "GreaterOrEqual": ">=",
}


def _path_to_column(path_els: list[MetaInstance]) -> str:
    """Resolve a `CONSTRAINT` path (`PathOrInspFactor.PathEls`) to the flattened SQL column name it maps to.

    Same scope as `constraint_eval.py`'s own `_resolve_path`: a `CONSTRAINT`
    navigates only nested `STRUCTURE` hops in the SAME object (never through
    a `REFERENCE TO`/role) - so 1 hop is a plain own column, and 2 or 3 hops
    are the SAME `<attr>_<subattr>[_<subsubattr>]` flattened name
    `tables.py::_columns_for_class` builds for up to `_MAX_STRUCT_FLATTEN_DEPTH` levels
    of STRUCTURE nesting (RULE #1: same join, not a parallel convention).
    More hops than that would need a deeper nesting level, refused the same
    way it is everywhere else in this module. The caller checks the result
    against the real column set, so a path that lands on a column that was
    NOT flattened degrades to a `-- NOTE`, never a broken `CHECK`.
    """
    if len(path_els) not in (1, 2, 3):
        raise _UnsupportedCheckExpression(
            f"path with {len(path_els)} hops needs more than {_MAX_STRUCT_FLATTEN_DEPTH} levels of STRUCTURE nesting"
        )
    refs = []
    for path_el in path_els:
        kind = getattr(path_el, "Kind", None)
        if kind not in ("ReferenceAttr", "Attribute"):
            raise _UnsupportedCheckExpression(f"path element kind {kind!r} needs object-graph context beyond one row")
        if getattr(path_el, "NumIndex", None) is not None or getattr(path_el, "SpecIndex", None) is not None:
            raise _UnsupportedCheckExpression("indexed path elements ([FIRST]/[LAST]/[n]) are not supported")
        ref = getattr(path_el, "Ref", None)
        if not ref:
            raise _UnsupportedCheckExpression("path element with no attribute name")
        refs.append(ref)
    return _sql_identifier("_".join(refs))


def _text_sql_literal(quoted_value: str) -> str:
    r"""Turn a `Constant.Value` STRING token (still INTERLIS-quoted, e.g. `'"a\\"b"'`) into a SQL string literal."""
    unescaped = _unquote_text(quoted_value)
    return "'" + unescaped.replace("'", "''") + "'"


def _numeric_sql_literal(raw: str) -> str:
    """Strip a leading `+` from a `Constant.Value` numeric token.

    `+` is not standard SQL numeric-literal syntax, unlike `-`.
    """
    return raw[1:] if raw.startswith("+") else raw


def _expression_to_sql(expr: MetaInstance, column_names: set[str], renamed: dict[str, str]) -> str:
    """Serialize `expr` (an already-built `Expression` node) into a SQL boolean expression for `CHECK (...)`.

    Same supported subset as `constraint_eval.py`'s `evaluate_expression`
    (relational operators, `And`/`Or`/`Not`/`Implication`, `DEFINED(...)`,
    plain attribute paths, `Numeric`/`Text`/`Enumeration` constants) -
    walks the SAME `Expression` tree, but emits SQL text instead of
    evaluating against a Python dict. `THIS`/`PARENT`/aggregate paths/
    `FunctionCall`/arithmetic raise `_UnsupportedCheckExpression`, same as
    that module's `UnsupportedExpressionError` for the same nodes.

    `renamed` is `tables.py::_avoid_identity_collision`'s `{old_name: new_name}` map -
    a path whose single hop is a real attribute literally named `id` must
    resolve to the column it was actually renamed to (`id_attr`), the same
    remap already applied to `UniqueConstraint.columns` in `build_tables`.
    """
    qualified = expr._qualified_class
    if qualified.endswith("CompoundExpr"):
        op = expr.Operation
        subs = expr.SubExpressions
        if op in _SQL_RELATIONAL_OPERATORS:
            if len(subs) != 2:
                raise _UnsupportedCheckExpression(
                    f"relational operator {op!r} needs exactly 2 operands, got {len(subs)}"
                )
            left = _expression_to_sql(subs[0], column_names, renamed)
            right = _expression_to_sql(subs[1], column_names, renamed)
            return f"({left} {_SQL_RELATIONAL_OPERATORS[op]} {right})"
        if op == "And":
            return "(" + " AND ".join(_expression_to_sql(sub, column_names, renamed) for sub in subs) + ")"
        if op == "Or":
            return "(" + " OR ".join(_expression_to_sql(sub, column_names, renamed) for sub in subs) + ")"
        if op == "Implication":
            if len(subs) != 2:
                raise _UnsupportedCheckExpression("implication needs exactly 2 operands")
            left = _expression_to_sql(subs[0], column_names, renamed)
            right = _expression_to_sql(subs[1], column_names, renamed)
            return f"(NOT {left} OR {right})"
        raise _UnsupportedCheckExpression(
            f"operator {op!r} needs numeric-domain context beyond boolean CHECK evaluation"
        )
    if qualified.endswith("UnaryExpr"):
        op = expr.Operation
        if op == "Not":
            return f"(NOT {_expression_to_sql(expr.SubExpression, column_names, renamed)})"
        if op == "Defined":
            sub = expr.SubExpression
            if sub is None or not sub._qualified_class.endswith("PathOrInspFactor"):
                raise _UnsupportedCheckExpression("DEFINED(...) is only supported for a plain attribute path")
            raw_column = _path_to_column(sub.PathEls)
            column = renamed.get(raw_column, raw_column)
            if column not in column_names:
                raise _UnsupportedCheckExpression(f"DEFINED({column}): no such column")
            return f"({_quote(column)} IS NOT NULL)"
        raise _UnsupportedCheckExpression(f"unary operator {op!r} is not supported")
    if qualified.endswith("PathOrInspFactor"):
        if getattr(expr, "Inspection", None):
            raise _UnsupportedCheckExpression("INSPECTION-based path factors are not supported")
        raw_column = _path_to_column(expr.PathEls)
        column = renamed.get(raw_column, raw_column)
        if column not in column_names:
            raise _UnsupportedCheckExpression(
                f"attribute path resolves to column {column!r}, which has no mapped SQL type"
            )
        return _quote(column)
    if qualified.endswith("Constant"):
        value, type_ = expr.Value, expr.Type
        if type_ == "Numeric":
            return _numeric_sql_literal(value)
        if type_ == "Text":
            return _text_sql_literal(value)
        if type_ == "Enumeration":
            return (
                # a plain dotted-path string (`_normalize_enumeration_const_value`), never
                # quoted to begin with - matches EnumType's own `text` SQL column type
                "'"
                + value.replace("'", "''")
                + "'"
            )
        raise _UnsupportedCheckExpression(f"constant of type {type_!r} is not supported")
    raise _UnsupportedCheckExpression(
        f"expression node {qualified} needs THIS/PARENT/aggregate/function-call context beyond one row",
    )
