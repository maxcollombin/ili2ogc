"""Resolution multi-fichiers (IMPORTS) : charge a la demande un modele
importe depuis un ensemble de repertoires locaux, pour que les references
qualifiees (ex. `GeometryCHLV95_V2.Coord2`) resolvent vers l'instance
reelle plutot qu'un `UnresolvedNamedReference`.

Portee volontairement locale (jamais de reseau pendant un build - le corpus
doit etre telecharge au prealable, voir README) et jamais fusionnee dans une
table de symboles globale (voir ForwardRefResolver/SymbolTable) : chaque
modele charge garde sa PROPRE SymbolTable isolee, pour ne jamais introduire
d'ambiguite de nom court ENTRE modeles sans rapport (seule la resolution par
nom qualifie complet, dans la table du modele explicitement cible, traverse
les fichiers)."""
import re
from pathlib import Path
from typing import Any

from interlis.builder.errors import BuildError
from interlis.runtime.parse import parse_file, parse_text

# Lot 36 : CORRIGE - matchait a tort `(?:MODEL|REFSYSTEM)`, comme si les
# deux keywords pouvaient chacun preceder directement le Name. Faux : la
# grammaire reelle (`modeldef`, vendor/interlis-antlr4/InterlisParser.g4)
# est `CONTRACTED? (TYPE | REFSYSTEM | SYMBOLOGY)? MODEL Name ...` -
# REFSYSTEM n'est JAMAIS qu'un prefixe optionnel AVANT MODEL, jamais un
# substitut. Sur `REFSYSTEM MODEL CoordSys`, l'ancien pattern matchait
# "REFSYSTEM MODEL" en capturant a tort le literal "MODEL" comme nom -
# CoordSys (importe par 100% du corpus XTF reel de ce projet) n'etait donc
# JAMAIS indexe, degradant silencieusement toute reference vers ce modele
# en UnresolvedNamedReference malgre le fichier present dans --repo (trouve
# en repondant a une question utilisateur sur la completude de la
# validation vs le header XTF, PAS un lot planifie). Meme bug pour
# `REFSYSTEM BASKET Name` (metaDataBasketDef, une regle SANS RAPPORT) :
# capturait a tort "BASKET" comme faux nom de modele. Fix : MODEL est
# TOUJOURS directement suivi du vrai Name, quel que soit le prefixe
# optionnel devant (ou son absence) - un seul mot-cle a chercher.
_MODEL_NAME_RE = re.compile(r"\bMODEL\s+([A-Za-z_][A-Za-z0-9_]*)")

