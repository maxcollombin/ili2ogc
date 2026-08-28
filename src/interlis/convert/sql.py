"""IlisMeta16 -> SQL DDL conversion: tables, columns, UNIQUE/FOREIGN KEY/CHECK constraints (backlog item 14).

See docs/sql-conversion-strategy.md for the design decision and scope, and
mappings/ilismeta16-to-sql-rules.yml / spec/conversion/sql-mapping.yml for
the concept/field contract this module implements.

Division of labor (docs/interlis-ogc-architecture.md): this module
generates the SCHEMA only (`CREATE TABLE` + `UNIQUE` + `FOREIGN KEY`) -
GDAL (`ogr2ogr -append`) still does the actual DATA LOADING, including
into `BAG`/`LIST OF` child tables: `convert/jsonfg.py`'s
`transfer_to_feature_collection(..., include_child_rows=True)` appends
one synthetic Feature per `BAG`/`LIST` occurrence to the SAME
FeatureCollection, each with its own `"featureType"` matching the child
table name here - GDAL's JSONFG driver already splits a collection by
`featureType` on `-append`, so ONE call loads parent and child rows both.
See docs/sql-conversion-strategy.md for why this keeps the "this project
transforms, GDAL loads" division intact rather than adding a
live-database-write dependency.
`build_tables()` produces a dialect-neutral intermediate representation
(`Table`/`Column`/`UniqueConstraint`/`ForeignKey`) from already-built
`IlisMeta16` instances, walked once via the SAME `resolve_attribute`/
`attributes_of`/`schema_members_of` helpers `convert/jsonschema.py`
already uses (no parallel resolution logic) - `render_postgresql`/
`render_gpkg` are the two renderers over that IR.

Scope (see mappings/ilismeta16-to-sql-rules.yml for the full, per-concept
rationale): scalar/geometry columns, one level of flattened STRUCTURE
nesting, FOREIGN KEY from REFERENCE TO/embedded roles, UNIQUE from the
simple (Kind=GlobalU, no `->` navigation) constraint form, `BAG`/`LIST OF`
-> a related child table with its own `UNIQUE (LOCAL)` support
(`_local_unique_constraints_for_class` - scoped to that child table via
its `<parent>_fk` column, e.g. `UNIQUE (LOCAL) Entries: Code;` ->
`UNIQUE (parent_fk, code)` on the `entries` child table; also reached when
the `BAG`/`LIST OF` AND its `UNIQUE (LOCAL)` are declared on a STRUCTURE
embedded one level down in the Class, the dominant real corpus idiom, e.g.
`LocalisationCH_V1.MultilingualText` - `_columns_for_class` qualifies the
child table's label with the STRUCTURE attribute's own name in that case,
e.g. `<class>_<struct_attr>_entries`), and CHECK from a
row-local `MANDATORY CONSTRAINT` (`_expression_to_sql`, same supported
`Expression` subset as `constraint_eval.py`'s `evaluate_expression` -
relational/logical operators, `DEFINED(...)`, plain attribute paths up to
one STRUCTURE hop). ABSTRACT structure polymorphism, cross-reference
(`->`) and basket-scoped UNIQUE, the percentage-based plausibility form,
and any `CONSTRAINT` needing `THIS`/`PARENT`/aggregate/function-call/
arithmetic context are all deliberately out of scope - never silently
dropped, each unsupported
construct is collected into `Table.notes` and rendered as a `-- NOTE` SQL
comment (RULE #5).
"""
from dataclasses import dataclass, field

from interlis.builder.forward_refs import SymbolTable
from interlis.convert.constraint_eval import _unquote_text
from interlis.convert.jsonfg import _meta_value
from interlis.convert.jsonschema import _is_integer_range, _is_structure
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.schema import (
    ResolvedAttribute,
    attributes_of,
    coord_axes,
    line_coord_type,
    reference_target_class,
    resolve_attribute,
    schema_members_of,
)

OID_COLUMN = "id"
"""Deliberately NOT `PRIMARY KEY`/GDAL's own default FID column name (`ogc_fid`) - verified empirically (2026-08-27, a real `ogr2ogr -append` against a live PostgreSQL AND GeoPackage) that GDAL treats WHATEVER column it detects as the table's `PRIMARY KEY` as an auto-managed FID slot, excluded from the INSERT column list entirely (expects the database to fill it in, e.g. `SERIAL`) - a Feature's `"id"` is NEVER written into it, `PRIMARY KEY "ogc_fid" text` silently stayed NULL and violated its own NOT NULL constraint. `"id"` matches EXACTLY what the JSON-FG reader (`ogrinfo`, confirmed) exposes as a plain STRING FIELD in its own right, separate from OGR's internal FID concept - declared `UNIQUE NOT NULL` (never `PRIMARY KEY`) so GDAL treats it as a normal field to WRITE, not a slot to manage. See docs/sql-conversion-strategy.md for the full investigation."""

_GEOMETRY_KINDS = {"CoordType", "LineType"}
_MAX_IDENTIFIER_LENGTH = 63  # PostgreSQL's own identifier length limit - a real ceiling, not an arbitrary one.


def _sql_identifier(name: str) -> str:
    """Lowercase an INTERLIS `Name` into the SQL identifier both PostgreSQL and GDAL would independently produce.

    PostgreSQL folds every UNQUOTED identifier to lowercase regardless of
    what this module writes - and GDAL's own PostgreSQL driver launders
    (lowercases) field names by default (`LAUNDER=YES`, verified
    2026-08-27) when it later `-append`s data into a table this module
    created. Writing every identifier already-lowercase here sidesteps
    BOTH mechanisms rather than relying on either implicitly - no quoting
    needed anywhere in the generated DDL.
    """
    return name.lower()


