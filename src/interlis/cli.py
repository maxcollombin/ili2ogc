"""INTERLIS runtime CLI: `interlis build <file.ili>`.

Thin entry point - all the logic lives in interlis.runtime/interlis.builder.
mappings/ and spec/grammar/mapping/ are resolved via _resource_dirs() below:
data embedded in the installed package (force-include at wheel build time,
see pyproject.toml) if present, otherwise falls back to a development repo
checkout (editable install/`uv run` - the data only exists at the repo
root in that mode, never copied under src/interlis/).
"""
import argparse
import importlib.resources
import json
import sys
import warnings
from contextlib import ExitStack, contextmanager
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.convert.jsonfg import transfer_to_feature_collection, unsupported_view_reason
from interlis.convert.jsonschema import model_to_json_schema
from interlis.convert.sql import build_tables, render_gpkg, render_postgresql
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import meta_attribute_comments_in_file, parse_file
from interlis.xtf.model_resolution import header_completeness, header_model_lookup, root_model_names
from interlis.xtf.parse import parse_xtf
from interlis.xtf.validate import validate_transfer

_DEV_ROOT = Path(__file__).resolve().parent.parent.parent


@contextmanager
def _resource_dirs():
    """Yield (mappings_dir, spec_dir) as real filesystem `Path`s.

    Uses the installed package's data (`importlib.resources.as_file`,
    extracted to a temp dir if the package is zipped) when present,
    otherwise falls back to the repo root (dev mode).
    """
    spec_pkg = importlib.resources.files("interlis") / "spec_data" / "grammar" / "mapping"
    if spec_pkg.is_dir():
        with ExitStack() as stack:
            mappings_dir = stack.enter_context(
                importlib.resources.as_file(importlib.resources.files("interlis") / "mappings_data"),
            )
            spec_dir = stack.enter_context(importlib.resources.as_file(spec_pkg))
            yield mappings_dir, spec_dir
    else:
        yield _DEV_ROOT / "mappings", _DEV_ROOT / "spec/grammar/mapping"


def _describe(value, indent: int = 0, seen: set[int] | None = None) -> None:
    seen = seen if seen is not None else set()
    pad = "  " * indent
    if isinstance(value, MetaInstance):
        if id(value) in seen:
            print(f"{pad}<{value._qualified_class} Name={getattr(value, 'Name', None)!r}> (already shown)")
            return
        seen.add(id(value))
        name = getattr(value, "Name", None)
        short = value._qualified_class.rsplit(".", 1)[-1]
        suffix = f" Name={name!r}" if name is not None else ""
        kind = getattr(value, "Kind", None)
        if kind is not None:
            suffix += f" Kind={kind!r}"
        print(f"{pad}{short}{suffix}")
        for field, field_value in sorted(value.__dict__.items()):
            if field.startswith("_") or field in ("Name", "Kind"):
                continue
            _describe_field(field, field_value, indent + 1, seen)
        extra = value.model_extra or {}
        for field, field_value in sorted(extra.items()):
            _describe_field(field, field_value, indent + 1, seen)
    elif isinstance(value, list):
        for item in value:
            _describe(item, indent, seen)
    else:
        pass


def _describe_field(field: str, value, indent: int, seen: set[int]) -> None:
    pad = "  " * indent
    if isinstance(value, MetaInstance):
        print(f"{pad}{field}:")
        _describe(value, indent + 1, seen)
    elif isinstance(value, list) and value and isinstance(value[0], MetaInstance):
        print(f"{pad}{field}:")
        _describe(value, indent + 1, seen)
    elif value not in (None, [], {}, False):
        print(f"{pad}{field} = {value!r}")


