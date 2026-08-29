"""Evaluate a built CONSTRAINT's Expression tree against one Feature's properties.

`class_to_json_schema`/`validate_feature_properties` (convert/jsonschema.py)
only check STRUCTURAL conformance (types, cardinalities, values) - no JSON
Schema 2020-12 keyword expresses an arbitrary compiled boolean expression
over several attributes, so a `CONSTRAINT`'s semantics were never checked
against real data by this runtime (see the `Constraint` entry in
mappings/ilismeta16-to-jsonschema-rules.yml). This module is the second,
complementary check: it walks the already-correctly-built `Expression` AST
(`SimpleConstraint.LogicalExpression` etc.) directly against a candidate
JSON-FG Feature's `properties` dict, producing the same "list of human
messages, empty = valid" contract as `validate_feature_properties` - not a
new JSON Schema keyword.

Scope of this first pass: relational operators (`Equal`/`NotEqual`/`Less`/
`Greater`/`LessOrEqual`/`GreaterOrEqual`), logical operators (`And`/`Or`/
`Not`/`Implication`), `DEFINED(...)`, and plain (possibly multi-hop)
attribute-name path resolution. `THIS`/`PARENT`/aggregate paths/
`FunctionCall`/arithmetic (`Mult`/`Div`) need object-graph or numeric-domain
context a single Feature's `properties` dict cannot provide - raised as
`UnsupportedExpressionError` rather than guessed at. `check_feature_constraints`
only evaluates `SimpleConstraint` entries with no `Percentage` (plain
`MANDATORY CONSTRAINT`/bare-expression forms): `UniqueConstraint`/
`SetConstraint`/`ExistenceConstraint` and the percentage-based plausibility
form (`Kind` `LowPercC`/`HighPercC`) are population/basket-level checks that
need more than one Feature to evaluate, out of scope here.
"""

import re
from typing import Any

from interlis.metamodel.instance import MetaInstance

_RELATIONAL_SYMBOLS = {
    "Equal": "==",
    "NotEqual": "!=",
    "Less": "<",
    "Greater": ">",
    "LessOrEqual": "<=",
    "GreaterOrEqual": ">=",
}
_MISSING = object()
_STRING_ESCAPE_RE = re.compile(r'\\(["\\]|u[0-9a-fA-F]{4})')


class UnsupportedExpressionError(Exception):
    """Raised when an Expression node needs context beyond a single Feature's `properties` dict."""


def _unquote_text(raw: str) -> str:
    r"""Undo the ANTLR `STRING` token's own quoting (`vendor/interlis-antlr4/InterlisLexer.g4`).

    `textConst`'s binding captures the STRING token verbatim (surrounding
    `"..."` and all) - `\\"`/`\\\\`/`\\uXXXX` are STRING's only 3 escape
    forms, none else needs resolving.
    """
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    inner = raw[1:-1]
    return _STRING_ESCAPE_RE.sub(
        lambda m: m.group(1) if m.group(1) in ('"', "\\") else chr(int(m.group(1)[1:], 16)),
        inner,
    )


def _try_number(text: str, fallback: Any) -> Any:
    try:
        return int(text) if re.fullmatch(r"[+-]?\d+", text) else float(text)
    except ValueError:
        return fallback


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value == "true"
    return bool(value)


def _align(left: Any, right: Any) -> tuple[Any, Any]:
    if isinstance(left, bool) or isinstance(right, bool):
        return _as_bool(left), _as_bool(right)
    if isinstance(left, str) and isinstance(right, (int, float)):
        left = _try_number(left, left)
    elif isinstance(right, str) and isinstance(left, (int, float)):
        right = _try_number(right, right)
    return left, right


def _compare(op: str, left: Any, right: Any) -> bool:
    left, right = _align(left, right)
    if op == "Equal":
        return bool(left == right)
    if op == "NotEqual":
        return bool(left != right)
    try:
        if op == "Less":
            return bool(left < right)
        if op == "Greater":
            return bool(left > right)
        if op == "LessOrEqual":
            return bool(left <= right)
        if op == "GreaterOrEqual":
            return bool(left >= right)
    except TypeError as exc:
        raise UnsupportedExpressionError(f"cannot compare {left!r} and {right!r} with {op}") from exc
    raise UnsupportedExpressionError(f"relational operator {op!r} is not supported")


def _resolve_path(path_els: list[MetaInstance], properties: dict[str, Any]) -> tuple[bool, Any]:
    """Walk `PathEls` through nested `properties` dicts (STRUCTURE nesting), one plain attribute name per hop."""
    value: Any = properties
    for path_el in path_els:
        kind = getattr(path_el, "Kind", None)
        if kind not in ("ReferenceAttr", "Attribute"):
            raise UnsupportedExpressionError(
                f"path element kind {kind!r} needs object-graph context beyond a single Feature's properties",
            )
        if getattr(path_el, "NumIndex", None) is not None or getattr(path_el, "SpecIndex", None) is not None:
            raise UnsupportedExpressionError("indexed path elements ([FIRST]/[LAST]/[n]) are not supported")
        ref = getattr(path_el, "Ref", None)
        if not isinstance(value, dict) or ref not in value:
            return False, None
        value = value[ref]
    return True, value


def _describe_path(path_els: list[MetaInstance]) -> str:
    return "->".join(str(getattr(el, "Ref", None) or "?") for el in path_els)


