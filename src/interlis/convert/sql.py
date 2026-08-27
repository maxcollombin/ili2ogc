"""IlisMeta16 -> SQL DDL conversion: tables, columns, UNIQUE/FOREIGN KEY constraints (backlog item 14, Lot 1).

See docs/sql-conversion-strategy.md for the design decision and scope, and
mappings/ilismeta16-to-sql-rules.yml / spec/conversion/sql-mapping.yml for
the concept/field contract this module implements.

Division of labor (docs/interlis-ogc-architecture.md): this module
generates the SCHEMA only (`CREATE TABLE` + `UNIQUE` + `FOREIGN KEY`) -
GDAL (`ogr2ogr -append`) still does the actual DATA LOADING, including
into `BAG`/`LIST OF` child tables: `convert/jsonfg.py`'s
`child_feature_collections` emits a companion FeatureCollection per
`BAG`/`LIST` attribute (one Feature per element, `<parent>_fk` carrying
the parent's OID) that GDAL loads with its own SEPARATE `ogr2ogr -append`
call, exactly like the main data - see docs/sql-conversion-strategy.md
for why this keeps the "this project transforms, GDAL loads" division
intact rather than adding a live-database-write dependency.
`build_tables()` produces a dialect-neutral intermediate representation
(`Table`/`Column`/`UniqueConstraint`/`ForeignKey`) from already-built
`IlisMeta16` instances, walked once via the SAME `resolve_attribute`/
`attributes_of`/`schema_members_of` helpers `convert/jsonschema.py`
already uses (no parallel resolution logic) - `render_postgresql`/
`render_gpkg` are the two renderers over that IR.

Lot 1 scope (see mappings/ilismeta16-to-sql-rules.yml for the full,
per-concept rationale): scalar/geometry columns, one level of flattened
STRUCTURE nesting, FOREIGN KEY from REFERENCE TO/embedded roles, UNIQUE
from the simple (Kind=GlobalU, no `->` navigation) constraint form, and
`BAG`/`LIST OF` -> a related child table. ABSTRACT structure
polymorphism, `(LOCAL)`/cross-reference UNIQUE, and CHECK from CONSTRAINT
are all deliberately out of scope - never silently dropped, each
unsupported construct is collected into `Table.notes` and rendered as a
`-- NOTE` SQL comment (RULE #5).
"""
from dataclasses import dataclass, field

from interlis.builder.forward_refs import SymbolTable
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
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    unique_constraints: list[UniqueConstraint] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
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
) -> tuple[list[Column], list[ForeignKey], list[str], list[tuple[str, MetaInstance]]]:
    """Return `(columns, foreign_keys, notes, child_specs)` for `cls`'s own+inherited members, flattening one level of STRUCTURE nesting inline.

    `prefix` is only ever non-empty on the recursive call flattening a
    STRUCTURE attribute (`"<attr>_"`) - used both to build flattened column
    names AND to detect/refuse a second level of nesting (RULE #7: not
    attempted without a policy for it, see
    mappings/ilismeta16-to-sql-rules.yml's StructureNesting entry).

    `child_specs` is `[(label, MultiValue instance), ...]` for every
    `BAG`/`LIST OF` member found at the TOP level (`prefix == ""`) - built
    into a related child table by `_build_child_table` (called from
    `build_tables`, which alone knows the already-used table names to
    disambiguate against). A `BAG`/`LIST OF` NESTED inside a flattened
    STRUCTURE (`prefix` non-empty) stays a plain `-- NOTE` - the SAME
    "one level only" scope limit as STRUCTURE nesting itself, not
    attempted here.
    """
    columns: list[Column] = []
    foreign_keys: list[ForeignKey] = []
    notes: list[str] = []
    child_specs: list[tuple[str, MetaInstance]] = []
    members = schema_members_of(cls, symbol_table) if symbol_table is not None else attributes_of(cls)
    for name, attr in members.items():
        resolved = resolve_attribute(attr)
        label = f"{prefix}{name}"
        col_name = _sql_identifier(label)

        if resolved.type_kind == "MultiValue":
            if prefix or not isinstance(resolved.type_instance, MetaInstance):
                notes.append(f"{label}: BAG/LIST OF nested inside a flattened STRUCTURE - not supported (Lot 1)")
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
            sub_columns, sub_fks, sub_notes, _sub_child_specs = _columns_for_class(
                resolved.type_instance, symbol_table, prefix=f"{label}_",
            )
            columns.extend(sub_columns)
            foreign_keys.extend(sub_fks)
            notes.extend(sub_notes)
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
    return columns, foreign_keys, notes, child_specs


