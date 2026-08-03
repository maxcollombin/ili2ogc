"""Classe de base commune a toutes les classes du metamodele IlisMeta16
generees dynamiquement par interlis.metamodel.registry."""
import itertools
from typing import Any

from pydantic import BaseModel, ConfigDict, PrivateAttr

_next_id = itertools.count(1)


class MetaInstance(BaseModel):
    """Base commune. `extra="allow"` : les champs alimentes par association
    (pas declares dans own/inherited de la classe) sont poses dynamiquement
    par AttachmentResolver au moment de la construction, sans avoir besoin
    d'etre pre-declares ici (voir interlis.metamodel.registry)."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True, validate_assignment=False)

    _qualified_class: str = PrivateAttr(default="")
    _source_ctx: Any | None = PrivateAttr(default=None)
    _id: int = PrivateAttr(default_factory=lambda: next(_next_id))
    # Cas topicDef uniquement : le SubModel et le DataUnit jumeaux construits
    # pour un meme TOPIC (pas une association IlisMeta16 formelle - voir
    # interlis.builder.model_builder).
    _twin: "MetaInstance | None" = PrivateAttr(default=None)

    def __repr__(self) -> str:
        name = getattr(self, "Name", None)
        suffix = f" Name={name!r}" if name is not None else ""
        return f"<{self._qualified_class}{suffix} id={self._id}>"
