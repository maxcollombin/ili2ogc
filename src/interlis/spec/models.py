"""Schema Pydantic pour une entree de spec/grammar/mapping/*.yml.

Schema unique partage entre la validation statique (scripts/validate_spec.py)
et l'execution du ModelBuilder (interlis.builder) - une seule definition de
"a quoi ressemble une entree de mapping", pour ne jamais faire diverger les
deux avec le temps.
"""
from typing import Any

from pydantic import BaseModel, field_validator

KNOWN_KINDS = {"Instance", "Container", "Reference", "Conditional", "Dispatcher", "ValueObject"}


class Discriminant(BaseModel, extra="allow"):
    attribute: str


class Parent(BaseModel, extra="allow"):
    association: str
    role: str


class TargetBranch(BaseModel, extra="allow"):
    target: str
    discriminant: Discriminant | None = None


class SpecEntry(BaseModel, extra="allow"):
    grammar: dict[str, Any]
    kind: str
    target: str | list[str] | None = None
    resolves_to: str | list[str] | None = None
    resolves_to_constraint: dict[str, Any] | None = None
    feeds_into: str | None = None
    discriminant: Discriminant | None = None
    parent: Parent | None = None
    dispatches_to: list[Any] | None = None
    children: list[Any] | None = None
    type: str | None = None
    when_present: dict[str, TargetBranch] | None = None
    default: dict[str, Any] | None = None
    attribute_bindings: dict[str, Any] | None = None
    note: str | None = None

    @field_validator("kind")
    @classmethod
    def kind_known(cls, v):
        if v not in KNOWN_KINDS:
            raise ValueError(f"kind inconnu: {v!r} (attendu un de {KNOWN_KINDS})")
        return v
