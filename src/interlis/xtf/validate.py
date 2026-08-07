"""Validateur XTF (Lot 30/31) : croise un XtfTransfer deja parse (parse.py,
couche structurelle, Lot 27) avec le schema resolu par InterlisModelBuilder
(schema.py, Lot 30) pour produire une liste d'anomalies (structure/
MANDATORY/type de base/reference).

Perimetre couvert :
- correction structurelle : classe/attribut inconnus du schema.
- MANDATORY : attribut absent alors que son Type.Mandatory est vrai.
- type de base : TEXT (presence de texte), NUMERIC (parseable + dans
  Min/Max), ENUM (valeur parmi les noms de EnumNode atteignables).
- reference (Lot 31) : TID/REF extrait (2 encodages documentes dans
  docs/xtf-transfer-encoding-notes.md + la forme "REF nu sur le noeud du
  role", confirmee Lot 31 sur `rMeasurementLocation`) et resolu contre TOUS
  les objets du transfert (TOUS les paniers, pas seulement celui de
  l'objet source - une reference peut viser un panier different du meme
  transfert). Un REF non trouve dans le transfert N'EST PAS traite comme
  une erreur ferme (RULE #5) : sans catalogue externe charge, impossible
  de distinguer une reference cassee d'une reference EXTERNE legitime
  (catalogue/table de reference transferee separement - cas reel confirme
  sur RoadTrafficCensus_V1_1, ou `MLocStatus` pointe vers
  "ch.astra.roadtrafficcensus.402", absent du fichier de donnees lui-meme
  mais legitime : RoadTrafficCensusCatalogues est un topic separe,
  `DEPENDS ON` declare mais pas necessairement inclus dans CE transfert) -
  degrade en `warning`, jamais `error`.
- roles d'association embarques comme pseudo-attributs (Lot 32, ex.
  `rMeasurementLocation` sur `Indicator`) : `schema.embedded_roles_of`
  determine, pour une classe donnee, quels roles d'association s'y
  embarquent (algorithme confirme contre le Reference Manual eCH-0031
  V2.1.0 §4.3.9) - traites ensuite EXACTEMENT comme un attribut de
  reference ordinaire (meme resolution TID/REF).
- PAS encore couvert (limites documentees, PROGRESS.md) : attributs herites
  via EXTENDS, compatibilite de classe d'une reference resolue avec sa
  BaseClass declaree, geometrie/coordonnees, roles d'association definis
  dans un modele IMPORTE (embedded_roles_of ne cherche que dans la table
  de symboles du modele racine)."""
from dataclasses import dataclass

from interlis.builder.repository import ModelRepository
from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer
from interlis.xtf.schema import ResolvedAttribute, enum_values, resolve_attribute, resolve_class, schema_members_of

# Classes de Type concretes reconnues comme "reference a un objet" (valeur
# structurelle attendue : REF vers un TID/OID, pas une valeur litterale) -
# ReferenceType (REFERENCE TO ...) et Class (restrictedStructureRef/
# restrictedClassOrAssRef resolvant DIRECTEMENT vers un Class[Kind=Class|
# Structure], voir spec/grammar/mapping/04_attributes.yml).
_REFERENCE_TYPE_KINDS = {"ReferenceType", "Class"}


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning" | "info"
    basket_bid: str
    object_tid: str | None
    qualified_class: str
    attribute: str | None
    message: str


def _extract_reference(node: RawNode) -> str | None:
    """Retrouve le TID/OID cible d'un attribut-reference, dans l'une des 3
    formes rencontrees (docs/xtf-transfer-encoding-notes.md) :
    - spec (INTERLIS 2.4 canonique) : attribut XML `ili:ref` directement sur
      le noeud - namespace non retire par le parseur structurel (RawNode
      garde `elem.attrib` tel quel), donc recherche par SUFFIXE de cle.
    - reel, attribut de reference simple (ili2fme, INTERLIS 2.3) : un
      descendant unique portant un attribut `REF` (majuscules, sans prefixe
      de namespace), imbrique dans un element intermediaire nomme par le
      role qualifie.
    - reel, role d'association embarque (Lot 31, confirme sur
      `rMeasurementLocation`) : attribut `REF` (majuscules) directement sur
      le noeud du role lui-meme, sans aucun element imbrique - deja couvert
      par le meme repli `"REF" in node.attrib` que la forme precedente, la
      recursion sur des enfants vides (`node.children == []`) ne faisant
      simplement rien avant d'atteindre ce repli."""
    for key, val in node.attrib.items():
        if key.rsplit("}", 1)[-1] == "ref":
            return val
    for child in node.children:
        found = _extract_reference(child)
        if found is not None:
            return found
    if "REF" in node.attrib:
        return node.attrib["REF"]
    return None


