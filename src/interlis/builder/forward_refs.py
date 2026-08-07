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
    # Nom du MODEL englobant la construction qui a produit ce ForwardRef
    # (Lot 33) - desambiguise un nom court present dans PLUSIEURS modeles
    # d'un meme fichier multi-MODEL (ex. "PointStructure" declare
    # separement dans BaseModel_SectoralPlans_LV03_V1_4 ET _LV95_V1_4,
    # meme fichier, MEME symbol_table depuis le fix multi-modeles du
    # Lot 28). None si non determinable (ex. modele predefini INTERLIS).
    home_model: str | None = None
    # Texte brut (Lot 38 point 2) du 1er topicRef() du TOPIC englobant cette
    # reference, UNIQUEMENT si ce TOPIC porte un EXTENDS (ex.
    # "CatalogueObjects_V1.Catalogues" pour une reference produite dans
    # `TOPIC AxisCatalogs EXTENDS CatalogueObjects_V1.Catalogues = ...`) -
    # None si le TOPIC englobant n'a pas d'EXTENDS, ou si aucun TOPIC
    # n'englobe cette reference. RULE #4, citation directe eCH-0031 V2.1.0
    # §3.5.4 "Namensraume" : "Erweitert ein Modellierungselement ein
    # anderes, werden seinen Namensraumen alle Namen des
    # Basis-Modellierungselementes zugefuegt" (etend un element de
    # modelisation un autre, tous les noms de l'element de base sont
    # ajoutes a son espace de noms) - un TOPIC B EXTENDS TOPIC A rend
    # visibles, SANS IMPORTS UNQUALIFIED, tous les noms courts declares
    # dans A a l'interieur de B. Utilise en dernier recours par
    # `ForwardRefResolver._resolve_one` pour un nom NON qualifie introuvable
    # localement, AVANT le repli IMPORTS UNQUALIFIED existant.
    topic_extends_hint: str | None = None
    # True pour un ForwardRef porte par le role Super de l'association
    # Inheritance (classDef/structureDef/topicDef EXTENDS, Lot 38 point 2) -
    # un EXTENDS non resolu ne doit JAMAIS faire planter tout le build,
    # symetriquement a la politique deja en place pour domainRef/BaseClass
    # (Lot 36) : degrade en UnresolvedNamedReference plutot que BuildError,
    # y compris quand `topic_extends_hint` a ete tente et a echoue (ex.
    # modele externe absent du `--repo` fourni). Positionne par
    # InterlisModelBuilder._apply_one_binding, jamais par _resolve_or_defer
    # (qui ne connait pas encore le role/association cible a ce stade).
    graceful: bool = False


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
        # Noms de modeles importes via `IMPORTS UNQUALIFIED X` dans CE
        # fichier (ex. {"INTERLIS"}) - jamais persiste cote metamodele
        # (Import n'a pas d'attribut propre pour UNQUALIFIED, confirme
        # metamodel.txt - voir spec/grammar/mapping/02_packages.yml,
        # _imports_unqualified), mais necessaire ici : seul un import
        # explicitement UNQUALIFIED autorise une reference NON qualifiee a
        # resoudre vers un modele importe plutot que de rester strictement
        # locale au fichier courant (Reference Manual eCH-0031 V2.1.0
        # §3.5.1). Peuple par InterlisModelBuilder._register_unqualified_imports.
        self.unqualified_imports: set[str] = set()

    def register(self, qualified_name: str, instance: Any) -> None:
        self._qualified[qualified_name] = instance
        short = qualified_name.rsplit(".", 1)[-1]
        self._by_short_name.setdefault(short, []).append(instance)

    def all_registered(self) -> list[Any]:
        """Toutes les instances enregistrees (deduplique par identite - un
        alias, ex. rekey_model_prefix, peut faire pointer 2 cles differentes
        vers le MEME objet). Utilise par xtf/schema.py pour enumerer les
        associations connues sans acceder a l'etat prive."""
        seen: set[int] = set()
        result = []
        for instance in self._qualified.values():
            if id(instance) not in seen:
                seen.add(id(instance))
                result.append(instance)
        return result

    def resolve(self, name: str, kind_hint: str | list[str] | None = None, home_model: str | None = None) -> Any | None:
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
        if len(candidates) > 1 and home_model:
            # CORRIGE (Lot 33) : un meme nom court peut aussi etre declare
            # dans PLUSIEURS MODELES du MEME fichier .ili (multi-MODEL,
            # fix Lot 28 - ex. "PointStructure" dans BaseModel_SectoralPlans_
            # LV03_V1_4 ET _LV95_V1_4, partageant cette MEME symbol_table).
            # Le kind_hint seul ne peut pas lever cette ambiguite (les 2
            # candidats sont de la MEME classe metamodele concrete, ex.
            # Class[Kind=Structure]) - repli sur le MODEL englobant de la
            # construction qui a demande cette resolution (voir
            # InterlisModelBuilder._current_model_name), en cherchant
            # directement dans `_qualified` (les candidats de `_by_short_name`
            # ne portent pas leur propre nom qualifie).
            candidate_ids = {id(c) for c in candidates}
            in_model = [
                v for k, v in self._qualified.items()
                if k.startswith(home_model + ".") and k.rsplit(".", 1)[-1] == short and id(v) in candidate_ids
            ]
            if len(in_model) == 1:
                return in_model[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def rekey_model_prefix(self, old_prefix: str, new_prefix: str) -> None:
        """Ajoute, pour toute entree qualifiee `old_prefix.X`, un ALIAS
        `new_prefix.X` -> meme instance (l'entree d'origine reste, jamais
        retiree) - utilise UNIQUEMENT par ModelRepository pour le modele
        INTERLIS predefini (voir repository.py, _PREDEFINED_INTERLIS_SOURCE) :
        son MODEL declare porte un nom interne different du nom reel
        documente par le manuel (contrainte lexer - "INTERLIS" est un token
        reserve, pas un Name valide) ; corrige ici une fois le modele
        construit pour que les references qualifiees ecrites par un
        utilisateur (`INTERLIS.I32OID`) trouvent une correspondance exacte
        plutot que de ne compter que sur le repli par nom court de
        `resolve()`. Ne touche QUE `_qualified`, jamais `_by_short_name` :
        l'instance y figure deja depuis son enregistrement d'origine
        (`register()`) - la retoucher dupliquerait l'entree et casserait le
        cas `len(candidates) == 1` de `resolve()` sur un nom court par
        ailleurs non ambigu (trouve en ecrivant le test de ce mecanisme)."""
        prefix = old_prefix + "."
        for qualified, instance in list(self._qualified.items()):
            if qualified.startswith(prefix):
                self._qualified[new_prefix + "." + qualified[len(prefix):]] = instance

    def has_prefix(self, name: str) -> bool:
        """True si `name` est QUALIFIE (contient un prefixe de modele) ET que
        ce prefixe designe le fichier courant lui-meme (par opposition a un
        prefixe de modele importe, totalement absent de la table). Ne
        s'applique qu'aux noms qualifies - un nom sans point releve d'une
        logique separee, voir ForwardRefResolver._resolve_one."""
        if "." not in name:
            return False
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
        found = self.symbol_table.resolve(ref.name, kind_hint=kind_hint, home_model=ref.home_model)
        if found is not None:
            return found
        if "." in ref.name:
            if not self.symbol_table.has_prefix(ref.name):
                # Prefixe absent de CE fichier : candidat a une resolution
                # cross-fichier si un ModelRepository est configure (voir
                # repository.py) - chaque modele charge garde sa PROPRE
                # table, jamais fusionnee, donc aucune ambiguite de nom court
                # introduite entre modeles sans rapport (seule une
                # correspondance de nom QUALIFIE COMPLET dans la table du
                # modele explicitement vise compte).
                if repository is not None:
                    prefix = ref.name.split(".", 1)[0]
                    found = repository.resolve_external(prefix, ref.name, kind_hint)
                    if found is not None:
                        return found
                return UnresolvedNamedReference(ref.name, reason="external_import")
            if ref.graceful:
                # EXTENDS non resolu (Lot 38 point 2, RULE #5) : jamais
                # fatal, symetrique a domainRef/BaseClass (Lot 36).
                return UnresolvedNamedReference(ref.name, reason="unresolved_extends")
            raise BuildError(f"reference non resolue et non attribuable a un import : {ref.name!r}", rule=ref.rule)
        # Nom NON qualifie introuvable localement : essayer d'abord le
        # namespace du TOPIC EXTENDS englobant, le cas echeant (RULE #4,
        # voir ForwardRef.topic_extends_hint) - AVANT le repli IMPORTS
        # UNQUALIFIED existant, les deux mecanismes etant independants.
        found = self._resolve_via_topic_extends(ref, kind_hint, repository)
        if found is not None:
            return found
        # Sinon, seule une reference vers un modele explicitement importe
        # UNQUALIFIED (voir SymbolTable.unqualified_imports) peut
        # legitimement la designer - sinon c'est un vrai bug local (nom
        # jamais declare dans ce fichier), a signaler par une exception
        # plutot qu'a masquer (RULE #5).
        if not self.symbol_table.unqualified_imports:
            if ref.graceful:
                return UnresolvedNamedReference(ref.name, reason="unresolved_extends")
            raise BuildError(
                f"reference non resolue et non attribuable a un import : {ref.name!r}", rule=ref.rule
            )
        if repository is not None:
            for model_name in self.symbol_table.unqualified_imports:
                found = repository.resolve_external(model_name, ref.name, kind_hint)
                if found is not None:
                    return found
        return UnresolvedNamedReference(ref.name, reason="external_import")

    def _resolve_via_topic_extends(self, ref: ForwardRef, kind_hint, repository=None) -> Any | None:
        """Tente de resoudre un nom NON qualifie comme membre du namespace du
        TOPIC EXTENDS englobant (voir ForwardRef.topic_extends_hint pour la
        justification RULE #4). Seul le cas d'un EXTENDS QUALIFIE (topic
        d'un AUTRE modele, ex. "CatalogueObjects_V1.Catalogues") necessite
        un traitement dedie ici : un EXTENDS sur un topic du MEME fichier
        (non qualifie, ex. "TOPIC Countries EXTENDS AdministrativeUnits")
        est deja couvert sans rien de special, la resolution par nom court
        generique (`SymbolTable.resolve` plus haut dans `_resolve_one`)
        trouvant deja le nom cible dans la MEME table de symboles, sans
        notion de portee par topic - retenter ici un candidat "hint.name"
        ne ferait que redupliquer exactement le meme repli par nom court,
        pour rien."""
        hint = ref.topic_extends_hint
        if not hint or "." not in hint:
            return None
        candidate = f"{hint}.{ref.name}"
        found = self.symbol_table.resolve(candidate, kind_hint=kind_hint)
        if found is not None:
            return found
        if repository is not None:
            prefix = hint.split(".", 1)[0]
            return repository.resolve_external(prefix, candidate, kind_hint)
        return None
