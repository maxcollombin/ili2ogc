"""Dialect-specific DDL text rendering (PostgreSQL, GeoPackage/SQLite) and the diagnostics rendering of the same
`Table`/`SqlView` notes.
"""

from __future__ import annotations

import re

from .identifiers import OID_COLUMN, _quote, _quote_list
from .model import SqlView, Table
from .views import (
    _render_view_unique_triggers_gpkg,
    _render_view_unique_triggers_postgresql,
    _render_views,
)


def render_postgresql(tables: list[Table], views: tuple[SqlView, ...] = ()) -> str:
    """Render `tables` as PostgreSQL DDL text - `CREATE TABLE` (with inline `UNIQUE`) then `ALTER TABLE ... ADD
    CONSTRAINT ... FOREIGN KEY`.

    FKs are added via a separate `ALTER TABLE` pass after every `CREATE
    TABLE` - sidesteps forward-reference ordering entirely instead of
    topologically sorting.
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

    Assumes the target `.gpkg` already has GDAL's standard GeoPackage
    system tables in place - this only ADDS rows/tables to it. `FOREIGN
    KEY` is declared INLINE (SQLite can't `ALTER TABLE` one in later).
    CAUTION: `gpkg_spatial_ref_sys.definition` (the SRS WKT) is a LOUD
    placeholder, not a real WKT string (no GDAL/PROJ dependency here) -
    verify/replace it via an authoritative source before treating the
    result as fully spec-compliant.
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