# Modele "INTERLIS" predefini (Reference Manual eCH-0031 V2.1.0/2024-04-24,
# Annexe A "Das interne INTERLIS-Datenmodell", citee telle quelle - RULE #4) :
# toujours disponible qualifie (`INTERLIS.I32OID`), et non qualifie
# uniquement via `IMPORTS UNQUALIFIED INTERLIS;` (voir
# SymbolTable.unqualified_imports / ForwardRefResolver._resolve_one).
# N'existe comme fichier .ili nulle part sur disque - construit via le VRAI
# pipeline parse+build (pas d'instances Python fabriquees a la main, RULE #1)
# pour beneficier des memes garanties que n'importe quel autre modele.
#
# Le MODEL declare ci-dessous ne peut PAS s'appeler litteralement "INTERLIS" :
# ce mot est son propre token lexer reserve (utilise par la clause
# 'IMPORTS (Name|INTERLIS)' elle-meme), distinct de `Name` - confirme par
# test empirique ("mismatched input 'INTERLIS' expecting Name" des que
# "MODEL INTERLIS" est tente). Sans consequence pratique : la resolution par
# nom court de SymbolTable.resolve() (voir forward_refs.py) retrouve
# "I32OID" quel que soit le nom qualifie interne reellement enregistre -
# seule la cle "INTERLIS" du dict ci-dessous (utilisee par ModelRepository,
# jamais par le texte source lui-meme) compte pour le lookup externe.
#
# ANYOID/UUIDOID VOLONTAIREMENT ABSENTS (perimetre reduit par rapport a la
# citation complete du manuel, confirme par test empirique) : contrairement
# a NOOID/I32OID/STANDARDOID, `ANYOID` et `UUIDOID` sont eux-memes des TOKENS
# LEXER RESERVES dans vendor/interlis-antlr4/InterlisLexer.g4 ("ANYOID :
# 'ANYOID';", "UUIDOID : 'UUIDOID';"), jamais un `Name` ordinaire - la
# grammaire ne les rend accessibles QUE via les formes qualifiees speciales
# `INTERLIS DOT ANYOID`/`INTERLIS DOT UUIDOID` de InterlisParser.g4 (clauses
# `OID AS`, `structureRef`, `oidType`, etc. - jamais via le `domainRef`
# ordinaire, qui n'accepte que des sequences de `Name`). Consequence :
# `DOMAIN ANYOID = ...;`/`DOMAIN UUIDOID = ...;` sont syntaxiquement
# IMPOSSIBLES a ecrire (confirme : "mismatched input 'ANYOID' expecting
# {'UUIDOID', Name}"), et `EXTENDS ANYOID` egalement (domainRef n'accepte
# pas ce token) - le modele predefini "litteral" du manuel n'est donc pas
# exprimable tel quel dans CETTE grammaire. Modeliser ANYOID/UUIDOID
# correctement necessiterait un binding dedie pour ces formes qualifiees
# speciales (kind: Reference distinct, jamais un domainRef) - hors perimetre
# de ce lot (Lot 23, motive par I32OID uniquement, seul echec reel du
# corpus). NOOID est inclus pour rester fidele a la citation du manuel mais
# n'est actuellement cible par aucun ForwardRef reel du corpus - I32OID
# n'EXTENDS plus ANYOID (impossible) mais reste un DOMAIN OID numerique
# independant, comportementalement equivalent pour toute resolution par nom.
#
# GregorianYear AJOUTE (Lot 47 point 3, demande explicite utilisateur suite
# a la categorisation des issues XTF - RULE #4, citation directe eCH-0031
# V2.1.0 §3.8.7 "Datum und Zeit" : `DOMAIN GregorianYear = 1582 .. 2999 [Y]
# {GregorianCalendar};`) : contrairement a BOOLEAN/ANYOID/UUIDOID ci-dessous,
# `GregorianYear` est un `Name` ORDINAIRE (pas un token lexer reserve) -
# `INTERLIS.GregorianYear` resout via le MEME chemin `domainRef` (confirme
# par tracage direct de l'arbre ANTLR, RULE #2) que I32OID/NOOID/STANDARDOID
# ci-dessus - aucun binding dedie necessaire. Unite `[Y]` et annotation
# `{GregorianCalendar}` volontairement OMISES (perimetre reduit,
# deliberement) : confirme empiriquement qu'une reference d'unite non
# resolue (`unitRef` degrade gracieusement, ex. `INTERLIS.M`/`INTERLIS.h`
# deja references ainsi par `RoadTrafficAccidentLocation_V2.ili` sans jamais
# bloquer la resolution Min/Max du NumType englobant) ne genait deja pas la
# verification NUMERIC reelle qui motive cet ajout - modeliser `UNIT Year
# [Y]`/`REFSYSTEM BASKET BaseTimeSystems` pour rester fidele a la citation
# complete n'apporterait donc aucun benefice fonctionnel supplementaire.
# Resultat confirme (RULE #6) : `AccidentYear`/`Year` (RoadTrafficAccidentLocation_V2.ili/
# RoadTrafficCensus_V1_1.ili, `INTERLIS.GregorianYear`) beneficient desormais
# d'une VRAIE verification NumType (Min=1582/Max=2999) au lieu d'un `type
# None` jamais verifie.
#
# BOOLEAN VOLONTAIREMENT ABSENT (meme categorie de limite qu'ANYOID/UUIDOID
# ci-dessus, investigue au Lot 47 point 3) : `BOOLEAN` EST un token lexer
# reserve (`vendor/interlis-antlr4/InterlisLexer.g4`, `BOOLEAN : 'BOOLEAN';`)
# - MAIS, contrairement a ANYOID/UUIDOID (inaccessibles hors de leurs formes
# qualifiees dediees), `INTERLIS.BOOLEAN` resout en realite via
# `structureRef` (`InterlisParser.g4` : `structureRef : (INTERLIS DOT (Name
# | BOOLEAN | UUIDOID | URI) ...)`), PAS `domainRef` - confirme par tracage
# direct de l'arbre ANTLR (RULE #2, meme methode que pour GregorianYear
# ci-dessus). `structureRef` (spec/grammar/mapping/03_classes_and_structures.yml)
# resout SEULEMENT vers `IlisMeta16.ModelData.Class` (`resolves_to: Class`) -
# jamais vers un `EnumType`, alors que la citation manuel reelle (§3.8.4,
# RULE #4) definit BOOLEAN comme `DOMAIN BOOLEAN (FINAL) = (false, true)
# ORDERED;`, une ENUMERATION. Deux options ecartees : (1) enregistrer une
# fausse instance `Class[Kind=Structure]` nommee "BOOLEAN" resoudrait le nom
# mais MENTIRAIT sur le metamodele reel (BOOLEAN n'est structurellement pas
# une STRUCTURE) ; (2) meme resolue "correctement", le `type_kind` resultant
# resterait hors du perimetre couvert par `xtf/validate.py` (seuls
# TextType/NumType/EnumType sont interpretes - un `Class`/STRUCTURE resolu
# resterait "info: type non verifie", EXACTEMENT le meme resultat qu'un
# `type_kind=None` non resolu) : AUCUN benefice fonctionnel a corriger ce
# cas, contrairement a GregorianYear. Necessiterait un binding dedie
# discriminant PAR ALTERNATIVE grammaticale de `structureRef` (Name vs
# BOOLEAN vs UUIDOID vs URI) - mecanisme de resolution actuel (`kind_hint`
# statique par regle) n'offre pas ce niveau de granularite - hors perimetre
# de ce lot.
_PREDEFINED_MODEL_INTERNAL_NAME = "PredefinedInterlisNamespace"
_PREDEFINED_INTERLIS_SOURCE = f"""\
INTERLIS 2.4;

MODEL {_PREDEFINED_MODEL_INTERNAL_NAME} AT "http://www.interlis.ch" VERSION "2024-04-24" =

  DOMAIN
    NOOID = OID ANY;
    I32OID = OID 0..2147483647;
    STANDARDOID = OID TEXT*16;
    GregorianYear = 1582..2999;

END {_PREDEFINED_MODEL_INTERNAL_NAME}.
"""
_BUILTIN_SOURCES = {"INTERLIS": _PREDEFINED_INTERLIS_SOURCE}