def describe_expression(expr: MetaInstance) -> str:
    """Render `expr` back into an approximate INTERLIS-like expression string, for violation messages."""
    qualified = expr._qualified_class
    if qualified.endswith("CompoundExpr"):
        op = expr.Operation
        subs = [describe_expression(sub) for sub in expr.SubExpressions]
        if op in _RELATIONAL_SYMBOLS:
            return f"{subs[0]} {_RELATIONAL_SYMBOLS[op]} {subs[1]}"
        if op == "And":
            return " AND ".join(subs)
        if op == "Or":
            return " OR ".join(subs)
        if op == "Implication":
            return f"{subs[0]} => {subs[1]}"
        return f"{op}({', '.join(subs)})"
    if qualified.endswith("UnaryExpr"):
        sub = describe_expression(expr.SubExpression) if expr.SubExpression is not None else "?"
        if expr.Operation == "Defined":
            return f"DEFINED({sub})"
        if expr.Operation == "Not":
            return f"NOT ({sub})"
        return f"{expr.Operation}({sub})"
    if qualified.endswith("PathOrInspFactor"):
        return _describe_path(expr.PathEls)
    if qualified.endswith("Constant"):
        if expr.Type == "Text":
            return expr.Value  # already the quoted STRING token verbatim
        if expr.Type == "Enumeration":
            return f"#{expr.Value}"
        return str(expr.Value)
    return "<expression>"


def evaluate_expression(expr: MetaInstance, properties: dict[str, Any]) -> Any:
    """Evaluate one `Expression` node against `properties`, returning its Python value (`bool`/`str`/`int`/`float`)."""
    qualified = expr._qualified_class
    if qualified.endswith("CompoundExpr"):
        return _evaluate_compound(expr, properties)
    if qualified.endswith("UnaryExpr"):
        return _evaluate_unary(expr, properties)
    if qualified.endswith("PathOrInspFactor"):
        return _evaluate_path_factor(expr, properties)
    if qualified.endswith("Constant"):
        return _evaluate_constant(expr)
    raise UnsupportedExpressionError(
        f"expression node {qualified} needs THIS/PARENT/aggregate/function-call context "
        f"beyond a single Feature's properties",
    )


def _evaluate_compound(expr: MetaInstance, properties: dict[str, Any]) -> Any:
    op = expr.Operation
    subs = expr.SubExpressions
    if op in _RELATIONAL_SYMBOLS:
        if len(subs) != 2:
            raise UnsupportedExpressionError(f"relational operator {op!r} needs exactly 2 operands, got {len(subs)}")
        left = evaluate_expression(subs[0], properties)
        right = evaluate_expression(subs[1], properties)
        return _compare(op, left, right)
    if op == "And":
        return all(evaluate_expression(sub, properties) for sub in subs)
    if op == "Or":
        return any(evaluate_expression(sub, properties) for sub in subs)
    if op == "Implication":
        if len(subs) != 2:
            raise UnsupportedExpressionError("implication needs exactly 2 operands")
        if not evaluate_expression(subs[0], properties):
            return True
        return bool(evaluate_expression(subs[1], properties))
    raise UnsupportedExpressionError(
        f"operator {op!r} needs numeric-domain context beyond boolean constraint evaluation",
    )


def _evaluate_unary(expr: MetaInstance, properties: dict[str, Any]) -> Any:
    op = expr.Operation
    if op == "Not":
        return not evaluate_expression(expr.SubExpression, properties)
    if op == "Defined":
        sub = expr.SubExpression
        if sub is None or not sub._qualified_class.endswith("PathOrInspFactor"):
            raise UnsupportedExpressionError("DEFINED(...) is only supported for a plain attribute path")
        found, _value = _resolve_path(sub.PathEls, properties)
        return found
    raise UnsupportedExpressionError(f"unary operator {op!r} is not supported")


def _evaluate_path_factor(expr: MetaInstance, properties: dict[str, Any]) -> Any:
    if getattr(expr, "Inspection", None):
        raise UnsupportedExpressionError("INSPECTION-based path factors are not supported")
    found, value = _resolve_path(expr.PathEls, properties)
    if not found:
        raise UnsupportedExpressionError(
            f"attribute path {_describe_path(expr.PathEls)!r} is not present in the payload"
        )
    return value


def _evaluate_constant(expr: MetaInstance) -> Any:
    value, type_ = expr.Value, expr.Type
    if type_ == "Numeric":
        return _try_number(value, value)
    if type_ == "Text":
        return _unquote_text(value)
    if type_ == "Enumeration":
        return value
    raise UnsupportedExpressionError(f"constant of type {type_!r} is not supported")


def check_feature_constraints(properties: dict[str, Any], class_instance: MetaInstance) -> list[str]:
    """Evaluate every per-Feature `SimpleConstraint` declared on `class_instance` against `properties`.

    Same "list of human messages, empty = valid" contract as
    `validate_feature_properties` (convert/jsonschema.py) - meant to be
    called alongside it, not as a replacement (this checks CONSTRAINT
    semantics, not payload shape). A constraint this evaluator cannot
    resolve for this particular Feature (an unsupported node, or a
    relational comparison against an attribute absent from `properties`)
    is silently skipped rather than reported - "not checked" is not the
    same verdict as "violated", and this function never fails a valid
    payload over a construct outside its documented scope.
    """
    messages: list[str] = []
    for constraint in getattr(class_instance, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("SimpleConstraint"):
            continue
        if getattr(constraint, "Kind", None) not in (None, "MandC"):
            continue
        if getattr(constraint, "Percentage", None) is not None:
            continue
        expr = getattr(constraint, "LogicalExpression", None)
        if expr is None:
            continue
        try:
            satisfied = bool(evaluate_expression(expr, properties))
        except UnsupportedExpressionError:
            continue
        if satisfied:
            continue
        description = describe_expression(expr)
        name = getattr(constraint, "Name", None)
        if name:
            messages.append(f"constraint {name!r} is not satisfied: {description}")
        else:
            messages.append(f"constraint not satisfied: {description}")
    return messages