def _validate_scalar(resolved: ResolvedAttribute, node: RawNode, ctx: str) -> list[str]:
    """Verifications de type de base sur UN noeud attribut present -
    retourne une liste de messages d'erreur (vide si conforme, ou si le
    Type ne se prete a aucune verification connue de ce lot)."""
    problems: list[str] = []
    kind = resolved.type_kind
    if kind == "NumType":
        text = node.text
        if text is None:
            return problems
        try:
            numeric_value = float(text)
        except ValueError:
            return [f"{ctx}: valeur {text!r} non numerique (Type=NumType)"]
        min_raw = getattr(resolved.type_instance, "Min", None)
        max_raw = getattr(resolved.type_instance, "Max", None)
        try:
            if min_raw is not None and numeric_value < float(min_raw):
                problems.append(f"{ctx}: valeur {text!r} < Min {min_raw!r}")
            if max_raw is not None and numeric_value > float(max_raw):
                problems.append(f"{ctx}: valeur {text!r} > Max {max_raw!r}")
        except ValueError:
            pass  # Min/Max non numeriques (ex. domaine predefini) - hors perimetre de ce lot
    elif kind == "EnumType" and resolved.type_instance is not None:
        text = node.text
        if text is None:
            return problems
        allowed = enum_values(resolved.type_instance)
        # "OTHERS" toujours valide (RULE #4, eCH-0031 V2.1.0 §4.3.11.3 :
        # "EnumValue = (EnumElement-Name {'.' EnumElement-Name}) | 'OTHERS'.")
        if allowed and text != "OTHERS" and text not in allowed:
            problems.append(f"{ctx}: valeur {text!r} absente de l'enumeration ({sorted(allowed)!r})")
    elif kind == "TextType":
        if node.text is None and not node.children:
            problems.append(f"{ctx}: attribut TEXT present mais vide")
    return problems


def _build_tid_index(transfer: XtfTransfer) -> dict[str, XtfObject]:
    """TID -> XtfObject, sur TOUS les paniers du transfert (une reference
    peut viser un objet d'un panier different du meme fichier - confirme
    reel sur wohnungsinventar-zweitwohnungsanteil_2019-10_2056.xtf, 2
    paniers). Un TID duplique entre paniers serait deja un objet invalide
    (RULE #4, chaque TID doit etre unique dans un transfert) - premier
    trouve gagne, pas un cas rencontre dans le corpus reel a ce jour."""
    index: dict[str, XtfObject] = {}
    for basket in transfer.baskets:
        for obj in basket.objects:
            if obj.tid is not None:
                index.setdefault(obj.tid, obj)
    return index


