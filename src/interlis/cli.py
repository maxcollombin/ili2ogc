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

from interlis.builder.forward_refs import SymbolTable
from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.convert.jsonfg import transfer_to_feature_collection, unsupported_view_reason
from interlis.convert.jsonschema import model_to_json_schema
from interlis.convert.sql import build_tables, build_views, render_gpkg, render_postgresql
from interlis.convert.translation import (
    load_translation,
    rename_feature_collection,
    rename_json_schema,
    rename_sql_ddl,
)
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
    if args.lang:
        translation = load_translation(getattr(root_model, "Name", None) or "", args.lang, repository)
        if translation is None:
            print(f"--lang {args.lang}: no TRANSLATION OF {getattr(root_model, 'Name', path.stem)!r} for '{args.lang}' in --repo", file=sys.stderr)
            return 1
        schema = rename_json_schema(schema, translation)
    text = json.dumps(schema, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def _fold_in_dependency_models(classes, views, root_table, repository, class_symbol_tables) -> None:
    """Append every already-resolved imported model's classes this conversion depends on to `classes` (in place).

    "Depends on" = a VIEW base class, or the target of a cross-model
    REFERENCE TO/embedded role, that lives in an IMPORTED model rather than
    the root file. Only models `builder.build()` already pulled in via
    `--repo` (`repository.loaded_models()`) are considered - never the
    whole `--repo` index, and never a model nothing here references (so a
    geometry/units helper model imported only for a domain stays out).
    Mirrors what `--catalog` does, without the caller naming each file.
    """
    if repository is None:
        return
    from interlis.xtf.schema import reference_target_class, resolve_attribute, schema_members_of

    needed_ids: set[int] = {
        id(rbv.BaseView)
        for view in views
        for rbv in (getattr(view, "RenamedBaseView", None) or [])
        if isinstance(getattr(rbv, "BaseView", None), MetaInstance)
    }
    for cls in list(classes):
        try:
            members = schema_members_of(cls, root_table)
        except Exception:  # noqa: BLE001 - a resolution quirk here must never break the conversion
            continue
        for attr in members.values():
            resolved = resolve_attribute(attr)
            if resolved.type_kind in ("Class", "ReferenceType"):
                target = reference_target_class(resolved)
                if isinstance(target, MetaInstance):
                    needed_ids.add(id(target))
    if not needed_ids:
        return

    already_ids = {id(c) for c in classes}
    # A model already supplied via --catalog is built by a SEPARATE builder,
    # so its classes are different instances than repository.loaded_models()'s
    # - dedup by Name too, so --catalog + this path don't both add it.
    already_names = {getattr(c, "Name", None) for c in classes}
    for model_table in repository.loaded_models().values():
        model_classes = [
            instance for instance in model_table.all_registered()
            if isinstance(instance, MetaInstance) and instance._qualified_class.rsplit(".", 1)[-1] == "Class"
        ]
        if not any(id(c) in needed_ids for c in model_classes):
            continue
        if any(getattr(c, "Name", None) in already_names for c in model_classes):
            continue  # this model is already in the conversion (typically via --catalog)
        for instance in model_classes:
            if id(instance) in already_ids:
                continue
            already_ids.add(id(instance))
            classes.append(instance)
            class_symbol_tables.setdefault(id(instance), model_table)


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
    inline in both dialects). `Kind=Class` roots become a `CREATE TABLE`;
    `Projection`/`Join` `View`s become a `CREATE VIEW` (same
    `_SUPPORTED_VIEW_FORMATION_KINDS` filter as `cmd_convert` -
    `Union`/`Aggregation`/`Inspection` are noted, not translated). A View's
    base classes live in an IMPORTED model, so pass that model via
    `--catalog` too - a View whose base table isn't in this conversion, or
    whose `Where`/`ATTRIBUTE` expressions fall outside the translatable
    subset, is emitted as a `-- NOTE` rather than a half-built `CREATE
    VIEW`. An attribute/constraint outside this module's mapped set never
    disappears silently - it becomes a `-- NOTE` SQL comment instead
    (RULE #5).

    `--catalog FILE.ili` (repeatable): each is built with its OWN
    `InterlisModelBuilder` (sharing `repository` so cross-references
    between `file` and a catalogue, or between two catalogues, still
    resolve) and its classes are appended to the SAME `classes` list
    `build_tables` receives - closes `build_tables`'s own documented
    cross-model FK-drop (see its "3rd real bug" comment): a `REFERENCE TO`
    a class NOT among `classes` gets its `FOREIGN KEY` constraint dropped,
    column kept, because that target table doesn't exist in THIS
    conversion's output. A catalogue model (e.g. a value-list Class
    hierarchy extending `CatalogueObjects_V1.Catalogues.Item`) is the
    single most common real case (docs/sql-conversion-strategy.md) - but
    this flag is generic, not catalogue-specific: any additional model
    works the same way.
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
    views = [
        instance for instance in builder.symbol_table.all_registered()
        if isinstance(instance, MetaInstance) and instance._qualified_class.rsplit(".", 1)[-1] == "View"
        and getattr(instance, "FormationKind", None) in _SUPPORTED_VIEW_FORMATION_KINDS
    ]
    for instance in builder.symbol_table.all_registered():
        if (
            isinstance(instance, MetaInstance) and instance._qualified_class.rsplit(".", 1)[-1] == "View"
            and getattr(instance, "FormationKind", None) not in _SUPPORTED_VIEW_FORMATION_KINDS
        ):
            print(
                f"note: VIEW {getattr(instance, 'Name', '?')} (FormationKind="
                f"{getattr(instance, 'FormationKind', None)}) is not translated to CREATE VIEW",
                file=sys.stderr,
            )

    # `id(cls) -> its OWN symbol table`, for every `--catalog` class - see
    # `build_tables`'s `class_symbol_tables` docstring: a catalogue class's
    # embedding association (if any) is declared in ITS OWN model's table,
    # never in `builder.symbol_table` (the root file being converted).
    class_symbol_tables: dict[int, SymbolTable] = {}

    seen_catalog_paths: set[Path] = set()
    for catalog_arg in args.catalog:
        catalog_path = Path(catalog_arg)
        if not catalog_path.exists():
            print(f"--catalog file not found: {catalog_path}", file=sys.stderr)
            return 1
        catalog_path = catalog_path.resolve()
        if catalog_path == path.resolve() or catalog_path in seen_catalog_paths:
            continue  # the same file given twice (as `file` or across --catalog) would otherwise duplicate its table
        seen_catalog_paths.add(catalog_path)
        catalog_tree, catalog_syntax_errors = parse_file(catalog_path)
        if catalog_syntax_errors:
            print(f"{len(catalog_syntax_errors)} syntax error(s) in {catalog_path}:", file=sys.stderr)
            for e in catalog_syntax_errors:
                print(f"  {e}", file=sys.stderr)
            return 1
        with _resource_dirs() as (mappings_dir, spec_dir):
            catalog_builder = InterlisModelBuilder(mappings_dir, spec_dir, repository=repository)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            catalog_builder.build(catalog_tree, meta_attributes=meta_attribute_comments_in_file(catalog_path))
        catalog_classes = [
            instance for instance in catalog_builder.symbol_table.all_registered()
            if isinstance(instance, MetaInstance) and instance._qualified_class.rsplit(".", 1)[-1] == "Class"
        ]
        classes.extend(catalog_classes)
        class_symbol_tables.update({id(instance): catalog_builder.symbol_table for instance in catalog_classes})

    # An imported model this conversion actually depends on - a VIEW's
    # JOIN OF/PROJECTION OF base classes, or the target of a cross-model
    # REFERENCE TO/role - must be part of the SAME conversion for its
    # tables (and the CREATE VIEW / FOREIGN KEY that need them) to exist.
    # Any such model that `builder.build()` already resolved through
    # `--repo` is folded in automatically here, so `--catalog` is only
    # needed for a model NOT reachable via `--repo`. See
    # docs/sql-conversion-strategy.md.
    _fold_in_dependency_models(classes, views, builder.symbol_table, repository, class_symbol_tables)

    class_table_names: dict[int, str] = {}
    tables = build_tables(
        classes, symbol_table=builder.symbol_table, class_symbol_tables=class_symbol_tables,
        class_table_names=class_table_names,
    )
    sql_views = tuple(build_views(
        views, tables, symbol_table=builder.symbol_table, class_symbol_tables=class_symbol_tables,
        class_table_names=class_table_names,
    ))
    ddl = render_gpkg(tables, sql_views) if args.dialect == "gpkg" else render_postgresql(tables, sql_views)
    if args.lang:
        root_names = builder.symbol_table.root_model_names() if hasattr(builder.symbol_table, "root_model_names") else []
        base_name = next(iter(root_names), None) or path.stem
        translation = load_translation(base_name, args.lang, repository)
        if translation is None:
            print(f"--lang {args.lang}: no TRANSLATION OF {base_name!r} for '{args.lang}' in --repo", file=sys.stderr)
            return 1
        ddl = rename_sql_ddl(ddl, translation)
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

    Backlog item 8: every `VIEW` is evaluated into Features -
    `PROJECTION`/`JOIN`/`UNION`/`AGGREGATION`/`INSPECTION`
    (`convert/jsonfg.evaluate_view`). A `WHERE` clause narrows a
    `JOIN`/`PROJECTION` and is evaluated by the CONSTRAINT evaluator
    (`And`/`Or`/`Not`/`Defined`/`Implication`/relational, constants,
    nested-STRUCTURE paths). The only VIEWs skipped, each with a clear
    stderr diagnostic (`unsupported_view_reason`): one whose base model
    isn't on `--repo` (provide it), and one whose `WHERE` needs a
    construct the CONSTRAINT evaluator itself doesn't support (arithmetic,
    a function call).

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
    if args.lang:
        root_names = builder.symbol_table.root_model_names() if hasattr(builder.symbol_table, "root_model_names") else []
        base_name = next(iter(root_names), None) or model_path.stem
        translation = load_translation(base_name, args.lang, repository)
        if translation is None:
            print(f"--lang {args.lang}: no TRANSLATION OF {base_name!r} for '{args.lang}' in --repo", file=sys.stderr)
            return 1
        collection = rename_feature_collection(collection, translation)
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
    convert_parser.add_argument(
        "--lang", default=None, metavar="CODE",
        help="Rename output identifiers through a `TRANSLATION OF` model for this language (e.g. `fr`), "
        "found by name in --repo. The input .ili and the transfer format are unchanged.",
    )
    convert_parser.add_argument("-o", "--output", default=None, metavar="FILE", help="Write to FILE instead of stdout.")
    convert_parser.set_defaults(func=cmd_convert)

    convert_sql_parser = subparsers.add_parser(
        "convert-sql", help="Convert an .ili model to SQL DDL (CREATE TABLE + UNIQUE/FOREIGN KEY/CHECK, and CREATE VIEW for Projection/Join VIEWs).",
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
        help="Directory of .ili models used to resolve IMPORTS - repeatable. An imported model this conversion "
             "actually depends on (a VIEW's JOIN OF/PROJECTION OF base classes, or a cross-model REFERENCE TO "
             "target) is folded into the output as CREATE TABLEs automatically when found here, so its CREATE "
             "VIEW / FOREIGN KEY can be generated without also listing it via --catalog.",
    )
    convert_sql_parser.add_argument(
        "--catalog", action="append", default=[], metavar="FILE.ili",
        help="Additional .ili model whose own classes also become tables in this SAME conversion (repeatable) - "
             "typically a catalogue/reference model (e.g. a value-list Class hierarchy) that another Class in "
             "'file' points to via REFERENCE TO. Without this, a REFERENCE TO a class from a model not converted "
             "in the SAME run keeps its column but drops the FOREIGN KEY constraint (the target table doesn't "
             "exist in this conversion's own output). Also required to turn a VIEW into a CREATE VIEW: pass the "
             "base model(s) the VIEW's JOIN OF/PROJECTION OF classes come from, else the VIEW is emitted as a "
             "-- NOTE - see docs/sql-conversion-strategy.md.",
    )
    convert_sql_parser.add_argument(
        "--lang", default=None, metavar="CODE",
        help="Rename tables/columns/views through a `TRANSLATION OF` model for this language (e.g. `fr`), "
        "found by name in --repo. Identifier-level: a base name that would translate two different ways "
        "across classes is left untranslated.",
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
        "--lang", default=None, metavar="CODE",
        help="Rename `featureType` and `properties` keys through a `TRANSLATION OF` model for this "
        "language (e.g. `fr`), found by name in --repo. The .xtf wire tags stay in the base language.",
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