def _build_child_table(
    parent_table: str, attr_name: str, multi_value: MetaInstance, symbol_table: SymbolTable | None,
) -> tuple[Table | None, str | None]:
    """Return `(child_table, None)` on success or `(None, reason)` on failure, for one `BAG`/`LIST OF` attribute.

    Companion to `convert/jsonfg.py`'s child feature collections (see
    docs/sql-conversion-strategy.md) - GDAL loads each via its OWN
    `ogr2ogr -append` into the table this returns, exactly like the main
    data (a `BAG`/`LIST` element is never inlined into the parent's own
    JSON-FG properties for this pipeline, unlike `.xtf -> JSON-FG`'s
    plain conversion). Schema: a `<parent>_fk` `FOREIGN KEY` back to the
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
        return None, "BaseType not resolved"
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
            return None, "BAG/LIST OF an ABSTRACT structure - polymorphism not supported (Lot 1)"
        sub_columns, sub_fks, sub_notes, _sub_child_specs = _columns_for_class(base_type, symbol_table)
        columns.extend(sub_columns)
        foreign_keys.extend(sub_fks)
        notes.extend(sub_notes)
    elif base_kind in ("Class", "ReferenceType"):
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        target = reference_target_class(synthetic)
        if target is None:
            return None, "reference target not resolved (no --repo, or external)"
        target_table = _sql_identifier(getattr(target, "Name", None) or "")
        columns.append(Column("value", "text", nullable=True))
        foreign_keys.append(ForeignKey(
            _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_value")), ["value"], target_table, [OID_COLUMN],
        ))
    elif base_kind in _GEOMETRY_KINDS:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        sfa_type, srid, reason = _geometry_column_info(synthetic)
        if sfa_type is None:
            return None, reason
        columns.append(Column("value", sql_type="", nullable=False, geometry_type=sfa_type, srid=srid))
    else:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        scalar_type = _scalar_sql_type(synthetic)
        if scalar_type is None:
            return None, f"unsupported element type {base_kind!r}"
        columns.append(Column("value", scalar_type, nullable=False))

    _avoid_identity_collision(columns)
    return Table(name=child_table_name, columns=columns, foreign_keys=foreign_keys, notes=notes), None


def _unique_constraints_for_class(cls: MetaInstance, table_name: str) -> tuple[list[UniqueConstraint], list[str]]:
    """Return `(constraints, notes)` for `cls`'s own `UniqueConstraint`s - simple `Kind=GlobalU`, no `->` navigation, only."""
    result: list[UniqueConstraint] = []
    notes: list[str] = []
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("UniqueConstraint"):
            continue
        kind = getattr(constraint, "Kind", None)
        path_defs = getattr(constraint, "UniqueDef", None) or []
        if kind != "GlobalU" or not path_defs:
            # `not path_defs` is its own, separate reason (not just
            # Kind != GlobalU): a `(LOCAL)` constraint's `UniqueDef` is
            # currently ALWAYS empty regardless of Kind (a known, deferred
            # `localUniqueness` construction gap, see
            # spec/grammar/mapping/07_constraints.yml) - checked
            # independently so an empty UniqueDef is never silently
            # skipped with no note just because Kind happened to default
            # to GlobalU (RULE #5).
            notes.append(f"UNIQUE ({kind}): (LOCAL)/basket-scoped UNIQUE not supported yet (Lot 1)")
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


def build_tables(classes: list[MetaInstance], symbol_table: SymbolTable | None = None) -> list[Table]:
    """Convert every `Class(Kind=Class)` in `classes` into a `Table` - the dialect-neutral IR every renderer consumes.

    Unlike `convert/jsonschema.py`'s `model_to_json_schema`, this performs
    NO reachability discovery beyond `classes` itself: a STRUCTURE-typed
    attribute is flattened INLINE (`_columns_for_class`), never a separate
    `Table`, so there is nothing beyond the given roots to discover (Lot 1
    scope - see mappings/ilismeta16-to-sql-rules.yml).

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

        columns, foreign_keys, notes, child_specs = _columns_for_class(cls, symbol_table)
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

        tables.append(Table(
            name=table_name, columns=columns, unique_constraints=valid_unique_constraints,
            foreign_keys=foreign_keys, notes=notes + unique_notes,
        ))

        for attr_name, multi_value in child_specs:
            child_table, reason = _build_child_table(table_name, attr_name, multi_value, symbol_table)
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
            tables.append(child_table)

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
