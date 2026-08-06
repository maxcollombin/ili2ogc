"""Resolution des references en avant (RULE : une seule passe de visite
ANTLR + resolution differee par table de symboles - voir plan de
conception du ModelBuilder, section 4).

Chaque regle Reference-kind (classRef, structureRef, associationRef,
domainRef, unitRef, viewRef, graphicRef, topicRef, metaDataBasketRef,
metaObjectRef, viewableRef) produit un ForwardRef pose directement dans le
champ concerne via AttachmentResolver, au lieu de resoudre immediatement.
Une passe finale (resolve_all) remplace chaque ForwardRef par l'instance
reelle trouvee dans la SymbolTable (fichier courant), ou - si un
ModelRepository est fourni (resolution multi-fichiers, voir repository.py) -
dans la table du modele importe correspondant, chargee a la demande. Sans
repository (ou si le modele reste introuvable), le nom devient un
UnresolvedNamedReference documente plutot qu'une exception."""
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

    def resolve(self, name: str, kind_hint: str | list[str] | None = None) -> Any | None:
        if name in self._qualified:
            return self._qualified[name]
        short = name.rsplit(".", 1)[-1]
        candidates = self._by_short_name.get(short, [])
        if kind_hint is not None and len(candidates) > 1:
            # Le meme nom court peut designer des entites metamodele
            # DIFFERENTES (ex. une CLASS "MetaElement" et un role anonyme
            # (nomme d'apres sa classe cible) "MetaElement" dans une autre
            # association, ou "Localisation" a la fois une CLASS et un role
            # anonyme d'association - trouve sur DMAV_AdressesDeBatiments_V1_0.ili)
            # - le kind_hint (target/resolves_to du binding Reference-kind,
            # ex. "Class", ou plusieurs valeurs possibles ex. viewableRef :
            # ["Class", "View"]) permet de lever l'ambiguite en ne retenant
            # que les instances de la/des classe(s) metamodele visee(s).
            hints = kind_hint if isinstance(kind_hint, list) else [kind_hint]
            filtered = [c for c in candidates if getattr(c, "_qualified_class", "").rsplit(".", 1)[-1] in hints]
            if len(filtered) == 1:
                return filtered[0]
            candidates = filtered if filtered else candidates
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

    def resolve_all(self, repository=None) -> None:
        for entry in self._pending:
            resolved = self._resolve_one(entry.ref, repository)
            current = getattr(entry.container, entry.field, None)
            if isinstance(current, list):
                for i, item in enumerate(current):
                    if item is entry.ref:
                        current[i] = resolved
            else:
                setattr(entry.container, entry.field, resolved)
        self._pending.clear()

    def _resolve_one(self, ref: ForwardRef, repository=None) -> Any:
        kind_hint = ref.resolves_to_hint if isinstance(ref.resolves_to_hint, (str, list)) else None
        if ref.always_external:
            # Le nom lui-meme (ex. modeldef.imports.ImportedP) EST le nom du
            # modele importe, jamais prefixe - si un ModelRepository est
            # configure, tenter de le charger et de le resoudre vers
            # l'instance Model reelle (elle s'enregistre sous son propre nom
            # nu, meme mecanisme que toute autre instance nommee) plutot que
            # de toujours retomber sur UnresolvedNamedReference.
            if repository is not None:
                found = repository.resolve_external(ref.name, ref.name, kind_hint)
                if found is not None:
                    return found
            return UnresolvedNamedReference(ref.name, reason="external_import")
        found = self.symbol_table.resolve(ref.name, kind_hint=kind_hint)
        if found is not None:
            return found
        if not self.symbol_table.has_prefix(ref.name):
            # Prefixe absent de CE fichier : candidat a une resolution
            # cross-fichier si un ModelRepository est configure (voir
            # repository.py) - chaque modele charge garde sa PROPRE table,
            # jamais fusionnee, donc aucune ambiguite de nom court introduite
            # entre modeles sans rapport (seule une correspondance de nom
            # QUALIFIE COMPLET dans la table du modele explicitement vise
            # compte).
            if repository is not None and "." in ref.name:
                prefix = ref.name.split(".", 1)[0]
                found = repository.resolve_external(prefix, ref.name, kind_hint)
                if found is not None:
                    return found
            return UnresolvedNamedReference(ref.name, reason="external_import")
        raise BuildError(f"reference non resolue et non attribuable a un import : {ref.name!r}", rule=ref.rule)
