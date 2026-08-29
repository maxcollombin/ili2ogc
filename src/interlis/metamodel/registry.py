"""Dynamically generate the Pydantic classes for the IlisMeta16 metamodel.

Built from mappings/ilismeta16-*.yml at every startup (never written to
disk or committed separately) - the YAML stays the single source of
truth. Design rationale (why fields are typed `Any`, why generation
needs no forward-ref resolution): docs/dev-notes/metamodel-registry-any-typing.md.
"""

from typing import Any

from pydantic import Field, create_model

from interlis.metamodel.instance import MetaInstance
from interlis.metamodel.uml_schema import GENERIC_ENUM_TYPE_UUID, MetamodelSchema


class MetamodelRegistry:
    def __init__(self, schema: MetamodelSchema, classes: dict[str, type[MetaInstance]]):
        self.schema = schema
        self.classes = classes  # qualified_name -> generated Pydantic class

    @classmethod
    def build(cls, schema: MetamodelSchema) -> "MetamodelRegistry":
        classes: dict[str, type[MetaInstance]] = {}
        for qn, element in schema.instantiable_classes().items():
            classes[qn] = cls._build_class(qn, element, schema)
        # Some associations are themselves constructed as "link objects" by
        # the spec (e.g. ObjectOID: Class<->Oid, see topicDef) - one field
        # per end role, no own/inherited attributes (associations have none
        # in ilismeta16-associations.yml).
        for qn, element in schema.associations().items():
            classes[qn] = cls._build_association_class(qn, element)
        return cls(schema, classes)

    def get(self, qualified_name: str) -> type[MetaInstance]:
        try:
            return self.classes[qualified_name]
        except KeyError:
            raise LookupError(f"no generated class for {qualified_name!r} (not a Class/DataType?)") from None

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