class ModelRepository:
    """Indexe un ensemble de repertoires par nom de MODEL/REFSYSTEM declare
    (scan texte leger, pas un parse ANTLR complet - le nom de fichier ou le
    <Name> d'un index externe type ilimodels.xml ne correspond pas forcement
    au nom de MODEL reellement declare, confirme sur le corpus reel
    models.geo.admin.ch : un fichier "obsolete/..." peut declarer un MODEL
    au nom totalement different du fichier courant)."""

    def __init__(self, search_dirs: list[Path]):
        self._index: dict[str, Path] = {}
        for directory in search_dirs:
            for path in sorted(Path(directory).glob("*.ili")):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for match in _MODEL_NAME_RE.finditer(text):
                    # premier trouve gagne (heuristique : un faux positif de
                    # scan texte, ex. dans un commentaire, ne doit jamais
                    # ecraser un nom deja indexe correctement).
                    self._index.setdefault(match.group(1), path)
        # nom de modele -> SymbolTable (deja construite, eventuellement
        # partiellement si en cours - voir garde anti-cycle ci-dessous), ou
        # None si tente et introuvable/echoue - jamais retente.
        self._cache: dict[str, Any] = {}
        self._make_sub_builder = None  # injecte par InterlisModelBuilder

    def path_for(self, model_name: str) -> Path | None:
        """Chemin du fichier `.ili` indexe pour ce nom de MODEL/REFSYSTEM, si
        connu (Lot 34 - resolution pilotee par la HEADERSECTION/MODELS d'un
        XTF a valider, voir xtf/model_resolution.py). `None` pour un modele
        predefini (`_BUILTIN_SOURCES`, jamais un vrai fichier disque) ou
        absent des repertoires `--repo` fournis."""
        return self._index.get(model_name)

    def register_prebuilt(self, model_name: str, symbol_table) -> None:
        """Enregistre dans le MEME cache que `resolve_external`/
        `availability` un modele DEJA construit ailleurs (Lot 39 - le
        modele racine de `interlis validate`, construit directement par le
        builder principal via son propre `parse_file`+`build()`, pas via
        `_get_table`) - evite de le re-parser/reconstruire en double lors
        de la verification de completude du header (`availability` ci-
        dessous)."""
        self._cache[model_name] = symbol_table

    def availability(self, model_name: str) -> str:
        """'builtin' | 'available' | 'indexed_but_failed' | 'missing' -
        etat de resolvabilite d'un modele nomme (Lot 39, verification
        PROACTIVE de completude header-vs-resolu pour `interlis validate`,
        voir xtf/model_resolution.py:header_completeness) - independant de
        ce qu'une DATASECTION exerce reellement (`resolve_external` ne
        charge que ce qui est effectivement REFERENCE, voir Lot 36 :
        'seuls les modeles reellement references... sont charges'`).
        Reutilise `_get_table` (meme cache que `resolve_external`) : aucun
        cout double si ce modele est de toute facon touche plus tard par
        une reference reelle, et `register_prebuilt` evite le cout double
        pour le modele racine lui-meme."""
        if model_name in _BUILTIN_SOURCES:
            return "builtin"
        if model_name not in self._index and model_name not in self._cache:
            return "missing"
        table = self._get_table(model_name)
        return "available" if table is not None else "indexed_but_failed"

    def bind_builder_factory(self, factory) -> None:
        """Injecte la fabrique de sous-builder (fournie par le builder
        racine, qui possede les composants partages - schema/registre/spec/
        attachment - a reutiliser pour chaque fichier importe plutot que de
        les recharger depuis disque). `factory() -> InterlisModelBuilder`,
        deja configure avec `repository=self` pour que SES PROPRES imports
        soient resolus recursivement de la meme facon."""
        self._make_sub_builder = factory

    def resolve_external(self, model_name: str, full_dotted_name: str, kind_hint) -> Any | None:
        table = self._get_table(model_name)
        if table is None:
            return None
        return table.resolve(full_dotted_name, kind_hint=kind_hint)

    def symbol_table_for(self, model_name: str):
        """Table de symboles complete d'un modele CHARGE (meme cache que
        `resolve_external`/`availability`) - expose separement pour permettre
        a un appelant de reutiliser la table ENTIERE (ex.
        `schema.home_symbol_table`, Lot 46 : `embedded_roles_of` doit
        chercher les associations la ou elles sont REELLEMENT declarees,
        pas seulement resoudre UN nom a la fois comme `resolve_external`)."""
        return self._get_table(model_name)

    def _get_table(self, model_name: str):
        if model_name in self._cache:
            return self._cache[model_name]
        if model_name in _BUILTIN_SOURCES:
            tree, syntax_errors = parse_text(_BUILTIN_SOURCES[model_name])
        else:
            path = self._index.get(model_name)
            if path is None:
                self._cache[model_name] = None
                return None
            tree, syntax_errors = parse_file(path)
        if syntax_errors or self._make_sub_builder is None:
            self._cache[model_name] = None
            return None
        builder = self._make_sub_builder()
        # Garde anti-cycle : le placeholder (la SymbolTable du sous-builder,
        # encore vide) est enregistre dans le cache AVANT que `build()` ne
        # tourne, pas apres - une reference reentrante vers ce meme modele
        # (import circulaire, ex. A importe B qui reference A) retrouve sa
        # table partiellement peuplee au lieu de relancer indefiniment le
        # chargement du meme fichier.
        self._cache[model_name] = builder.symbol_table
        try:
            result = builder.build(tree)
        except BuildError:
            # Lot 36 : un modele EXTERNE indexe avec succes peut quand meme
            # echouer a construire pour de vrai (ex. CoordSys-20151124.ili,
            # alternative `DOMAIN X = STRING DOTDOT STRING` de domainDef -
            # PAS encore mappee, deja documente Lot 29 comme limitation
            # connue de ce fichier utilise EN ROOT DIRECT ; jusqu'ici jamais
            # exercee via ce chemin cross-modele a cause d'un bug d'indexation
            # SEPARE, Lot 36, qui empechait CoordSys d'etre trouve du tout).
            # Politique deja existante juste au-dessus pour une erreur de
            # SYNTAXE (`if syntax_errors: ... return None`) : un modele
            # externe qui ne construit pas degrade en `None` (comme absent)
            # plutot que de faire planter tout `validate`/`build` racine -
            # RULE #5, le placeholder deja mis en cache (garde anti-cycle
            # ci-dessus) reste vide, jamais retente.
            self._cache[model_name] = None
            return None
        if model_name in _BUILTIN_SOURCES:
            # Le Model reellement declare porte un nom interne different du
            # nom reel documente par le manuel (voir
            # _PREDEFINED_INTERLIS_SOURCE, contrainte lexer) - corrige ici :
            # Name de l'instance elle-meme (ce n'est pas une fabrication,
            # juste la correction du contournement de syntaxe vers la vraie
            # valeur), toutes les entrees qualifiees de sa table, et un alias
            # sous le nom nu attendu pour que `Import.ImportedP` (resolution
            # `always_external`, cible le nom du modele lui-meme) retrouve
            # l'instance Model reelle plutot qu'un UnresolvedNamedReference.
            result.Name = model_name
            builder.symbol_table.rekey_model_prefix(_PREDEFINED_MODEL_INTERNAL_NAME, model_name)
            builder.symbol_table.register(model_name, result)
        return builder.symbol_table
