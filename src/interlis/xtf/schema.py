"""Couche SEMANTIQUE (Lot 30) : croise les objets XTF structurels (parse.py,
Lot 27) avec le schema deja construit par InterlisModelBuilder (Class/
AttrOrParam/Type), pour que validate.py puisse interpreter chaque attribut
selon son type DECLARE plutot que sa seule forme XML brute.

Reutilise directement la SymbolTable/ModelRepository deja construites par le
ModelBuilder (voir builder/forward_refs.py, builder/repository.py) - un
Class instance est deja enregistre sous son nom qualifie complet
(Model.Topic.ClassName, via InterlisModelBuilder._qualify_name) qui
correspond EXACTEMENT a XtfObject.qualified_class (meme convention de
nommage - confirme empiriquement, RULE #1)."""
from dataclasses import dataclass

from interlis.builder.repository import ModelRepository
from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance


def resolve_class(qualified_class: str, *, symbol_table: SymbolTable, repository: ModelRepository | None) -> MetaInstance | None:
    """Retrouve l'instance IlisMeta16.ModelData.Class correspondant a un
    XtfObject.qualified_class ("Model.Topic.ClassName"). Cherche d'abord dans
    la table du modele racine (classe locale ou deja resolue par import),
    puis - si absente et qu'un repository est fourni - dans le modele
    designe par le PREMIER segment du nom qualifie (meme mecanisme que la
    resolution de reference croisee du ModelBuilder, ForwardRefResolver)."""
    found = symbol_table.resolve(qualified_class, kind_hint=["Class"])
    if isinstance(found, MetaInstance):
        return found
    if repository is not None and "." in qualified_class:
        model_name = qualified_class.split(".", 1)[0]
        found = repository.resolve_external(model_name, qualified_class, kind_hint=["Class"])
        if isinstance(found, MetaInstance):
            return found
    return None


def attributes_of(class_instance: MetaInstance) -> dict[str, MetaInstance]:
    """Nom d'attribut -> instance AttrOrParam, pour les attributs PROPRES a
    cette classe (association ClassAttr, role ClassAttribute - voir
    spec/grammar/mapping/04_attributes.yml, attributeDef.parent).
    N'inclut PAS les attributs herites via EXTENDS - limite documentee
    (README/PROGRESS), hors perimetre de ce lot (necessiterait de remonter
    la chaine Inheritance, jamais exercee par le corpus XTF cible a ce
    jour)."""
    return {
        a.Name: a
        for a in (getattr(class_instance, "ClassAttribute", None) or [])
        if isinstance(a, MetaInstance) and getattr(a, "Name", None)
    }


def _role_base_class(role: MetaInstance) -> MetaInstance | None:
    base = getattr(role, "BaseClass", None)
    if isinstance(base, list):
        base = base[0] if base else None
    return base if isinstance(base, MetaInstance) else None


def _role_is_multi(role: MetaInstance) -> bool:
    """True si la cardinalite du role est > 1 (Multiplicity.Max == '*') -
    Min/Max sont des chaines TEXT (confirme ilismeta16-classes.yml,
    Multiplicity.own.Max: type TEXT), jamais absentes quand une clause
    cardinality() est presente ; `Multiplicity is None` (aucune clause dans
    le .ili) signifie la cardinalite par defaut, jamais > 1 (confirme
    empiriquement sur roleDef sans cardinality() explicite, ex.
    `rMeasurementLocation -<#> MeasurementLocation;`, Lot 32)."""
    mult = getattr(role, "Multiplicity", None)
    return isinstance(mult, MetaInstance) and getattr(mult, "Max", None) == "*"


