"""Vue combinee du metamodele IlisMeta16 : UmlIndex (classes/datatypes/
associations/enumerations) + ilismeta16-kind-values.yml (valeurs des
enumerations anonymes referencees par UUID generique - voir GENERIC_ENUM_TYPE_UUID
ci-dessous).

Quatre types primitifs UML anonymes (xmi:id brut, pas de qualified_name)
sont utilises comme `type:` d'attribut dans ilismeta16-classes.yml/
datatypes.yml (verifie empiriquement par grep sur le YAML + le XMI source,
docs/uml/IlisMeta16-formatted.xmi) : EnumerationType (279A049B...),
NumericType (39FDCDA0...), TextType (C16095C6...), PolylineType
(F466480C..., un seul usage : INTERLIS.SurfaceEdge.Geometry - type
geometrique hors perimetre de cette conception, voir registry.py). Seul
EnumerationType a un traitement special (resolution via kind-values.yml en
Literal[...]) - les 3 autres sont directement resolus par le typage
permissif `Any` du registry (voir sa docstring pour la justification)."""
from pathlib import Path

import yaml

from interlis.spec.uml_index import UmlIndex

GENERIC_ENUM_TYPE_UUID = "279A049B-2BCC-4fb5-9C8F-3B22EF3EE0ED"


class MetamodelSchema:
    def __init__(self, uml: UmlIndex, kind_values: dict[str, dict]):
        self.uml = uml
        self.kind_values = kind_values  # {"Qualified.Attr": {"mandatory": bool, "values": [...]}}

    @classmethod
    def load(cls, mappings_dir: Path) -> "MetamodelSchema":
        uml = UmlIndex.load(mappings_dir)
        kv_path = mappings_dir / "ilismeta16-kind-values.yml"
        kv_data = yaml.safe_load(kv_path.read_text(encoding="utf-8")) or {}
        return cls(uml, kv_data.get("kind_values", {}))

    def instantiable_classes(self) -> dict[str, dict]:
        """Classes et datatypes (kind Class ou DataType) - ce que le registry
        doit generer comme classes Pydantic. Exclut Association et
        Enumeration (structures distinctes, non generees comme instances)."""
        return {
            qn: el for qn, el in self.uml.qualified.items()
            if el.get("kind") in ("Class", "DataType")
        }

    def associations(self) -> dict[str, dict]:
        return {qn: el for qn, el in self.uml.qualified.items() if el.get("kind") == "Association"}

    def enum_values_for(self, qualified_class: str, attribute: str) -> list[str] | None:
        return (self.kind_values.get(f"{qualified_class}.{attribute}") or {}).get("values")