def cmd_build(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    tree, syntax_errors = parse_file(path)
    if syntax_errors:
        print(f"{len(syntax_errors)} syntax error(s):", file=sys.stderr)
        for e in syntax_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    repository = ModelRepository([Path(d) for d in args.repo]) if args.repo else None
    with _resource_dirs() as (mappings_dir, spec_dir):
        builder = InterlisModelBuilder(mappings_dir, spec_dir, repository=repository)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        model = builder.build(tree)

    _describe(model)

    if caught and not args.quiet:
        print(f"\n{len(caught)} warning(s) (known spec gaps):", file=sys.stderr)
        for w in caught:
            print(f"  {w.message}", file=sys.stderr)

    return 0


_SUPPORTED_VIEW_FORMATION_KINDS = ("Projection", "Join")


def cmd_convert(args: argparse.Namespace) -> int:
    """Convert an .ili model to JSON Schema.

    See docs/jsonschema-conversion-strategy.md and
    mappings/ilismeta16-to-jsonschema-rules.yml for scope - a mapped type
    outside the current lot's coverage gets an explicit
    `x-unsupported` marker rather than being silently dropped.

    Every `Class` becomes its own `$defs` entry, as before. A `VIEW` is
    ALSO a root, restricted to `FormationKind in {Projection, Join}`
    (`_SUPPORTED_VIEW_FORMATION_KINDS`, backlog item 8's Lot B scope,
    `.claude/PROGRESS.md` - matches the FGDM4GS report's own §4.4.2/4.4.3
    prioritization; `Union`/`Aggregation`/`Inspection` Views are excluded
    from this CLI's root selection rather than converted, a deliberate
    scope decision, not a silent drop of supported data). `View` extends
    `Class` in the metamodel (`ilismeta16-classes.yml`) and its
    `ClassAttribute` list is populated the same way (backlog item 8's
    Lot A2) - `class_to_json_schema`/`model_to_json_schema` need no View-
    specific code at all, confirmed empirically: a View's flattened
    (`JOIN OF`-joined, or `PROJECTION OF`-selected) attribute set already
    produces a correct JSON Schema `$defs` entry through the exact same
    path as a plain Class.
    """
    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    tree, syntax_errors = parse_file(path)
    if syntax_errors:
        print(f"{len(syntax_errors)} syntax error(s):", file=sys.stderr)
        for e in syntax_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    repository = ModelRepository([Path(d) for d in args.repo]) if args.repo else None
    with _resource_dirs() as (mappings_dir, spec_dir):
        builder = InterlisModelBuilder(mappings_dir, spec_dir, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # eCH-0117 `!!@Name=Value` meta-attributes declared directly in
        # THIS file (technicalContact/furtherInformation/IDGeoIV at MODEL
        # level, CRS on a locally-declared CoordType domain, etc.) -
        # previously never captured here (only an IMPORTED model's own
        # comments were, via ModelRepository._get_table), so a model that
        # declares its own geometry domain rather than importing
        # CHBase_Part1_GEOMETRY_V1 silently lost its CRS, and MODEL-level
        # metadata had nowhere to attach at all - see
        # docs/ech-0117-meta-attributes.md.
        root = builder.build(tree, meta_attributes=meta_attribute_comments_in_file(path))

    classes = [
        instance for instance in builder.symbol_table.all_registered()
        if isinstance(instance, MetaInstance) and instance._qualified_class.rsplit(".", 1)[-1] == "Class"
    ]
    views = [
        instance for instance in builder.symbol_table.all_registered()
        if isinstance(instance, MetaInstance) and instance._qualified_class.rsplit(".", 1)[-1] == "View"
        and getattr(instance, "FormationKind", None) in _SUPPORTED_VIEW_FORMATION_KINDS
    ]
    # `build()` returns the root Model instance directly for the (real-corpus
    # dominant) single-MODEL-per-file case - a file declaring more than one
    # MODEL yields something else here, so `x-meta` is simply omitted rather
    # than guessing which MODEL the file-level metadata belongs to.
    root_model = root if isinstance(root, MetaInstance) and root._qualified_class.rsplit(".", 1)[-1] == "Model" else None
    schema = model_to_json_schema(classes + views, symbol_table=builder.symbol_table, model=root_model)
    text = json.dumps(schema, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def cmd_convert_sql(args: argparse.Namespace) -> int:
    """Convert an .ili model to SQL DDL, PostgreSQL or GeoPackage/SQLite (backlog item 14).

    See docs/sql-conversion-strategy.md for the design decision and scope
    - this project generates the full schema (`CREATE TABLE` + `UNIQUE` +
    `FOREIGN KEY` + `CHECK`, the latter from a row-local `MANDATORY
    CONSTRAINT`); GDAL (`ogr2ogr -append`) is expected to load the actual
    .xtf-derived data into the tables this command creates, never the
    other way around. `--dialect gpkg` assumes the target `.gpkg` file
    already has the standard GeoPackage system tables (created by GDAL
    beforehand) and declares every constraint INLINE, at `CREATE TABLE`
    time (SQLite cannot add one to an existing table at all, unlike
    `--dialect postgresql`'s default, which uses a separate `ALTER TABLE
    ... ADD CONSTRAINT` pass for `FOREIGN KEY` only - `UNIQUE`/`CHECK` are
    inline in both dialects). Only `Kind=Class` roots become a table (no
    `View`, unlike `cmd_convert` - `CREATE VIEW` generation is a later
    lot). An attribute/constraint outside this module's mapped set never
    disappears silently - it becomes a `-- NOTE` SQL comment instead
    (RULE #5).
    """
    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    tree, syntax_errors = parse_file(path)
    if syntax_errors:
        print(f"{len(syntax_errors)} syntax error(s):", file=sys.stderr)
        for e in syntax_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    repository = ModelRepository([Path(d) for d in args.repo]) if args.repo else None
    with _resource_dirs() as (mappings_dir, spec_dir):
        builder = InterlisModelBuilder(mappings_dir, spec_dir, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree, meta_attributes=meta_attribute_comments_in_file(path))

    classes = [
        instance for instance in builder.symbol_table.all_registered()
        if isinstance(instance, MetaInstance) and instance._qualified_class.rsplit(".", 1)[-1] == "Class"
    ]
    tables = build_tables(classes, symbol_table=builder.symbol_table)
    ddl = render_gpkg(tables) if args.dialect == "gpkg" else render_postgresql(tables)
    if args.output:
        Path(args.output).write_text(ddl, encoding="utf-8")
    else:
        print(ddl, end="")
    return 0


def _resolve_schema_model_path(
    xtf_path: Path, transfer, args: argparse.Namespace, repository: ModelRepository | None,
) -> tuple[Path, str | None] | None:
    """Resolve the .ili file describing `transfer`'s schema, or print an error and return `None`.

    Shared between `cmd_validate` and `cmd_convert_jsonfg` (same
    resolution rule, see docs/model-resolution-strategy.md):
    - an explicit `--model <file.ili>` (historical behavior, still
      supported);
    - otherwise, auto-detected from the transfer itself (never a Model
      Repository queried directly): the "root" models actually used by its
      DATASECTION (see xtf.model_resolution) are looked up in the given
      `--repo` directories - one is enough as an entry point, the rest
      (remaining roots, or an imported "core" model) resolve through the
      existing cross-model mechanism (ModelRepository.resolve_external).

    Returns `(model_path, root_model_name)` - `root_model_name` is `None`
    for the explicit `--model` path (never looked up by name in that
    case).
    """
    if args.model:
        model_path = Path(args.model)
        if not model_path.exists():
            print(f".ili file not found: {model_path}", file=sys.stderr)
            return None
        return model_path, None
    if repository is None:
        print("no --model given: --repo is required for schema auto-detection.", file=sys.stderr)
        return None
    for name in root_model_names(transfer):
        candidate = repository.path_for(name)
        if candidate is not None:
            return candidate, name
    header = header_model_lookup(transfer)
    print(f"no root model of {xtf_path} is available in --repo. Required models (HEADERSECTION):", file=sys.stderr)
    for name in root_model_names(transfer):
        version, uri = header.get(name, ["?", "?"])
        print(f"  {name} VERSION={version!r} URI={uri!r}", file=sys.stderr)
    return None


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate an .xtf transfer file against its schema.

    Checks base types, MANDATORY, structure, and cross-basket TID/REF
    resolution (see docs/xtf-transfer-encoding-notes.md for exact scope).
    Schema resolution: see `_resolve_schema_model_path`.
    """
    xtf_path = Path(args.xtf)
    if not xtf_path.exists():
        print(f".xtf file not found: {xtf_path}", file=sys.stderr)
        return 1

    transfer = parse_xtf(xtf_path)
    repository = ModelRepository([Path(d) for d in args.repo]) if args.repo else None

    resolved = _resolve_schema_model_path(xtf_path, transfer, args, repository)
    if resolved is None:
        return 1
    model_path, root_model_name = resolved

    tree, syntax_errors = parse_file(model_path)
    if syntax_errors:
        print(f"{len(syntax_errors)} syntax error(s) in {model_path}:", file=sys.stderr)
        for e in syntax_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    with _resource_dirs() as (mappings_dir, spec_dir):
        builder = InterlisModelBuilder(mappings_dir, spec_dir, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)

    if repository is not None and root_model_name is not None:
        # Avoids rebuilding the root model twice (already built above via
        # its own parse_file/build()) when header_completeness() checks its
        # resolvability below.
        repository.register_prebuilt(root_model_name, builder.symbol_table)

    # header_completeness only makes sense when a --repo is given - without
    # it, no model can be checked/resolved anyway, and this mode (`--model`
    # alone, historical) never assumed cross-model resolution: don't change
    # its default behavior/output. Same warning suppression as the root
    # build above: without it, every header model built HERE (potentially
    # never touched otherwise) would emit its own UserWarning (known spec
    # gaps) straight to stderr, even under -q.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        header_status = header_completeness(transfer, repository) if repository is not None else []
    incomplete = [s for s in header_status if s.status not in ("builtin", "available")]
    if incomplete and not args.quiet:
        print(f"Header models (HEADERSECTION/MODELS): {len(header_status) - len(incomplete)}/{len(header_status)} resolved")
        for s in incomplete:
            label = {"missing": "not in --repo", "indexed_but_failed": "found but failed to build"}[s.status]
            print(f"  [{label}] {s.name} VERSION={s.version!r} URI={s.uri!r}")
        print()

    catalogs = [parse_xtf(Path(c)) for c in args.catalog]
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table, repository=repository, catalogs=catalogs)

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
        if args.quiet or (issue.severity == "info" and not args.verbose):
            continue
        print(f"[{issue.severity:7s}] {issue.qualified_class}[{issue.object_tid}].{issue.attribute}: {issue.message}")

    header_suffix = (
        f" - {len(header_status) - len(incomplete)}/{len(header_status)} header models resolved"
        if header_status else ""
    )
    print(
        f"\n{len(issues)} issue(s): "
        f"{counts.get('error', 0)} error(s), {counts.get('warning', 0)} warning(s), "
        f"{counts.get('info', 0)} info(s){'' if args.verbose else ' (hidden, --verbose to show)'}"
        f"{header_suffix}",
    )
    return 1 if counts.get("error") else 0


def cmd_convert_jsonfg(args: argparse.Namespace) -> int:
    """Convert an .xtf transfer to a JSON-FG FeatureCollection.

    See docs/jsonfg-conversion-strategy.md for scope - the "core" and
    "types-schemas" JSON-FG requirements classes only: scalar properties,
    OID, featureType, and single-attribute point/line/polygon geometry
    (`"place"`) with its `"coordRefSys"` resolved from the eCH-0117
    `!!@CRS` meta-attribute; `"geometry"` (the WGS84 GeoJSON fallback)
    always stays `null`. Schema resolution: see
    `_resolve_schema_model_path` (same `--model`/`--repo` rule as
    `interlis validate`).

    Backlog item 8 Lot C: `VIEW`s are ALSO evaluated into Features, same
    root selection as `cmd_convert`'s JSON Schema path
    (`_SUPPORTED_VIEW_FORMATION_KINDS` - `Union`/`Aggregation`/`Inspection`
    excluded silently, a pre-existing deliberate scope decision, not
    repeated here as a diagnostic). Among `Projection`/`Join` Views, one
    with a `WHERE` clause is additionally excluded HERE, with a clear
    stderr diagnostic (`unsupported_view_reason`) - Expression tree
    evaluation isn't supported yet (see .claude/HANDOFF.md), so silently
    dropping or wrongly evaluating it would misrepresent the data.

    `--feature-schema-url`, when given, wires "featureSchema" (JSON-FG
    clause 13) to the companion JSON Schema `interlis convert` would
    produce for the SAME `.ili` model - this runtime has no schema-hosting
    story of its own, so the URL is always user-supplied (same stance as
    `--repo`/`--model`), never derived automatically.
    """
    xtf_path = Path(args.xtf)
    if not xtf_path.exists():
        print(f".xtf file not found: {xtf_path}", file=sys.stderr)
        return 1

    transfer = parse_xtf(xtf_path)
    repository = ModelRepository([Path(d) for d in args.repo]) if args.repo else None

    resolved = _resolve_schema_model_path(xtf_path, transfer, args, repository)
    if resolved is None:
        return 1
    model_path, _root_model_name = resolved

    tree, syntax_errors = parse_file(model_path)
    if syntax_errors:
        print(f"{len(syntax_errors)} syntax error(s) in {model_path}:", file=sys.stderr)
        for e in syntax_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    with _resource_dirs() as (mappings_dir, spec_dir):
        builder = InterlisModelBuilder(mappings_dir, spec_dir, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # eCH-0117 `!!@Name=Value` meta-attributes declared directly in the
        # schema model itself - previously never captured here (only an
        # IMPORTED model's own comments were, via
        # ModelRepository._get_table), so a model that declares its own
        # geometry domain locally (rather than importing
        # CHBase_Part1_GEOMETRY_V1) would never resolve a CRS, and "place"
        # would silently stay unsupported - see
        # docs/ech-0117-meta-attributes.md.
        builder.build(tree, meta_attributes=meta_attribute_comments_in_file(model_path))

    candidate_views = [
        instance for instance in builder.symbol_table.all_registered()
        if isinstance(instance, MetaInstance) and instance._qualified_class.rsplit(".", 1)[-1] == "View"
        and getattr(instance, "FormationKind", None) in _SUPPORTED_VIEW_FORMATION_KINDS
    ]
    views = []
    for view in candidate_views:
        reason = unsupported_view_reason(view)
        if reason is not None:
            print(f"skipping VIEW {getattr(view, 'Name', None)!r}: {reason}", file=sys.stderr)
            continue
        views.append(view)

    collection = transfer_to_feature_collection(
        transfer, symbol_table=builder.symbol_table, repository=repository, views=views,
        schema_url=args.feature_schema_url, include_child_rows=args.include_child_rows,
    )
    text = json.dumps(collection, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="interlis", description="Pure-Python INTERLIS runtime.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Parse an .ili file and print the built model.")
    build_parser.add_argument("file", help="Path to the .ili file to build.")
    build_parser.add_argument("-q", "--quiet", action="store_true", help="Don't print warnings.")
    build_parser.add_argument(
        "--repo", action="append", default=[], metavar="DIR",
        help="Directory of .ili models used to resolve references to imported models "
             "(IMPORTS) - repeatable. Absent by default: no cross-file resolution (V1 behavior).",
    )
    build_parser.set_defaults(func=cmd_build)

    convert_parser = subparsers.add_parser(
        "convert", help="Convert an .ili model to JSON Schema.",
    )
    convert_parser.add_argument("file", help="Path to the .ili file to convert.")
    convert_parser.add_argument(
        "--repo", action="append", default=[], metavar="DIR",
        help="Directory of .ili models used to resolve references to imported models (IMPORTS) - repeatable.",
    )
    convert_parser.add_argument("-o", "--output", default=None, metavar="FILE", help="Write to FILE instead of stdout.")
    convert_parser.set_defaults(func=cmd_convert)

    convert_sql_parser = subparsers.add_parser(
        "convert-sql", help="Convert an .ili model to SQL DDL (CREATE TABLE + UNIQUE/FOREIGN KEY constraints).",
    )
    convert_sql_parser.add_argument("file", help="Path to the .ili file to convert.")
    convert_sql_parser.add_argument(
        "--dialect", choices=("postgresql", "gpkg"), default="postgresql",
        help="Target SQL dialect. 'postgresql' (default): CREATE TABLE + a separate ALTER TABLE ... ADD CONSTRAINT "
             "pass for FOREIGN KEY. 'gpkg': GeoPackage/SQLite - everything declared INLINE at CREATE TABLE time "
             "(SQLite can't add a constraint to an existing table), plus gpkg_contents/gpkg_geometry_columns/"
             "gpkg_spatial_ref_sys bootstrap rows - assumes the target .gpkg already has the standard GeoPackage "
             "system tables (created by GDAL beforehand).",
    )
    convert_sql_parser.add_argument(
        "--repo", action="append", default=[], metavar="DIR",
        help="Directory of .ili models used to resolve references to imported models (IMPORTS) - repeatable.",
    )
    convert_sql_parser.add_argument("-o", "--output", default=None, metavar="FILE", help="Write to FILE instead of stdout.")
    convert_sql_parser.set_defaults(func=cmd_convert_sql)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate an .xtf file against an .ili file's schema.",
    )
    validate_parser.add_argument("xtf", help="Path to the .xtf file to validate.")
    validate_parser.add_argument(
        "--model", default=None,
        help="Path to the .ili file describing the expected schema. Omitted: auto-detected from the "
        "transfer's own HEADERSECTION/DATASECTION (requires --repo, see docs/model-resolution-strategy.md).",
    )
    validate_parser.add_argument(
        "--repo", action="append", default=[], metavar="DIR",
        help="Directory of .ili models to resolve the schema's IMPORTS (repeatable).",
    )
    validate_parser.add_argument(
        "--catalog", action="append", default=[], metavar="FILE.xtf",
        help="Additional catalogue .xtf file (repeatable) - its objects also count for TID/REF "
        "resolution (EXTERNAL references not included in the main transfer, see "
        "docs/model-resolution-strategy.md).",
    )
    validate_parser.add_argument("-q", "--quiet", action="store_true", help="Only print the final summary.")
    validate_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Also print 'info'-severity issues (unresolved references).",
    )
    validate_parser.set_defaults(func=cmd_validate)

    convert_jsonfg_parser = subparsers.add_parser(
        "convert-jsonfg", help="Convert an .xtf transfer to a JSON-FG FeatureCollection.",
    )
    convert_jsonfg_parser.add_argument("xtf", help="Path to the .xtf file to convert.")
    convert_jsonfg_parser.add_argument(
        "--model", default=None,
        help="Path to the .ili file describing the expected schema. Omitted: auto-detected from the "
        "transfer's own HEADERSECTION/DATASECTION (requires --repo, see docs/model-resolution-strategy.md).",
    )
    convert_jsonfg_parser.add_argument(
        "--repo", action="append", default=[], metavar="DIR",
        help="Directory of .ili models to resolve the schema's IMPORTS (repeatable).",
    )
    convert_jsonfg_parser.add_argument(
        "-o", "--output", default=None, metavar="FILE", help="Write to FILE instead of stdout.",
    )
    convert_jsonfg_parser.add_argument(
        "--feature-schema-url", default=None, metavar="URL",
        help="URL/path of the companion 'interlis convert' JSON Schema output for the same .ili model - "
        "when given, populates the JSON-FG 'featureSchema' member (clause 13). Omitted: 'featureSchema' is not emitted.",
    )
    convert_jsonfg_parser.add_argument(
        "--include-child-rows", action="store_true",
        help="Also emit one Feature per BAG/LIST OF occurrence, with its own 'featureType' matching a "
        "'interlis convert-sql' child table name (see docs/sql-conversion-strategy.md) - loaded into that same "
        "table by GDAL's own featureType-based table splitting, in the SAME 'ogr2ogr -append' as the main data. "
        "Omitted (default): BAG/LIST occurrences stay inlined as a plain JSON array property, as before.",
    )
    convert_jsonfg_parser.set_defaults(func=cmd_convert_jsonfg)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
