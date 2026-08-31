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
rationale): scalar/geometry columns, up to two levels of flattened
STRUCTURE nesting, one child table per concrete subclass of an ABSTRACT
STRUCTURE attribute (`concrete_structure_subclasses`, mirroring the JSON
Schema `anyOf`), FOREIGN KEY from REFERENCE TO/embedded roles, UNIQUE from the
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
one STRUCTURE hop). Cross-reference
(`->`) and basket-scoped UNIQUE, the percentage-based plausibility form,
and any `CONSTRAINT` needing `THIS`/`PARENT`/aggregate/function-call/
arithmetic context are all deliberately out of scope - never silently
dropped, each unsupported
construct is collected into `Table.notes` and rendered as a `-- NOTE` SQL
comment (RULE #5).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from interlis.builder.forward_refs import SymbolTable
from interlis.convert.constraint_eval import _unquote_text
from interlis.convert.jsonfg import _meta_value
from interlis.convert.jsonschema import _is_integer_range, _is_structure
from interlis.diagnostic_ids import note as _diag
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.schema import (
    ResolvedAttribute,
    _class_related_base_class,
    _role_is_multi,
    attributes_of,
    concrete_structure_subclasses,
    coord_axes,
    is_class_compatible,
    line_coord_type,
    reference_target_class,
    resolve_attribute,
    schema_members_of,
)

OID_COLUMN = "id"
"""Deliberately NOT `PRIMARY KEY`/GDAL's own default FID column name (`ogc_fid`) - verified empirically (2026-08-27, a
real `ogr2ogr -append` against a live PostgreSQL AND GeoPackage) that GDAL treats WHATEVER column it detects as the
table's `PRIMARY KEY` as an auto-managed FID slot, excluded from the INSERT column list entirely (expects the database
to fill it in, e.g. `SERIAL`) - a Feature's `"id"` is NEVER written into it, `PRIMARY KEY "ogc_fid" text` silently
stayed NULL and violated its own NOT NULL constraint. `"id"` matches EXACTLY what the JSON-FG reader (`ogrinfo`,
confirmed) exposes as a plain STRING FIELD in its own right, separate from OGR's internal FID concept - declared `UNIQUE
NOT NULL` (never `PRIMARY KEY`) so GDAL treats it as a normal field to WRITE, not a slot to manage. See
docs/sql-conversion-strategy.md for the full investigation.
"""

_GEOMETRY_KINDS = {"CoordType", "LineType"}
_MAX_IDENTIFIER_LENGTH = 63  # PostgreSQL's own identifier length limit - a real ceiling, not an arbitrary one.
# STRUCTURE-in-STRUCTURE levels flattened inline as `<a>_<b>_<c>` columns; a STRUCTURE deeper than this is a `-- NOTE`.
_MAX_STRUCT_FLATTEN_DEPTH = 2


def _dedup_name(base: str, used: set[str]) -> str:
    """Return `base`, or `base_2`/`base_3`/... if already in `used`; records the result in `used`."""
    name = base
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


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
    """Truncate `name` to PostgreSQL's identifier limit, collision-safe.

    A naive `name[:63]` risks 2 DIFFERENT long names sharing the same
    63-char prefix truncating to the exact same identifier - not
    hypothetical, a real corpus run (`LWB_Bewirtschaftungseinheiten_V3_0`)
    already produced a foreign key name truncated to exactly 63 chars
    mid-word. Replacing the tail with a short hash of the FULL name
    (rather than just chopping it) makes 2 unrelated long names collide
    only in the astronomically unlikely case of a hash collision, without
    needing a globally-tracked `used` set the way `_dedup_name` does for
    VIEW/table names (every one of this function's 15+ call sites would
    otherwise need one threaded through) - and stays deterministic (the
    same full name always truncates to the same result, needed for a
    reproducible re-run of `convert-sql` on an unchanged model).
    """
    if len(name) <= _MAX_IDENTIFIER_LENGTH:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]  # noqa: S324 - collision-avoidance, not security
    keep = _MAX_IDENTIFIER_LENGTH - len(digest) - 1
    return f"{name[:keep]}_{digest}"


def _avoid_identity_collision(columns: list[Column]) -> dict[str, str]:
    """Rename any column literally named `OID_COLUMN` ("id") to `"id_attr"` (or `"id_attr_2"`, ... on a further
    collision), IN PLACE - returns the `{old_name: new_name}` rename map.

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
    """A dialect-portable scalar type name (e.g. "text"/"integer"/"varchar(20)") - ignored by every renderer when
    `geometry_type` is set (each renderer formats geometry columns its own way, see `render_postgresql`/`render_gpkg`).
    """
    nullable: bool = True
    geometry_type: str | None = None
    """SFA type name (e.g. "Point", "MultiPolygonZ") - set ONLY for a geometry column, structured (not pre-formatted) so
    each renderer can express it its own way.
    """
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
    """A complete SQL boolean expression, already portable across PostgreSQL and SQLite (no dialect-specific syntax) -
    see `_expression_to_sql`.
    """


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    unique_constraints: list[UniqueConstraint] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    check_constraints: list[CheckConstraint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Human-readable reasons an attribute/constraint was skipped (RULE #5) - never a silent drop."""


@dataclass
class SqlView:
    name: str
    body: str | None
    """A complete, dialect-portable `SELECT ... FROM ... [WHERE ...]` (comma-join, no dialect-specific syntax), or
    `None` when the View could not be translated - `notes` then says why (RULE #5).
    """
    notes: list[str] = field(default_factory=list)
    triggers: list[UniqueViewTrigger] = field(default_factory=list)
    """A `CREATE VIEW` carries no constraint of its own - a VIEW-level `UNIQUE` that resolves to plain columns of a
    single base table (no reference-hop `extra_joins`, no geometry column) becomes one of these instead of a
    `-- NOTE`, see `_view_unique_constraint_ddl`.
    """


@dataclass
class UniqueViewTrigger:
    """A VIEW-level `UniqueConstraint` translated into a `BEFORE INSERT`/`BEFORE UPDATE` trigger on its base table.

    Dialect-neutral (`where`/`columns` reference only the base table's own
    alias/columns, no dialect syntax) - `_render_view_unique_triggers_postgresql`/
    `_render_view_unique_triggers_gpkg` each wrap the SAME predicate
    (`_view_unique_trigger_predicate`) in their own `CREATE TRIGGER` form:
    PostgreSQL needs a separate PL/pgSQL function; SQLite/GPKG needs two
    triggers (one per INSERT/UPDATE), since a single `CREATE TRIGGER`
    cannot combine both events.
    """

    view_name: str
    label: str
    base_table: str
    alias: str
    columns: list[str]
    where: list[str]
    """The view's own `WHERE` conjuncts, still qualified with `alias` - reused both as-is (to test whether an
    EXISTING other row is itself part of the view) and with `alias` substituted for the trigger row reference (to
    test whether the row being written is itself part of the view), see `_view_unique_trigger_predicate`.
    """


