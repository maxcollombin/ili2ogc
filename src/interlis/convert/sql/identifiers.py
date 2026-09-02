"""SQL identifier naming: lowercase folding, length truncation, reserved-word quoting.

Every table/column/constraint/view name this package emits funnels
through these helpers so PostgreSQL's own unquoted-identifier folding and
GDAL's field-name laundering agree with what gets written here (see
`_sql_identifier`) - a pure leaf module, no other submodule of this
package needed to build it.
"""

from __future__ import annotations

import hashlib

OID_COLUMN = "id"
"""Deliberately NOT `PRIMARY KEY`/GDAL's own default FID column name (`ogc_fid`) - verified empirically (a
real `ogr2ogr -append` against a live PostgreSQL AND GeoPackage) that GDAL treats WHATEVER column it detects as the
table's `PRIMARY KEY` as an auto-managed FID slot, excluded from the INSERT column list entirely (expects the database
to fill it in, e.g. `SERIAL`) - a Feature's `"id"` is NEVER written into it, `PRIMARY KEY "ogc_fid" text` silently
stayed NULL and violated its own NOT NULL constraint. `"id"` matches EXACTLY what the JSON-FG reader (`ogrinfo`,
confirmed) exposes as a plain STRING FIELD in its own right, separate from OGR's internal FID concept - declared `UNIQUE
NOT NULL` (never `PRIMARY KEY`) so GDAL treats it as a normal field to WRITE, not a slot to manage.
"""

_MAX_IDENTIFIER_LENGTH = 63  # PostgreSQL's own identifier length limit - a real ceiling, not an arbitrary one.


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
    (lowercases) field names by default (`LAUNDER=YES`, verified against
    a live database) when it later `-append`s data into a table this module
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


def _quote(name: str) -> str:
    """Double-quote a table/column identifier (ANSI SQL, both PostgreSQL and SQLite accept it).

    Found necessary by executing generated DDL against a real
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
