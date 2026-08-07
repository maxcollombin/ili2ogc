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
  degrade en `warning`, jamais `error`. Lot 34 : le message distingue
  desormais ce cas (`ReferenceType.External == True`, deja construit par
  referenceAttr() depuis la clause `(EXTERNAL)`) d'un REF non resolu sur
  une reference NON declaree EXTERNAL - qui devrait normalement resoudre
  dans le MEME panier (eCH-0031 V2.1.0 §3.6.3) et est donc un signal plus
  probable d'une donnee reellement incorrecte - sans changer la severite
  (RULE #5, aucun catalogue n'est charge pour verifier positivement).
- roles d'association embarques comme pseudo-attributs (Lot 32, ex.
  `rMeasurementLocation` sur `Indicator`) : `schema.embedded_roles_of`
  determine, pour une classe donnee, quels roles d'association s'y
  embarquent (algorithme confirme contre le Reference Manual eCH-0031
  V2.1.0 §4.3.9) - traites ensuite EXACTEMENT comme un attribut de
  reference ordinaire (meme resolution TID/REF).
- compatibilite de classe d'une reference RESOLUE (Lot 40) : quand un REF
  extrait resout bien vers un objet du transfert, verifie que la classe
  REELLE de cet objet est `declared` elle-meme OU une SOUS-CLASSE (chaine
  `Inheritance`/`Super`, Lot 38) de la classe DECLAREE par la reference/le
  role (`schema.reference_target_class`/`is_class_compatible`) -
  polymorphisme INTERLIS standard. Information COMPLETE des lors qu'un REF
  resout (contrairement au cas "REF introuvable", intrinsequement ambigu,
  voir ci-dessus) : severite `error`, meme politique que MANDATORY/NUMERIC/
  ENUM. Silencieux (pas de check) si la classe declaree ou la classe reelle
  ne peut pas etre resolue avec certitude (cross-modele non charge via
  --repo, role d'association sans BaseClass confirme, etc.) - RULE #5,
  jamais de faux positif sur une incertitude.
- 3e forme d'encodage XTF (Lot 41, `_validate_restriction_text`) : un
  `CLASS RESTRICTION(A; B; C)` (voir
  docs/xtf-transfer-encoding-notes.md "Third form found") ou CHAQUE
  candidat est une STRUCTURE a UN SEUL attribut PROPRE se transfere comme
  la valeur TEXTE NUE de cet attribut, sans REF - desormais interprete en
  validant cette valeur contre CHAQUE candidat verifiable
  (`schema.restriction_candidates`/`single_own_attribute`) : aucune issue
  si au moins un candidat accepte la valeur, `warning` (pas `error`, choix
  du bon candidat heuristique) si tous les candidats VERIFIABLES la
  rejettent, `info` (statut indetermine, RULE #5) si aucun candidat n'est
  verifiable (motif absent, ou type interne non resolu - ex. domaine
  externe non charge via `--repo`).
- geometrie/coordonnees (Lot 42, `_validate_coord_attribute`/
  `_validate_line_attribute`) : COORD/MULTICOORD (CoordType) et
  POLYLINE/SURFACE/AREA/MULTI* (LineType, segments COORD/ARC) - structure
  (balise attendue selon Kind/Multi) ET plage Min/Max par axe (association
  AxisSpec.Axis, ordonnee) quand connue - RULE #4, eCH-0031 V2.1.0
  §4.3.11.13/.14/.15, voir le detail complet dans la note precedant ces
  fonctions. Necessitait un correctif prealable du ModelBuilder (Lot 42,
  `LineType.CoordType` jamais attache depuis l'origine du binding
  `controlPoints`/VERTEX - voir InterlisModelBuilder._build_control_points_ref) :
  sans lui, aucune plage d'axe n'aurait ete disponible pour la MAJORITE des
  fichiers reels de l'inventaire (POLYLINE/SURFACE dominent sur COORD nu).
  `error` (information complete des que le Type resout, meme politique que
  MANDATORY/NUMERIC/ENUM/Lot 40) - un segment LINE FORM personnalise
  (structure arbitraire, ni COORD ni ARC) reste NON interprete (aucun
  candidat reel dans l'inventaire XTF, ignore silencieusement plutot qu'une
  fausse alerte).
- PAS encore couvert (limites documentees, PROGRESS.md) : attributs herites
  via EXTENDS depuis un modele IMPORTE non charge (chaine Super tronquee),
  roles d'association definis dans un modele IMPORTE (embedded_roles_of ne
  cherche que dans la table de symboles du modele racine), segments LINE
  FORM personnalises (structure arbitraire, WITH (...) autre que
  STRAIGHTS/ARCS), MULTICOORD/MULTIPOLYLINE/MULTISURFACE/MULTIAREA/AREA/ARC
  extrapoles du manuel (aucun exemple reel dans l'inventaire XTF actuel,
  voir note precedant _validate_coord_attribute)."""
from dataclasses import dataclass

from interlis.builder.repository import ModelRepository
from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer
from interlis.xtf.schema import (
    ResolvedAttribute, coord_axes, enum_values, is_class_compatible, line_coord_type, reference_external_status,
    reference_target_class, resolve_attribute, resolve_class, restriction_candidates, schema_members_of,
    single_own_attribute,
)

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


# --- Geometrie/coordonnees (Lot 42) -----------------------------------------
#
# RULE #4, citation directe eCH-0031 V2.1.0 :
# §4.3.11.13 "Codierung von Koordinaten" : "CoordValue = <geom:coord>
#   <geom:c1>NumericConst</geom:c1> <geom:c2>NumericConst</geom:c2>
#   [<geom:c3>NumericConst</geom:c3>] </geom:coord>." / "MultiCoordValue =
#   <geom:multicoord> (* CoordValue *) </geom:multicoord>."
# §4.3.11.14 "Codierung von Linienzuegen" : "PolylineValue = <geom:polyline>
#   SegmentSequence </geom:polyline>." ou SegmentSequence = StartSegment
#   (CoordValue) (* StraightSegment (CoordValue) | ArcSegment | LineFormSegment *).
#   ArcSegment = <geom:arc> <geom:c1>..<geom:c2>..[<geom:c3>..] <geom:a1>..
#   <geom:a2>.. [<geom:r>..] </geom:arc>. "MultiPolylineValue =
#   <geom:multipolyline> (* PolylineValue *) </geom:multipolyline>."
# §4.3.11.15 "Codierung von Einzelflaechen..." : "SurfaceValue = <geom:surface>
#   Boundaries </geom:surface>." Boundaries = OuterBoundary {InnerBoundary}.
#   "MultiSurfaceValue = <geom:multisurface> (* SurfaceValue *) </geom:multisurface>."
#   Meme structure pour AREA (le manuel dit explicitement "SURFACE und AREA
#   werden wie folgt codiert" - un seul jeu de regles pour les deux Kind).
#
# Corpus reel (RULE #1, xtf_corpus/*.xtf, 2026-08-07) CONFIRME sans le
# namespace "geom:" (comme ili:ref/REF deja documente pour les references,
# docs/xtf-transfer-encoding-notes.md) et en MAJUSCULES, mirroir exact du
# mot-cle grammatical (COORD/POLYLINE/SURFACE, comme deja confirme pour
# REFERENCE/REF) : `<AttrName><COORD><C1>x</C1><C2>y</C2>[<C3>z</C3>]</COORD>
# </AttrName>` (RoadTrafficAccidentLocations.xtf, 3D) ; `<AttrName><SURFACE>
# <BOUNDARY><POLYLINE><COORD>...</COORD>...</POLYLINE></BOUNDARY>[<BOUNDARY>
# ...]</SURFACE></AttrName>` (alpenkonvention_2056.xtf, exterieur PUIS
# interieur(s), sans distinction de balise - "OuterBoundary"/"InnerBoundary"
# de la grammaire abstraite partagent la MEME balise concrete BOUNDARY,
# seul l'ORDRE - premier = exterieur - porte l'information, conforme au
# manuel : "Der erste Rand einer Flaeche (OuterBoundary) ist der aeussere
# Rand"). AUCUN exemple reel de MULTICOORD/MULTIPOLYLINE/MULTISURFACE/
# MULTIAREA/AREA/ARC/LINE FORM personnalise dans les 12 fichiers de
# l'inventaire (grep verifie, RULE #1) - ces formes restent implementees par
# EXTRAPOLATION directe du manuel + de la convention MAJUSCULES=mot-cle deja
# confirmee deux fois (COORD/POLYLINE), documentee comme telle plutot que
# "confirmee reel", RULE #5. Un segment LINE FORM personnalise (structure
# arbitraire, ni COORD ni ARC) n'est PAS interprete par ce lot (aucun
# candidat reel dans l'inventaire) - silencieusement ignore (pas de fausse
# alerte), limite documentee dans le docstring module.


def _find_child(node: RawNode, tag: str) -> RawNode | None:
    return next((c for c in node.children if c.tag == tag), None)


def _numeric_problems(text: str | None, min_raw, max_raw, ctx: str) -> list[str]:
    """Meme logique que la branche NumType de `_validate_scalar` (Min/Max
    textuels, tolerance sur une borne non numerique - ex. domaine predefini),
    factorisee ici pour etre reutilisee sur les composantes de coordonnees
    (C1/C2/C3/A1/A2/R) - `min_raw`/`max_raw` a `None` pour une composante
    dont l'axe/la borne n'est pas connue (verification de PARSEABILITE
    seule, jamais de plage, RULE #5) - voir schema.coord_axes."""
    if text is None:
        return [f"{ctx}: composante absente"]
    try:
        value = float(text)
    except ValueError:
        return [f"{ctx}: valeur {text!r} non numerique"]
    problems: list[str] = []
    try:
        if min_raw is not None and value < float(min_raw):
            problems.append(f"{ctx}: valeur {text!r} < Min {min_raw!r}")
        if max_raw is not None and value > float(max_raw):
            problems.append(f"{ctx}: valeur {text!r} > Max {max_raw!r}")
    except ValueError:
        pass
    return problems


def _axis_components(node: RawNode, prefix: str) -> list[RawNode]:
    """Composantes `{prefix}1`, `{prefix}2`, ... presentes sur `node`, dans
    l'ordre, jusqu'au premier trou (ex. prefix="C" -> C1/C2/[C3] d'un COORD/
    ARC ; prefix="A" -> A1/A2 du point intermediaire d'un ARC)."""
    out: list[RawNode] = []
    i = 1
    while True:
        child = _find_child(node, f"{prefix}{i}")
        if child is None:
            break
        out.append(child)
        i += 1
    return out


def _validate_axis_values(components: list[RawNode], axes: list[MetaInstance], ctx: str, *, label: str) -> list[str]:
    """Verifie chaque composante contre l'axe CORRESPONDANT (par position,
    `AxisSpec.Axis` est ORDONNE - confirme ilismeta16-associations.yml/RULE
    #4) - plage Min/Max si `axes` est connu (schema.coord_axes non vide),
    PARSEABILITE numerique seule sinon. Signale un ecart de CARDINALITE
    (nombre de composantes different du nombre d'axes declares) UNIQUEMENT
    quand `axes` est connu - RULE #5, un desaccord de compte n'est un signal
    fiable que si le nombre attendu l'est aussi."""
    problems: list[str] = []
    if axes and len(components) != len(axes):
        problems.append(f"{ctx}: {len(components)} composante(s) {label}, {len(axes)} attendue(s) (CoordType.Axis)")
    for i, comp in enumerate(components):
        axis = axes[i] if i < len(axes) else None
        problems.extend(_numeric_problems(
            comp.text, getattr(axis, "Min", None), getattr(axis, "Max", None), f"{ctx}.{label}{i + 1}",
        ))
    return problems


def _validate_coord_node(node: RawNode, axes: list[MetaInstance], ctx: str) -> list[str]:
    if node.tag != "COORD":
        return [f"{ctx}: geometrie COORD attendue, balise {node.tag!r} trouvee"]
    return _validate_axis_values(_axis_components(node, "C"), axes, ctx, label="C")


def _validate_arc_node(node: RawNode, axes: list[MetaInstance], ctx: str) -> list[str]:
    """ArcSegment (eCH-0031 V2.1.0 §4.3.11.14) - point intermediaire A1/A2
    verifie contre les 2 PREMIERS axes (memes composantes X/Y qu'un COORD,
    jamais de 3e composante intermediaire pour un arc, confirme par la
    grammaire : geom:a1/geom:a2 seulement, pas de geom:a3). R (rayon,
    optionnel) : PARSEABILITE numerique seule, jamais de plage - aucun axe
    ne le couvre (c'est une longueur derivee, pas une coordonnee)."""
    if node.tag != "ARC":
        return [f"{ctx}: geometrie ARC attendue, balise {node.tag!r} trouvee"]
    problems = _validate_axis_values(_axis_components(node, "C"), axes, ctx, label="C")
    mid = _axis_components(node, "A")
    if len(mid) < 2:
        problems.append(f"{ctx}: point intermediaire A1/A2 absent (ARC)")
    else:
        problems.extend(_validate_axis_values(mid, axes[:2], ctx, label="A"))
    r_node = _find_child(node, "R")
    if r_node is not None:
        problems.extend(_numeric_problems(r_node.text, None, None, f"{ctx}.R"))
    return problems


def _validate_polyline_node(node: RawNode, axes: list[MetaInstance], ctx: str) -> list[str]:
    if node.tag != "POLYLINE":
        return [f"{ctx}: geometrie POLYLINE attendue, balise {node.tag!r} trouvee"]
    if not node.children:
        return [f"{ctx}: POLYLINE vide (aucun segment)"]
    problems: list[str] = []
    for i, seg in enumerate(node.children):
        seg_ctx = f"{ctx}[{i}]"
        if seg.tag == "COORD":
            problems.extend(_validate_coord_node(seg, axes, seg_ctx))
        elif seg.tag == "ARC":
            problems.extend(_validate_arc_node(seg, axes, seg_ctx))
        # sinon : segment LINE FORM personnalise (structure arbitraire, WITH
        # (...) autre que STRAIGHTS/ARCS) - non interprete par ce lot (aucun
        # candidat reel dans l'inventaire XTF, voir note module), ignore
        # silencieusement plutot qu'une fausse alerte structurelle.
    return problems


def _validate_boundary_node(node: RawNode, axes: list[MetaInstance], ctx: str) -> list[str]:
    if node.tag != "BOUNDARY":
        return [f"{ctx}: geometrie BOUNDARY attendue, balise {node.tag!r} trouvee"]
    polyline = _find_child(node, "POLYLINE")
    if polyline is None:
        return [f"{ctx}: BOUNDARY sans POLYLINE"]
    return _validate_polyline_node(polyline, axes, f"{ctx}/POLYLINE")


def _validate_surface_node(node: RawNode, expected_tag: str, axes: list[MetaInstance], ctx: str) -> list[str]:
    """`expected_tag` = "SURFACE" ou "AREA" (meme structure Boundaries pour
    les deux Kind, RULE #4 - voir note module)."""
    if node.tag != expected_tag:
        return [f"{ctx}: geometrie {expected_tag} attendue, balise {node.tag!r} trouvee"]
    boundaries = [c for c in node.children if c.tag == "BOUNDARY"]
    if not boundaries:
        return [f"{ctx}: {expected_tag} sans aucun BOUNDARY"]
    problems: list[str] = []
    for i, boundary in enumerate(boundaries):
        problems.extend(_validate_boundary_node(boundary, axes, f"{ctx}[{i}]"))
    return problems


def _validate_coord_attribute(resolved: ResolvedAttribute, node: RawNode, ctx: str) -> list[str]:
    """Attribut de Type resolu en CoordType (COORD/MULTICOORD, eCH-0031
    V2.1.0 §4.3.11.13) - `node` est le noeud de l'ATTRIBUT lui-meme (ex.
    `<AccidentLocation>`), son 1er enfant doit etre COORD (ou MULTICOORD si
    `CoordType.Multi`)."""
    coord_type = resolved.type_instance
    multi = bool(getattr(coord_type, "Multi", False))
    axes = coord_axes(coord_type)
    expected_tag = "MULTICOORD" if multi else "COORD"
    child = node.children[0] if node.children else None
    if child is None or child.tag != expected_tag:
        found = child.tag if child is not None else "(vide)"
        return [f"{ctx}: geometrie {expected_tag} attendue (Type=CoordType, Multi={multi}), {found!r} trouvee"]
    if not multi:
        return _validate_coord_node(child, axes, ctx)
    coords = [c for c in child.children if c.tag == "COORD"]
    if not coords:
        return [f"{ctx}: MULTICOORD sans aucun COORD interne"]
    problems: list[str] = []
    for i, c in enumerate(coords):
        problems.extend(_validate_coord_node(c, axes, f"{ctx}[{i}]"))
    return problems


_LINE_KIND_TAGS = {"Polyline": "POLYLINE", "DirectedPolyline": "POLYLINE", "Surface": "SURFACE", "Area": "AREA"}
_LINE_KIND_MULTI_TAGS = {
    "Polyline": "MULTIPOLYLINE", "DirectedPolyline": "MULTIPOLYLINE", "Surface": "MULTISURFACE", "Area": "MULTIAREA",
}


def _validate_line_attribute(resolved: ResolvedAttribute, node: RawNode, ctx: str) -> list[str]:
    """Attribut de Type resolu en LineType (POLYLINE/SURFACE/AREA/MULTI*,
    eCH-0031 V2.1.0 §4.3.11.14/.15). `axes` provient de `schema.
    line_coord_type` (association LineCoord, Lot 42 - VIDE, verification
    numerique seule sans plage, si la clause VERTEX est absente/non resolue,
    ex. `DirectedLine EXTENDS Line = DIRECTED POLYLINE;` sans VERTEX propre -
    limite documentee, RULE #5)."""
    line_type = resolved.type_instance
    kind = getattr(line_type, "Kind", None)
    multi = bool(getattr(line_type, "Multi", False))
    axes = coord_axes(line_coord_type(line_type))
    single_tag = _LINE_KIND_TAGS.get(kind)
    if single_tag is None:
        return []  # Kind non resolu/inattendu - rien de fiable a verifier (RULE #5)
    expected_tag = _LINE_KIND_MULTI_TAGS[kind] if multi else single_tag
    child = node.children[0] if node.children else None
    if child is None or child.tag != expected_tag:
        found = child.tag if child is not None else "(vide)"
        return [f"{ctx}: geometrie {expected_tag} attendue (LineType Kind={kind!r}, Multi={multi}), {found!r} trouvee"]
    validator = _validate_polyline_node if single_tag == "POLYLINE" else (
        lambda n, ax, c: _validate_surface_node(n, single_tag, ax, c)
    )
    if not multi:
        return validator(child, axes, ctx)
    parts = [c for c in child.children if c.tag == single_tag]
    if not parts:
        return [f"{ctx}: {expected_tag} sans aucun {single_tag} interne"]
    problems: list[str] = []
    for i, part in enumerate(parts):
        problems.extend(validator(part, axes, f"{ctx}[{i}]"))
    return problems


_GENERIC_RESTRICTION_INFO = (
    "attribut de type reference/structure sans REF reconnu (valeur texte nue, "
    "probable structure a 1 attribut - voir docs/xtf-transfer-encoding-notes.md)"
)


def _validate_restriction_text(
    resolved: ResolvedAttribute, node: RawNode | None, basket: XtfBasket, obj: XtfObject, attr_name: str, ctx: str,
) -> "ValidationIssue | None":
    """Interprete la 3e forme d'encodage XTF (Lot 41, voir
    docs/xtf-transfer-encoding-notes.md "Third form found") : un attribut
    dont le Type resout en `ReferenceType` avec PLUSIEURS candidats
    `BaseClass` (`CLASS RESTRICTION(A; B; C)`, `restriction_candidates`),
    chaque candidat etant lui-meme une STRUCTURE a UN SEUL attribut PROPRE
    (`single_own_attribute`) - ili2fme semble transferer ceci comme la
    valeur TEXTE NUE de cet unique attribut, sans wrapper `<Reference>`,
    ni meme de wrapper structure. Valide `node.text` contre CHAQUE candidat
    dont le type interne est verifiable (`_validate_scalar` sur
    TextType/NumType/EnumType) :
    - au moins un candidat accepte la valeur sans probleme -> aucune issue
      (comme un REF resolu, Lot 31/40).
    - aucun candidat verifiable (BaseClass absent, pas de motif 1-attribut,
      ou motif present mais le/les type(s) internes ne resolvent pas -
      ex. domaine externe non charge via `--repo`, CHAdminCodes_V1 sur ce
      corpus, voir PROGRESS.md Lot 39) -> `info`, statut reellement
      indetermine, RULE #5 - IDENTIQUE au message d'avant ce lot si aucun
      candidat structurel n'a meme ete trouve.
    - au moins un candidat verifiable existe mais AUCUN n'accepte la
      valeur -> `warning` (pas `error` : le choix du "bon" candidat parmi
      plusieurs reste une heuristique structurelle, contrairement a la
      resolution TID/REF exacte du Lot 40 - RULE #5, ne jamais surclasser
      une interpretation heuristique en certitude)."""
    if node is None:
        return ValidationIssue("info", basket.bid, obj.tid, obj.qualified_class, attr_name, f"{ctx}: {_GENERIC_RESTRICTION_INFO}")
    single_attr_candidates = [
        (candidate, resolve_attribute(attr))
        for candidate in restriction_candidates(resolved)
        if (attr := single_own_attribute(candidate)) is not None
    ]
    if not single_attr_candidates:
        return ValidationIssue("info", basket.bid, obj.tid, obj.qualified_class, attr_name, f"{ctx}: {_GENERIC_RESTRICTION_INFO}")
    checkable = [(c, ir) for c, ir in single_attr_candidates if ir.type_kind in ("TextType", "NumType", "EnumType")]
    if any(not _validate_scalar(inner, node, ctx) for _, inner in checkable):
        return None
    if len(checkable) < len(single_attr_candidates):
        return ValidationIssue(
            "info", basket.bid, obj.tid, obj.qualified_class, attr_name,
            f"{ctx}: valeur texte nue {node.text!r} (CLASS RESTRICTION, 3e forme d'encodage) ne correspond "
            f"a aucun des {len(checkable)}/{len(single_attr_candidates)} candidat(s) verifiable(s) - le "
            "reste n'est pas resolu (modele externe non charge via --repo)",
        )
    names = [getattr(c, "Name", None) for c, _ in checkable]
    return ValidationIssue(
        "warning", basket.bid, obj.tid, obj.qualified_class, attr_name,
        f"{ctx}: valeur texte nue {node.text!r} (CLASS RESTRICTION, 3e forme d'encodage) ne correspond a "
        f"aucun des {len(checkable)} candidat(s) declares ({names!r})",
    )


def _build_tid_index(transfer: XtfTransfer, catalogs: list[XtfTransfer] | None = None) -> dict[str, XtfObject]:
    """TID -> XtfObject, sur TOUS les paniers du transfert (une reference
    peut viser un objet d'un panier different du meme fichier - confirme
    reel sur wohnungsinventar-zweitwohnungsanteil_2019-10_2056.xtf, 2
    paniers), PUIS sur tous les paniers de chaque transfert-catalogue
    fourni (Lot 35 - `--catalog`, voir cli.py) : un objet-catalogue EXTERNAL
    (ex. MLocStatus, RULE #4 eCH-0031 V2.1.0 §3.6.3) vit typiquement dans un
    panier/fichier SEPARE du transfert de donnees metier - ce fichier n'est
    PAS auto-decouvert (aucune source publique identifiee pour ce corpus,
    voir docs/model-resolution-strategy.md et PROGRESS.md Lot 35), mais si
    l'operateur en fournit un, ses objets deviennent resolubles au meme
    titre que ceux du transfert principal. Un TID duplique entre paniers
    (transfert principal OU catalogue) serait deja un objet invalide
    (RULE #4, chaque TID doit etre unique dans un transfert) - premier
    trouve gagne, transfert principal prioritaire sur les catalogues."""
    index: dict[str, XtfObject] = {}
    for basket in transfer.baskets:
        for obj in basket.objects:
            if obj.tid is not None:
                index.setdefault(obj.tid, obj)
    for catalog in catalogs or []:
        for basket in catalog.baskets:
            for obj in basket.objects:
                if obj.tid is not None:
                    index.setdefault(obj.tid, obj)
    return index


def _resolved_schema_of(
    cls: MetaInstance, symbol_table: SymbolTable, cache: dict[int, dict[str, ResolvedAttribute]],
) -> dict[str, ResolvedAttribute]:
    """`schema_members_of`+`resolve_attribute`, memoises par CLASSE (`id(cls)`)
    pour la duree d'un `validate_transfer` (Lot 36 - optimisation, aucun
    changement de comportement). `embedded_roles_of` (appele par
    `schema_members_of`) reparcourt TOUT le symbol_table a chaque appel -
    profile reel (cProfile, ch.astra.nationalstrassenachsen.xtf, 34533
    objets mais SEULEMENT 4 classes distinctes) : ~82% du temps de
    `validate_transfer` etait passe a recalculer le MEME resultat pour
    chaque objet d'une classe deja vue, alors que le schema d'une classe ne
    change jamais pendant une validation. Cle par identite d'objet Python
    (`id`), pas par nom qualifie : deux `MetaInstance` differentes ne
    doivent jamais partager une entree, meme homonymes (cas deja gere
    ailleurs par kind_hint - RULE #1, ne pas re-introduire une ambiguite
    par un raccourci de cache)."""
    key = id(cls)
    cached = cache.get(key)
    if cached is None:
        cached = {name: resolve_attribute(attr) for name, attr in schema_members_of(cls, symbol_table).items()}
        cache[key] = cached
    return cached


def _validate_object(
    obj: XtfObject, basket: XtfBasket, *, symbol_table: SymbolTable, repository: ModelRepository | None,
    tid_index: dict[str, XtfObject], schema_cache: dict[int, dict[str, ResolvedAttribute]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    cls = resolve_class(obj.qualified_class, symbol_table=symbol_table, repository=repository)
    if cls is None:
        issues.append(ValidationIssue(
            "error", basket.bid, obj.tid, obj.qualified_class, None,
            "classe absente du schema resolu (modele non charge/introuvable via --repo, ou nom qualifie incorrect)",
        ))
        return issues

    schema_attrs = _resolved_schema_of(cls, symbol_table, schema_cache)
    for attr_name, raw_nodes in obj.attributes.items():
        if attr_name not in schema_attrs:
            issues.append(ValidationIssue(
                "warning", basket.bid, obj.tid, obj.qualified_class, attr_name,
                "attribut absent du schema (classe connue) - inconnu, herite via EXTENDS, ou role "
                "d'association defini dans un modele importe (non couvert par ce lot)",
            ))
            continue
        resolved = schema_attrs[attr_name]
        ctx = f"{obj.qualified_class}[{obj.tid}].{attr_name}"
        if resolved.type_kind in _REFERENCE_TYPE_KINDS:
            # Note (Lot 30) : un Type resolu en ReferenceType/Class ne
            # signifie pas toujours un REF/ili:ref dans le XML reel -
            # `CLASS RESTRICTION(...)` sur des STRUCTUREs a UN SEUL
            # attribut (motif trouve sur RoadTrafficCensus_V1_1 :
            # `Owner`/`Canton`, restreints a sCHOwnerCode/sCHCantonCode/...)
            # semble transfere par ili2fme comme la valeur TEXTE nue de cet
            # unique attribut, PAS comme une reference - 3e forme
            # d'encodage, INTERPRETEE depuis le Lot 41 (voir
            # `_validate_restriction_text` ci-dessous) quand possible,
            # sinon repli sur le meme message "info" qu'avant.
            ref = _extract_reference(raw_nodes[0]) if raw_nodes else None
            if ref is None:
                restriction_issue = _validate_restriction_text(
                    resolved, raw_nodes[0] if raw_nodes else None, basket, obj, attr_name, ctx,
                )
                if restriction_issue is not None:
                    issues.append(restriction_issue)
            elif ref not in tid_index:
                # Lot 31 : REF extrait avec succes mais AUCUN objet de CE
                # transfert (tous paniers confondus) ne porte ce TID -
                # `warning`, jamais `error` (voir docstring module : sans
                # catalogue externe charge, une reference externe legitime
                # est indiscernable d'une reference cassee).
                #
                # Lot 34 : le schema DISTINGUE deja les deux cas via
                # ReferenceType.External (own BOOLEAN, deja construit par
                # referenceAttr() - spec/grammar/mapping/04_attributes.yml -
                # depuis la clause optionnelle "(EXTERNAL)" sur `REFERENCE TO
                # (EXTERNAL) X`). RULE #4, citation directe eCH-0031 V2.1.0
                # §3.6.3 : "Soll die Referenz auf ein Objekt eines anderen
                # Behaelters ... verweisen duerfen, muss die Eigenschaft
                # EXTERNAL angegeben werden" - SANS cette clause, la cible
                # DOIT normalement resoudre dans le MEME panier ; un REF non
                # resolu sur une reference NON-EXTERNAL est donc un signal
                # plus probable d'une donnee reellement incorrecte qu'une
                # reference EXTERNAL non resolue (catalogue legitime,
                # confirme reel sur MLocStatusRef.Reference.Type.External =
                # True). Severite volontairement INCHANGEE (toujours
                # `warning`, jamais `error`) - ce lot ne fait QUE distinguer
                # les deux cas dans le message, il ne pretend pas verifier
                # positivement la premiere hypothese (aucun catalogue
                # n'est charge, voir docs/model-resolution-strategy.md).
                # `reference_external_status` gere aussi le motif
                # STRUCTURE-enveloppe standard (CatalogueObjects_V1.
                # Catalogues.MandatoryCatalogueReference, ex. MLocStatusRef)
                # - confirme empiriquement etre la forme REELLE la plus
                # frequente dans ce corpus, pas seulement le cas
                # ReferenceType direct. Tri-state (RULE #5) : `None` (statut
                # reellement indetermine - ex. role d'association embarque,
                # ou structure enveloppant un contenu non-reference comme
                # une geometrie, confirme reel sur Axis_V1_1.
                # AxisSegmentGeometry) garde le libelle neutre d'origine,
                # PLUTOT que d'affirmer a tort "NON declaree" par defaut.
                status = reference_external_status(resolved)
                if status is True:
                    detail = (
                        "reference declaree (EXTERNAL) : objet cible attendu dans un panier/catalogue "
                        "externe (DEPENDS ON), non resolu faute de catalogue charge - situation normale"
                    )
                elif status is False:
                    detail = (
                        "reference NON declaree (EXTERNAL) : devrait normalement resoudre dans ce meme "
                        "panier (eCH-0031 V2.1.0 3.6.3) - signal plus probable d'une donnee incorrecte"
                    )
                else:
                    detail = (
                        "reference externe/catalogue probable, ou reference cassee - statut EXTERNAL "
                        "indetermine par ce validateur (role d'association, ou structure non reconnue)"
                    )
                issues.append(ValidationIssue(
                    "warning", basket.bid, obj.tid, obj.qualified_class, attr_name,
                    f"{ctx}: REF {ref!r} introuvable dans ce transfert ({detail})",
                ))
            else:
                # Lot 40 : REF resolu avec succes - verifie desormais la
                # compatibilite de classe de l'objet cible reel avec la
                # classe DECLAREE par la reference/le role. Silencieux (pas
                # d'issue) si l'une des deux classes ne peut pas etre
                # etablie avec certitude, ou si compatible - RULE #5, jamais
                # de faux positif sur une incertitude cross-modele.
                declared = reference_target_class(resolved)
                if declared is not None:
                    target_obj = tid_index[ref]
                    actual_cls = resolve_class(target_obj.qualified_class, symbol_table=symbol_table, repository=repository)
                    if actual_cls is not None and not is_class_compatible(actual_cls, declared):
                        issues.append(ValidationIssue(
                            "error", basket.bid, obj.tid, obj.qualified_class, attr_name,
                            f"{ctx}: REF {ref!r} resolu vers {target_obj.qualified_class!r}, incompatible "
                            f"avec la classe declaree {getattr(declared, 'Name', '?')!r} "
                            "(ni identique, ni sous-classe via EXTENDS)",
                        ))
            continue
        if resolved.type_kind == "CoordType":
            # Geometrie/coordonnees (Lot 42) - voir note module pour le
            # detail des formes reelles (COORD/MULTICOORD) et la citation
            # eCH-0031 V2.1.0 §4.3.11.13. `error` : information COMPLETE des
            # que le Type resout en CoordType (Multi/Axis toujours connus
            # directement sur l'instance elle-meme, jamais une incertitude
            # cross-modele) - meme politique que MANDATORY/NUMERIC/ENUM/Lot 40.
            for node in raw_nodes:
                for problem in _validate_coord_attribute(resolved, node, ctx):
                    issues.append(ValidationIssue("error", basket.bid, obj.tid, obj.qualified_class, attr_name, problem))
            continue
        if resolved.type_kind == "LineType":
            # Geometrie/coordonnees (Lot 42) - POLYLINE/SURFACE/AREA/MULTI*,
            # eCH-0031 V2.1.0 §4.3.11.14/.15. `error` pour la meme raison que
            # CoordType ci-dessus (structure/Kind/Multi toujours connus) -
            # la seule incertitude possible (plage Min/Max par axe si VERTEX
            # non resolu, schema.line_coord_type) degrade deja gracieusement
            # vers une verification de parseabilite seule (RULE #5), jamais
            # un skip complet de la structure.
            for node in raw_nodes:
                for problem in _validate_line_attribute(resolved, node, ctx):
                    issues.append(ValidationIssue("error", basket.bid, obj.tid, obj.qualified_class, attr_name, problem))
            continue
        if resolved.type_kind not in ("TextType", "NumType", "EnumType"):
            # Type resolu vers autre chose que les kinds geres par ce lot
            # (ex. "FormattedType"/"BooleanType"/"BlackboxType"/"AnyOIDType"
            # - jamais interpretes, ou None - Type jamais resolu, cf.
            # Municipality/AttrOrParam Lot 29) : rendu VISIBLE explicitement
            # plutot que silencieusement ignore par `_validate_scalar` (qui
            # renverrait une liste vide pour un `type_kind` inconnu) - eviter
            # qu'un total "0 probleme" donne une fausse impression de
            # conformite complete.
            issues.append(ValidationIssue(
                "info", basket.bid, obj.tid, obj.qualified_class, attr_name,
                f"{ctx}: type {resolved.type_kind!r} non verifie par ce lot (type non couvert/non resolu)",
            ))
            continue
        for node in raw_nodes:
            for problem in _validate_scalar(resolved, node, ctx):
                issues.append(ValidationIssue("error", basket.bid, obj.tid, obj.qualified_class, attr_name, problem))

    for attr_name, resolved in schema_attrs.items():
        if resolved.mandatory and attr_name not in obj.attributes:
            issues.append(ValidationIssue(
                "error", basket.bid, obj.tid, obj.qualified_class, attr_name,
                f"{obj.qualified_class}[{obj.tid}]: attribut MANDATORY {attr_name!r} absent",
            ))
    return issues


def validate_transfer(
    transfer: XtfTransfer, *, symbol_table: SymbolTable, repository: ModelRepository | None = None,
    catalogs: list[XtfTransfer] | None = None,
) -> list[ValidationIssue]:
    """`catalogs` (Lot 35, optionnel) : transferts XTF supplementaires deja
    parses (`parse_xtf`) dont les objets doivent aussi compter comme
    resolubles pour la resolution TID/REF - typiquement un panier de
    donnees-catalogue (RoadTrafficCensusCatalogues et famille) distribue
    separement du transfert de donnees metier principal (voir
    `_build_tid_index`)."""
    tid_index = _build_tid_index(transfer, catalogs)
    schema_cache: dict[int, dict[str, ResolvedAttribute]] = {}
    issues: list[ValidationIssue] = []
    for basket in transfer.baskets:
        for obj in basket.objects:
            issues.extend(_validate_object(
                obj, basket, symbol_table=symbol_table, repository=repository, tid_index=tid_index,
                schema_cache=schema_cache,
            ))
    return issues