def _truncate_identifier(name: str) -> str:
    return name if len(name) <= _MAX_IDENTIFIER_LENGTH else name[:_MAX_IDENTIFIER_LENGTH]


def _avoid_identity_collision(columns: list[Column]) -> dict[str, str]:
    """Rename any column literally named `OID_COLUMN` ("id") to `"id_attr"` (or `"id_attr_2"`, ... on a further collision), IN PLACE - returns the `{old_name: new_name}` rename map.

    Real corpus case (found 2026-08-27 via a live SQLite run,
    `ili_corpus/WasserBase_V1_1.ili`: `ID : MANDATORY TEXT*25;` -
    "duplicate column name: id"): a genuine INTERLIS attribute literally
    named `Id`/`ID` lowercases to the SAME name this module reserves for
    the synthetic identity column (`OID_COLUMN`) - renaming the ATTRIBUTE's
    own column here rather than the reserved one, which every FOREIGN KEY
    and every GDAL `-append` already depends on matching exactly. The
    caller MUST also apply the returned rename map to any `UniqueConstraint`
    built from the SAME attribute set (its own column list is computed
    independently, straight from `PathEl.Ref`, and would otherwise still
    reference the OLD, no-longer-existing name).
    """
    used = {c.name for c in columns}
    renamed: dict[str, str] = {}
    for column in columns:
        if column.name != OID_COLUMN:
            continue
        base_name = f"{OID_COLUMN}_attr"
        new_name = base_name
        suffix = 2
        while new_name in used:
            new_name = f"{base_name}_{suffix}"
            suffix += 1
        used.discard(OID_COLUMN)
        used.add(new_name)
        column.name = new_name
        renamed[OID_COLUMN] = new_name
    return renamed


def _quote(name: str) -> str:
    """Double-quote a table/column identifier (ANSI SQL, both PostgreSQL and SQLite accept it).

    Found necessary (2026-08-27) by executing generated DDL against a real
    SQLite engine, not just eyeballing the text: a real corpus INTERLIS
    `Class`/attribute can be named after a SQL reserved word (confirmed:
    `Union`, `Index`) - unquoted, `CREATE TABLE union (...)` is a syntax
    error in both dialects. Since every identifier this module emits is
    already lowercase (`_sql_identifier`), quoting changes nothing about
    the STORED name (PostgreSQL folds unquoted identifiers to lowercase
    anyway) - it only prevents reserved-word collisions, uniformly, without
    needing a maintained keyword list for either dialect.
    """
    return f'"{name}"'


def _quote_list(names: list[str]) -> str:
    return ", ".join(_quote(n) for n in names)


@dataclass
class Column:
    name: str
    sql_type: str
    """A dialect-portable scalar type name (e.g. "text"/"integer"/"varchar(20)") - ignored by every renderer when `geometry_type` is set (each renderer formats geometry columns its own way, see `render_postgresql`/`render_gpkg`)."""
    nullable: bool = True
    geometry_type: str | None = None
    """SFA type name (e.g. "Point", "MultiPolygonZ") - set ONLY for a geometry column, structured (not pre-formatted) so each renderer can express it its own way."""
    srid: int | None = None
    """EPSG numeric code - set ONLY alongside `geometry_type`."""


@dataclass
class ForeignKey:
    name: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class UniqueConstraint:
    name: str
    columns: list[str]


