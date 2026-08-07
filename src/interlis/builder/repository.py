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
_PREDEFINED_MODEL_INTERNAL_NAME = "PredefinedInterlisNamespace"
_PREDEFINED_INTERLIS_SOURCE = f"""\
INTERLIS 2.4;

MODEL {_PREDEFINED_MODEL_INTERNAL_NAME} AT "http://www.interlis.ch" VERSION "2024-04-24" =

  DOMAIN
    NOOID = OID ANY;
    I32OID = OID 0..2147483647;
    STANDARDOID = OID TEXT*16;

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
