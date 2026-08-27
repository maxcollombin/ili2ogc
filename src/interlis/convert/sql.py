"""IlisMeta16 -> SQL DDL conversion: tables, columns, UNIQUE/FOREIGN KEY constraints (backlog item 14, Lot 1).

See docs/sql-conversion-strategy.md for the design decision and scope, and
mappings/ilismeta16-to-sql-rules.yml / spec/conversion/sql-mapping.yml for
the concept/field contract this module implements.

Division of labor (docs/interlis-ogc-architecture.md): this module
generates the SCHEMA only (`CREATE TABLE` + inline `UNIQUE` +
`ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY`) - GDAL (`ogr2ogr
-append`) still does the actual DATA LOADING from this project's own `.xtf
-> JSON-FG` output into the tables this module creates. `build_tables()`
produces a dialect-neutral intermediate representation (`Table`/`Column`/
`UniqueConstraint`/`ForeignKey`) from already-built `IlisMeta16` instances,
walked once via the SAME `resolve_attribute`/`attributes_of`/
`schema_members_of` helpers `convert/jsonschema.py` already uses (no
parallel resolution logic) - `render_postgresql` is the first, complete
renderer over that IR; a future GeoPackage/SQLite renderer would consume
the SAME IR (see docs/sql-conversion-strategy.md's SQLite ALTER TABLE
finding for why it needs its own renderer, not just different
identifier-quoting rules).

Lot 1 scope (see mappings/ilismeta16-to-sql-rules.yml for the full,
per-concept rationale): scalar/geometry columns, one level of flattened
STRUCTURE nesting, FOREIGN KEY from REFERENCE TO/embedded roles, and
UNIQUE from the simple (Kind=GlobalU, no `->` navigation) constraint form
only. `BAG`/`LIST OF` attributes, ABSTRACT structure polymorphism,
`(LOCAL)`/cross-reference UNIQUE, and CHECK from CONSTRAINT are all
deliberately out of scope - never silently dropped, each unsupported
construct is collected into `Table.notes` and rendered as a `-- NOTE`
SQL comment (RULE #5).
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

OID_COLUMN = "ogc_fid"
"""Matches GDAL's PostgreSQL driver's own default FID column name (verified 2026-08-27, `pg.html`) - `ogr2ogr -append` needs no extra `-lco FID=...`."""

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


@dataclass
class Column:
    name: str
    sql_type: str
    nullable: bool = True


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


def _geometry_sql_type(resolved: ResolvedAttribute) -> tuple[str, None] | tuple[None, str]:
    """Return `(sql_type, None)` on success or `(None, reason)` on failure, for a CoordType/LineType attribute."""
    if resolved.type_kind == "CoordType":
        coord_type = resolved.type_instance
        sfa = "Point"
    elif resolved.type_kind == "LineType":
        line_type = resolved.type_instance
        coord_type = line_coord_type(line_type)
        kind = getattr(line_type, "Kind", None)
        sfa = "LineString" if kind in ("Polyline", "DirectedPolyline") else "Polygon"
    else:
        return None, "not a geometry type"
    if coord_type is None:
        return None, "vertex CoordType not resolved"
    if len(coord_axes(coord_type)) >= 3:
        sfa += "Z"
    if bool(getattr(resolved.type_instance, "Multi", False)):
        sfa = "Multi" + sfa
    srid = _srid(coord_type)
    if srid is None:
        return None, "no resolved CRS (!!@CRS meta-attribute)"
    return f"geometry({sfa}, {srid})", None


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
) -> tuple[list[Column], list[ForeignKey], list[str]]:
    """Return `(columns, foreign_keys, notes)` for `cls`'s own+inherited members, flattening one level of STRUCTURE nesting inline.

    `prefix` is only ever non-empty on the recursive call flattening a
    STRUCTURE attribute (`"<attr>_"`) - used both to build flattened column
    names AND to detect/refuse a second level of nesting (RULE #7: not
    attempted without a policy for it, see
    mappings/ilismeta16-to-sql-rules.yml's StructureNesting entry).
    """
    columns: list[Column] = []
    foreign_keys: list[ForeignKey] = []
    notes: list[str] = []
    members = schema_members_of(cls, symbol_table) if symbol_table is not None else attributes_of(cls)
    for name, attr in members.items():
        resolved = resolve_attribute(attr)
        label = f"{prefix}{name}"
        col_name = _sql_identifier(label)

        if resolved.type_kind == "MultiValue":
            notes.append(f"{label}: BAG/LIST OF - needs a related child table, not supported yet (Lot 1)")
            continue

        if resolved.type_kind == "Class" and _is_structure(resolved.type_instance):
            if prefix:
                notes.append(f"{label}: STRUCTURE nested more than one level deep - not flattened (Lot 1)")
                continue
            if bool(getattr(resolved.type_instance, "Abstract", False)):
                notes.append(f"{label}: ABSTRACT structure - polymorphism not supported (Lot 1)")
                continue
            sub_columns, sub_fks, sub_notes = _columns_for_class(resolved.type_instance, symbol_table, prefix=f"{label}_")
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
            sql_type, reason = _geometry_sql_type(resolved)
            if sql_type is None:
                notes.append(f"{label}: {reason}")
                continue
            columns.append(Column(col_name, sql_type, nullable=not resolved.mandatory))
            continue

        scalar_type = _scalar_sql_type(resolved)
        if scalar_type is not None:
            columns.append(Column(col_name, scalar_type, nullable=not resolved.mandatory))
            continue

        notes.append(f"{label}: unsupported type {resolved.type_kind!r}")
    return columns, foreign_keys, notes


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
    """
    tables = []
    for cls in classes:
        if getattr(cls, "Kind", None) != "Class":
            continue
        table_name = _sql_identifier(getattr(cls, "Name", None) or "")
        columns, foreign_keys, notes = _columns_for_class(cls, symbol_table)
        unique_constraints, unique_notes = _unique_constraints_for_class(cls, table_name)
        tables.append(Table(
            name=table_name, columns=columns, unique_constraints=unique_constraints,
            foreign_keys=foreign_keys, notes=notes + unique_notes,
        ))
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
        lines = [f"    {OID_COLUMN} text PRIMARY KEY"]
        for column in table.columns:
            null_clause = "" if column.nullable else " NOT NULL"
            lines.append(f"    {column.name} {column.sql_type}{null_clause}")
        for unique in table.unique_constraints:
            lines.append(f"    CONSTRAINT {unique.name} UNIQUE ({', '.join(unique.columns)})")
        body = ",\n".join(lines)
        statements.append(f"CREATE TABLE {table.name} (\n{body}\n);")
        for note in table.notes:
            statements.append(f"-- NOTE ({table.name}): {note}")
    for table in tables:
        for fk in table.foreign_keys:
            statements.append(
                f"ALTER TABLE {table.name} ADD CONSTRAINT {fk.name} "
                f"FOREIGN KEY ({', '.join(fk.columns)}) REFERENCES {fk.ref_table} ({', '.join(fk.ref_columns)});",
            )
    return "\n".join(statements) + "\n"
