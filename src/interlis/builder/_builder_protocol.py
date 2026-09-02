"""Typing-only description of what a VIEW/OID/TRANSLATION mixin can assume `self` provides.

Never a real runtime base of anything - referenced under `TYPE_CHECKING`
by `view_mixin.py`/`oid_mixin.py`/`translation_mixin.py` so mypy can see
`self.registry`/`self.attachment`/... without a circular import back to
`model_builder.py` (which imports all three mixins as real runtime
bases, so a mixin importing the concrete `InterlisModelBuilder` class -
even under `TYPE_CHECKING` - creates a cycle mypy cannot resolve; this
Protocol lives in its own leaf module instead).
"""

from __future__ import annotations

from typing import Any, Protocol

from interlis.builder.attach import AttachmentResolver
from interlis.builder.forward_refs import ForwardRefResolver, SymbolTable
from interlis.builder.repository import ModelRepository
from interlis.metamodel.instance import MetaInstance
from interlis.metamodel.registry import MetamodelRegistry
from interlis.metamodel.uml_schema import MetamodelSchema


class _BuilderHost(Protocol):
    schema: MetamodelSchema
    registry: MetamodelRegistry
    attachment: AttachmentResolver
    repository: ModelRepository
    symbol_table: SymbolTable
    forward_refs: ForwardRefResolver
    _pending_view_all_of: list[tuple[Any, Any]]
    _pending_view_bare_attrs: list[tuple[Any, Any, Any]]
    _pending_translations: list[tuple[Any, str]]

    def visit(self, ctx: Any) -> Any: ...
    def _merge_bag_into_instance(self, instance: MetaInstance, bag: Any, rule_name: str) -> None: ...
    def _current_model_name(self) -> str | None: ...
    def _current_topic_extends_hint(self, ctx: Any) -> str | None: ...
