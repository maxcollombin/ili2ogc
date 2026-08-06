"""Validateur XTF (Lot 30) : croise un XtfTransfer deja parse (parse.py,
couche structurelle, Lot 27) avec le schema resolu par InterlisModelBuilder
(schema.py, Lot 30) pour produire une liste d'anomalies (structure/
MANDATORY/type de base).

Perimetre de ce lot (borne intentionnellement, RULE #7) :
- correction structurelle : classe/attribut inconnus du schema.
- MANDATORY : attribut absent alors que son Type.Mandatory est vrai.
- type de base : TEXT (presence de texte), NUMERIC (parseable + dans
  Min/Max), ENUM (valeur parmi les noms de EnumNode atteignables).
- reference : reconnue structurellement (les 2 encodages documentes dans
  docs/xtf-transfer-encoding-notes.md), TID extrait, mais PAS encore
  resolue contre les autres objets du transfert (TID/REF cross-panier) -
  prochain lot, deja annonce dans PROGRESS.md Lot 29."""
from dataclasses import dataclass

from interlis.builder.repository import ModelRepository
from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer
from interlis.xtf.schema import ResolvedAttribute, attributes_of, enum_values, resolve_attribute, resolve_class

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


def _first_text(nodes: list[RawNode]) -> str | None:
    return nodes[0].text if nodes else None


def _extract_reference(node: RawNode) -> str | None:
    """Retrouve le TID/OID cible d'un attribut-reference, dans l'une des 2
    formes documentees (docs/xtf-transfer-encoding-notes.md) :
    - spec (INTERLIS 2.4 canonique) : attribut XML `ili:ref` directement sur
      le noeud - namespace non retire par le parseur structurel (RawNode
      garde `elem.attrib` tel quel), donc recherche par SUFFIXE de cle.
    - reel (ili2fme, INTERLIS 2.3) : un descendant unique portant un
      attribut `REF` (majuscules, sans prefixe de namespace), potentiellement
      imbrique dans un element intermediaire nomme par le role qualifie."""
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
        if allowed and text not in allowed:
            problems.append(f"{ctx}: valeur {text!r} absente de l'enumeration ({sorted(allowed)!r})")
    elif kind == "TextType":
        if node.text is None and not node.children:
            problems.append(f"{ctx}: attribut TEXT present mais vide")
    return problems


def _validate_object(
    obj: XtfObject, basket: XtfBasket, *, symbol_table: SymbolTable, repository: ModelRepository | None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    cls = resolve_class(obj.qualified_class, symbol_table=symbol_table, repository=repository)
    if cls is None:
        issues.append(ValidationIssue(
            "error", basket.bid, obj.tid, obj.qualified_class, None,
            "classe absente du schema resolu (modele non charge/introuvable via --repo, ou nom qualifie incorrect)",
        ))
        return issues

    schema_attrs = attributes_of(cls)
    for attr_name, raw_nodes in obj.attributes.items():
        if attr_name not in schema_attrs:
            issues.append(ValidationIssue(
                "warning", basket.bid, obj.tid, obj.qualified_class, attr_name,
                "attribut absent du schema (classe connue) - inconnu ou herite (non couvert par ce lot)",
            ))
            continue
        resolved = resolve_attribute(schema_attrs[attr_name])
        ctx = f"{obj.qualified_class}[{obj.tid}].{attr_name}"
        if resolved.type_kind in _REFERENCE_TYPE_KINDS:
            # Resolution TID/REF cross-panier hors perimetre de ce lot (voir
            # docstring module). Note (Lot 30) : un Type resolu en
            # ReferenceType/Class ne signifie pas toujours un REF/ili:ref
            # dans le XML reel - `CLASS RESTRICTION(...)` sur des STRUCTUREs
            # a UN SEUL attribut (motif trouve sur RoadTrafficCensus_V1_1 :
            # `Owner`/`Canton`, restreints a sCHOwnerCode/sCHCantonCode/...)
            # semble transfere par ili2fme comme la valeur TEXTE nue de cet
            # unique attribut, PAS comme une reference - 3e forme
            # d'encodage, pas encore documentee dans
            # docs/xtf-transfer-encoding-notes.md avant ce lot. Purement
            # informatif ici (jamais "error") tant que cette forme n'est pas
            # interpretee.
            ref = _extract_reference(raw_nodes[0]) if raw_nodes else None
            shape = f"REF={ref!r} trouve" if ref is not None else "valeur texte nue (probable structure a 1 attribut)"
            issues.append(ValidationIssue(
                "info", basket.bid, obj.tid, obj.qualified_class, attr_name,
                f"{ctx}: attribut de type reference/structure - resolution non implementee ({shape}, voir "
                "docs/xtf-transfer-encoding-notes.md)",
            ))
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
    issues: list[ValidationIssue] = []
    for basket in transfer.baskets:
        for obj in basket.objects:
            issues.extend(_validate_object(obj, basket, symbol_table=symbol_table, repository=repository))
    return issues