def _srid(coord_type: MetaInstance | None) -> str | None:
    """Return the bare numeric EPSG code (e.g. `"2056"`) from a CoordType's `!!@CRS=EPSG:<code>` meta-attribute, or
    `None`.

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
    """Return `(sfa_type, srid, None)` on success or `(None, None, reason)` on failure, for a CoordType/LineType
    attribute.
    """
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
        return {"XMLDate": "date", "XMLTime": "time", "XMLDateTime": "timestamp"}.get(
            getattr(inst, "Format", None), "text"
        )
    if kind == "BlackboxType":
        return "text"
    return None


def _columns_for_class(
    cls: MetaInstance,
    symbol_table: SymbolTable | None,
    *,
    prefix: str = "",
    depth: int = 0,
) -> tuple[
    list[Column],
    list[ForeignKey],
    list[str],
    list[tuple[str, MetaInstance]],
    dict[str, list[list[str]]],
    list[tuple[str, MetaInstance, bool, bool]],
]:
    """Return `(columns, foreign_keys, notes, child_specs, local_unique, abstract_specs)` for `cls`'s own+inherited
    members, flattening up to `_MAX_STRUCT_FLATTEN_DEPTH` levels of STRUCTURE nesting inline.

    `prefix` is non-empty on the recursive call flattening a STRUCTURE
    attribute (`"<attr>_"`, or `"<attr>_<subattr>_"` at the second level) -
    it builds the flattened column names. `depth` counts how many STRUCTURE
    levels have already been entered; a STRUCTURE found at
    `depth == _MAX_STRUCT_FLATTEN_DEPTH` is refused with a `-- NOTE` rather
    than flattened into an ever-deeper column name (RULE #7: bounded, see
    mappings/ilismeta16-to-sql-rules.yml's StructureNesting entry).

    `child_specs` is `[(label, MultiValue instance), ...]` for every
    `BAG`/`LIST OF` member found at the TOP level or while flattening a
    STRUCTURE (`label` carries the full `"<struct_attr>_"` /
    `"<struct_attr>_<sub_attr>_"` prefix in the nested case, e.g.
    `"zustaendige_behoerde_entries"`) - built into a related child table by
    `_build_child_table` (called from `build_tables`, which alone knows the
    already-used table names to disambiguate against). A `BAG`/`LIST OF`
    reached through one or two flattened STRUCTURE levels still becomes one
    child table keyed by the parent's OID.

    `local_unique` is the SAME shape `_local_unique_constraints_for_class`
    returns, merged up from any nested STRUCTURE's OWN `Kind=LocalU`
    `UniqueConstraint` (the real corpus idiom, e.g. `LocalisationCH_V1.
    MultilingualText` wraps a `BAG`/`LIST` AND declares `UNIQUE (LOCAL)` on
    itself, not on the embedding Class) - keys qualified with the SAME
    `"<struct_attr>_"` prefix as the matching `child_specs` label, so
    `build_tables` can look them up together without knowing they came
    from a nested STRUCTURE at all.

    `abstract_specs` is `[(label, abstract STRUCTURE class, ordered,
    from_multivalue), ...]` for an attribute whose (element) type is an
    ABSTRACT structure - `build_tables` emits one child table per concrete
    subclass found in the symbol table (`concrete_structure_subclasses`),
    mirroring the JSON Schema pipeline's `anyOf`. `from_multivalue`
    distinguishes a `BAG`/`LIST OF <abstract>` from a single-valued
    ABSTRACT structure attribute, only to pick the right `-- NOTE` id when
    no concrete subclass is in the conversion.
    """
    columns: list[Column] = []
    foreign_keys: list[ForeignKey] = []
    notes: list[str] = []
    child_specs: list[tuple[str, MetaInstance]] = []
    local_unique: dict[str, list[list[str]]] = {}
    abstract_specs: list[tuple[str, MetaInstance, bool, bool]] = []
    members = schema_members_of(cls, symbol_table) if symbol_table is not None else attributes_of(cls)
    for name, attr in members.items():
        resolved = resolve_attribute(attr)
        label = f"{prefix}{name}"
        col_name = _sql_identifier(label)

        if resolved.type_kind == "MultiValue":
            multi_value = resolved.type_instance
            if not isinstance(multi_value, MetaInstance):
                notes.append(
                    _diag(
                        "SQL-BAGLIST-ELEMENT-UNRESOLVED",
                        f"{label}: BAG/LIST OF element type not resolved - provide its model via --repo",
                    )
                )
                continue
            element = getattr(multi_value, "BaseType", None)
            is_abstract_struct = (
                isinstance(element, MetaInstance)
                and _is_structure(element)
                and bool(getattr(element, "Abstract", False))
            )
            if is_abstract_struct:
                abstract_specs.append((label, element, bool(getattr(multi_value, "Ordered", False)), True))
                continue
            child_specs.append((label, multi_value))
            continue

        if resolved.type_kind == "Class" and _is_structure(resolved.type_instance):
            if depth >= _MAX_STRUCT_FLATTEN_DEPTH:
                notes.append(
                    _diag(
                        "SQL-STRUCT-NESTED-DEEP",
                        f"{label}: STRUCTURE nested more than {_MAX_STRUCT_FLATTEN_DEPTH} levels deep - not flattened",
                    )
                )
                continue
            if bool(getattr(resolved.type_instance, "Abstract", False)):
                abstract_specs.append((label, resolved.type_instance, False, False))
                continue
            sub_columns, sub_fks, sub_notes, sub_child_specs, sub_local_unique, sub_abstract_specs = _columns_for_class(
                resolved.type_instance,
                symbol_table,
                prefix=f"{label}_",
                depth=depth + 1,
            )
            columns.extend(sub_columns)
            foreign_keys.extend(sub_fks)
            notes.extend(sub_notes)
            child_specs.extend(sub_child_specs)
            abstract_specs.extend(sub_abstract_specs)
            for nested_key, nested_groups in sub_local_unique.items():
                local_unique.setdefault(nested_key, []).extend(nested_groups)
            struct_local_unique, struct_local_unique_notes = _local_unique_constraints_for_class(resolved.type_instance)
            for role_attr, groups in struct_local_unique.items():
                local_unique.setdefault(f"{label}_{role_attr}", []).extend(groups)
            notes.extend(f"{label}: {note}" for note in struct_local_unique_notes)
            continue

        if resolved.type_kind in ("Class", "ReferenceType") and resolved.type_instance is not None:
            target = reference_target_class(resolved)
            if target is None:
                notes.append(
                    _diag(
                        "SQL-REF-TARGET-UNRESOLVED",
                        f"{label}: reference target not resolved - pass its model's directory "
                        f"to --repo, or the model file to --catalog",
                    )
                )
                continue
            target_table = _sql_identifier(getattr(target, "Name", None) or "")
            columns.append(Column(col_name, "text", nullable=not resolved.mandatory))
            fk_name = _truncate_identifier(_sql_identifier(f"fk_{getattr(cls, 'Name', '')}_{label}"))
            foreign_keys.append(ForeignKey(fk_name, [col_name], target_table, [OID_COLUMN]))
            continue

        if resolved.type_kind in _GEOMETRY_KINDS:
            sfa_type, srid, reason = _geometry_column_info(resolved)
            if sfa_type is None:
                notes.append(
                    _diag("SQL-GEOM-NO-CRS", f"{label}: {reason} - provide the geometry base model via --repo")
                )
                continue
            columns.append(
                Column(
                    col_name,
                    sql_type="",
                    nullable=not resolved.mandatory,
                    geometry_type=sfa_type,
                    srid=srid,
                )
            )
            continue

        scalar_type = _scalar_sql_type(resolved)
        if scalar_type is not None:
            columns.append(Column(col_name, scalar_type, nullable=not resolved.mandatory))
            continue

        if resolved.type_kind is None:
            notes.append(
                _diag(
                    "BUILD-TYPE-UNRESOLVED",
                    f"{label}: attribute type not resolved by the model builder - "
                    f"provide the imported model via --repo",
                )
            )
        else:
            notes.append(_diag("SQL-ATTR-TYPE-UNMAPPED", f"{label}: unsupported type {resolved.type_kind!r}"))
    return columns, foreign_keys, notes, child_specs, local_unique, abstract_specs


def _build_child_table(
    parent_table: str,
    attr_name: str,
    multi_value: MetaInstance,
    symbol_table: SymbolTable | None,
) -> tuple[Table | None, dict[str, str], str | None, list[tuple[str, MetaInstance]]]:
    """Return `(child_table, renamed, None, nested_child_specs)` on success or `(None, {}, reason, [])` on failure,
    for one `BAG`/`LIST OF` attribute.

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
    instance). A `BAG`/`LIST OF` found INSIDE that structure - a nested
    multi-value, not flattenable into a column - is returned as
    `nested_child_specs` rather than built here: the caller (`build_tables`)
    turns each into its own `<this table>_<subattr>` child table, one
    level deeper (`build_tables`'s own recursion bound, mirroring
    `_MAX_STRUCT_FLATTEN_DEPTH`). The `ReferenceType`/non-structure `Class`
    branch below is DEFENSIVE only -
    verified (2026-08-27, `attrTypeDef`'s real ANTLR bytecode, RULE #2bis)
    that `BAG`/`LIST OF REFERENCE TO X` is NOT actually constructible by
    this project's vendored grammar at all (`attrTypeDef`'s `(BAG|LIST)
    OF` alternative only ever calls `restrictedStructureRef()` - a named
    `STRUCTURE`, `ANYSTRUCTURE`, or a bare scalar `type_()`, never
    `referenceAttr()`) - contrary to what the abstract eCH-0031 EBNF
    alone would suggest, and confirmed absent from the real corpus too.
    An unmapped `BaseType` kind returns `(None, {}, reason, [])` - the caller
    keeps the pre-existing "-- NOTE" on the PARENT table instead of
    creating an empty/broken child table.
    """
    base_type = getattr(multi_value, "BaseType", None)
    if not isinstance(base_type, MetaInstance):
        return None, {}, "BaseType not resolved", []
    base_kind = base_type._qualified_class.rsplit(".", 1)[-1]

    child_table_name = _sql_identifier(f"{parent_table}_{attr_name}")
    fk_column = _sql_identifier(f"{parent_table}_fk")
    columns: list[Column] = [Column(fk_column, "text", nullable=False)]
    foreign_keys: list[ForeignKey] = [
        ForeignKey(
            _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_{fk_column}")),
            [fk_column],
            parent_table,
            [OID_COLUMN],
        )
    ]
    notes: list[str] = []

    if bool(getattr(multi_value, "Ordered", False)):
        columns.append(Column("seq", "integer", nullable=False))

    nested_child_specs: list[tuple[str, MetaInstance]] = []
    if base_kind == "Class" and _is_structure(base_type):
        if bool(getattr(base_type, "Abstract", False)):
            # An abstract element is routed to `abstract_specs` by `_columns_for_class`
            # (one child table per concrete subclass) - never reaches here.
            return None, {}, "BAG/LIST OF an ABSTRACT structure - subclass polymorphism not mapped to a table", []
        sub_columns, sub_fks, sub_notes, sub_child_specs, _sub_local_unique, _sub_abstract = _columns_for_class(
            base_type, symbol_table
        )
        columns.extend(sub_columns)
        foreign_keys.extend(sub_fks)
        notes.extend(sub_notes)
        nested_child_specs = sub_child_specs
    elif base_kind in ("Class", "ReferenceType"):
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        target = reference_target_class(synthetic)
        if target is None:
            return None, {}, "reference target not resolved - pass its model to --repo or --catalog", []
        target_table = _sql_identifier(getattr(target, "Name", None) or "")
        columns.append(Column("value", "text", nullable=True))
        foreign_keys.append(
            ForeignKey(
                _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_value")),
                ["value"],
                target_table,
                [OID_COLUMN],
            )
        )
    elif base_kind in _GEOMETRY_KINDS:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        sfa_type, srid, reason = _geometry_column_info(synthetic)
        if sfa_type is None:
            return None, {}, reason, []
        columns.append(Column("value", sql_type="", nullable=False, geometry_type=sfa_type, srid=srid))
    else:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        scalar_type = _scalar_sql_type(synthetic)
        if scalar_type is None:
            return None, {}, f"unsupported element type {base_kind!r}", []
        columns.append(Column("value", scalar_type, nullable=False))

    renamed = _avoid_identity_collision(columns)
    table = Table(name=child_table_name, columns=columns, foreign_keys=foreign_keys, notes=notes)
    return table, renamed, None, nested_child_specs


def _build_nested_child_tables(
    parent_child_table: Table,
    child_specs: list[tuple[str, MetaInstance]],
    symbol_table: SymbolTable | None,
    used_table_names: set[str],
) -> list[Table]:
    """Build one `<parent_child_table>_<attr>` table per `BAG`/`LIST OF` attribute found one level inside it.

    `INSPECTION OF <class> -> a -> b` (an indirect/multi-hop path) needs
    exactly this chain of tables to exist (`_build_inspection_view`
    resolves it by walking `<base>_a_b`); a `BAG`/`LIST OF` attribute
    nested this way was previously discarded silently by
    `_build_child_table` (`_sub_child_specs`, now `nested_child_specs`).
    Bounded to ONE level (no recursive call back into this function): no
    real corpus evidence of a THIRD nesting level, and the immediate
    `UNIQUE (LOCAL)`/`struct_global_unique` bookkeeping `build_tables`
    does for a first-level child table doesn't apply here (a `UNIQUE
    (LOCAL)` this deep has no observed real-world case either) - a
    further-nested `BAG`/`LIST OF` inside one of these tables is simply
    noted, not built, same "no real corpus evidence, no crash" stance as
    `_MAX_STRUCT_FLATTEN_DEPTH` for STRUCTURE flattening.
    """
    out: list[Table] = []
    for attr_name, multi_value in child_specs:
        child_table, _renamed, reason, deeper_specs = _build_child_table(
            parent_child_table.name, attr_name, multi_value, symbol_table
        )
        if child_table is None:
            rule = (
                "SQL-BAGLIST-ELEMENT-UNRESOLVED"
                if reason and "not resolved" in reason
                else "SQL-BAGLIST-ELEMENT-UNMAPPED"
            )
            parent_child_table.notes.append(_diag(rule, f"{attr_name}: BAG/LIST OF - {reason}"))
            continue
        base_name = child_table.name
        name = base_name
        suffix = 2
        while name in used_table_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_table_names.add(name)
        child_table.name = name
        if deeper_specs:
            child_table.notes.append(
                _diag(
                    "SQL-BAGLIST-ELEMENT-UNMAPPED",
                    "a further nested BAG/LIST OF attribute is not built as a table (2 levels of nesting is the bound)",
                )
            )
        out.append(child_table)
    return out


def _structure_child_table(
    parent_table: str,
    table_name: str,
    struct_cls: MetaInstance,
    symbol_table: SymbolTable | None,
    *,
    ordered: bool,
) -> tuple[Table, dict[str, str]]:
    """One child table holding instances of a concrete STRUCTURE `struct_cls`.

    Used for an ABSTRACT structure attribute's concrete subclasses (one
    table per subclass, `concrete_structure_subclasses`) - same shape as
    `_build_child_table`'s own STRUCTURE branch (a `<parent>_fk` back to the
    parent, `seq` when `ordered`, then the structure's own flattened
    columns), factored out so `build_tables` can call it per subclass.
    """
    fk_column = _sql_identifier(f"{parent_table}_fk")
    columns: list[Column] = [Column(fk_column, "text", nullable=False)]
    foreign_keys: list[ForeignKey] = [
        ForeignKey(
            _truncate_identifier(_sql_identifier(f"fk_{table_name}_{fk_column}")),
            [fk_column],
            parent_table,
            [OID_COLUMN],
        )
    ]
    if ordered:
        columns.append(Column("seq", "integer", nullable=False))
    sub_columns, sub_fks, sub_notes, _sub_child_specs, _sub_local_unique, _sub_abstract = _columns_for_class(
        struct_cls, symbol_table
    )
    columns.extend(sub_columns)
    foreign_keys.extend(sub_fks)
    renamed = _avoid_identity_collision(columns)
    return Table(name=table_name, columns=columns, foreign_keys=foreign_keys, notes=list(sub_notes)), renamed


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
    `_columns_for_class` builds for up to `_MAX_STRUCT_FLATTEN_DEPTH` levels
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


def _check_constraints_for_class(
    cls: MetaInstance,
    table_name: str,
    column_names: set[str],
    renamed: dict[str, str],
) -> tuple[list[CheckConstraint], list[str]]:
    """Return `(constraints, notes)` for `cls`'s own row-local `MANDATORY CONSTRAINT`s - same scope as
    `constraint_eval.py`'s `check_feature_constraints`.

    `UniqueConstraint` is handled by `_unique_constraints_for_class`/
    `_local_unique_constraints_for_class`. `SetConstraint`/
    `ExistenceConstraint` and the percentage-based plausibility form
    (`SimpleConstraint` with `Percentage`, or `Kind` `LowPercC`/`HighPercC`)
    are population/basket-level checks a single-row `CHECK` cannot express -
    same exclusion as `check_feature_constraints`, not attempted here
    either, but each is surfaced as a `-- NOTE` rather than dropped
    silently (RULE #5 - `SET`/`EXISTENCE` touch ~3%/~8% of the real
    corpus).
    """
    result: list[CheckConstraint] = []
    notes: list[str] = []
    counter = 0
    for constraint in getattr(cls, "Constraint", None) or []:
        qname = constraint._qualified_class.rsplit(".", 1)[-1]
        label = repr(getattr(constraint, "Name", None)) if getattr(constraint, "Name", None) else "<unnamed>"
        if qname == "UniqueConstraint":
            continue  # handled by _unique_constraints_for_class / _local_unique_constraints_for_class
        if qname == "ExistenceConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-EXISTENCE",
                    f"EXISTENCE CONSTRAINT {label}: a check that a value also occurs in another class - "
                    "no single-row SQL CHECK can express it",
                )
            )
            continue
        if qname == "SetConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-SET",
                    f"SET CONSTRAINT {label}: a whole-population check - no single-row SQL CHECK can express it",
                )
            )
            continue
        if qname != "SimpleConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-NOT-ROWLOCAL",
                    f"CONSTRAINT {label} ({qname}): not a row-local MANDATORY CONSTRAINT - no CHECK generated",
                )
            )
            continue
        if (
            getattr(constraint, "Kind", None) not in (None, "MandC")
            or getattr(constraint, "Percentage", None) is not None
        ):
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-PLAUSIBILITY",
                    f"CONSTRAINT {label}: percentage-based plausibility form (Kind="
                    f"{getattr(constraint, 'Kind', None)!r}) - a population ratio no single-row SQL CHECK can express",
                )
            )
            continue
        expr = getattr(constraint, "LogicalExpression", None)
        if expr is None:
            continue
        counter += 1
        name = getattr(constraint, "Name", None)
        try:
            sql_expr = _expression_to_sql(expr, column_names, renamed)
        except _UnsupportedCheckExpression as exc:
            notes.append(
                _diag(
                    "SQL-CHECK-EXPR-UNSUPPORTED",
                    f"MANDATORY CONSTRAINT {name or f'#{counter}'!r}: {exc} - CHECK not generated",
                )
            )
            continue
        constraint_name = _truncate_identifier(
            _sql_identifier(f"chk_{table_name}_{name}" if name else f"chk_{table_name}_{counter}")
        )
        result.append(CheckConstraint(constraint_name, sql_expr))
    return result, notes


def _unique_constraints_for_class(
    cls: MetaInstance, table_name: str
) -> tuple[list[UniqueConstraint], list[str], dict[str, list[list[str]]]]:
    """Return `(constraints, notes, struct_global_unique)` for `cls`'s own `Kind=GlobalU` `UniqueConstraint`s.

    `constraints`: plain `UNIQUE (...)` over own columns (every path a single
    own-attribute hop).

    `struct_global_unique`: `{struct attr: [[sub-attr column, ...], ...]}` for a
    `UNIQUE X->Y` (or compound `UNIQUE X->Y, X->Z`) where `X` is this class's
    own `BAG`/`LIST OF STRUCTURE` attribute - the global (not `(LOCAL)`, so no
    per-parent scoping) counterpart of `_local_unique_constraints_for_class`.
    `build_tables` turns each group into a `UNIQUE` on the `<parent>_<attr>`
    child table, WITHOUT the `<parent>_fk` prefix. A first hop that never
    matches a child table is a real `->` reference/role navigation and gets a
    `SQL-UNIQUE-CROSS-REF` note there.

    `Kind=LocalU` is handled by `_local_unique_constraints_for_class` (skipped
    here, never noted twice). `Kind` on every `PathEl` is always
    "ReferenceAttr" regardless of whether the hop is a role or a structure
    step (confirmed empirically 2026-08-27), so path LENGTH plus the child-
    table match in `build_tables` are the only real signals.
    """
    result: list[UniqueConstraint] = []
    notes: list[str] = []
    struct_global_unique: dict[str, list[list[str]]] = {}
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("UniqueConstraint"):
            continue
        kind = getattr(constraint, "Kind", None)
        if kind == "LocalU":
            continue
        path_defs = getattr(constraint, "UniqueDef", None) or []
        if kind != "GlobalU" or not path_defs:
            notes.append(_diag("SQL-UNIQUE-BASKET", f"UNIQUE ({kind}): basket-scoped UNIQUE not supported yet"))
            continue
        own_columns: list[str] = []
        two_hop: list[tuple[str, str]] = []  # (raw first-hop attr name, sql sub-column)
        bad = False
        for path in path_defs:
            path_els = getattr(path, "PathEls", None) or []
            if any(getattr(pe, "Kind", None) not in ("ReferenceAttr", "Attribute") for pe in path_els):
                bad = True
                break
            refs = [getattr(pe, "Ref", None) or "" for pe in path_els]
            if len(refs) == 1:
                own_columns.append(_sql_identifier(refs[0]))
            elif len(refs) == 2:
                two_hop.append((refs[0], _sql_identifier(refs[1])))
            else:
                bad = True
                break
        if bad or (own_columns and two_hop):
            # 3+ hops, an unexpected PathEl kind, or a mix of an own column
            # and a navigated one in the SAME constraint - not one plain
            # UNIQUE and not one child-table UNIQUE either.
            notes.append(
                _diag(
                    "SQL-UNIQUE-CROSS-REF",
                    "UNIQUE across a '->' reference - not expressible as a plain SQL table constraint",
                )
            )
            continue
        if two_hop:
            first_hops = {h[0] for h in two_hop}
            if len(first_hops) != 1:
                notes.append(
                    _diag(
                        "SQL-UNIQUE-CROSS-REF",
                        "UNIQUE mixing several '->' navigations in one constraint - not expressible in SQL",
                    )
                )
                continue
            struct_global_unique.setdefault(two_hop[0][0], []).append([h[1] for h in two_hop])
            continue
        if own_columns:
            name = _truncate_identifier(_sql_identifier(f"uq_{table_name}_{'_'.join(own_columns)}"))
            result.append(UniqueConstraint(name, own_columns))
    return result, notes, struct_global_unique


def _local_unique_constraints_for_class(cls: MetaInstance) -> tuple[dict[str, list[list[str]]], list[str]]:
    """Return `({BAG/LIST attr name: [[sub-attr column, ...], ...]}, notes)` for `cls`'s own `Kind=LocalU`
    `UniqueConstraint`s.

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
            notes.append(
                _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL): role path could not be resolved - not supported")
            )
            continue
        role_attr: str | None = None
        columns: list[str] = []
        supported = True
        for path in path_defs:
            path_els = getattr(path, "PathEls", None) or []
            if len(path_els) < 2 or any(
                getattr(pe, "Kind", None) not in ("ReferenceAttr", "Attribute") for pe in path_els
            ):
                notes.append(_diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL): path shape not supported"))
                supported = False
                break
            *role_hops, sub_attr = path_els
            if len(role_hops) != 1:
                notes.append(
                    _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL) across a multi-hop role path - not supported")
                )
                supported = False
                break
            hop_name = getattr(role_hops[0], "Ref", None) or ""
            if role_attr is None:
                role_attr = hop_name
            elif hop_name != role_attr:
                notes.append(
                    _diag(
                        "SQL-UNIQUE-LOCAL-UNSUPPORTED",
                        "UNIQUE (LOCAL) mixing several BAG/LIST attributes in one constraint - not supported",
                    )
                )
                supported = False
                break
            columns.append(_sql_identifier(getattr(sub_attr, "Ref", None) or ""))
        if supported and role_attr and columns:
            result.setdefault(role_attr, []).append(columns)
    return result, notes


def build_tables(
    classes: list[MetaInstance],
    symbol_table: SymbolTable | None = None,
    *,
    class_symbol_tables: dict[int, SymbolTable] | None = None,
    class_table_names: dict[int, str] | None = None,
) -> list[Table]:
    """Convert every `Class(Kind=Class)` in `classes` into a `Table` - the dialect-neutral IR every renderer consumes.

    Unlike `convert/jsonschema.py`'s `model_to_json_schema`, this performs
    NO reachability discovery beyond `classes` itself: a STRUCTURE-typed
    attribute is flattened INLINE (`_columns_for_class`), never a separate
    `Table`, so there is nothing beyond the given roots to discover (Lot 1
    scope - see mappings/ilismeta16-to-sql-rules.yml).

    `class_table_names` (`id(cls) -> str`, optional out-param) is filled
    with the final table name chosen for every class - `build_views` uses
    it to map a View's base classes to their tables by identity rather
    than by re-deriving a possibly-disambiguated name.

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
    # For abstract-STRUCTURE subclass discovery: the root table plus every
    # distinct per-class table (--catalog / folded-in models) - a concrete
    # subclass can be registered in a different model's table than the
    # abstract base it extends.
    _distinct_catalog_tables = {id(t): t for t in (class_symbol_tables or {}).values()}.values()
    scan_symbol_tables: list[SymbolTable] = [st for st in [symbol_table, *_distinct_catalog_tables] if st is not None]
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
        if class_table_names is not None:
            class_table_names[id(cls)] = table_name

        home_table = (class_symbol_tables or {}).get(id(cls), symbol_table)
        columns, foreign_keys, notes, child_specs, nested_local_unique, abstract_specs = _columns_for_class(
            cls, home_table
        )
        renamed = _avoid_identity_collision(columns)
        unique_constraints, unique_notes, struct_global_unique = _unique_constraints_for_class(cls, table_name)
        for unique in unique_constraints:
            unique.columns = [renamed.get(c, c) for c in unique.columns]
        column_names = {c.name for c in columns}
        valid_unique_constraints = []
        for unique in unique_constraints:
            missing = [c for c in unique.columns if c not in column_names]
            if missing:
                unique_notes.append(
                    _diag(
                        "SQL-UNIQUE-COL-UNMAPPED",
                        f"UNIQUE ({', '.join(unique.columns)}): column(s) {missing} have no mapped SQL type",
                    )
                )
                continue
            valid_unique_constraints.append(unique)
        check_constraints, check_notes = _check_constraints_for_class(cls, table_name, column_names, renamed)
        local_unique, local_unique_notes = _local_unique_constraints_for_class(cls)
        for attr_name, groups in nested_local_unique.items():
            local_unique.setdefault(attr_name, []).extend(groups)

        parent_table = Table(
            name=table_name,
            columns=columns,
            unique_constraints=valid_unique_constraints,
            foreign_keys=foreign_keys,
            check_constraints=check_constraints,
            notes=notes + unique_notes + check_notes + local_unique_notes,
        )
        tables.append(parent_table)

        for attr_name, multi_value in child_specs:
            child_table, child_renamed, reason, nested_specs = _build_child_table(
                table_name, attr_name, multi_value, home_table
            )
            if child_table is None:
                rule = (
                    "SQL-BAGLIST-ELEMENT-UNRESOLVED"
                    if reason and "not resolved" in reason
                    else "SQL-BAGLIST-ELEMENT-UNMAPPED"
                )
                tables[-1].notes.append(_diag(rule, f"{attr_name}: BAG/LIST OF - {reason}"))
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
                        _diag(
                            "SQL-UNIQUE-COL-UNMAPPED",
                            f"UNIQUE (LOCAL) {attr_name}: column(s) {missing} have no mapped SQL type",
                        )
                    )
                    continue
                name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(full_columns)}"))
                child_table.unique_constraints.append(UniqueConstraint(name, full_columns))

            # `UNIQUE <this attr>->Sub` (global, no per-parent scoping): a
            # plain UNIQUE over the child table's own sub-attribute columns.
            for group_columns in struct_global_unique.pop(attr_name, []):
                remapped = [child_renamed.get(c, c) for c in group_columns]
                missing = [c for c in remapped if c not in child_column_names]
                if missing:
                    cols = ", ".join(group_columns)
                    child_table.notes.append(
                        _diag(
                            "SQL-UNIQUE-COL-UNMAPPED",
                            f"UNIQUE {attr_name}->({cols}): column(s) {missing} have no mapped SQL type",
                        )
                    )
                    continue
                name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(remapped)}"))
                child_table.unique_constraints.append(UniqueConstraint(name, remapped))

            tables.append(child_table)
            if nested_specs:
                tables.extend(_build_nested_child_tables(child_table, nested_specs, home_table, used_table_names))

        # An ABSTRACT structure attribute (single-valued or BAG/LIST OF):
        # one child table per concrete subclass reachable in the symbol
        # table, mirroring the JSON Schema pipeline's `anyOf`. The table
        # name (`<parent>_<attr>_<subclass>`) is the discriminant - no
        # extra `kind` column. No subclass in the conversion -> a `-- NOTE`.
        for attr_name, abstract_cls, ordered, from_multivalue in abstract_specs:
            subclasses = concrete_structure_subclasses(abstract_cls, home_table, *scan_symbol_tables)
            if not subclasses:
                if from_multivalue:
                    rule, kind = "SQL-BAGLIST-ELEMENT-UNMAPPED", "BAG/LIST OF an ABSTRACT structure"
                else:
                    rule, kind = "SQL-STRUCT-ABSTRACT", "ABSTRACT structure"
                parent_table.notes.append(
                    _diag(
                        rule,
                        f"{attr_name}: {kind} - no concrete subclass in this conversion to build a "
                        f"table per subtype (provide the model that defines them via --repo/--catalog)",
                    )
                )
                continue
            groups_for_attr = local_unique.pop(attr_name, [])
            fk_column = _sql_identifier(f"{table_name}_fk")
            for sub in subclasses:
                sub_name = _sql_identifier(getattr(sub, "Name", None) or "")
                child_name = _dedup_name(_sql_identifier(f"{table_name}_{attr_name}_{sub_name}"), used_table_names)
                child_table, child_renamed = _structure_child_table(
                    table_name, child_name, sub, home_table, ordered=ordered
                )
                child_column_names = {c.name for c in child_table.columns}
                for group_columns in groups_for_attr:
                    full_columns = [fk_column, *(child_renamed.get(c, c) for c in group_columns)]
                    missing = [c for c in full_columns if c not in child_column_names]
                    if missing:
                        child_table.notes.append(
                            _diag(
                                "SQL-UNIQUE-COL-UNMAPPED",
                                f"UNIQUE (LOCAL) {attr_name}: column(s) {missing} have no mapped SQL type",
                            )
                        )
                        continue
                    name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(full_columns)}"))
                    child_table.unique_constraints.append(UniqueConstraint(name, full_columns))
                tables.append(child_table)

        # Any UNIQUE (LOCAL) whose role hop never matched a real BAG/LIST OF
        # attribute on this class (typo, or a role path this project's
        # grammar mapping doesn't reach) - never silently dropped (RULE #5).
        for attr_name in local_unique:
            parent_table.notes.append(
                _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", f"UNIQUE (LOCAL) {attr_name}: no matching BAG/LIST OF attribute")
            )

        # `UNIQUE X->Y` whose first hop `X` is not a `BAG`/`LIST OF STRUCTURE`
        # attribute of this class: a real `REFERENCE TO`/role navigation to
        # another table's column (or an abstract structure split across
        # subtype tables). No single table/index constraint expresses it -
        # a BEFORE INSERT/UPDATE trigger would.
        for attr_name, groups in struct_global_unique.items():
            for group_columns in groups:
                parent_table.notes.append(
                    _diag(
                        "SQL-UNIQUE-CROSS-REF",
                        f"UNIQUE {attr_name}->({', '.join(group_columns)}): navigates a '->' reference to another "
                        "table - needs a trigger, not a table constraint",
                    )
                )

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
                    _diag(
                        "SQL-FK-CROSS-MODEL-DROPPED",
                        f"FOREIGN KEY ({', '.join(fk.columns)}): target table {fk.ref_table!r} belongs to a "
                        "different model, not created by this conversion - pass that model via --catalog "
                        "(constraint dropped, column kept)",
                    )
                )
                continue
            kept_fks.append(fk)
        table.foreign_keys = kept_fks
    return tables


class _UnsupportedView(Exception):
    """A View shape this module cannot faithfully turn into a `CREATE VIEW` - caught per-View, surfaced as a `-- NOTE`
    (RULE #5), never a crash.

    `rule` is the stable diagnostic id (`interlis.diagnostic_ids`) - the
    default covers the "expression outside the translatable subset"
    family; the class-C "a base/target table is missing" sites pass
    `SQL-VIEW-BASE-MISSING` explicitly so the message names `--repo`/
    `--catalog` as the fix.
    """

    def __init__(self, message: str, rule: str = "SQL-VIEW-EXPR-UNTRANSLATABLE") -> None:
        super().__init__(message)
        self.rule = rule


class _ViewResolver:
    """Resolves a View's `RenamedBaseView`/`ClassAttribute`/`Where` paths against the already-built `Table`s.

    A View's bases live in an IMPORTED model, so the base `Table`s must be
    part of the SAME conversion (via `interlis convert-sql --catalog
    <base>.ili`) - a missing base table raises `_UnsupportedView` rather
    than emitting a `CREATE VIEW` that would not compile.

    `symbol_for(cls)` returns the `SymbolTable` `cls` was built with (its
    OWN model's, for a `--catalog` class - same `class_symbol_tables` map
    `build_tables` uses), needed to see `cls`'s embedded association roles.

    `assoc_near_roles` (`alias -> role name`, from `_resolve_view_bases`)
    covers a base that is really an embedded 2-role `ASSOCIATION`:
    `_resolve_view_bases` already remapped `alias` onto the CARRIER
    class/table (the endpoint the association is embedded on), so a path
    hop naming the "near" role (the one whose own target IS that carrier -
    a self-reference back to the very row the association is embedded on)
    is a no-op, not a real member lookup - `scalar_ref` skips it and
    continues resolving the REST of the path against the SAME `cls`/`table`.
    The "far" role (the other one, the actual embedded FK) needs no special
    handling at all - it is already a normal pseudo-attribute in
    `schema_members_of`.
    """

    def __init__(
        self,
        bases: list[tuple[str, MetaInstance, str]],
        tables_by_name: dict[str, Table],
        symbol_for,
        assoc_near_roles: dict[str, str] | None = None,
    ) -> None:
        self.by_alias = {alias: (cls, table) for alias, cls, table in bases}
        self.tables_by_name = tables_by_name
        self.symbol_for = symbol_for
        self.assoc_near_roles = assoc_near_roles or {}
        self.extra_joins: list[tuple[str, str, str]] = []  # (table, alias, ON-condition SQL)
        self._counter = 0

    def _members(self, cls: MetaInstance) -> dict[str, MetaInstance]:
        st = self.symbol_for(cls)
        return schema_members_of(cls, st) if st is not None else attributes_of(cls)

    def _columns(self, table_name: str) -> set[str]:
        return {c.name for c in self.tables_by_name[table_name].columns}

    def _flattened_struct_column(self, resolved: ResolvedAttribute, col: str, table: str) -> str | None:
        """Resolve a VIEW attribute naming a single-valued STRUCTURE to its ONE flattened column, or `None`.

        `build_tables`/`_columns_for_class` flattens a single-valued
        non-abstract STRUCTURE inline (`"<attr>_<subattr>"`, up to
        `_MAX_STRUCT_FLATTEN_DEPTH` levels) - `scalar_ref`'s bare
        single-hop lookup doesn't know that convention, so a VIEW
        attribute naming the STRUCTURE itself (`Name := Class -> Struct`,
        not `Class -> Struct -> SubField`) always failed even though the
        base table has a real column for it. Real corpus idiom this
        unblocks: eCH-0031's `MandatoryCatalogueReference` (a STRUCTURE
        wrapping exactly one `Reference : REFERENCE TO` attribute, e.g.
        `RichtplanungErneuerbareEnergien_V1.Katalog_Energieform.EnergieformRef`)
        - only handled when the STRUCTURE flattens to EXACTLY one column,
        since a bare struct-typed hop is otherwise ambiguous about which
        of several flattened columns it means.
        """
        if resolved.type_kind != "Class" or not _is_structure(resolved.type_instance):
            return None
        if bool(getattr(resolved.type_instance, "Abstract", False)):
            return None
        sub_columns, *_ = _columns_for_class(
            resolved.type_instance, self.symbol_for(resolved.type_instance), prefix=f"{col}_", depth=1
        )
        if len(sub_columns) != 1:
            return None
        flattened = sub_columns[0].name
        return flattened if flattened in self._columns(table) else None

    def scalar_ref(self, factor: MetaInstance) -> str:
        """Return `"alias"."column"` for a `PathOrInspFactor`, registering any JOINs its intermediate reference hops
        need.
        """
        if factor._qualified_class.endswith("Constant"):
            return _view_constant_literal(factor)
        if not factor._qualified_class.endswith("PathOrInspFactor") or getattr(factor, "Inspection", None):
            raise _UnsupportedView("an expression is not a plain attribute path or constant")
        refs = [getattr(el, "Ref", None) for el in (getattr(factor, "PathEls", None) or [])]
        if not refs or refs[0] is None:
            raise _UnsupportedView("empty attribute path")
        alias = refs[0].lower()
        if alias not in self.by_alias:
            raise _UnsupportedView(f"path root {refs[0]!r} is not a base of this view")
        cls, table = self.by_alias[alias]
        cur_alias = alias
        if len(refs) == 1:
            # a bare base reference denotes the object itself -> its identity column
            return f'"{cur_alias}"."{OID_COLUMN}"'
        for i, hop in enumerate(refs[1:], start=1):
            if hop is None:
                raise _UnsupportedView("path element with no name")
            is_last = i == len(refs) - 1
            if hop == self.assoc_near_roles.get(cur_alias):
                # The association is embedded ON this very row - navigating
                # its "near" role is a self-reference, not a real hop.
                if is_last:
                    return f'"{cur_alias}"."{OID_COLUMN}"'
                continue
            attr = self._members(cls).get(hop)
            if attr is None:
                raise _UnsupportedView(f"{hop!r} is not an attribute/role of {getattr(cls, 'Name', None)!r}")
            resolved = resolve_attribute(attr)
            col = _sql_identifier(hop)
            if is_last:
                if col not in self._columns(table):
                    flattened = self._flattened_struct_column(resolved, col, table)
                    if flattened is None:
                        raise _UnsupportedView(f"{hop!r} has no mapped column on table {table!r}")
                    col = flattened
                return f'"{cur_alias}"."{col}"'
            target = reference_target_class(resolved) if resolved.type_kind in ("Class", "ReferenceType") else None
            if target is None:
                raise _UnsupportedView(f"cannot navigate through {hop!r} - not a resolvable reference/role")
            target_table = _sql_identifier(getattr(target, "Name", None) or "")
            if target_table not in self.tables_by_name:
                raise _UnsupportedView(
                    f"join target table {target_table!r} not built - pass its model via --repo or --catalog",
                    "SQL-VIEW-BASE-MISSING",
                )
            if col not in self._columns(table):
                raise _UnsupportedView(f"reference {hop!r} has no FK column on table {table!r}")
            self._counter += 1
            new_alias = _truncate_identifier(f"j{self._counter}_{target_table}")
            self.extra_joins.append(
                (target_table, new_alias, f'"{cur_alias}"."{col}" = "{new_alias}"."{OID_COLUMN}"'),
            )
            cls, table, cur_alias = target, target_table, new_alias
        raise _UnsupportedView("unreachable")  # pragma: no cover

    def defined_sql(self, factor: MetaInstance) -> str:
        """Return a SQL boolean for `DEFINED(<base-alias> -> role -> role ...)` - an association-navigation existence
        test.

        This is the DMAV `*_Gueltig` VIEW idiom: a `WHERE` built only from
        nested `DEFINED()` over association-role paths. Each hop is a
        2-role association; the hop becomes an `EXISTS (SELECT 1 FROM
        <next> <v> WHERE <join> [AND <rest>])`, with `<join>` reading the
        FK from whichever side actually carries it (the DMAV 1:0..1
        associations embed it on the child). A path element that is a
        scalar attribute, not a role, or a many-to-many association (no
        embedded FK) demotes the whole VIEW (RULE #5).
        """
        if not factor._qualified_class.endswith("PathOrInspFactor") or getattr(factor, "Inspection", None):
            raise _UnsupportedView("DEFINED(...) argument is not a plain association path")
        refs = [getattr(el, "Ref", None) for el in (getattr(factor, "PathEls", None) or [])]
        if len(refs) < 2 or refs[0] is None or refs[0].lower() not in self.by_alias:
            raise _UnsupportedView("DEFINED(...) path root is not a base of this view")
        cls, table = self.by_alias[refs[0].lower()]
        return self._defined_step(cls, f'"{refs[0].lower()}"', table, refs[1:])

    def _defined_step(self, cls: MetaInstance, cur_alias: str, cur_table: str, hops: list[str]) -> str:
        hop, rest = hops[0], hops[1:]
        if hop is None:
            raise _UnsupportedView("DEFINED(...) path element has no name")
        if not rest:
            # A final scalar attribute: DEFINED(...->attr) == <attr> IS NOT NULL.
            # The DMAV `*_Gueltig` idiom ends on `GSNachfuehrung.Grundbucheintrag`,
            # a plain XMLDateTime, not another association hop.
            col = _sql_identifier(hop)
            if col in self._columns(cur_table):
                return f'{cur_alias}."{col}" IS NOT NULL'
        target_cls, target_table, fk_on_current, fk_col = self._resolve_association_hop(cls, hop)
        self._counter += 1
        v_quoted = f'"v{self._counter}"'
        join = (
            f'{v_quoted}."{OID_COLUMN}" = {cur_alias}."{fk_col}"'
            if fk_on_current
            else f'{v_quoted}."{fk_col}" = {cur_alias}."{OID_COLUMN}"'
        )
        tail = f" AND {self._defined_step(target_cls, v_quoted, target_table, rest)}" if rest else ""
        return f'EXISTS (SELECT 1 FROM "{target_table}" {v_quoted} WHERE {join}{tail})'

    def _resolve_association_hop(self, cls: MetaInstance, hop: str) -> tuple[MetaInstance, str, bool, str]:
        """Find the 2-role association connecting `cls` to `hop`; return `(target class, target table, fk_on_current,
        fk_column)`.

        FK placement mirrors `xtf.schema.embedded_roles_of` exactly (the
        same `build_tables` used to make the columns): the FK sits on the
        `> 1` role's target, else on the second-declared role's target,
        and its column is named after the opposite role.
        """
        st = self.symbol_for(cls)
        candidates = st.all_registered() if st is not None else []
        for cand in candidates:
            if not isinstance(cand, MetaInstance) or cand._qualified_class.rsplit(".", 1)[-1] != "Class":
                continue
            if getattr(cand, "Kind", None) != "Association":
                continue
            roles = [r for r in (getattr(cand, "Role", None) or []) if isinstance(r, MetaInstance)]
            if len(roles) != 2:
                continue
            role_a, role_b = roles
            tgt_a, tgt_b = _class_related_base_class(role_a), _class_related_base_class(role_b)
            if tgt_a is None or tgt_b is None:
                continue

            def matches_hop(role: MetaInstance, tgt: MetaInstance) -> bool:
                return getattr(role, "Name", None) == hop or getattr(tgt, "Name", None) == hop

            if is_class_compatible(cls, tgt_a) and matches_hop(role_b, tgt_b):
                near_role, far_role, far_tgt = role_a, role_b, tgt_b
            elif is_class_compatible(cls, tgt_b) and matches_hop(role_a, tgt_a):
                near_role, far_role, far_tgt = role_b, role_a, tgt_a
            else:
                continue

            multi_a, multi_b = _role_is_multi(role_a), _role_is_multi(role_b)
            if multi_a and multi_b:
                raise _UnsupportedView(f"{hop!r}: a many-to-many association has no embedded FK to navigate")
            if multi_a:
                embed_on = tgt_a
            elif multi_b:
                embed_on = tgt_b
            else:
                embed_on = tgt_b
            fk_on_current = is_class_compatible(cls, embed_on)
            fk_col = _sql_identifier((far_role if fk_on_current else near_role).Name or "")
            far_table = _sql_identifier(getattr(far_tgt, "Name", None) or "")
            if far_table not in self.tables_by_name:
                raise _UnsupportedView(
                    f"navigation target table {far_table!r} not built - pass its model via --repo or --catalog",
                    "SQL-VIEW-BASE-MISSING",
                )
            return far_tgt, far_table, fk_on_current, fk_col
        raise _UnsupportedView(
            f"cannot navigate {hop!r} from {getattr(cls, 'Name', None)!r} - no 2-role association found"
        )


def _view_constant_literal(node: MetaInstance) -> str:
    value, type_ = getattr(node, "Value", None), getattr(node, "Type", None)
    if type_ == "Numeric":
        return _numeric_sql_literal(value)
    if type_ == "Text":
        return _text_sql_literal(value)
    if type_ == "Enumeration":
        return "'" + value.replace("'", "''") + "'"
    raise _UnsupportedView(f"constant of type {type_!r} is not supported in a view expression")


def _view_where_conjuncts(expr: MetaInstance | None, resolver: _ViewResolver) -> list[str]:
    """Flatten a View's `Where` `Expression` tree into a list of SQL boolean strings (implicitly AND-ed)."""
    if expr is None:
        return []
    qname = expr._qualified_class.rsplit(".", 1)[-1]
    op = getattr(expr, "Operation", None)
    if qname == "CompoundExpr" and op == "And":
        out: list[str] = []
        for sub in getattr(expr, "SubExpressions", None) or []:
            out.extend(_view_where_conjuncts(sub, resolver))
        return out
    return [_view_where_sql(expr, resolver)]


def _view_where_sql(expr: MetaInstance, resolver: _ViewResolver) -> str:
    """One View `Where` sub-expression as a parenthesised SQL boolean.

    Same supported subset as `convert/constraint_eval.py` and
    `convert/jsonfg.py`'s `evaluate_view` WHERE evaluation - relational
    comparison of two paths, `And`/`Or`/`Not`, and `DEFINED()` over an
    association path (`_ViewResolver.defined_sql`). Anything else (a
    function call, arithmetic) demotes the whole VIEW (RULE #5).
    """
    qname = expr._qualified_class.rsplit(".", 1)[-1]
    op = getattr(expr, "Operation", None)
    if qname == "CompoundExpr":
        subs = list(getattr(expr, "SubExpressions", None) or [])
        if op == "And":
            return "(" + " AND ".join(_view_where_sql(s, resolver) for s in subs) + ")"
        if op == "Or":
            return "(" + " OR ".join(_view_where_sql(s, resolver) for s in subs) + ")"
        if op in _SQL_RELATIONAL_OPERATORS and len(subs) == 2:
            return f"({resolver.scalar_ref(subs[0])} {_SQL_RELATIONAL_OPERATORS[op]} {resolver.scalar_ref(subs[1])})"
    if qname == "UnaryExpr":
        sub = getattr(expr, "SubExpression", None)
        if op == "Not" and sub is not None:
            return f"(NOT {_view_where_sql(sub, resolver)})"
        if op == "Defined" and sub is not None:
            return f"({resolver.defined_sql(sub)})"
    raise _UnsupportedView(f"WHERE operation {op!r} ({qname}) is not translatable to a SQL view predicate")


def build_views(
    views: list[MetaInstance],
    tables: list[Table],
    *,
    symbol_table: SymbolTable | None = None,
    class_symbol_tables: dict[int, SymbolTable] | None = None,
    class_table_names: dict[int, str] | None = None,
) -> list[SqlView]:
    """Translate each `View` (`FormationKind` Projection/Join only) into a `CREATE VIEW` body, or a `-- NOTE` when it
    can't be done faithfully.

    A View becomes `SELECT <attr := path> ... FROM <base tables + navigated
    join tables, comma-joined> WHERE <translated Where predicates>`. Its
    base classes must be among `tables` (pass their model via
    `interlis convert-sql --catalog`). Anything outside the translatable
    subset - a `Where` predicate that isn't a relational comparison of two
    plain paths, an attribute path that navigates through something other
    than a resolvable reference/role, a base table that wasn't built -
    demotes the WHOLE view to `body=None` with an explanatory note (RULE #5),
    never a half-built `CREATE VIEW`.
    """
    tables_by_name = {t.name: t for t in tables}
    table_name_by_class_id = class_table_names or {}

    def symbol_for(cls: MetaInstance) -> SymbolTable | None:
        return (class_symbol_tables or {}).get(id(cls), symbol_table)

    result: list[SqlView] = []
    used_names: set[str] = {t.name for t in tables}
    for view in views:
        vname = _truncate_identifier(_sql_identifier(getattr(view, "Name", None) or ""))
        while vname in used_names:
            vname = _truncate_identifier(f"{vname}_v")
        used_names.add(vname)
        try:
            bases, assoc_near_roles = _resolve_view_bases(view, tables_by_name, table_name_by_class_id)
            formation = getattr(view, "FormationKind", None)
            if formation == "Union":
                body = _build_union_view(view, bases, tables_by_name, symbol_for, assoc_near_roles)
                result.append(SqlView(vname, body, _view_constraint_notes(view)))
                continue
            if formation == "Inspection":
                body = _build_inspection_view(view, bases, tables_by_name, symbol_for)
                result.append(SqlView(vname, body, _view_constraint_notes(view)))
                continue
            if formation == "Aggregation":
                body = _build_aggregation_view(view, bases, tables_by_name, symbol_for, assoc_near_roles)
                result.append(SqlView(vname, body, _view_constraint_notes(view)))
                continue
            resolver = _ViewResolver(bases, tables_by_name, symbol_for, assoc_near_roles)
            select_items: list[str] = []
            notes: list[str] = []
            attr_col: dict[str, str] = {}
            for attr in getattr(view, "ClassAttribute", None) or []:
                aname = getattr(attr, "Name", None)
                derivates = getattr(attr, "Derivates", None) or []
                if not derivates:
                    raise _UnsupportedView(f"view attribute {aname!r} has no assigned expression")
                out_col = _sql_identifier(aname or "")
                try:
                    col_ref = resolver.scalar_ref(derivates[0])
                    select_items.append(f'{col_ref} AS "{out_col}"')
                    attr_col[(aname or "").lower()] = col_ref
                except _UnsupportedView as exc:
                    # An `ALL OF` pass-through re-exports every base attribute; one it
                    # cannot project as a single column (a STRUCTURE, an unmapped type)
                    # is dropped with a note. An explicit `Name := expression` was asked
                    # for by name and still fails the whole VIEW.
                    if not getattr(derivates[0], "_all_of_identity", False):
                        raise
                    notes.append(_diag("SQL-VIEW-ATTR-DROPPED", f"attribute {aname!r} not in the CREATE VIEW: {exc}"))
            if not select_items:
                raise _UnsupportedView("view has no projectable ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
            where = _view_where_conjuncts(getattr(view, "Where", None), resolver)
            if len(bases) > 1 and getattr(view, "Where", None) is None:
                where += _auto_join_conditions(bases, symbol_for)
            constraint_notes, triggers = _view_unique_constraint_ddl(
                view, vname, bases, tables_by_name, attr_col, where, extra_joins_present=bool(resolver.extra_joins)
            )
            from_parts = [f'"{table}" "{alias}"' for alias, _cls, table in bases]
            from_parts += [f'"{table}" "{alias}"' for table, alias, _on in resolver.extra_joins]
            where += [on for _t, _a, on in resolver.extra_joins]
            body = "SELECT\n    " + ",\n    ".join(select_items) + "\nFROM " + ", ".join(from_parts)
            if where:
                body += "\nWHERE " + "\n  AND ".join(where)
            notes.extend(constraint_notes)
            result.append(SqlView(vname, body, notes, triggers))
        except _UnsupportedView as exc:
            result.append(SqlView(vname, None, [_diag(exc.rule, str(exc))]))
    return result


def _build_union_view(
    view: MetaInstance,
    bases: list[tuple[str, MetaInstance, str]],
    tables_by_name: dict[str, Table],
    symbol_for,
    assoc_near_roles: dict[str, str],
) -> str:
    """Return a `SELECT ... UNION ALL SELECT ...` body for a `FormationKind=Union` view.

    Each union-view `ClassAttribute` carries one `Derivates` entry per base
    (`Attr := C1->A, C2->B`), in base declaration order; branch `i` projects
    every attribute's `Derivates[i]` from base `i` alone. `UNION ALL`, not
    `UNION` - INTERLIS union is a merge (two base objects that project equal
    rows stay two rows), not a set operation. A branch attribute navigating
    a reference hop (`Attr := C1->Role->Field`) joins the target table into
    that BRANCH's own `FROM`, same `resolver.extra_joins` machinery
    PROJECTION/JOIN already use - each branch resolves independently (its
    own `_ViewResolver`), so one branch's join never leaks into another's.
    Any branch expression that is not a plain path/constant (a function, an
    unmapped type) demotes the whole view (RULE #5).
    """
    attrs = getattr(view, "ClassAttribute", None) or []
    if not attrs:
        raise _UnsupportedView("union view has no ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
    branches: list[str] = []
    for branch_index, (alias, cls, table) in enumerate(bases):
        resolver = _ViewResolver([(alias, cls, table)], tables_by_name, symbol_for, assoc_near_roles)
        items: list[str] = []
        for attr in attrs:
            aname = getattr(attr, "Name", None)
            derivates = getattr(attr, "Derivates", None) or []
            if len(derivates) != len(bases):
                raise _UnsupportedView(
                    f"union attribute {aname!r}: {len(derivates)} assigned expression(s) for {len(bases)} bases"
                )
            items.append(f'{resolver.scalar_ref(derivates[branch_index])} AS "{_sql_identifier(aname or "")}"')
        from_parts = [f'"{table}" "{alias}"']
        from_parts += [f'"{t}" "{a}"' for t, a, _on in resolver.extra_joins]
        branch = "SELECT\n    " + ",\n    ".join(items) + "\nFROM " + ", ".join(from_parts)
        join_conditions = [on for _t, _a, on in resolver.extra_joins]
        if join_conditions:
            branch += "\nWHERE " + "\n  AND ".join(join_conditions)
        branches.append(branch)
    return "\nUNION ALL\n".join(branches)


def _build_inspection_view(
    view: MetaInstance,
    bases: list[tuple[str, MetaInstance, str]],
    tables_by_name: dict[str, Table],
    symbol_for,
) -> str:
    """Return a `SELECT ... FROM "<parent>_<attr>[_<sub-attr>]"` body for a `FormationKind=Inspection` view.

    `INSPECTION OF <base> -> attr` yields every element of the inspected
    `BAG`/`LIST OF` structure attribute; `build_tables` already emits that
    extent as the child table `<base_table>_<attr>`, so the view is just a
    projection over it. An indirect path (`-> attr -> sub_attr`, a
    `BAG`/`LIST OF` nested one level inside `attr`'s element structure)
    walks the SAME chain of tables `build_tables`/`_build_nested_child_tables`
    now emits (`<base_table>_<attr>_<sub_attr>`) - a missing table at any
    hop (a chain deeper than that one nesting level, or a geometry
    decomposition with no table at all) demotes the whole view. Each view
    `ClassAttribute` (`out := <base> -> field`) reads `field` straight from
    the FINAL hop's column; `out := PARENT -> field` reads `field` from
    the owning object instead, resolved by joining back to the base table
    on the same `<base_table>_fk` column `build_tables` already puts on
    the child table (`RULE #1`: reuses that naming, does not invent a new
    join convention) - only supported for a single-hop path, since a
    multi-hop child table's FK points at the INTERMEDIATE table, not the
    base table. A single-hop geometry inspection over a SURFACE/AREA
    attribute (`INSPECTION OF <class> -> surfaceAttr`, conceptually
    `SurfaceBoundary`/`SurfaceEdge`, eCH-0031 SS3.15) has no child table
    either, but IS translated - delegated to
    `_build_geometry_inspection_view` (`ST_Boundary` of the base table's
    own geometry column, one row per base object, no `Lines`/`SurfaceEdge`
    sub-structure). A LINE/POLYLINE geometry, or a path nesting more than
    one level deep, still has no child table at all and demotes.
    """
    if len(bases) != 1:
        raise _UnsupportedView("an inspection view has exactly one base", "SQL-VIEW-FORMATION-UNSUPPORTED")
    base_alias, base_cls, base_table = bases[0]
    path = list(getattr(view, "_inspection_path", None) or [])
    if not path:
        raise _UnsupportedView(
            "INSPECTION path (the '-> attribute' chain) was not built - InterlisModelBuilder gap",
            "SQL-VIEW-FORMATION-UNSUPPORTED",
        )
    child_table = base_table
    for hop in path:
        candidate = _sql_identifier(f"{child_table}_{hop}")
        if candidate in tables_by_name:
            child_table = candidate
            continue
        if child_table == base_table and len(path) == 1:
            geom_col = _geometry_inspection_column(base_cls, hop, base_table, tables_by_name, symbol_for)
            if geom_col is not None:
                return _build_geometry_inspection_view(view, base_alias, base_table, hop, geom_col)
        raise _UnsupportedView(
            f"the inspected attribute {' -> '.join(path)!r} has no child table "
            f"(a geometry inspection over a SURFACE/AREA attribute decomposes to its own boundary "
            f"instead - see _build_geometry_inspection_view - a LINE/POLYLINE geometry or a path "
            f"nesting more than one level deep still has no child table at all)",
            "SQL-VIEW-FORMATION-UNSUPPORTED",
        )
    columns = {c.name for c in tables_by_name[child_table].columns}
    base_columns = {c.name for c in tables_by_name[base_table].columns} if base_table in tables_by_name else set()
    fk_col = _sql_identifier(f"{base_table}_fk")
    elem_alias = "insp"
    select_items: list[str] = []
    needs_parent_join = False
    for attr in getattr(view, "ClassAttribute", None) or []:
        aname = getattr(attr, "Name", None)
        derivates = getattr(attr, "Derivates", None) or []
        if not derivates:
            raise _UnsupportedView(f"inspection view attribute {aname!r} has no assigned expression")
        factor = derivates[0]
        if not factor._qualified_class.endswith("PathOrInspFactor") or getattr(factor, "Inspection", None):
            raise _UnsupportedView(f"inspection view attribute {aname!r} is not a plain element path")
        path_els = getattr(factor, "PathEls", None) or []
        refs = [getattr(el, "Ref", None) for el in path_els]
        if len(refs) == 2 and (refs[0] or "").lower() == base_alias and refs[1] is not None:
            col = _sql_identifier(refs[1])
            if col not in columns:
                raise _UnsupportedView(f"element attribute {refs[1]!r} has no column on {child_table!r}")
            select_items.append(f'"{elem_alias}"."{col}" AS "{_sql_identifier(aname or "")}"')
        elif len(path_els) == 2 and getattr(path_els[0], "Kind", None) == "Parent" and refs[1] is not None:
            if len(path) > 1:
                raise _UnsupportedView(
                    "an inspection view attribute navigates PARENT-> on a multi-hop path - the child table's "
                    "FK points at the intermediate table, not the base table",
                    "SQL-VIEW-FORMATION-UNSUPPORTED",
                )
            if fk_col not in columns:
                raise _UnsupportedView(
                    f"the inspected element table {child_table!r} has no {fk_col!r} column to join back to "
                    f"{base_table!r}",
                    "SQL-VIEW-FORMATION-UNSUPPORTED",
                )
            col = _sql_identifier(refs[1])
            if col not in base_columns:
                raise _UnsupportedView(f"PARENT-> attribute {refs[1]!r} has no column on {base_table!r}")
            select_items.append(f'"{base_alias}"."{col}" AS "{_sql_identifier(aname or "")}"')
            needs_parent_join = True
        else:
            raise _UnsupportedView(f"inspection view attribute {aname!r}: unsupported element path {refs}")
    if not select_items:
        raise _UnsupportedView("inspection view has no projectable ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
    from_clause = f'FROM "{child_table}" "{elem_alias}"'
    if needs_parent_join:
        from_clause += (
            f'\nJOIN "{base_table}" "{base_alias}" ON "{elem_alias}"."{fk_col}" = "{base_alias}"."{OID_COLUMN}"'
        )
    return "SELECT\n    " + ",\n    ".join(select_items) + f"\n{from_clause}"


def _geometry_inspection_column(
    base_cls: MetaInstance,
    attr_name: str,
    base_table: str,
    tables_by_name: dict[str, Table],
    symbol_for,
) -> str | None:
    """Return `attr_name`'s SQL column name on `base_table` if it's a single-valued SURFACE/AREA-`Kind` geometry
    attribute, else `None`.

    The shape `_build_inspection_view` delegates to
    `_build_geometry_inspection_view` for. `Kind in ("Surface", "Area")`
    only - a `Polyline`/`DirectedPolyline` LINE attribute has a different
    conceptual decomposition (`LineGeometry`/`LineSegment`, eCH-0031
    SS3.15) not covered here.
    """
    st = symbol_for(base_cls)
    members = schema_members_of(base_cls, st) if st is not None else attributes_of(base_cls)
    attr = members.get(attr_name)
    if attr is None:
        return None
    resolved = resolve_attribute(attr)
    if resolved.type_kind != "LineType" or getattr(resolved.type_instance, "Kind", None) not in ("Surface", "Area"):
        return None
    col = _sql_identifier(attr_name)
    if base_table not in tables_by_name or col not in {c.name for c in tables_by_name[base_table].columns}:
        return None
    return col


def _build_geometry_inspection_view(
    view: MetaInstance,
    base_alias: str,
    base_table: str,
    geom_attr_name: str,
    geom_col: str,
) -> str:
    """Return a `SELECT ST_Boundary(...) FROM "<base>"` body for a single-hop geometry `INSPECTION`.

    `INSPECTION OF <class> -> surfaceAttr` conceptually yields one
    `SurfaceBoundary` (`Lines: LIST OF SurfaceEdge`, eCH-0031 SS3.15) per
    surface - decomposed here as ONE row per base object whose geometry
    IS that boundary (`ST_Boundary`, OGC SFA), the pragmatic reading a
    `GRAPHIC ... BASED ON` an inspection view actually needs (drawing the
    boundary - see docs/view-formation-support.md), rather than the full
    nested `Lines`/`SurfaceEdge` structure - out of scope, eCH-0031 itself
    calls the geometric INSPECTION structures "a conceptual description
    only... generating views belongs to a separate conformance level".
    `ST_Boundary` is OGC SFA / PostGIS SQL - correct against
    `render_postgresql`'s output, but needs SpatiaLite loaded to actually
    EXECUTE against `render_gpkg`'s plain SQLite (both renderers emit the
    SAME dialect-agnostic VIEW body via `_render_views` - this module has
    no per-renderer VIEW SQL yet). Only a view attribute reading the SAME
    inspected geometry attribute back (`out := <base> -> <attr>`) is
    translatable - there is no further sub-structure to select from
    (RULE #5).
    """
    select_items: list[str] = []
    for attr in getattr(view, "ClassAttribute", None) or []:
        aname = getattr(attr, "Name", None)
        derivates = getattr(attr, "Derivates", None) or []
        if not derivates:
            raise _UnsupportedView(f"inspection view attribute {aname!r} has no assigned expression")
        factor = derivates[0]
        if not factor._qualified_class.endswith("PathOrInspFactor") or getattr(factor, "Inspection", None):
            raise _UnsupportedView(f"inspection view attribute {aname!r} is not a plain element path")
        refs = [getattr(el, "Ref", None) for el in (getattr(factor, "PathEls", None) or [])]
        if len(refs) == 2 and (refs[0] or "").lower() == base_alias and refs[1] == geom_attr_name:
            select_items.append(f'ST_Boundary("{base_alias}"."{geom_col}") AS "{_sql_identifier(aname or "")}"')
        else:
            raise _UnsupportedView(
                f"geometry inspection view attribute {aname!r}: only the inspected geometry's own "
                f"boundary ({geom_attr_name!r}) is selectable - no further sub-structure is translated"
            )
    if not select_items:
        raise _UnsupportedView("inspection view has no projectable ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
    return "SELECT\n    " + ",\n    ".join(select_items) + f'\nFROM "{base_table}" "{base_alias}"'


def _build_aggregation_view(
    view: MetaInstance,
    bases: list[tuple[str, MetaInstance, str]],
    tables_by_name: dict[str, Table],
    symbol_for,
    assoc_near_roles: dict[str, str],
) -> str:
    """Return a `SELECT [DISTINCT] ... FROM "<base>" [GROUP BY ...]` body for a `FormationKind=Aggregation` view,
    or demote.

    `AGGREGATION OF <base> (ALL | EQUAL(key))` collapses base objects into
    one instance; inside the view the implicit `AGGREGATES` bag holds the
    grouped objects, for a FUNCTION (`ElementCount := countB(AGGREGATES)`).
    A user FUNCTION body is out of scope by design (delegated to an
    external engine - see docs/fgdm4gs-view-strategy.md) and demotes the
    whole view - EXCEPT the 2 INTERLIS STANDARD functions whose signature
    IS "count the members of a bag/object set" (`INTERLIS.objectCount`/
    `elementCount`), applied to the bag AGGREGATES itself: that becomes a
    plain `COUNT(*)`, no external engine needed. `EQUAL(key)` (stashed by
    the builder as `view._aggregation_key`, `_stash_aggregation_key`) adds
    a real `GROUP BY <key>` - every OTHER plain-path attribute joins the
    key in `GROUP BY` too (same practical effect as `ALL`'s `DISTINCT`,
    now expressed correctly alongside a real aggregate column). `ALL`
    (no key) with a standard-function column and no plain column
    alongside it is a single ungrouped aggregate row (no `GROUP BY`
    needed); mixing a plain column into that combination has no
    well-defined single value to show (no key to group by) and demotes
    the whole view - no real corpus case combines the two. An attribute
    navigating a reference hop (`Attr := <base>->Role->Field`) joins the
    target table in, same `resolver.extra_joins` machinery PROJECTION/JOIN
    already use.
    """
    if len(bases) != 1:
        raise _UnsupportedView("an aggregation view has exactly one base", "SQL-VIEW-FORMATION-UNSUPPORTED")
    resolver = _ViewResolver(bases, tables_by_name, symbol_for, assoc_near_roles)
    key_factor = getattr(view, "_aggregation_key", None)
    group_by: list[str] = []
    if key_factor is not None:
        group_by.append(resolver.scalar_ref(key_factor))
    select_items: list[str] = []
    has_aggregate = False
    has_plain = False
    for attr in getattr(view, "ClassAttribute", None) or []:
        aname = getattr(attr, "Name", None)
        derivates = getattr(attr, "Derivates", None) or []
        if not derivates:
            raise _UnsupportedView(f"aggregation view attribute {aname!r} has no assigned expression")
        factor = derivates[0]
        out_col = _sql_identifier(aname or "")
        if factor._qualified_class.endswith("FunctionCall"):
            select_items.append(f'{_standard_aggregate_function_sql(factor, aname)} AS "{out_col}"')
            has_aggregate = True
            continue
        if not factor._qualified_class.endswith(("PathOrInspFactor", "Constant")):
            raise _UnsupportedView(
                f"aggregation view attribute {aname!r} is a function/expression over the implicit AGGREGATES bag - "
                f"a user FUNCTION body is not translated to SQL",
                "SQL-VIEW-FORMATION-UNSUPPORTED",
            )
        expr = resolver.scalar_ref(factor)
        select_items.append(f'{expr} AS "{out_col}"')
        has_plain = True
        if key_factor is not None and expr not in group_by:
            group_by.append(expr)
    if not select_items:
        raise _UnsupportedView("aggregation view has no projectable ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
    if key_factor is None and has_aggregate and has_plain:
        raise _UnsupportedView(
            "an ALL aggregation combines a FUNCTION over AGGREGATES with a plain attribute - "
            "no EQUAL(...) grouping key to make that combination well-defined",
            "SQL-VIEW-FORMATION-UNSUPPORTED",
        )
    _alias, _cls, table = bases[0]
    from_parts = [f'"{table}" "{_alias}"']
    from_parts += [f'"{t}" "{a}"' for t, a, _on in resolver.extra_joins]
    verb = "SELECT" if key_factor is not None or has_aggregate else "SELECT DISTINCT"
    body = f"{verb}\n    " + ",\n    ".join(select_items) + "\nFROM " + ", ".join(from_parts)
    join_conditions = [on for _t, _a, on in resolver.extra_joins]
    if join_conditions:
        body += "\nWHERE " + "\n  AND ".join(join_conditions)
    if key_factor is not None:
        body += "\nGROUP BY " + ", ".join(group_by)
    return body


_STANDARD_AGGREGATE_COUNT_FUNCTIONS = {"INTERLIS.objectCount", "INTERLIS.elementCount"}


def _is_aggregates_marker(expr: MetaInstance) -> bool:
    """True for the bare `AGGREGATES` argument (`PathEl(Kind=Attribute, Ref=None)` - no `Name`, unlike a real
    attribute).
    """
    if not expr._qualified_class.endswith("PathOrInspFactor"):
        return False
    path_els = getattr(expr, "PathEls", None) or []
    return (
        len(path_els) == 1
        and getattr(path_els[0], "Kind", None) == "Attribute"
        and getattr(path_els[0], "Ref", None) is None
    )


def _standard_aggregate_function_sql(factor: MetaInstance, aname: str | None) -> str:
    """Return `COUNT(*)` for `INTERLIS.objectCount(AGGREGATES)`/`elementCount(AGGREGATES)`, or demote.

    Both standard functions' refman signature ("number of objects/elements
    a bag/object set contains") is exactly `COUNT(*)` when applied to the
    grouped bag itself - a call to any OTHER function, or one of these two
    applied to something other than the bare `AGGREGATES` argument (e.g.
    `INTERLIS.objectCount(SomeOtherClass)`, a valid but UNRELATED whole-
    population idiom used elsewhere for `SET CONSTRAINT`), is a user
    FUNCTION body / an expression outside this narrow subset and demotes
    the whole view.
    """
    func_name = getattr(factor, "Function", None)
    args = getattr(factor, "Arguments", None) or []
    arg_expr = getattr(args[0], "Expression", None) if len(args) == 1 else None
    if func_name in _STANDARD_AGGREGATE_COUNT_FUNCTIONS and arg_expr is not None and _is_aggregates_marker(arg_expr):
        return "COUNT(*)"
    raise _UnsupportedView(
        f"aggregation view attribute {aname!r} is a function/expression over the implicit AGGREGATES bag - "
        f"only INTERLIS.objectCount(AGGREGATES)/elementCount(AGGREGATES) are translated to SQL, "
        f"a user FUNCTION body is not",
        "SQL-VIEW-FORMATION-UNSUPPORTED",
    )


def _view_constraint_notes(view: MetaInstance) -> list[str]:
    """Return a `-- NOTE` per VIEW-level `UNIQUE` / `SET` / `EXISTENCE` constraint - a `CREATE VIEW` cannot carry them.

    DMAV `*_Gueltig` views carry a catalogue-numbered `UNIQUE CHxxxxxx:`;
    a few also carry `SET CONSTRAINT ... INTERLIS.areAreas(...)`. Neither
    is expressible on a SQL view - surfaced here rather than dropped
    silently (RULE #5); enforce downstream (a unique index on a
    materialised view, an application check).
    """
    notes: list[str] = []
    for constraint in getattr(view, "Constraint", None) or []:
        qname = constraint._qualified_class.rsplit(".", 1)[-1]
        label = repr(getattr(constraint, "Name", None)) if getattr(constraint, "Name", None) else "<unnamed>"
        if qname == "UniqueConstraint":
            cols = [
                getattr(pe, "Ref", None)
                for factor in getattr(constraint, "UniqueDef", None) or []
                for pe in getattr(factor, "PathEls", None) or []
            ]
            notes.append(
                _diag(
                    "SQL-VIEW-CONSTRAINT-DROPPED",
                    f"VIEW-level UNIQUE {label} ({', '.join(c for c in cols if c)}) - a CREATE VIEW cannot enforce it",
                )
            )
        elif qname in ("SetConstraint", "ExistenceConstraint"):
            notes.append(
                _diag(
                    "SQL-VIEW-CONSTRAINT-DROPPED",
                    f"VIEW-level {qname} {label} - a whole-population check no CREATE VIEW can carry",
                )
            )
        else:
            notes.append(
                _diag(
                    "SQL-VIEW-CONSTRAINT-DROPPED",
                    f"VIEW-level CONSTRAINT {label} ({qname}) - not carried onto the CREATE VIEW",
                )
            )
    return notes


def _view_unique_constraint_ddl(
    view: MetaInstance,
    vname: str,
    bases: list[tuple[str, MetaInstance, str]],
    tables_by_name: dict[str, Table],
    attr_col: dict[str, str],
    where: list[str],
    *,
    extra_joins_present: bool,
) -> tuple[list[str], list[UniqueViewTrigger]]:
    """Return `(notes, triggers)` for every VIEW-level `Constraint` of a PROJECTION/JOIN view (`Union`/`Aggregation`/
    `Inspection` still go through `_view_constraint_notes` unchanged - no real corpus case combines them with a
    view-level `UNIQUE`).

    A `UniqueConstraint` whose key is a plain view attribute (found in
    `attr_col`, i.e. resolves to a bare `"<alias>"."<col>"` with no
    reference-hop join) on a SINGLE-base view with no `extra_joins`
    becomes a real `UniqueViewTrigger` instead of a dropped note - the
    real corpus case (DMAV `*_Gueltig`, `UNIQUE CHxxxxxx:` on
    `Grundstueck_Gueltig`/`Grenzpunkt_Gueltig`). A geometry-typed key
    column stays a note: SQL `=` is bounding-box equality on a PostGIS
    `geometry`, not exact equality, and would silently accept two
    distinct overlapping-bbox geometries as "unique" - dialect-ambiguous
    correctness, not attempted (RULE #5). `SetConstraint`/
    `ExistenceConstraint` (`INTERLIS.areAreas(...)`, a whole-population
    topology check) stay notes too - same "no engine for a spatial/
    aggregate function" stance as the arithmetic `WHERE` guard.
    """
    notes: list[str] = []
    triggers: list[UniqueViewTrigger] = []
    single_base = bases[0] if len(bases) == 1 and not extra_joins_present else None
    for constraint in getattr(view, "Constraint", None) or []:
        qname = constraint._qualified_class.rsplit(".", 1)[-1]
        label = repr(getattr(constraint, "Name", None)) if getattr(constraint, "Name", None) else "<unnamed>"
        if qname != "UniqueConstraint":
            reason = (
                "a whole-population check no CREATE VIEW/TRIGGER can carry"
                if qname in ("SetConstraint", "ExistenceConstraint")
                else f"not carried onto the CREATE VIEW ({qname})"
            )
            notes.append(_diag("SQL-VIEW-CONSTRAINT-DROPPED", f"VIEW-level {qname} {label} - {reason}"))
            continue
        cols = [
            getattr(pe, "Ref", None)
            for factor in getattr(constraint, "UniqueDef", None) or []
            for pe in getattr(factor, "PathEls", None) or []
        ]
        trigger: UniqueViewTrigger | None = None
        if single_base is not None:
            alias, _cls, table = single_base
            table_columns = {c.name: c for c in tables_by_name[table].columns}
            columns: list[str] = []
            ok = True
            for factor in getattr(constraint, "UniqueDef", None) or []:
                pathels = getattr(factor, "PathEls", None) or []
                col = _sql_identifier(pathels[0].Ref or "") if len(pathels) == 1 else None
                sql_expr = attr_col.get((pathels[0].Ref or "").lower()) if len(pathels) == 1 else None
                if col is None or sql_expr != f'"{alias}"."{col}"' or table_columns.get(col) is None:
                    ok = False
                    break
                if table_columns[col].geometry_type is not None:
                    ok = False
                    break
                columns.append(col)
            if ok and columns:
                trigger = UniqueViewTrigger(vname, label.strip("'"), table, alias, columns, list(where))
        if trigger is not None:
            triggers.append(trigger)
            continue
        notes.append(
            _diag(
                "SQL-VIEW-CONSTRAINT-DROPPED",
                f"VIEW-level UNIQUE {label} ({', '.join(c for c in cols if c)}) - a CREATE VIEW cannot enforce it, "
                "and it is outside the single-base/plain-column subset a BEFORE INSERT/UPDATE trigger can",
            )
        )
    return notes, triggers


def _association_embedding(assoc_cls: MetaInstance) -> tuple[MetaInstance, str, str] | None:
    """Return `(carrier_class, near_role_name, far_role_name)` for a 2-role embedded `ASSOCIATION`.

    Mirrors `xtf.schema.embedded_roles_of`'s own embedding rule, but from
    the association's own perspective (which class does IT embed onto),
    not a candidate target class scanning every association in a
    `SymbolTable` - no `SymbolTable` needed, this only reads
    `assoc_cls.Role` directly. `near_role_name` is the role whose OWN
    target class IS the carrier - a self-reference when navigated FROM
    the association (`PROJECTION OF <assoc> -> <near_role> -> ...`: the
    association's row IS that very carrier row, so `<near_role>` names no
    real hop). `far_role_name` is the OTHER role - already resolvable
    generically as an embedded pseudo-attribute (`schema_members_of`), no
    special handling needed for it. `None` for a many-to-many association
    (not embedded, transferred as its own object per eCH-0031 SS4.3.9.2)
    or a non-2-role/unresolved-target one.
    """
    roles = [r for r in getattr(assoc_cls, "Role", None) or [] if isinstance(r, MetaInstance)]
    if len(roles) != 2:
        return None
    role_a, role_b = roles
    target_a, target_b = _class_related_base_class(role_a), _class_related_base_class(role_b)
    if target_a is None or target_b is None:
        return None
    multi_a, multi_b = _role_is_multi(role_a), _role_is_multi(role_b)
    if multi_a and multi_b:
        return None
    carrier, near_role, far_role = (target_a, role_a, role_b) if multi_a else (target_b, role_b, role_a)
    near_name, far_name = getattr(near_role, "Name", None), getattr(far_role, "Name", None)
    if not near_name or not far_name:
        return None
    return carrier, near_name, far_name


def _direct_association_join(
    cls_a: MetaInstance, cls_b: MetaInstance, symbol_table: SymbolTable | None
) -> tuple[bool, str] | None:
    """Find the (unique) 2-role association directly linking `cls_a` and `cls_b`.

    Returns `(fk_on_a, fk_col)`: `fk_on_a` True means `cls_a`'s table
    carries the FK column `fk_col` referencing `cls_b`'s id, False the
    reverse. Same embedding rule as `_resolve_association_hop`/
    `xtf.schema.embedded_roles_of` (the FK sits on the `> 1`-cardinality
    role's target, else the second-declared role's target; its column is
    named after the OTHER role) - `None` for no such association, a
    many-to-many one (nothing embedded to join on), or 2+ candidates that
    disagree (ambiguous, left to an explicit `WHERE` rather than guessed).
    """
    if symbol_table is None:
        return None
    result: tuple[bool, str] | None = None
    for cand in symbol_table.all_registered():
        if not isinstance(cand, MetaInstance) or cand._qualified_class.rsplit(".", 1)[-1] != "Class":
            continue
        if getattr(cand, "Kind", None) != "Association":
            continue
        roles = [r for r in (getattr(cand, "Role", None) or []) if isinstance(r, MetaInstance)]
        if len(roles) != 2:
            continue
        role_a, role_b = roles
        tgt_a, tgt_b = _class_related_base_class(role_a), _class_related_base_class(role_b)
        if tgt_a is None or tgt_b is None:
            continue
        if is_class_compatible(cls_a, tgt_a) and is_class_compatible(cls_b, tgt_b):
            a_target = tgt_a
        elif is_class_compatible(cls_a, tgt_b) and is_class_compatible(cls_b, tgt_a):
            a_target = tgt_b
        else:
            continue
        multi_a, multi_b = _role_is_multi(role_a), _role_is_multi(role_b)
        if multi_a and multi_b:
            continue
        embed_on, fk_role = (tgt_a, role_b) if multi_a else ((tgt_b, role_a) if multi_b else (tgt_b, role_a))
        fk_col = _sql_identifier(getattr(fk_role, "Name", None) or "")
        if not fk_col:
            continue
        candidate = (embed_on is a_target, fk_col)
        if result is not None and result != candidate:
            return None
        result = candidate
    return result


def _auto_join_conditions(
    bases: list[tuple[str, MetaInstance, str]],
    symbol_for,
) -> list[str]:
    """Derive `WHERE` join predicates connecting every JOIN OF base, for a `Where`-less multi-base VIEW.

    Real corpus `JOIN OF A, B;` with no `WHERE` at all (e.g.
    `Waldabstandslinien_V1_2`'s `Waldabstand_Linie`/`Typ`,
    `ERKAS_Strassen_V2_0`'s `Verkehrsaufkommen`/`Vollzug`) relies on the
    classes being linked by their OWN association - confirmed against
    `ili2c` (`JOIN OF A,B;` alone compiles when exactly one 2-role
    association connects them). Builds a spanning tree over `bases` via
    `_direct_association_join`: each base beyond the first must be
    linkable to some base already in the tree. A base with no direct
    association to the rest (`ERKAS_Strassen_V2_0`'s case - Verkehrsaufkommen
    and Vollzug are only related transitively, through Datenpunkt) can't
    be joined without risking a Cartesian product - demotes the whole VIEW
    (RULE #5) instead of emitting a wrong `FROM a, b` comma-join.
    """
    connected = {bases[0][0]}
    conditions: list[str] = []
    remaining = list(bases[1:])
    progress = True
    while remaining and progress:
        progress = False
        for alias, cls, _table in list(remaining):
            for other_alias, other_cls, _other_table in bases:
                if other_alias not in connected or other_alias == alias:
                    continue
                link = _direct_association_join(cls, other_cls, symbol_for(cls)) or _direct_association_join(
                    cls, other_cls, symbol_for(other_cls)
                )
                if link is None:
                    continue
                fk_on_current, fk_col = link
                conditions.append(
                    f'"{alias}"."{fk_col}" = "{other_alias}"."{OID_COLUMN}"'
                    if fk_on_current
                    else f'"{other_alias}"."{fk_col}" = "{alias}"."{OID_COLUMN}"'
                )
                connected.add(alias)
                remaining.remove((alias, cls, _table))
                progress = True
                break
            if progress:
                break
    if remaining:
        names = ", ".join(getattr(cls, "Name", None) or "?" for _a, cls, _t in remaining)
        raise _UnsupportedView(
            f"JOIN OF has no WHERE and {names} has no direct association linking it to the other base(s) - "
            "cannot derive a join condition without risking a Cartesian product",
            "SQL-VIEW-JOIN-UNLINKED",
        )
    return conditions


def _resolve_view_bases(
    view: MetaInstance,
    tables_by_name: dict[str, Table],
    table_name_by_class_id: dict[int, str],
) -> tuple[list[tuple[str, MetaInstance, str]], dict[str, str]]:
    """Return `(bases, assoc_near_roles)` - `assoc_near_roles` (`alias -> role name`) for `_ViewResolver`, see its
    own docstring.

    A base that is `Kind=Association` has no `CREATE TABLE` of its own
    (`build_tables` only tables `Kind=Class`) - resolved instead to its
    2-role embedding's CARRIER class/table (`_association_embedding`),
    the real corpus shape for `PROJECTION OF <association>`
    (`tests/fixtures/fgdm4gs/Planungszonen_V2_d_B.ili`'s
    `TypPZ_Planungszone`). The alias stays the association's OWN
    name/rename - view attribute paths still spell it that way.
    """
    bases: list[tuple[str, MetaInstance, str]] = []
    assoc_near_roles: dict[str, str] = {}
    used_aliases: set[str] = set()
    for rbv in getattr(view, "RenamedBaseView", None) or []:
        base_cls = getattr(rbv, "BaseView", None)
        if not isinstance(base_cls, MetaInstance):
            raise _UnsupportedView(
                "a base class did not resolve - pass --repo for the base model's own imports", "SQL-VIEW-BASE-MISSING"
            )
        resolved_cls = base_cls
        near_role_name: str | None = None
        if getattr(base_cls, "Kind", None) == "Association":
            embedding = _association_embedding(base_cls)
            if embedding is None:
                raise _UnsupportedView(
                    f"{getattr(base_cls, 'Name', '?')!r} is an ASSOCIATION with no 2-role embedding "
                    "(many-to-many, or fewer/more than 2 roles) - not represented by a table",
                    "SQL-VIEW-BASE-MISSING",
                )
            resolved_cls, near_role_name, _far_role_name = embedding
        table = table_name_by_class_id.get(id(resolved_cls)) or _sql_identifier(
            getattr(resolved_cls, "Name", None) or ""
        )
        if table not in tables_by_name:
            raise _UnsupportedView(
                f"base table {table!r} not built - pass "
                f"{getattr(resolved_cls, 'Name', '?')}'s model via --repo or --catalog",
                "SQL-VIEW-BASE-MISSING",
            )
        alias = (getattr(rbv, "Name", None) or getattr(base_cls, "Name", None) or "").lower()
        base_alias = alias
        suffix = 2
        while alias in used_aliases:
            alias = f"{base_alias}_{suffix}"
            suffix += 1
        used_aliases.add(alias)
        bases.append((alias, resolved_cls, table))
        if near_role_name is not None:
            assoc_near_roles[alias] = near_role_name
    if not bases:
        raise _UnsupportedView(
            "no resolved base classes - pass the base model via --repo or --catalog", "SQL-VIEW-BASE-MISSING"
        )
    return bases, assoc_near_roles


def _render_views(views: tuple[SqlView, ...]) -> list[str]:
    statements: list[str] = []
    for view in views:
        for note in view.notes:
            statements.append(f"-- NOTE (view {view.name}): {note}")
        if view.body is None:
            continue
        indented = view.body.replace("\n", "\n    ")
        statements.append(f'CREATE VIEW "{view.name}" AS\n    {indented};')
    return statements


def _view_unique_trigger_predicate(trig: UniqueViewTrigger, new_ref: str) -> tuple[str, str]:
    """Return `(new_is_in_the_view, a_duplicate_exists)` SQL booleans for `trig` - `new_ref` is the trigger row
    reference (`"NEW"` in both dialects).

    `new_is_in_the_view` reapplies the view's own `WHERE` to the row being
    written (its base-table alias substituted for `new_ref`) - without
    this, a not-yet-valid row (e.g. `DEFINED(...->Entstehung)` still
    false) would be wrongly rejected just for sharing a key with an
    already-valid row. `a_duplicate_exists` re-queries the base table
    (not the `CREATE VIEW` itself - simpler, and avoids depending on the
    view exposing its own identity column) for another row, excluding
    `new_ref` itself, that matches the key AND still satisfies the SAME
    `WHERE` (an existing row that has since become invalid no longer
    counts as a duplicate).
    """
    substituted = [w.replace(f'"{trig.alias}".', f"{new_ref}.") for w in trig.where]
    not_null = " AND ".join(f'{new_ref}."{c}" IS NOT NULL' for c in trig.columns)
    new_is_in_the_view = " AND ".join([f"({not_null})", *substituted])
    key_match = " AND ".join(f'"{trig.alias}"."{c}" = {new_ref}."{c}"' for c in trig.columns)
    conditions = [f'"{trig.alias}"."{OID_COLUMN}" <> {new_ref}."{OID_COLUMN}"', key_match, *trig.where]
    duplicate_exists = (
        f'EXISTS (SELECT 1 FROM "{trig.base_table}" "{trig.alias}" WHERE ' + " AND ".join(conditions) + ")"
    )
    return new_is_in_the_view, duplicate_exists


def _view_unique_trigger_message(trig: UniqueViewTrigger) -> str:
    return f'view "{trig.view_name}": UNIQUE {trig.label} ({", ".join(trig.columns)}) violated'.replace("'", "''")


def _render_view_unique_triggers_postgresql(views: tuple[SqlView, ...]) -> list[str]:
    """Render each `SqlView.triggers` entry as a PL/pgSQL trigger function + `CREATE TRIGGER`.

    A single `BEFORE INSERT OR UPDATE` trigger covers both events -
    PostgreSQL, unlike SQLite, allows combining them in one `CREATE
    TRIGGER`.
    """
    statements: list[str] = []
    for view in views:
        for trig in view.triggers:
            new_ok, duplicate_exists = _view_unique_trigger_predicate(trig, "NEW")
            base = _truncate_identifier(_sql_identifier(f"uq_{trig.view_name}_{trig.label}"))
            fn_name, trg_name = f"{base}_check", f"{base}_trg"
            statements.append(
                f"CREATE OR REPLACE FUNCTION {_quote(fn_name)}() RETURNS trigger AS $$\n"
                "BEGIN\n"
                f"    IF ({new_ok}) AND {duplicate_exists} THEN\n"
                f"        RAISE EXCEPTION '{_view_unique_trigger_message(trig)}';\n"
                "    END IF;\n"
                "    RETURN NEW;\n"
                "END;\n"
                "$$ LANGUAGE plpgsql;"
            )
            statements.append(
                f"CREATE TRIGGER {_quote(trg_name)} BEFORE INSERT OR UPDATE ON {_quote(trig.base_table)}\n"
                f"    FOR EACH ROW EXECUTE FUNCTION {_quote(fn_name)}();"
            )
    return statements


def _render_view_unique_triggers_gpkg(views: tuple[SqlView, ...]) -> list[str]:
    """Render each `SqlView.triggers` entry as two SQLite `CREATE TRIGGER` statements (INSERT + UPDATE).

    SQLite's trigger event is singular (`INSERT`/`UPDATE`/`DELETE`) -
    unlike PostgreSQL's `BEFORE INSERT OR UPDATE`, it cannot be combined
    into one `CREATE TRIGGER`.
    """
    statements: list[str] = []
    for view in views:
        for trig in view.triggers:
            new_ok, duplicate_exists = _view_unique_trigger_predicate(trig, "NEW")
            base = _truncate_identifier(_sql_identifier(f"uq_{trig.view_name}_{trig.label}"))
            message = _view_unique_trigger_message(trig)
            for event in ("INSERT", "UPDATE"):
                trg_name = _truncate_identifier(f"{base}_{event.lower()}")
                statements.append(
                    f"CREATE TRIGGER {_quote(trg_name)}\n"
                    f"BEFORE {event} ON {_quote(trig.base_table)}\n"
                    f"WHEN ({new_ok}) AND {duplicate_exists}\n"
                    "BEGIN\n"
                    f"    SELECT RAISE(ABORT, '{message}');\n"
                    "END;"
                )
    return statements


def render_postgresql(tables: list[Table], views: tuple[SqlView, ...] = ()) -> str:
    """Render `tables` as PostgreSQL DDL text - `CREATE TABLE` (with inline `UNIQUE`) then `ALTER TABLE ... ADD
    CONSTRAINT ... FOREIGN KEY`.

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
                f"FOREIGN KEY ({_quote_list(fk.columns)}) "
                f"REFERENCES {_quote(fk.ref_table)} ({_quote_list(fk.ref_columns)});",
            )
    statements += _render_views(views)
    statements += _render_view_unique_triggers_postgresql(views)
    return "\n".join(statements) + "\n"


def render_gpkg(tables: list[Table], views: tuple[SqlView, ...] = ()) -> str:
    """Render `tables` as SQLite/GeoPackage DDL text - everything inline at `CREATE TABLE` time, plus the GeoPackage
    bootstrap rows.

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
    statements += _render_views(views)
    statements += _render_view_unique_triggers_gpkg(views)
    return "\n".join(statements) + "\n"


_NOTE_RULE_RE = re.compile(r"^\[([A-Z0-9-]+)\]\s*(.*)$", re.DOTALL)


def collect_diagnostics(tables: list[Table], views: tuple[SqlView, ...] = (), *, file: str | None = None):
    """Turn every `Table`/`SqlView` `-- NOTE` back into a `Diagnostic` - the same objects, a third rendering.

    Each note is already `[RULE-ID] message` (`diagnostic_ids.note`), so
    the id, the class (A/B/C -> note/warning) and the message come straight
    back out. The `.sql` keeps its self-describing `-- NOTE` lines; this is
    what feeds `--output-format sarif` and the exit code.
    """
    from interlis.diagnostic_ids import REGISTRY
    from interlis.diagnostics import Diagnostic, Location, severity_for_class

    out: list[Diagnostic] = []

    def _emit(owner: str, note: str) -> None:
        m = _NOTE_RULE_RE.match(note)
        if not m or m.group(1) not in REGISTRY:
            return
        rule, message = m.group(1), m.group(2)
        klass = REGISTRY[rule][0]
        hlp = None
        if klass == "C":
            for marker in (" - pass ", " - provide "):
                head, sep, tail = message.partition(marker)
                if sep:
                    message, hlp = head, marker.strip(" -") + " " + tail
                    break
        out.append(
            Diagnostic(
                severity_for_class(klass),
                rule,
                message,
                Location(file=file, element_path=owner),
                help=hlp,
            )
        )

    for table in tables:
        for note in table.notes:
            _emit(table.name, note)
    for view in views:
        for note in view.notes:
            _emit(f"view {view.name}", note)
    return out