def _validate_object(
    obj: XtfObject, basket: XtfBasket, *, symbol_table: SymbolTable, repository: ModelRepository | None,
    tid_index: dict[str, XtfObject],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    cls = resolve_class(obj.qualified_class, symbol_table=symbol_table, repository=repository)
    if cls is None:
        issues.append(ValidationIssue(
            "error", basket.bid, obj.tid, obj.qualified_class, None,
            "classe absente du schema resolu (modele non charge/introuvable via --repo, ou nom qualifie incorrect)",
        ))
        return issues

    schema_attrs = schema_members_of(cls, symbol_table)
    for attr_name, raw_nodes in obj.attributes.items():
        if attr_name not in schema_attrs:
            issues.append(ValidationIssue(
                "warning", basket.bid, obj.tid, obj.qualified_class, attr_name,
                "attribut absent du schema (classe connue) - inconnu, herite via EXTENDS, ou role "
                "d'association defini dans un modele importe (non couvert par ce lot)",
            ))
            continue
        resolved = resolve_attribute(schema_attrs[attr_name])
        ctx = f"{obj.qualified_class}[{obj.tid}].{attr_name}"
        if resolved.type_kind in _REFERENCE_TYPE_KINDS:
            # Note (Lot 30) : un Type resolu en ReferenceType/Class ne
            # signifie pas toujours un REF/ili:ref dans le XML reel -
            # `CLASS RESTRICTION(...)` sur des STRUCTUREs a UN SEUL
            # attribut (motif trouve sur RoadTrafficCensus_V1_1 :
            # `Owner`/`Canton`, restreints a sCHOwnerCode/sCHCantonCode/...)
            # semble transfere par ili2fme comme la valeur TEXTE nue de cet
            # unique attribut, PAS comme une reference - 3e forme
            # d'encodage, toujours pas interpretee (reste "info" - on ne
            # sait meme pas QUOI comparer dans ce cas).
            ref = _extract_reference(raw_nodes[0]) if raw_nodes else None
            if ref is None:
                issues.append(ValidationIssue(
                    "info", basket.bid, obj.tid, obj.qualified_class, attr_name,
                    f"{ctx}: attribut de type reference/structure sans REF reconnu (valeur texte nue, "
                    "probable structure a 1 attribut - voir docs/xtf-transfer-encoding-notes.md)",
                ))
            elif ref not in tid_index:
                # Lot 31 : REF extrait avec succes mais AUCUN objet de CE
                # transfert (tous paniers confondus) ne porte ce TID -
                # `warning`, jamais `error` (voir docstring module : sans
                # catalogue externe charge, une reference externe legitime
                # est indiscernable d'une reference cassee).
                issues.append(ValidationIssue(
                    "warning", basket.bid, obj.tid, obj.qualified_class, attr_name,
                    f"{ctx}: REF {ref!r} introuvable dans ce transfert (reference externe/catalogue "
                    "probable, ou reference cassee - indiscernable sans catalogue charge)",
                ))
            # else : REF resolu avec succes dans le transfert - rien a signaler.
            continue
        if resolved.type_kind not in ("TextType", "NumType", "EnumType"):
            # Type resolu vers autre chose que les 3 kinds geres par ce lot
            # (ex. "LineType"/"CoordType" - geometrie, ou None - Type jamais
            # resolu, cf. Municipality/AttrOrParam Lot 29) : rendu VISIBLE
            # explicitement plutot que silencieusement ignore par
            # `_validate_scalar` (qui renverrait une liste vide pour un
            # `type_kind` inconnu) - eviter qu'un total "0 probleme" donne
            # une fausse impression de conformite complete.
            issues.append(ValidationIssue(
                "info", basket.bid, obj.tid, obj.qualified_class, attr_name,
                f"{ctx}: type {resolved.type_kind!r} non verifie par ce lot (geometrie/type non resolu)",
            ))
            continue
        for node in raw_nodes:
            for problem in _validate_scalar(resolved, node, ctx):
                issues.append(ValidationIssue("error", basket.bid, obj.tid, obj.qualified_class, attr_name, problem))

    for attr_name, resolved_attr in schema_attrs.items():
        resolved = resolve_attribute(resolved_attr)
        if resolved.mandatory and attr_name not in obj.attributes:
            issues.append(ValidationIssue(
                "error", basket.bid, obj.tid, obj.qualified_class, attr_name,
                f"{obj.qualified_class}[{obj.tid}]: attribut MANDATORY {attr_name!r} absent",
            ))
    return issues


def validate_transfer(
    transfer: XtfTransfer, *, symbol_table: SymbolTable, repository: ModelRepository | None = None,
) -> list[ValidationIssue]:
    tid_index = _build_tid_index(transfer)
    issues: list[ValidationIssue] = []
    for basket in transfer.baskets:
        for obj in basket.objects:
            issues.extend(_validate_object(
                obj, basket, symbol_table=symbol_table, repository=repository, tid_index=tid_index,
            ))
    return issues