def embedded_roles_of(class_instance: MetaInstance, symbol_table: SymbolTable) -> dict[str, MetaInstance]:
    """Nom de role -> instance Role, pour les roles d'ASSOCIATION EMBARQUES
    (transferes comme pseudo-attributs de CETTE classe dans le XTF, ex.
    `rMeasurementLocation` sur `Indicator`) - PAS les attributs ClassAttr
    ordinaires (voir attributes_of).

    Algorithme confirme (RULE #4) contre le Reference Manual eCH-0031
    V2.1.0 §4.3.9 "Codierung von Beziehungen" (citation directe, lue avant
    tout code - Lot 32) :
    - Une association a EXACTEMENT 2 roles est TOUJOURS embarquee, sauf cas
      hors perimetre de ce lot (>2 roles, OID explicite sur l'association,
      certaines relations inter-topics - non geres ici, association alors
      simplement ignoree plutot que mal classee).
    - Si UN SEUL des 2 roles (base) a une cardinalite max > 1 : embarquee
      cote classe CIBLE de CE role ; le NOM du pseudo-attribut embarque est
      celui de l'AUTRE role (§4.3.9.1 : "pour RoleName, le nom du role
      pointant vers l'objet OPPOSE doit etre donne").
    - Si les 2 roles ont une cardinalite max <= 1 : embarquee cote classe
      cible du DEUXIEME role declare (ordre du fichier .ili) ; pseudo-attribut
      nomme d'apres le PREMIER role.
    - Si les 2 roles ont une cardinalite max > 1 : PAS embarquee (transferee
      comme instance de classe separee, §4.3.9.2) - absente du resultat.
    Limite assumee (RULE #7, hors perimetre) : la nuance "meme Topic que
    l'association" du manuel (qui peut forcer une association a NE PAS
    s'embarquer si les classes cibles sont dans un topic different) n'est
    PAS verifiee - toutes les associations resolues sont traitees comme si
    elles etaient dans le meme topic que leurs classes cibles (cas de loin
    le plus frequent, confirme sur le corpus reel des 3 fichiers XTF
    cibles). Ne cherche que dans `symbol_table` (associations locales au
    modele racine) - PAS dans un ModelRepository (associations definies
    dans un modele importe non couvertes, hors perimetre)."""
    result: dict[str, MetaInstance] = {}
    for candidate in symbol_table.all_registered():
        if not isinstance(candidate, MetaInstance) or candidate._qualified_class.rsplit(".", 1)[-1] != "Class":
            continue
        if getattr(candidate, "Kind", None) != "Association":
            continue
        roles = [r for r in (getattr(candidate, "Role", None) or []) if isinstance(r, MetaInstance)]
        if len(roles) != 2:
            continue
        role_a, role_b = roles
        target_a, target_b = _role_base_class(role_a), _role_base_class(role_b)
        if target_a is None or target_b is None:
            continue
        multi_a, multi_b = _role_is_multi(role_a), _role_is_multi(role_b)
        if multi_a and multi_b:
            continue
        if multi_a:
            embed_on, embedded_role = target_a, role_b
        elif multi_b:
            embed_on, embedded_role = target_b, role_a
        else:
            embed_on, embedded_role = target_b, role_a
        if embed_on is class_instance and getattr(embedded_role, "Name", None):
            result[embedded_role.Name] = embedded_role
    return result


def schema_members_of(class_instance: MetaInstance, symbol_table: SymbolTable) -> dict[str, MetaInstance]:
    """Union de attributes_of (ClassAttr) et embedded_roles_of (roles
    d'association embarques, Lot 32) - la vue complete des pseudo-attributs
    qu'un objet XTF de cette classe peut porter."""
    members = dict(attributes_of(class_instance))
    members.update(embedded_roles_of(class_instance, symbol_table))
    return members


@dataclass
class ResolvedAttribute:
    """Un attribut de schema pret a etre interprete/valide : son instance
    AttrOrParam OU Role (Lot 32 : les roles d'association embarques sont
    traites de facon uniforme, via BaseClass au lieu de Type), son Type/
    classe-cible resolu (peut etre None si non resolu - reference externe
    hors perimetre, cf. docs xtf-transfer-encoding-notes.md / README Known
    limitations), et le nom court de la classe metamodele concrete du Type
    (ex. "TextType", "NumType", "EnumType", "ReferenceType", "Class" -
    jamais l'abstrait "DomainType")."""
    attr: MetaInstance
    type_instance: MetaInstance | None
    type_kind: str | None
    mandatory: bool


def resolve_attribute(attr: MetaInstance) -> ResolvedAttribute:
    if attr._qualified_class.rsplit(".", 1)[-1] == "Role":
        # Role EXTENDS ReferenceType EXTENDS ClassRelatedType EXTENDS
        # DomainType (confirme ilismeta16-classes.yml) : porte son PROPRE
        # Mandatory (herite de DomainType) - contrairement a AttrOrParam,
        # pas de champ Type separe, la classe cible vient de BaseClass
        # (attache via l'association BaseClass, comme pour tout autre
        # ClassRelatedType - meme mecanisme que ReferenceType).
        target = _role_base_class(attr)
        return ResolvedAttribute(
            attr=attr, type_instance=target, type_kind="Class" if target is not None else None,
            mandatory=bool(getattr(attr, "Mandatory", False)),
        )
    type_instance = getattr(attr, "Type", None)
    type_instance = type_instance if isinstance(type_instance, MetaInstance) else None
    type_kind = type_instance._qualified_class.rsplit(".", 1)[-1] if type_instance is not None else None
    mandatory = bool(getattr(type_instance, "Mandatory", False)) if type_instance is not None else False
    return ResolvedAttribute(attr=attr, type_instance=type_instance, type_kind=type_kind, mandatory=mandatory)


