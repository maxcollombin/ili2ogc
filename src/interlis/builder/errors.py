"""Erreurs et objets de diagnostic du ModelBuilder."""
from typing import Any


class BuildError(Exception):
    """Erreur de construction non recuperable - binding mal forme, valeur
    introuvable sur le ctx ANTLR, association/role inconnu, etc. Porte la
    regle grammaticale et, si disponible, la position source (ligne/colonne
    du token de depart du ctx) pour un diagnostic actionnable plutot qu'un
    crash Python opaque (RULE #5 du skill interlis-mapping : documenter
    l'incertitude/l'erreur plutot que la masquer)."""

    def __init__(self, message: str, *, rule: str | None = None, ctx: Any = None):
        self.rule = rule
        self.ctx = ctx
        location = ""
        start = getattr(ctx, "start", None)
        if start is not None:
            location = f" (ligne {start.line}, colonne {start.column})"
        prefix = f"[{rule}]" if rule else ""
        super().__init__(f"{prefix}{location} {message}".strip())


class UnresolvedNamedReference:
    """Reference nommee non resolue - decision de perimetre V1 (voir plan de
    conception ModelBuilder) : une reference vers un modele importe
    (IMPORTS) devient cet objet documente explicitement plutot qu'une
    exception ou un None silencieux."""

    __slots__ = ("name", "reason")

    def __init__(self, name: str, reason: str = "external_import"):
        self.name = name
        self.reason = reason

    def __repr__(self) -> str:
        return f"<UnresolvedNamedReference {self.name!r} reason={self.reason!r}>"

    def __eq__(self, other):
        return isinstance(other, UnresolvedNamedReference) and (self.name, self.reason) == (other.name, other.reason)
