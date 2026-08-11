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
import sys
import warnings
from contextlib import ExitStack, contextmanager
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_file
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


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate an .xtf transfer file against its schema.

    Checks base types, MANDATORY, structure, and cross-basket TID/REF
    resolution (see docs/xtf-transfer-encoding-notes.md for exact scope).

    The schema is resolved in one of two ways (see
    docs/model-resolution-strategy.md for the architecture decision):
    - an explicit `--model <file.ili>` (historical behavior, still
      supported);
    - otherwise, auto-detected from the transfer itself (never a Model
      Repository queried directly): the "root" models actually used by its
      DATASECTION (see xtf.model_resolution) are looked up in the given
      `--repo` directories - one is enough as an entry point, the rest
      (remaining roots, or an imported "core" model) resolve through the
      existing cross-model mechanism (ModelRepository.resolve_external).
    """
    xtf_path = Path(args.xtf)
    if not xtf_path.exists():
        print(f".xtf file not found: {xtf_path}", file=sys.stderr)
        return 1

    transfer = parse_xtf(xtf_path)
    repository = ModelRepository([Path(d) for d in args.repo]) if args.repo else None

    root_model_name = None
    if args.model:
        model_path = Path(args.model)
        if not model_path.exists():
            print(f".ili file not found: {model_path}", file=sys.stderr)
            return 1
    else:
        if repository is None:
            print("no --model given: --repo is required for schema auto-detection.", file=sys.stderr)
            return 1
        model_path = None
        for name in root_model_names(transfer):
            candidate = repository.path_for(name)
            if candidate is not None:
                model_path = candidate
                root_model_name = name
                break
        if model_path is None:
            header = header_model_lookup(transfer)
            print(f"no root model of {xtf_path} is available in --repo. Required models (HEADERSECTION):", file=sys.stderr)
            for name in root_model_names(transfer):
                version, uri = header.get(name, ["?", "?"])
                print(f"  {name} VERSION={version!r} URI={uri!r}", file=sys.stderr)
            return 1

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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
