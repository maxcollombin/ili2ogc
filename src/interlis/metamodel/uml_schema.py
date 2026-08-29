"""Combined view of the IlisMeta16 metamodel.

Combines UmlIndex (classes/datatypes/associations/enumerations) with
ilismeta16-kind-values.yml (values of the anonymous enumerations
referenced by generic UUID - see GENERIC_ENUM_TYPE_UUID below).

Four anonymous UML primitive types (raw xmi:id, no qualified_name) are used
as an attribute's `type:` in ilismeta16-classes.yml/datatypes.yml (verified
against the YAML + the source XMI, docs/uml/IlisMeta16-formatted.xmi):
EnumerationType (279A049B...), NumericType (39FDCDA0...), TextType
(C16095C6...), PolylineType (F466480C..., single use:
INTERLIS.SurfaceEdge.Geometry - a geometry type out of scope for this
design, see registry.py). Only EnumerationType gets special handling
(resolved via kind-values.yml into `Literal[...]`) - the other 3 are
resolved directly by the registry's permissive `Any` typing (see its
docstring for the rationale).
"""

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
        """Return classes and datatypes - what the registry must generate.

        Selects kind Class or DataType. Excludes Association and
        Enumeration (separate structures, not generated as instances).
        """
        return {qn: el for qn, el in self.uml.qualified.items() if el.get("kind") in ("Class", "DataType")}

    def associations(self) -> dict[str, dict]:
        return {qn: el for qn, el in self.uml.qualified.items() if el.get("kind") == "Association"}

    def enum_values_for(self, qualified_class: str, attribute: str) -> list[str] | None:
        return (self.kind_values.get(f"{qualified_class}.{attribute}") or {}).get("values")