def reference_external_status(resolved: ResolvedAttribute) -> bool | None:
    """Statut de la clause optionnelle `REFERENCE TO (EXTERNAL) X` pour cet
    attribut (Lot 34, `ReferenceType.External`, own BOOLEAN deja construit
    par referenceAttr() - spec/grammar/mapping/04_attributes.yml). Utilise
    pour distinguer, quand un REF extrait ne resout vers AUCUN objet du
    transfert, une reference EXTERNE legitime (catalogue/panier separe,
    RULE #4 - eCH-0031 V2.1.0 §3.6.3 : SANS cette clause, la cible DOIT
    normalement resoudre dans le MEME panier) d'un signal plus probable de
    donnee incorrecte. Tri-state (`True`/`False`/`None`) plutot que bool :
    `None` signifie "ce validateur ne sait pas identifier une REFERENCE
    TO ici avec confiance" - a NE PAS confondre avec `False` ("confirme
    NON-EXTERNAL") ; RULE #5, ne jamais deguiser une incertitude en fait.

    2 formes CONFIRMEES (True/False, jamais None) dans le corpus reel
    (RoadTrafficCensus_V1_1.MLocStatusRef) :
    - `resolved.type_kind == "ReferenceType"` : direct, `.External` lu tel
      quel sur `resolved.type_instance`.
    - `resolved.type_kind == "Class"` (Structure) ENVELOPPANT exactement un
      ClassAttribute dont le Type resout LUI-MEME en `ReferenceType` : motif
      standard `CatalogueObjects_V1.Catalogues.MandatoryCatalogueReference`
      (ex. `MLocStatusRef.Reference: REFERENCE TO (EXTERNAL) MLocStatus`) -
      l'attribut XTF observe (ex. "MLocStatus") porte Type=Class (la
      structure elle-meme), pas directement ReferenceType ; descend d'UN
      niveau pour retrouver le VRAI `.External`.

    `None` (statut REELLEMENT indetermine, PAS "suppose False") pour tout
    le reste : ex. `resolved.type_kind == "Class"` enveloppant un attribut
    UNIQUE qui n'est PAS une reference (trouve reel sur
    `Axis_V1_1.AxisSegmentGeometry` - une STRUCTURE a un seul attribut,
    mais de type geometrie `LineWithAltitude`, pas une reference du tout -
    memes conditions structurelles que le motif catalogue, contenu
    different) ; roles d'association embarques (Lot 32, `Role` - le manuel
    permet AUSSI un `EXTERNAL` sur un role, mais son mapping actuel,
    `roleDef.EmbeddedTransfer`, est documente "polarity unconfirmed" depuis
    ce lot, spec/grammar/mapping/05_associations.yml - PAS reutilise ici
    tant que non confirme)."""
    if resolved.type_kind == "ReferenceType" and resolved.type_instance is not None:
        return bool(getattr(resolved.type_instance, "External", False))
    if resolved.type_kind == "Class" and resolved.type_instance is not None:
        wrapped = attributes_of(resolved.type_instance)
        if len(wrapped) == 1:
            inner = resolve_attribute(next(iter(wrapped.values())))
            if inner.type_kind == "ReferenceType" and inner.type_instance is not None:
                return bool(getattr(inner.type_instance, "External", False))
    return None


def enum_values(enum_type: MetaInstance) -> set[str]:
    """Tous les CHEMINS POINTES valides pour ce EnumType (RULE #4, Reference
    Manual eCH-0031 V2.1.0 §4.3.11.3, citation exacte : "EnumValue =
    (EnumElement-Name {'.' EnumElement-Name}) | 'OTHERS'." - un enum
    HIERARCHIQUE (EnumNode avec des enfants) se transfere comme le CHEMIN
    COMPLET depuis la racine, pas le seul nom du noeud feuille (Lot 33,
    bug trouve sur KGS_PBC_V2_2.KGS_Kategorie reel : "A (A,
    verstaerkter_Schutz), B" - la valeur reelle transferee est "A.A", PAS
    "A" seul, pour le noeud "A" imbrique sous le noeud racine "A" homonyme).
    "Pour l'encodage ... la syntaxe est appliquee INDEPENDAMMENT du fait que
    le domaine de valeurs ne couvre que les feuilles ou aussi les noeuds" -
    donc CHAQUE noeud contribue son propre chemin, pas seulement les
    feuilles. EnumType.TopNode est un noeud RACINE SYNTHETIQUE (Name="TOP",
    jamais une valeur reelle - confirme par `models/IlisMeta16.ili`,
    commentaire sur EnumNode : "MetaElement.Name := 'TOP' for topnode" -
    et construit comme tel depuis le Lot 33, voir InterlisModelBuilder.
    _build_enumeration_tree) - exclu des chemins retournes, seuls SES
    enfants (les vraies valeurs de premier niveau) demarrent un chemin.
    Descend recursivement EnumNode.Node (association SubNode, role Node -
    nom de champ confirme empiriquement ET par construction explicite
    depuis le Lot 33)."""
    values: set[str] = set()

    def walk(node: MetaInstance | None, prefix: str, *, is_synthetic_root: bool) -> None:
        if node is None:
            return
        name = getattr(node, "Name", None)
        if not name:
            return
        if is_synthetic_root:
            path = ""
        else:
            path = f"{prefix}.{name}" if prefix else name
            values.add(path)
        for child in getattr(node, "Node", None) or []:
            walk(child, path, is_synthetic_root=False)

    walk(getattr(enum_type, "TopNode", None), "", is_synthetic_root=True)
    return values
