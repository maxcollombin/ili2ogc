"""Index of the IlisMeta16 metamodel (mappings/ilismeta16-*.yml).

Reused by scripts/validate_spec.py (static validation, errors collected
and reported at the end of the pass) and by interlis.builder
(construction at runtime, errors propagated immediately as exceptions).
Hence a side-effect-free API: resolve()/find_association() raise an
exception instead of writing to a global list - it's up to the caller to
decide whether to collect or let it propagate.
"""

import warnings
from pathlib import Path

import yaml

UML_FILES = [
    "ilismeta16-classes.yml",
    "ilismeta16-datatypes.yml",
    "ilismeta16-associations.yml",
    "ilismeta16-enumerations.yml",
    "ilismeta16-extensions.yml",
]


class UnknownClassError(LookupError):
    """Aucune classe/type/association ne correspond au nom demande."""


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class UmlIndex:
    """Index of qualified_name -> UML element, plus a short-name index.

    The short-name index (name -> [qualified_name, ...]) resolves
    unqualified references (e.g. resolves_to: Class, resolves_to:
    SubModel).
    """

    def __init__(self, qualified: dict[str, dict], by_name: dict[str, list[str]]):
        self.qualified = qualified
        self.by_name = by_name

    @classmethod
    def load(cls, mappings_dir: Path) -> "UmlIndex":
        qualified: dict[str, dict] = {}
        by_name: dict[str, list[str]] = {}
        for filename in UML_FILES:
            path = mappings_dir / filename
            if not path.exists():
                continue
            data = _load_yaml(path) or {}
            for _section, values in data.items():
                if not isinstance(values, dict):
                    continue
                for qn, element in values.items():
                    if not isinstance(element, dict):
                        continue
                    qualified[qn] = element
                    short = element.get("name")
                    if short:
                        by_name.setdefault(short, []).append(qn)
        return cls(qualified, by_name)

    def resolve(self, name: str) -> dict:
        """Resolve a target/resolves_to value (qualified or bare) to its UML element.

        Raises UnknownClassError if not found. Warns (via the stdlib
        `warnings` module) and returns the first candidate if the bare name
        is ambiguous - never fails silently, but does not force callers to
        handle ambiguity as a hard error.
        """
        if "." in name:
            el = self.qualified.get(name)
            if el is None:
                raise UnknownClassError(f"classe/type introuvable (nom qualifie) : {name!r}")
            return el
        candidates = self.by_name.get(name, [])
        if len(candidates) == 1:
            return self.qualified[candidates[0]]
        if len(candidates) == 0:
            raise UnknownClassError(f"classe/type introuvable (nom court) : {name!r}")
        warnings.warn(f"nom court ambigu {name!r} -> {candidates} (a qualifier explicitement)")
        return self.qualified[candidates[0]]

    def find_association_by_name(self, name: str) -> dict | None:
        """Find an Association element by its short `name`.

        Not qualified_name - parent.association in spec entries always
        uses the short form.
        """
        for el in self.qualified.values():
            if el.get("kind") == "Association" and el.get("name") == name:
                return el
        return None

    @staticmethod
    def attribute_exists(element: dict, attribute: str) -> bool:
        attrs = element.get("attributes", {}) or {}
        return attribute in (attrs.get("own") or {}) or attribute in (attrs.get("inherited") or {})