@dataclass
class CheckConstraint:
    name: str
    expression: str
    """A complete SQL boolean expression, already portable across PostgreSQL and SQLite (no dialect-specific syntax) - see `_expression_to_sql`."""


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    unique_constraints: list[UniqueConstraint] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    check_constraints: list[CheckConstraint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Human-readable reasons an attribute/constraint was skipped (RULE #5) - never a silent drop."""


def _srid(coord_type: MetaInstance | None) -> str | None:
    """Return the bare numeric EPSG code (e.g. `"2056"`) from a CoordType's `!!@CRS=EPSG:<code>` meta-attribute, or `None`.

    Same resolution as `convert/jsonfg.py`'s `_crs_uri` (reused via the
    same `_meta_value` primitive), returning just the numeric code instead
    of the full `coordRefSys` URI - `None` (never guessed) when the
    meta-attribute is absent or not an `EPSG:<digits>` value, same
    RULE #5 stance as `_crs_uri`.
    """
    raw = _meta_value(coord_type, "CRS")
    if raw is None:
        return None
    scheme, _, code = raw.partition(":")
    if scheme.strip().upper() != "EPSG" or not code.strip().isdigit():
        return None
    return code.strip()


def _geometry_column_info(resolved: ResolvedAttribute) -> tuple[str, int, None] | tuple[None, None, str]:
    """Return `(sfa_type, srid, None)` on success or `(None, None, reason)` on failure, for a CoordType/LineType attribute."""
    if resolved.type_kind == "CoordType":
        coord_type = resolved.type_instance
        sfa = "Point"
    elif resolved.type_kind == "LineType":
        line_type = resolved.type_instance
        coord_type = line_coord_type(line_type)
        kind = getattr(line_type, "Kind", None)
        sfa = "LineString" if kind in ("Polyline", "DirectedPolyline") else "Polygon"
    else:
        return None, None, "not a geometry type"
    if coord_type is None:
        return None, None, "vertex CoordType not resolved"
    if len(coord_axes(coord_type)) >= 3:
        sfa += "Z"
    if bool(getattr(resolved.type_instance, "Multi", False)):
        sfa = "Multi" + sfa
    srid = _srid(coord_type)
    if srid is None:
        return None, None, "no resolved CRS (!!@CRS meta-attribute)"
    return sfa, int(srid), None


def _scalar_sql_type(resolved: ResolvedAttribute) -> str | None:
    """Return a scalar SQL column type, or `None` if `resolved` isn't one of the mapped scalar kinds."""
    kind = resolved.type_kind
    inst = resolved.type_instance
    if kind == "NumType" and inst is not None:
        is_int = _is_integer_range(getattr(inst, "Min", None), getattr(inst, "Max", None))
        return "integer" if is_int else "numeric"
    if kind == "TextType" and inst is not None:
        text_kind = getattr(inst, "Kind", None)
        if text_kind == "Name":
            return "varchar(255)"
        if text_kind == "Uri":
            return "varchar(1023)"
        max_length = getattr(inst, "MaxLength", None)
        if max_length is not None:
            try:
                return f"varchar({int(max_length)})"
            except (TypeError, ValueError):
                pass
        return "text"
    if kind == "EnumType":
        return "text"
    if kind == "BooleanType":
        return "boolean"
    if kind == "FormattedType" and inst is not None:
        return {"XMLDate": "date", "XMLTime": "time", "XMLDateTime": "timestamp"}.get(getattr(inst, "Format", None), "text")
    if kind == "BlackboxType":
        return "text"
    return None


def _columns_for_class(
    cls: MetaInstance, symbol_table: SymbolTable | None, *, prefix: str = "",
) -> tuple[list[Column], list[ForeignKey], list[str], list[tuple[str, MetaInstance]], dict[str, list[list[str]]]]:
    """Return `(columns, foreign_keys, notes, child_specs, local_unique)` for `cls`'s own+inherited members, flattening one level of STRUCTURE nesting inline.

    `prefix` is only ever non-empty on the recursive call flattening a
    STRUCTURE attribute (`"<attr>_"`) - used both to build flattened column
    names AND to detect/refuse a second level of nesting (RULE #7: not
    attempted without a policy for it, see
    mappings/ilismeta16-to-sql-rules.yml's StructureNesting entry).

    `child_specs` is `[(label, MultiValue instance), ...]` for every
    `BAG`/`LIST OF` member found either at the TOP level OR while
    flattening a STRUCTURE one level deep (`label` already carries the
    `"<struct_attr>_"` prefix in the latter case, e.g.
    `"zustaendige_behoerde_entries"`) - built into a related child table by
    `_build_child_table` (called from `build_tables`, which alone knows the
    already-used table names to disambiguate against). A `BAG`/`LIST OF`
    found NESTED TWO levels deep (inside a STRUCTURE reached from another
    flattened STRUCTURE) can't occur here: STRUCTURE-in-STRUCTURE is itself
    refused one check below, so `prefix` is never more than one hop by the
    time a `MultiValue` is seen.

    `local_unique` is the SAME shape `_local_unique_constraints_for_class`
    returns, merged up from any nested STRUCTURE's OWN `Kind=LocalU`
    `UniqueConstraint` (the real corpus idiom, e.g. `LocalisationCH_V1.
    MultilingualText` wraps a `BAG`/`LIST` AND declares `UNIQUE (LOCAL)` on
    itself, not on the embedding Class) - keys qualified with the SAME
    `"<struct_attr>_"` prefix as the matching `child_specs` label, so
    `build_tables` can look them up together without knowing they came
    from a nested STRUCTURE at all.
    """
    columns: list[Column] = []
    foreign_keys: list[ForeignKey] = []
    notes: list[str] = []
    child_specs: list[tuple[str, MetaInstance]] = []
    local_unique: dict[str, list[list[str]]] = {}
    members = schema_members_of(cls, symbol_table) if symbol_table is not None else attributes_of(cls)
    for name, attr in members.items():
        resolved = resolve_attribute(attr)
        label = f"{prefix}{name}"
        col_name = _sql_identifier(label)

        if resolved.type_kind == "MultiValue":
            if not isinstance(resolved.type_instance, MetaInstance):
                notes.append(f"{label}: BAG/LIST OF element type not resolved")
                continue
            child_specs.append((label, resolved.type_instance))
            continue

        if resolved.type_kind == "Class" and _is_structure(resolved.type_instance):
            if prefix:
                notes.append(f"{label}: STRUCTURE nested more than one level deep - not flattened (Lot 1)")
                continue
            if bool(getattr(resolved.type_instance, "Abstract", False)):
                notes.append(f"{label}: ABSTRACT structure - polymorphism not supported (Lot 1)")
                continue
            sub_columns, sub_fks, sub_notes, sub_child_specs, _sub_local_unique = _columns_for_class(
                resolved.type_instance, symbol_table, prefix=f"{label}_",
            )
            columns.extend(sub_columns)
            foreign_keys.extend(sub_fks)
            notes.extend(sub_notes)
            child_specs.extend(sub_child_specs)
            struct_local_unique, struct_local_unique_notes = _local_unique_constraints_for_class(resolved.type_instance)
            for role_attr, groups in struct_local_unique.items():
                local_unique.setdefault(f"{label}_{role_attr}", []).extend(groups)
            notes.extend(f"{label}: {note}" for note in struct_local_unique_notes)
            continue

        if resolved.type_kind in ("Class", "ReferenceType") and resolved.type_instance is not None:
            target = reference_target_class(resolved)
            if target is None:
                notes.append(f"{label}: reference target not resolved (no --repo, or external)")
                continue
            target_table = _sql_identifier(getattr(target, "Name", None) or "")
            columns.append(Column(col_name, "text", nullable=not resolved.mandatory))
            fk_name = _truncate_identifier(_sql_identifier(f"fk_{getattr(cls, 'Name', '')}_{label}"))
            foreign_keys.append(ForeignKey(fk_name, [col_name], target_table, [OID_COLUMN]))
            continue

        if resolved.type_kind in _GEOMETRY_KINDS:
            sfa_type, srid, reason = _geometry_column_info(resolved)
            if sfa_type is None:
                notes.append(f"{label}: {reason}")
                continue
            columns.append(Column(
                col_name, sql_type="", nullable=not resolved.mandatory, geometry_type=sfa_type, srid=srid,
            ))
            continue

        scalar_type = _scalar_sql_type(resolved)
        if scalar_type is not None:
            columns.append(Column(col_name, scalar_type, nullable=not resolved.mandatory))
            continue

        notes.append(f"{label}: unsupported type {resolved.type_kind!r}")
    return columns, foreign_keys, notes, child_specs, local_unique


def _build_child_table(
    parent_table: str, attr_name: str, multi_value: MetaInstance, symbol_table: SymbolTable | None,
) -> tuple[Table | None, dict[str, str], str | None]:
    """Return `(child_table, renamed, None)` on success or `(None, {}, reason)` on failure, for one `BAG`/`LIST OF` attribute.

    Companion to `convert/jsonfg.py`'s `include_child_rows` synthetic
    Features (see docs/sql-conversion-strategy.md) - GDAL loads them into
    the table this returns via the SAME `ogr2ogr -append` call that loads
    the parent data, routed by `"featureType"`. Schema: a `<parent>_fk` `FOREIGN KEY` back to the
    parent (own `id` identity column added by the renderer, like every
    table) +
    `seq` (only when `Ordered=True` - `LIST` is order-significant, `BAG`
    is not) + the element's own value column(s), dispatched the SAME way
    as a plain attribute: scalar/geometry -> one `value` column;
    `STRUCTURE` -> its own columns (reusing `_columns_for_class` directly
    with no prefix, since THIS table already represents one structure
    instance - a NESTED `BAG`/`LIST` inside it still isn't supported, same
    one-level scope limit as everywhere else in this module). The
    `ReferenceType`/non-structure `Class` branch below is DEFENSIVE only -
    verified (2026-08-27, `attrTypeDef`'s real ANTLR bytecode, RULE #2bis)
    that `BAG`/`LIST OF REFERENCE TO X` is NOT actually constructible by
    this project's vendored grammar at all (`attrTypeDef`'s `(BAG|LIST)
    OF` alternative only ever calls `restrictedStructureRef()` - a named
    `STRUCTURE`, `ANYSTRUCTURE`, or a bare scalar `type_()`, never
    `referenceAttr()`) - contrary to what the abstract eCH-0031 EBNF
    alone would suggest, and confirmed absent from the real corpus too.
    An unmapped `BaseType` kind returns `(None, reason)` - the caller
    keeps the pre-existing "-- NOTE" on the PARENT table instead of
    creating an empty/broken child table.
    """
    base_type = getattr(multi_value, "BaseType", None)
    if not isinstance(base_type, MetaInstance):
        return None, {}, "BaseType not resolved"
    base_kind = base_type._qualified_class.rsplit(".", 1)[-1]

    child_table_name = _sql_identifier(f"{parent_table}_{attr_name}")
    fk_column = _sql_identifier(f"{parent_table}_fk")
    columns: list[Column] = [Column(fk_column, "text", nullable=False)]
    foreign_keys: list[ForeignKey] = [ForeignKey(
        _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_{fk_column}")),
        [fk_column], parent_table, [OID_COLUMN],
    )]
    notes: list[str] = []

    if bool(getattr(multi_value, "Ordered", False)):
        columns.append(Column("seq", "integer", nullable=False))

    if base_kind == "Class" and _is_structure(base_type):
        if bool(getattr(base_type, "Abstract", False)):
            return None, {}, "BAG/LIST OF an ABSTRACT structure - polymorphism not supported (Lot 1)"
        sub_columns, sub_fks, sub_notes, _sub_child_specs, _sub_local_unique = _columns_for_class(base_type, symbol_table)
        columns.extend(sub_columns)
        foreign_keys.extend(sub_fks)
        notes.extend(sub_notes)
    elif base_kind in ("Class", "ReferenceType"):
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        target = reference_target_class(synthetic)
        if target is None:
            return None, {}, "reference target not resolved (no --repo, or external)"
        target_table = _sql_identifier(getattr(target, "Name", None) or "")
        columns.append(Column("value", "text", nullable=True))
        foreign_keys.append(ForeignKey(
            _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_value")), ["value"], target_table, [OID_COLUMN],
        ))
    elif base_kind in _GEOMETRY_KINDS:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        sfa_type, srid, reason = _geometry_column_info(synthetic)
        if sfa_type is None:
            return None, {}, reason
        columns.append(Column("value", sql_type="", nullable=False, geometry_type=sfa_type, srid=srid))
    else:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        scalar_type = _scalar_sql_type(synthetic)
        if scalar_type is None:
            return None, {}, f"unsupported element type {base_kind!r}"
        columns.append(Column("value", scalar_type, nullable=False))

    renamed = _avoid_identity_collision(columns)
    return Table(name=child_table_name, columns=columns, foreign_keys=foreign_keys, notes=notes), renamed, None


class _UnsupportedCheckExpression(Exception):
    """Raised when an `Expression` node needs context a single-row SQL `CHECK` cannot express - caught by the caller, never propagated (RULE #5: a `-- NOTE`, not a crash)."""


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
    never navigates beyond one nested `STRUCTURE` in the SAME object (never
    through a `REFERENCE TO`/role - confirmed empirically there, docstring
    "STRUCTURE nesting" only) - so 1 hop is a plain own column, 2 hops is
    the SAME `<attr>_<subattr>` flattened name `_columns_for_class` already
    builds for one level of STRUCTURE nesting (RULE #1: same join, not a
    parallel convention). 3+ hops would need a second nesting level, out of
    Lot 1 scope everywhere else in this module - rejected the same way.
    """
    if len(path_els) not in (1, 2):
        raise _UnsupportedCheckExpression(f"path with {len(path_els)} hops needs more than one level of STRUCTURE nesting")
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
    """Strip a leading `+` (not standard SQL numeric-literal syntax, unlike `-`) from a `Constant.Value` numeric token."""
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

    `renamed` is `_avoid_identity_collision`'s `{old_name: new_name}` map -
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
                raise _UnsupportedCheckExpression(f"relational operator {op!r} needs exactly 2 operands, got {len(subs)}")
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
        raise _UnsupportedCheckExpression(f"operator {op!r} needs numeric-domain context beyond boolean CHECK evaluation")
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
            raise _UnsupportedCheckExpression(f"attribute path resolves to column {column!r}, which has no mapped SQL type")
        return _quote(column)
    if qualified.endswith("Constant"):
        value, type_ = expr.Value, expr.Type
        if type_ == "Numeric":
            return _numeric_sql_literal(value)
        if type_ == "Text":
            return _text_sql_literal(value)
        if type_ == "Enumeration":
            return "'" + value.replace("'", "''") + "'"  # a plain dotted-path string (`_normalize_enumeration_const_value`), never quoted to begin with - matches EnumType's own `text` SQL column type
        raise _UnsupportedCheckExpression(f"constant of type {type_!r} is not supported")
    raise _UnsupportedCheckExpression(
        f"expression node {qualified} needs THIS/PARENT/aggregate/function-call context beyond one row",
    )


def _check_constraints_for_class(
    cls: MetaInstance, table_name: str, column_names: set[str], renamed: dict[str, str],
) -> tuple[list[CheckConstraint], list[str]]:
    """Return `(constraints, notes)` for `cls`'s own row-local `MANDATORY CONSTRAINT`s - same scope as `constraint_eval.py`'s `check_feature_constraints`.

    `UniqueConstraint`/`SetConstraint`/`ExistenceConstraint` and the
    percentage-based plausibility form (`Kind` `LowPercC`/`HighPercC`) are
    population/basket-level checks a single-row `CHECK` cannot express -
    same exclusion as `check_feature_constraints`, not attempted here
    either.
    """
    result: list[CheckConstraint] = []
    notes: list[str] = []
    counter = 0
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("SimpleConstraint"):
            continue
        if getattr(constraint, "Kind", None) not in (None, "MandC"):
            continue
        if getattr(constraint, "Percentage", None) is not None:
            continue
        expr = getattr(constraint, "LogicalExpression", None)
        if expr is None:
            continue
        counter += 1
        name = getattr(constraint, "Name", None)
        label = name or f"CONSTRAINT #{counter}"
        try:
            sql_expr = _expression_to_sql(expr, column_names, renamed)
        except _UnsupportedCheckExpression as exc:
            notes.append(f"MANDATORY CONSTRAINT {label!r}: {exc} - CHECK not generated (Lot 2)")
            continue
        constraint_name = _truncate_identifier(_sql_identifier(f"chk_{table_name}_{name}" if name else f"chk_{table_name}_{counter}"))
        result.append(CheckConstraint(constraint_name, sql_expr))
    return result, notes


def _unique_constraints_for_class(cls: MetaInstance, table_name: str) -> tuple[list[UniqueConstraint], list[str]]:
    """Return `(constraints, notes)` for `cls`'s own `UniqueConstraint`s - simple `Kind=GlobalU`, no `->` navigation, only.

    `Kind=LocalU` is handled separately by `_local_unique_constraints_for_class`
    (a different SQL shape entirely - scoped to a `BAG`/`LIST OF STRUCTURE`
    child table, not this table) - silently skipped here, never noted twice.
    """
    result: list[UniqueConstraint] = []
    notes: list[str] = []
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("UniqueConstraint"):
            continue
        kind = getattr(constraint, "Kind", None)
        if kind == "LocalU":
            continue
        path_defs = getattr(constraint, "UniqueDef", None) or []
        if kind != "GlobalU" or not path_defs:
            notes.append(f"UNIQUE ({kind}): basket-scoped UNIQUE not supported yet (Lot 1)")
            continue
        columns: list[str] = []
        supported = True
        for path in path_defs:
            path_els = getattr(path, "PathEls", None) or []
            # A single PathEl means a plain own-attribute reference; 2+
            # means the path crossed a role/reference (e.g. `Owner->Code`)
            # to ANOTHER table's attribute - `Kind` itself is always
            # "ReferenceAttr" on every hop (confirmed empirically,
            # 2026-08-27: `Owner->Code` produces PathEls=[('ReferenceAttr',
            # 'Owner'), ('ReferenceAttr', 'Code')], never a distinct Kind
            # for the intermediate role step), so path LENGTH is the only
            # real signal here - same permissive Kind set as
            # `constraint_eval.py`'s own `_resolve_path`.
            if len(path_els) != 1 or getattr(path_els[0], "Kind", None) not in ("ReferenceAttr", "Attribute"):
                notes.append("UNIQUE across a '->' reference - not expressible as a plain SQL table constraint (Lot 1)")
                supported = False
                break
            columns.append(_sql_identifier(getattr(path_els[0], "Ref", None) or ""))
        if supported and columns:
            name = _truncate_identifier(_sql_identifier(f"uq_{table_name}_{'_'.join(columns)}"))
            result.append(UniqueConstraint(name, columns))
    return result, notes


def _local_unique_constraints_for_class(cls: MetaInstance) -> tuple[dict[str, list[list[str]]], list[str]]:
    """Return `({BAG/LIST attr name: [[sub-attr column, ...], ...]}, notes)` for `cls`'s own `Kind=LocalU` `UniqueConstraint`s.

    Each `UniqueDef` entry's `PathEls` is `[role_hop, sub_attr]` (see
    `InterlisModelBuilder._build_local_uniqueness_def` - real corpus usage
    is always exactly this shape, e.g. `UNIQUE (LOCAL) Entries: Code;` ->
    `PathEls=[('ReferenceAttr','Entries'), ('ReferenceAttr','Code')]`): the
    role path (all but the last hop) must be exactly ONE hop naming the
    `BAG`/`LIST OF` attribute, and every entry of the SAME `UniqueConstraint`
    must share that SAME role hop (one `UNIQUE (LOCAL) X: A, B;`-style
    compound constraint over the SAME `BAG`/`LIST`, matching the grammar's
    own single-role-path-then-attribute-list shape) - a multi-hop role
    path or a mix of role hops is grammatically possible but never seen in
    the real corpus, rejected with a note rather than guessed at (RULE #7).
    `build_tables` attaches the resulting column list as one compound
    `UNIQUE` on the matching child table, prefixed with that table's own
    `<parent>_fk` column (RULE #1: reuses the SAME child-table naming
    `_build_child_table` already establishes, not a parallel convention).
    """
    result: dict[str, list[list[str]]] = {}
    notes: list[str] = []
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("UniqueConstraint"):
            continue
        if getattr(constraint, "Kind", None) != "LocalU":
            continue
        path_defs = getattr(constraint, "UniqueDef", None) or []
        if not path_defs:
            notes.append("UNIQUE (LOCAL): role path could not be resolved - not supported")
            continue
        role_attr: str | None = None
        columns: list[str] = []
        supported = True
        for path in path_defs:
            path_els = getattr(path, "PathEls", None) or []
            if len(path_els) < 2 or any(getattr(pe, "Kind", None) not in ("ReferenceAttr", "Attribute") for pe in path_els):
                notes.append("UNIQUE (LOCAL): path shape not supported")
                supported = False
                break
            *role_hops, sub_attr = path_els
            if len(role_hops) != 1:
                notes.append("UNIQUE (LOCAL) across a multi-hop role path - not supported")
                supported = False
                break
            hop_name = getattr(role_hops[0], "Ref", None) or ""
            if role_attr is None:
                role_attr = hop_name
            elif hop_name != role_attr:
                notes.append("UNIQUE (LOCAL) mixing several BAG/LIST attributes in one constraint - not supported")
                supported = False
                break
            columns.append(_sql_identifier(getattr(sub_attr, "Ref", None) or ""))
        if supported and role_attr and columns:
            result.setdefault(role_attr, []).append(columns)
    return result, notes


def build_tables(
    classes: list[MetaInstance], symbol_table: SymbolTable | None = None,
    *, class_symbol_tables: dict[int, SymbolTable] | None = None,
) -> list[Table]:
    """Convert every `Class(Kind=Class)` in `classes` into a `Table` - the dialect-neutral IR every renderer consumes.

    Unlike `convert/jsonschema.py`'s `model_to_json_schema`, this performs
    NO reachability discovery beyond `classes` itself: a STRUCTURE-typed
    attribute is flattened INLINE (`_columns_for_class`), never a separate
    `Table`, so there is nothing beyond the given roots to discover (Lot 1
    scope - see mappings/ilismeta16-to-sql-rules.yml).

    `class_symbol_tables` (`id(cls) -> SymbolTable`, optional) overrides
    `symbol_table` for one specific class when looking up its embedded
    association roles (`_columns_for_class` -> `schema_members_of` ->
    `embedded_roles_of`) - needed for a class that belongs to a DIFFERENT
    model than `symbol_table` (e.g. `cli.cmd_convert_sql`'s `--catalog`
    classes), whose embedding association may be declared in that OTHER
    model's own table, never in `symbol_table`. Deliberately NOT
    `xtf/schema.py`'s `home_symbol_table` (used by `xtf/validate.py` for
    the analogous problem): that helper DISCOVERS the right table from a
    bare qualified-name string via `ModelRepository`, which here would
    return a table built by a SEPARATE parse of the same model file - a
    different Python object graph than the one `cls` itself belongs to,
    breaking `is_class_compatible`'s identity comparison
    (`embedded_roles_of`'s `Super`-chain walk). The caller (`cli.py`)
    already knows, by construction, the exact `SymbolTable` each class
    came from (one `InterlisModelBuilder` per `--catalog` file) - passing
    it directly keeps the class and the table it's queried against in the
    SAME identity graph, which discovery-via-repository cannot guarantee.

    Two real bugs found and fixed by executing the generated DDL against a
    real SQLite engine (2026-08-27, not just eyeballing the text) - neither
    was specific to one renderer, both affect PostgreSQL too:
    1. A short `Class.Name` collision across TOPICs (real corpus cases,
       e.g. two different `Item` classes) produced two `CREATE TABLE item`
       statements - disambiguated the SAME way as
       `convert/jsonschema.py`'s `_assign_keys` (`_2`/`_3` suffix).
    2. `UNIQUE <attr>;` on an attribute whose type never resolved to a
       mapped column (e.g. `INTERLIS.UUIDOID`, real corpus case
       `ili_corpus/Axis_V1_1.ili`) still built a `UNIQUE` constraint
       naming that (never-created) column - `CONSTRAINT ... UNIQUE
       (databaseid)` referencing a column that plain doesn't exist.
       Filtered out here (RULE #5: a note, not a crash-only-at-DDL-time
       surprise) by cross-checking against the columns actually built.
    """
    tables = []
    used_table_names: set[str] = set()
    for cls in classes:
        if getattr(cls, "Kind", None) != "Class":
            continue
        base_name = _sql_identifier(getattr(cls, "Name", None) or "")
        table_name = base_name
        suffix = 2
        while table_name in used_table_names:
            table_name = f"{base_name}_{suffix}"
            suffix += 1
        used_table_names.add(table_name)

        home_table = (class_symbol_tables or {}).get(id(cls), symbol_table)
        columns, foreign_keys, notes, child_specs, nested_local_unique = _columns_for_class(cls, home_table)
        renamed = _avoid_identity_collision(columns)
        unique_constraints, unique_notes = _unique_constraints_for_class(cls, table_name)
        for unique in unique_constraints:
            unique.columns = [renamed.get(c, c) for c in unique.columns]
        column_names = {c.name for c in columns}
        valid_unique_constraints = []
        for unique in unique_constraints:
            missing = [c for c in unique.columns if c not in column_names]
            if missing:
                unique_notes.append(f"UNIQUE ({', '.join(unique.columns)}): column(s) {missing} have no mapped SQL type")
                continue
            valid_unique_constraints.append(unique)
        check_constraints, check_notes = _check_constraints_for_class(cls, table_name, column_names, renamed)
        local_unique, local_unique_notes = _local_unique_constraints_for_class(cls)
        for attr_name, groups in nested_local_unique.items():
            local_unique.setdefault(attr_name, []).extend(groups)

        parent_table = Table(
            name=table_name, columns=columns, unique_constraints=valid_unique_constraints,
            foreign_keys=foreign_keys, check_constraints=check_constraints,
            notes=notes + unique_notes + check_notes + local_unique_notes,
        )
        tables.append(parent_table)

        for attr_name, multi_value in child_specs:
            child_table, child_renamed, reason = _build_child_table(table_name, attr_name, multi_value, home_table)
            if child_table is None:
                tables[-1].notes.append(f"{attr_name}: BAG/LIST OF - {reason}")
                continue
            child_base_name = child_table.name
            child_name = child_base_name
            suffix = 2
            while child_name in used_table_names:
                child_name = f"{child_base_name}_{suffix}"
                suffix += 1
            used_table_names.add(child_name)
            child_table.name = child_name

            fk_column = _sql_identifier(f"{table_name}_fk")
            child_column_names = {c.name for c in child_table.columns}
            for group_columns in local_unique.pop(attr_name, []):
                remapped = [child_renamed.get(c, c) for c in group_columns]
                full_columns = [fk_column, *remapped]
                missing = [c for c in full_columns if c not in child_column_names]
                if missing:
                    child_table.notes.append(
                        f"UNIQUE (LOCAL) {attr_name}: column(s) {missing} have no mapped SQL type",
                    )
                    continue
                name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(full_columns)}"))
                child_table.unique_constraints.append(UniqueConstraint(name, full_columns))

            tables.append(child_table)

        # Any UNIQUE (LOCAL) whose role hop never matched a real BAG/LIST OF
        # attribute on this class (typo, or a role path this project's
        # grammar mapping doesn't reach) - never silently dropped (RULE #5).
        for attr_name in local_unique:
            parent_table.notes.append(f"UNIQUE (LOCAL) {attr_name}: no matching BAG/LIST OF attribute")

    # 3rd real bug found the same way (PostgreSQL, live `psycopg`-free
    # verification against a real `postgis/postgis` container, 2026-08-27):
    # a `REFERENCE TO`/Role target belonging to a DIFFERENT model (real
    # corpus case, `ili_corpus/LWB_Bewirtschaftungseinheiten_V3_0.ili`'s
    # `Zone_Ausland` -> `LWB_Landwirtschaftliche_Zonengrenzen_Kataloge_V2_0.
    # LZ_Kataloge.LZ_Katalog_TypRef`) resolves to a REAL Class via
    # `reference_target_class` (repository-loaded, so not caught by the
    # "unresolved reference" check in `_columns_for_class`) but that class
    # is NOT among `classes` - Lot 1 deliberately converts ONE model's OWN
    # classes only (no cross-model reachability discovery, unlike
    # `convert/jsonschema.py`'s `_discover_classes` - a real, larger scope
    # decision for a later lot, not made here). The FK column itself
    # (a valid OID string either way) is kept; only the now-dangling
    # `FOREIGN KEY` constraint - which would `ALTER TABLE ... REFERENCES` a
    # table this conversion never creates - is dropped.
    final_table_names = {t.name for t in tables}
    for table in tables:
        kept_fks = []
        for fk in table.foreign_keys:
            if fk.ref_table not in final_table_names:
                table.notes.append(
                    f"FOREIGN KEY ({', '.join(fk.columns)}): target table {fk.ref_table!r} belongs to a "
                    "different model, not created by this conversion - constraint dropped, column kept",
                )
                continue
            kept_fks.append(fk)
        table.foreign_keys = kept_fks
    return tables


def render_postgresql(tables: list[Table]) -> str:
    """Render `tables` as PostgreSQL DDL text - `CREATE TABLE` (with inline `UNIQUE`) then `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY`.

    Foreign keys are added via a SEPARATE `ALTER TABLE` pass after every
    `CREATE TABLE` - sidesteps forward-reference ordering entirely (a
    table's own FK target may be declared later in `tables`) rather than
    topologically sorting them, a real simplification PostgreSQL affords
    (unlike a future SQLite/GPKG renderer, which cannot add a FOREIGN KEY
    to an existing table at all - see docs/sql-conversion-strategy.md -
    and so will need to sort tables and declare FKs inline instead).
    """
    statements: list[str] = []
    for table in tables:
        lines = [f"    {_quote(OID_COLUMN)} text UNIQUE NOT NULL"]
        for column in table.columns:
            null_clause = "" if column.nullable else " NOT NULL"
            sql_type = f"geometry({column.geometry_type}, {column.srid})" if column.geometry_type else column.sql_type
            lines.append(f"    {_quote(column.name)} {sql_type}{null_clause}")
        for unique in table.unique_constraints:
            lines.append(f"    CONSTRAINT {unique.name} UNIQUE ({_quote_list(unique.columns)})")
        for check in table.check_constraints:
            lines.append(f"    CONSTRAINT {check.name} CHECK ({check.expression})")
        body = ",\n".join(lines)
        statements.append(f"CREATE TABLE {_quote(table.name)} (\n{body}\n);")
        for note in table.notes:
            statements.append(f"-- NOTE ({table.name}): {note}")
    for table in tables:
        for fk in table.foreign_keys:
            statements.append(
                f"ALTER TABLE {_quote(table.name)} ADD CONSTRAINT {fk.name} "
                f"FOREIGN KEY ({_quote_list(fk.columns)}) REFERENCES {_quote(fk.ref_table)} ({_quote_list(fk.ref_columns)});",
            )
    return "\n".join(statements) + "\n"


def render_gpkg(tables: list[Table]) -> str:
    """Render `tables` as SQLite/GeoPackage DDL text - everything inline at `CREATE TABLE` time, plus the GeoPackage bootstrap rows.

    Assumes the target `.gpkg` file already exists with the standard
    GeoPackage system tables (`gpkg_contents`/`gpkg_geometry_columns`/
    `gpkg_spatial_ref_sys`/...) already in place - created by GDAL itself
    (e.g. `ogr2ogr -f GPKG target.gpkg -dsco VERSION=1.3` with no layers,
    or any prior GDAL write to the same file) - this function only ADDS
    rows/tables to it, never creates the container from scratch (same
    "GDAL owns the mature bootstrapping, this project owns the schema on
    top" stance as the rest of this module).

    Unlike `render_postgresql`, `FOREIGN KEY` is declared INLINE at
    `CREATE TABLE` time (SQLite cannot add one to an existing table via
    `ALTER TABLE` at all - see docs/sql-conversion-strategy.md) - verified
    empirically that this needs NO topological sort of `tables`: SQLite
    tolerates a `FOREIGN KEY REFERENCES` naming a table that does not YET
    exist at `CREATE TABLE` time (only enforced later, at INSERT/UPDATE,
    and only when `PRAGMA foreign_keys=ON`), unlike PostgreSQL.

    `gpkg_spatial_ref_sys.definition` (the SRS WKT text) is written as an
    explicit, LOUD placeholder rather than fabricated - this project stays
    pure Python (no GDAL/PROJ dependency, see docs/sql-conversion-strategy.md),
    so it has no authoritative source for a real WKT string; `organization`/
    `organization_coordsys_id` (the EPSG code) are correct and are what
    `gpkg_geometry_columns.srs_id` actually keys off in practice - the
    caller should verify/replace `definition` via an authoritative source
    (e.g. `gdalsrsinfo -o wkt2 EPSG:<code>`) before treating the resulting
    GeoPackage as fully spec-compliant. EPSG:4326 is skipped (every valid
    GeoPackage already has it pre-registered per the spec's own mandatory
    default rows).
    """
    statements: list[str] = []
    srids: set[int] = set()
    for table in tables:
        lines = [f"    {_quote(OID_COLUMN)} TEXT UNIQUE NOT NULL"]
        for column in table.columns:
            null_clause = "" if column.nullable else " NOT NULL"
            if column.geometry_type:
                base_type = column.geometry_type[:-1] if column.geometry_type.endswith("Z") else column.geometry_type
                sql_type = base_type.upper()
                srids.add(column.srid)
            else:
                sql_type = column.sql_type
            lines.append(f"    {_quote(column.name)} {sql_type}{null_clause}")
        for unique in table.unique_constraints:
            lines.append(f"    CONSTRAINT {unique.name} UNIQUE ({_quote_list(unique.columns)})")
        for fk in table.foreign_keys:
            lines.append(
                f"    CONSTRAINT {fk.name} FOREIGN KEY ({_quote_list(fk.columns)}) "
                f"REFERENCES {_quote(fk.ref_table)} ({_quote_list(fk.ref_columns)})",
            )
        for check in table.check_constraints:
            lines.append(f"    CONSTRAINT {check.name} CHECK ({check.expression})")
        body = ",\n".join(lines)
        statements.append(f"CREATE TABLE {_quote(table.name)} (\n{body}\n);")
        for note in table.notes:
            statements.append(f"-- NOTE ({table.name}): {note}")

    for table in tables:
        geometry_columns = [c for c in table.columns if c.geometry_type]
        if geometry_columns:
            geom = geometry_columns[0]
            statements.append(
                f"INSERT INTO gpkg_contents (table_name, data_type, identifier, srs_id) "
                f"VALUES ('{table.name}', 'features', '{table.name}', {geom.srid});",
            )
            for column in geometry_columns:
                base_type = column.geometry_type[:-1] if column.geometry_type.endswith("Z") else column.geometry_type
                z = 1 if column.geometry_type.endswith("Z") else 0
                statements.append(
                    f"INSERT INTO gpkg_geometry_columns (table_name, column_name, geometry_type_name, srs_id, z, m) "
                    f"VALUES ('{table.name}', '{column.name}', '{base_type.upper()}', {column.srid}, {z}, 0);",
                )
        else:
            statements.append(
                f"INSERT INTO gpkg_contents (table_name, data_type, identifier) "
                f"VALUES ('{table.name}', 'attributes', '{table.name}');",
            )

    for srid in sorted(srids - {4326}):
        statements.append(
            f"-- TODO: verify/replace this placeholder with the authoritative EPSG:{srid} WKT "
            f"(e.g. `gdalsrsinfo -o wkt2 EPSG:{srid}`) before treating this GeoPackage as fully spec-compliant.",
        )
        statements.append(
            f"INSERT OR IGNORE INTO gpkg_spatial_ref_sys "
            f"(srs_name, srs_id, organization, organization_coordsys_id, definition) "
            f"VALUES ('EPSG:{srid}', {srid}, 'EPSG', {srid}, 'undefined');",
        )
    return "\n".join(statements) + "\n"
