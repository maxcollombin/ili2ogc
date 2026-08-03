"""Chargement de spec/grammar/mapping/*.yml (les 9 fichiers thematiques)
en un dict unique {nom_de_regle: SpecEntry}."""
from pathlib import Path

import yaml

from interlis.spec.models import SpecEntry


def load_raw_spec(spec_dir: Path) -> tuple[dict[str, dict], list[str]]:
    """Charge les 9 fichiers en un seul dict {regle: entree brute (dict)}.
    Retourne aussi la liste des messages de doublon inter-fichiers (la
    premiere occurrence d'une regle en doublon est gardee, les suivantes
    ignorees) plutot que de lever au premier - a l'appelant de decider s'il
    s'arrete ou continue (le validateur CLI veut voir tous les problemes en
    un seul passage)."""
    merged: dict[str, dict] = {}
    origin: dict[str, str] = {}
    duplicates: list[str] = []
    for path in sorted(spec_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for rule, entry in data.items():
            if rule in merged:
                duplicates.append(f"{rule}: present dans {origin[rule]} ET {path.name} (doublon inter-fichiers)")
                continue
            merged[rule] = entry
            origin[rule] = path.name
    return merged, duplicates


def load_spec(spec_dir: Path) -> dict[str, SpecEntry]:
    """Charge et valide (Pydantic) les 9 fichiers. Leve pydantic.ValidationError
    au premier probleme de schema, ValueError s'il y a un doublon
    inter-fichiers - utilise par le ModelBuilder, qui a besoin d'une spec
    garantie valide avant de commencer a construire."""
    raw, duplicates = load_raw_spec(spec_dir)
    if duplicates:
        raise ValueError("; ".join(duplicates))
    return {rule: SpecEntry(**entry) for rule, entry in raw.items()}
