"""IlisMeta16 -> SQL DDL conversion: tables, columns, UNIQUE/FOREIGN KEY/CHECK constraints.

Generates the SCHEMA only - GDAL (`ogr2ogr -append`) does the actual data
loading. Unsupported constructs are never silently dropped: collected
into `Table.notes` and rendered as a `-- NOTE` (RULE #5).

Package layout (concept -> module): `identifiers.py` (naming/quoting),
`model.py` (dialect-neutral IR), `types.py` (type mapping),
`expressions.py` (CONSTRAINT/WHERE -> SQL text), `tables.py` (`Class` ->
`Table`), `views.py` (`View` -> `CREATE VIEW`), `render.py`
(PostgreSQL/GeoPackage rendering). This file re-exports the public API
below.
"""

from __future__ import annotations

from .identifiers import OID_COLUMN, _sql_identifier, _truncate_identifier
from .model import CheckConstraint, Column, ForeignKey, SqlView, Table, UniqueConstraint, UniqueViewTrigger
from .render import collect_diagnostics, render_gpkg, render_postgresql
from .tables import build_tables
from .views import build_views

__all__ = [
    "OID_COLUMN",
    "_sql_identifier",
    "_truncate_identifier",
    "Column",
    "ForeignKey",
    "UniqueConstraint",
    "CheckConstraint",
    "Table",
    "SqlView",
    "UniqueViewTrigger",
    "build_tables",
    "build_views",
    "render_postgresql",
    "render_gpkg",
    "collect_diagnostics",
]
