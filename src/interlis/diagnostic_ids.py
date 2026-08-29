"""Stable identifiers for every converter/builder diagnostic this runtime emits.

Each id names ONE signal site (or a family of structurally identical
sites): a construct a converter cannot faithfully translate, or an input
the caller did not supply. The id is stable - it is what a downstream
tool, a SARIF consumer, or `docs/converter-limitations.md` keys off - so
never rename one; retire it (drop the row here and in the doc) only once
no site emits it.

`REGISTRY` maps id -> (klass, one_line). `klass` is the disposition:

- ``"A"`` - not yet implemented. A real backlog item; the id disappears
  once the construct is supported.
- ``"B"`` - permanent category mismatch. The construct cannot be
  expressed in the target formalism without inventing semantics INTERLIS
  does not have (a whole-population check in a per-row SQL ``CHECK``, a
  ``UNIQUE`` on a SQL view, an OID as a JSON-Schema property). Kept and
  documented, not "fixable".
- ``"C"`` - missing input. Not a limitation at all: the caller did not
  pass ``--repo`` / ``--catalog``, or a reference target did not resolve.
  The message names the fix.

`docs/converter-limitations.md` carries the human-facing detail (corpus
evidence, where each is tracked). `scripts/check_diagnostic_ids.py`
asserts this dict and that table stay in sync, and that every id here is
actually referenced from ``src/``.
"""

from __future__ import annotations

REGISTRY: dict[str, tuple[str, str]] = {
    # -- convert-sql: CREATE TABLE ---------------------------------------
    "SQL-STRUCT-NESTED-DEEP": ("A", "STRUCTURE nested more than one level deep is not flattened"),
    "SQL-STRUCT-ABSTRACT": ("A", "ABSTRACT structure attribute - subclass polymorphism not mapped to a table"),
    "SQL-ATTR-TYPE-UNMAPPED": (
        "A",
        "a RESOLVED attribute type has no column in the mapped scalar/geometry/reference/structure set",
    ),
    "SQL-BAGLIST-ELEMENT-UNMAPPED": ("A", "BAG/LIST OF element type outside the mapped set"),
    "SQL-BAGLIST-ELEMENT-UNRESOLVED": ("C", "BAG/LIST OF element type did not resolve - provide its model via --repo"),
    "SQL-REF-TARGET-UNRESOLVED": (
        "C",
        "REFERENCE TO / role target did not resolve - provide its model via --repo/--catalog",
    ),
    "SQL-GEOM-NO-CRS": (
        "C",
        "geometry column has no resolvable CRS/domain - provide the geometry base model via --repo",
    ),
    "SQL-FK-CROSS-MODEL-DROPPED": (
        "C",
        "FOREIGN KEY target lives in a model not in this conversion - pass it via --catalog",
    ),
    # -- convert-sql: UNIQUE -------------------------------------------------
    "SQL-UNIQUE-BASKET": ("A", "basket-scoped UNIQUE (no per-attribute path) is not generated"),
    "SQL-UNIQUE-CROSS-REF": ("A", "UNIQUE navigating a '->' reference is not a plain SQL table constraint"),
    "SQL-UNIQUE-LOCAL-UNSUPPORTED": (
        "A",
        "UNIQUE (LOCAL) path shape outside the single-role-hop + attribute-list form",
    ),
    "SQL-UNIQUE-COL-UNMAPPED": ("A", "UNIQUE references a column whose attribute type was not mapped"),
    # -- convert-sql: CHECK / CONSTRAINT ----------------------------------
    "SQL-CHECK-EXPR-UNSUPPORTED": (
        "B",
        "CONSTRAINT expression needs object-graph/aggregate/function context a per-row CHECK cannot express",
    ),
    "SQL-CONSTRAINT-NOT-ROWLOCAL": ("B", "CONSTRAINT is not a row-local MANDATORY CONSTRAINT - no CHECK generated"),
    "SQL-CONSTRAINT-PLAUSIBILITY": (
        "B",
        "percentage-based plausibility CONSTRAINT - a population ratio, not a per-row CHECK",
    ),
    "SQL-CONSTRAINT-SET": ("B", "SET CONSTRAINT is a whole-population check - no per-row CHECK can express it"),
    "SQL-CONSTRAINT-EXISTENCE": ("B", "EXISTENCE CONSTRAINT is a cross-class check - no per-row CHECK can express it"),
    # -- convert-sql: CREATE VIEW --------------------------------------------
    "SQL-VIEW-BASE-MISSING": (
        "C",
        "a VIEW base/join/navigation target table is not in this conversion - pass its model via --repo/--catalog",
    ),
    "SQL-VIEW-EXPR-UNTRANSLATABLE": (
        "B",
        "a VIEW attribute/WHERE expression is outside the path/constant/DEFINED/relational subset a SQL view can carry",
    ),
    "SQL-VIEW-ATTR-DROPPED": (
        "A",
        "an ALL OF pass-through attribute could not be projected as a single column and was dropped",
    ),
    "SQL-VIEW-NO-ATTRS": ("B", "a VIEW has no projectable ATTRIBUTE definitions - nothing to SELECT"),
    "SQL-VIEW-CONSTRAINT-DROPPED": (
        "B",
        "a VIEW-level UNIQUE/SET/EXISTENCE/CONSTRAINT cannot be carried onto a CREATE VIEW",
    ),
    # -- convert (.ili -> JSON Schema) -------------------------------------
    "JSONSCHEMA-CLASS-UNRESOLVED": (
        "C",
        "a nested-structure Class is not reachable from the given roots - provide its model via --repo",
    ),
    "JSONSCHEMA-TYPE-UNSUPPORTED": ("A", "an attribute type is outside the mapped set - marked x-unsupported"),
    # -- convert-jsonfg (.xtf -> JSON-FG) --------------------------------
    "JSONFG-TYPE-UNSUPPORTED": ("A", "an attribute value's type is outside the mapped set - marked x-unsupported"),
    "JSONFG-MULTIVALUE-UNRESOLVED": ("C", "a BAG/LIST OF element type did not resolve - provide its model via --repo"),
    "JSONFG-VIEW-BASE-MISSING": ("C", "a VIEW base model is not resolvable - provide it via --repo"),
    "JSONFG-VIEW-WHERE-UNSUPPORTED": (
        "B",
        "a VIEW WHERE clause needs arithmetic/function-call context the evaluator does not support",
    ),
    "JSONFG-VIEW-INSPECTION-GAP": ("A", "an INSPECTION VIEW's '-> attribute' path was not built by the model builder"),
    # -- builder ----------------------------------------------------------
    "BUILD-TYPE-UNRESOLVED": (
        "A",
        "the model builder produced no Type instance for an attribute "
        "(unresolved cross-model name, or a predefined type not yet materialised)",
    ),
    "BUILD-SPEC-GAP-ALT-ABSENT": ("A", "a grammar alternative had no mapping rule and was treated as absent"),
    "BUILD-TRANSLATION-MISMATCH": (
        "B",
        "a TRANSLATION OF model's element count differs from its base - only the common prefix is aligned",
    ),
    "BUILD-TRANSLATION-BASE-MISSING": (
        "C",
        "a TRANSLATION OF base model did not resolve - provide its directory via --repo",
    ),
}


def note(rule: str, message: str) -> str:
    """Return a `-- NOTE` body prefixed with its stable rule id: ``[RULE-ID] message``."""
    return f"[{rule}] {message}"
