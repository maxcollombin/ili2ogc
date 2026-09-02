"""IlisMeta16 -> SQL DDL conversion: tables, columns, UNIQUE/FOREIGN KEY/CHECK constraints.

See `mappings/ilismeta16-to-sql-rules.yml` / `spec/conversion/sql-mapping.yml` for
the concept/field contract this module implements.

Division of labor: this module
generates the SCHEMA only (`CREATE TABLE` + `UNIQUE` + `FOREIGN KEY`) -
GDAL (`ogr2ogr -append`) still does the actual DATA LOADING, including
into `BAG`/`LIST OF` child tables: `convert/jsonfg.py`'s
`transfer_to_feature_collection(..., include_child_rows=True)` appends
one synthetic Feature per `BAG`/`LIST` occurrence to the SAME
FeatureCollection, each with its own `"featureType"` matching the child
table name here - GDAL's JSONFG driver already splits a collection by
`featureType` on `-append`, so ONE call loads parent and child rows both -
keeps the "this project transforms, GDAL loads" division intact rather
than adding a live-database-write dependency.
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

Package layout (concept -> module): `identifiers.py` (naming/quoting),
`model.py` (dialect-neutral `Table`/`Column`/... IR), `types.py`
(INTERLIS type -> SQL column type mapping), `expressions.py`
(CONSTRAINT/WHERE `Expression` -> SQL text), `tables.py` (`Class` ->
`Table`, `build_tables`), `views.py` (`View` -> `CREATE VIEW`,
`build_views`), `render.py` (PostgreSQL/GeoPackage DDL text +
diagnostics). This file re-exports the public API below; a caller
reaching for something not in this list should import directly from the
concept module that owns it.
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
