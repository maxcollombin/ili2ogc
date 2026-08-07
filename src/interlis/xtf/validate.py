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
from interlis.xtf.schema import (
    ResolvedAttribute, enum_values, reference_external_status, resolve_attribute, resolve_class, schema_members_of,
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
