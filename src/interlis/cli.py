"""CLI du runtime INTERLIS : `interlis build <fichier.ili>`.

Point d'entree fin - toute la logique vit dans interlis.runtime/interlis.builder.
Suppose une execution depuis un checkout du depot (mappings/ et
spec/grammar/mapping/ resolus relativement a la racine du projet, pas
empaquetes comme donnees de distribution) - coherent avec l'etat actuel du
projet (runtime lie a son propre depot, pas encore une librairie
distribuable independamment)."""
import argparse
import sys
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_file
from interlis.xtf.parse import parse_xtf
from interlis.xtf.validate import validate_transfer

ROOT = Path(__file__).resolve().parent.parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"


def _describe(value, indent: int = 0, seen: set[int] | None = None) -> None:
    seen = seen if seen is not None else set()
    pad = "  " * indent
    if isinstance(value, MetaInstance):
        if id(value) in seen:
            print(f"{pad}<{value._qualified_class} Name={getattr(value, 'Name', None)!r}> (deja affiche)")
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
        print(f"fichier introuvable : {path}", file=sys.stderr)
        return 1

    tree, syntax_errors = parse_file(path)
    if syntax_errors:
        print(f"{len(syntax_errors)} erreur(s) de syntaxe :", file=sys.stderr)
        for e in syntax_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    repository = ModelRepository([Path(d) for d in args.repo]) if args.repo else None
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        model = builder.build(tree)

    _describe(model)

    if caught and not args.quiet:
        print(f"\n{len(caught)} avertissement(s) (gaps de spec connus, voir .claude/PROGRESS.md) :", file=sys.stderr)
        for w in caught:
            print(f"  {w.message}", file=sys.stderr)

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Valide un fichier de transfert .xtf contre le schema d'un fichier
    .ili donne (Lot 30 - types de base, MANDATORY, structure ; PAS encore
    la resolution TID/REF cross-panier, voir docs/xtf-transfer-encoding-notes.md
    et .claude/PROGRESS.md pour le perimetre exact)."""
    model_path = Path(args.model)
    xtf_path = Path(args.xtf)
    if not model_path.exists():
        print(f"fichier .ili introuvable : {model_path}", file=sys.stderr)
        return 1
    if not xtf_path.exists():
        print(f"fichier .xtf introuvable : {xtf_path}", file=sys.stderr)
        return 1

    tree, syntax_errors = parse_file(model_path)
    if syntax_errors:
        print(f"{len(syntax_errors)} erreur(s) de syntaxe dans {model_path} :", file=sys.stderr)
        for e in syntax_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    repository = ModelRepository([Path(d) for d in args.repo]) if args.repo else None
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)

    transfer = parse_xtf(xtf_path)
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table, repository=repository)

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
        if args.quiet or (issue.severity == "info" and not args.verbose):
            continue
        print(f"[{issue.severity:7s}] {issue.qualified_class}[{issue.object_tid}].{issue.attribute}: {issue.message}")

    print(
        f"\n{len(issues)} probleme(s) : "
        f"{counts.get('error', 0)} erreur(s), {counts.get('warning', 0)} avertissement(s), "
        f"{counts.get('info', 0)} info(s){'' if args.verbose else ' (masquees, --verbose pour les voir)'}",
    )
    return 1 if counts.get("error") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="interlis", description="Runtime INTERLIS en Python pur.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Parse un fichier .ili et affiche le modele construit.")
    build_parser.add_argument("file", help="Chemin du fichier .ili a construire.")
    build_parser.add_argument("-q", "--quiet", action="store_true", help="Ne pas afficher les avertissements.")
    build_parser.add_argument(
        "--repo", action="append", default=[], metavar="DIR",
        help="Repertoire de modeles .ili a utiliser pour resoudre les references vers des modeles importes "
             "(IMPORTS) - repetable. Absent par defaut : aucune resolution cross-fichier (comportement V1).",
    )
    build_parser.set_defaults(func=cmd_build)

    validate_parser = subparsers.add_parser(
        "validate", help="Valide un fichier .xtf contre le schema d'un fichier .ili.",
    )
    validate_parser.add_argument("xtf", help="Chemin du fichier .xtf a valider.")
    validate_parser.add_argument("--model", required=True, help="Chemin du fichier .ili decrivant le schema attendu.")
    validate_parser.add_argument(
        "--repo", action="append", default=[], metavar="DIR",
        help="Repertoire de modeles .ili pour resoudre les IMPORTS du schema (repetable).",
    )
    validate_parser.add_argument("-q", "--quiet", action="store_true", help="N'afficher que le resume final.")
    validate_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Afficher aussi les problemes de severite 'info' (references non resolues).",
    )
    validate_parser.set_defaults(func=cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
