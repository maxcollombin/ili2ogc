"""Dialect-neutral SQL schema/view intermediate representation.

`Table`/`Column`/`ForeignKey`/`UniqueConstraint`/`CheckConstraint` are
`tables.py::build_tables`'s output; `SqlView`/`UniqueViewTrigger` are
`views.py::build_views`'s. Every renderer in `render.py` consumes these
same objects - a pure leaf module, no other submodule of this package
needed to build it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    see `expressions.py::_expression_to_sql`.
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
    `-- NOTE`, see `views.py::_view_unique_constraint_ddl`.
    """


@dataclass
class UniqueViewTrigger:
    """A VIEW-level `UniqueConstraint` translated into a `BEFORE INSERT`/`BEFORE UPDATE` trigger on its base table.

    Dialect-neutral (`where`/`columns` reference only the base table's own
    alias/columns, no dialect syntax) - `render.py::_render_view_unique_triggers_postgresql`/
    `_render_view_unique_triggers_gpkg` each wrap the SAME predicate
    (`views.py::_view_unique_trigger_predicate`) in their own `CREATE TRIGGER` form:
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
    test whether the row being written is itself part of the view), see `views.py::_view_unique_trigger_predicate`.
    """
