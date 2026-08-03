"""Genere dynamiquement les classes Pydantic representant les classes/
datatypes du metamodele IlisMeta16, a partir de mappings/ilismeta16-*.yml.
Jamais ecrit sur disque, jamais commite separement - reconstruit a chaque
demarrage depuis le YAML, qui reste l'unique source de verite.

Simplification assumee (decision de conception) : les champs generes sont
types `Any` (scalaire) / `list[Any]` (multiplicite `*`), pas des types
Python precis derives du metamodele - sauf pour les attributs
d'enumeration anonyme (voir GENERIC_ENUM_TYPE_UUID), types en Literal[...]
car la source (ilismeta16-kind-values.yml) est fermee et sans ambiguite.
Raison : la distinction "valeur scalaire native (bool/str/float) vs
instance imbriquee d'une autre classe du metamodele" n'est PAS marquee
explicitement dans ilismeta16-*.yml - des classes comme INTERLIS.BOOLEAN/
INTERLIS.NAME sont des marqueurs vides (own/inherited = {}) utilises tantot
comme "type primitif" (valeur True/False/str attendue), tantot comme
supertype abstrait dans une hierarchie de generalisation (ex.
INTERLIS.ANYUNIT) - une inference statique fiable demanderait une etude
dediee non couverte par cette conception. La bonne valeur (scalaire ou
MetaInstance imbriquee) est produite au moment voulu par
interlis.builder.source_resolver, qui lit attribute_bindings.source - pas
par le schema Pydantic statique. Consequence positive : aucun probleme de
reference croisee/forward-ref entre les ~84 classes generees (Any n'a pas
besoin d'etre resolu), donc generation en une seule passe.

Les champs alimentes par association (pas dans own/inherited, ex.
AttrOrParam.Type via l'association AttrOrParamType) ne sont pas
pre-declares : ils passent par `extra="allow"` de MetaInstance, poses
dynamiquement par interlis.builder.attach.AttachmentResolver."""
from typing import Any

from pydantic import Field, create_model

from interlis.metamodel.instance import MetaInstance
from interlis.metamodel.uml_schema import GENERIC_ENUM_TYPE_UUID, MetamodelSchema


class MetamodelRegistry:
    def __init__(self, schema: MetamodelSchema, classes: dict[str, type[MetaInstance]]):
        self.schema = schema
        self.classes = classes  # qualified_name -> classe Pydantic generee

    @classmethod
    def build(cls, schema: MetamodelSchema) -> "MetamodelRegistry":
        classes: dict[str, type[MetaInstance]] = {}
        for qn, element in schema.instantiable_classes().items():
            classes[qn] = cls._build_class(qn, element, schema)
        # Certaines associations sont elles-memes construites comme des
        # "objets-lien" par la spec (ex. ObjectOID : Class<->Oid, cf.
        # topicDef) - un champ par role de bout, pas d'attributs own/inherited
        # (les associations n'en ont pas dans ilismeta16-associations.yml).
        for qn, element in schema.associations().items():
            classes[qn] = cls._build_association_class(qn, element)
        return cls(schema, classes)

    def get(self, qualified_name: str) -> type[MetaInstance]:
        try:
            return self.classes[qualified_name]
        except KeyError:
            raise LookupError(f"aucune classe generee pour {qualified_name!r} (pas Class/DataType ?)") from None

    def new_instance(self, qualified_name: str, **initial) -> MetaInstance:
        model_cls = self.get(qualified_name)
        instance = model_cls.model_construct(**initial)
        instance._qualified_class = qualified_name
        return instance

    @staticmethod
    def _build_class(qn: str, element: dict, schema: MetamodelSchema) -> type[MetaInstance]:
        py_name = qn.replace(".", "_")
        fields: dict[str, tuple[Any, Any]] = {}
        attrs = element.get("attributes", {}) or {}
        for group in ("own", "inherited"):
            for attr_name, attr_def in (attrs.get(group) or {}).items():
                fields[attr_name] = MetamodelRegistry._field_spec(qn, attr_name, attr_def, schema)
        return create_model(py_name, __base__=MetaInstance, **fields)

    @staticmethod
    def _build_association_class(qn: str, element: dict) -> type[MetaInstance]:
        py_name = qn.replace(".", "_")
        fields: dict[str, tuple[Any, Any]] = {}
        for end in element.get("ends", []):
            role = end.get("role")
            if not role:
                continue
            if end.get("upper") == "*":
                fields[role] = (list[Any], Field(default_factory=list))
            else:
                fields[role] = (Any | None, None)
        return create_model(py_name, __base__=MetaInstance, **fields)

    @staticmethod
    def _field_spec(qn: str, attr_name: str, attr_def: dict, schema: MetamodelSchema) -> tuple[Any, Any]:
        upper = (attr_def.get("multiplicity") or {}).get("upper", "1")
        py_type: Any = Any
        if attr_def.get("type") == GENERIC_ENUM_TYPE_UUID:
            values = schema.enum_values_for(qn, attr_name)
            if values:
                from typing import Literal
                py_type = Literal[tuple(values)]  # type: ignore[valid-type]
        if upper == "*":
            return (list[py_type], Field(default_factory=list))
        return (py_type | None, None)
