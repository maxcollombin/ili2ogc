"""Resolution des references en avant (RULE : une seule passe de visite
ANTLR + resolution differee par table de symboles - voir plan de
conception du ModelBuilder, section 4).

Chaque regle Reference-kind (classRef, structureRef, associationRef,
domainRef, unitRef, viewRef, graphicRef, topicRef, metaDataBasketRef,
metaObjectRef, viewableRef) produit un ForwardRef pose directement dans le
champ concerne via AttachmentResolver, au lieu de resoudre immediatement.
Une passe finale (resolve_all) remplace chaque ForwardRef par l'instance
reelle trouvee dans la SymbolTable, ou par une UnresolvedNamedReference si
le nom semble venir d'un modele importe (decision de perimetre V1 - voir
CLAUDE.md/le plan de conception)."""
from dataclasses import dataclass, field
from typing import Any

from interlis.builder.errors import BuildError, UnresolvedNamedReference


@dataclass
class ForwardRef:
    """Placeholder pose dans un champ pendant la construction, en attendant
    la passe de resolution finale."""
    name: str
    resolves_to_hint: str | list[str] | None = None
    rule: str = ""
    # True pour les references dont la NATURE MEME garantit qu'elles visent
    # toujours un modele importe (ex. modeldef.imports - IMPORTS designe par
    # definition un AUTRE modele, jamais une declaration locale) - contourne
    # l'heuristique generique has_prefix (pensee pour des references qui
    # POURRAIENT etre locales, ex. classRef/associationRef).
    always_external: bool = False


@dataclass
class _Pending:
    ref: ForwardRef
    container: Any
    field: str
    is_list_item: bool = False


class SymbolTable:
    """Nom qualifie (ou court, si non ambigu) -> instance construite.
    Peuplee au fil de l'eau a chaque instance Instance-kind construite dont
    la classe a un attribut Name (direct ou herite)."""

    def __init__(self):
        self._qualified: dict[str, Any] = {}
        self._by_short_name: dict[str, list[Any]] = {}

    def register(self, qualified_name: str, instance: Any) -> None:
        self._qualified[qualified_name] = instance
        short = qualified_name.rsplit(".", 1)[-1]
        self._by_short_name.setdefault(short, []).append(instance)

    def resolve(self, name: str) -> Any | None:
        if name in self._qualified:
            return self._qualified[name]
        short = name.rsplit(".", 1)[-1]
        candidates = self._by_short_name.get(short, [])
        if len(candidates) == 1:
            return candidates[0]
        return None

    def has_prefix(self, name: str) -> bool:
        """True si `name` semble faire reference a un modele/prefixe connu
        du fichier courant (par opposition a un prefixe de modele importe,
        totalement absent de la table)."""
        if "." not in name:
            return True
        prefix = name.split(".", 1)[0]
        return any(qn.startswith(prefix + ".") or qn == prefix for qn in self._qualified)


class ForwardRefResolver:
    def __init__(self, symbol_table: SymbolTable):
        self.symbol_table = symbol_table
        self._pending: list[_Pending] = []

    def register_pending(self, ref: ForwardRef, container: Any, field: str) -> None:
        self._pending.append(_Pending(ref, container, field))

    def resolve_all(self) -> None:
        for entry in self._pending:
            resolved = self._resolve_one(entry.ref)
            current = getattr(entry.container, entry.field, None)
            if isinstance(current, list):
                for i, item in enumerate(current):
                    if item is entry.ref:
                        current[i] = resolved
            else:
                setattr(entry.container, entry.field, resolved)
        self._pending.clear()

    def _resolve_one(self, ref: ForwardRef) -> Any:
        if ref.always_external:
            return UnresolvedNamedReference(ref.name, reason="external_import")
        found = self.symbol_table.resolve(ref.name)
        if found is not None:
            return found
        if not self.symbol_table.has_prefix(ref.name):
            return UnresolvedNamedReference(ref.name, reason="external_import")
        raise BuildError(f"reference non resolue et non attribuable a un import : {ref.name!r}", rule=ref.rule)
