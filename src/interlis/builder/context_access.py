"""Lecture generique des accesseurs d'un contexte ANTLR4-Python.

Par introspection de signature (pas via generated/ast-accessors.yml, verifie
incomplet pour certaines regles - ex. attributeDef n'a qu'un placeholder
`'...': null`). Convention ANTLR4-Python confirmee empiriquement sur le code
genere reel (src/interlis/antlr/InterlisParser.py) : un accesseur pouvant
matcher plusieurs fois (regle repetee, token repete) prend un parametre
optionnel `i: int = None` (None -> liste complete via getTypedRuleContexts/
getTokens, entier -> element a cette position) ; un accesseur garanti
unique n'a AUCUN parametre. L'introspection de la signature bindee
(len(parameters) == 0 vs 1) distingue les deux cas de facon fiable, sans
dependre d'un cache externe."""
import inspect
from typing import Any


def has_accessor(ctx: Any, field: str) -> bool:
    return callable(getattr(ctx, field, None))


def call(ctx: Any, field: str, index: int | None = None) -> Any:
    """Appelle ctx.<field>(...) en respectant sa signature reelle. Leve
    AttributeError si l'accesseur n'existe pas sur ce ctx, TypeError si un
    index non nul est demande sur un accesseur "single" (garanti unique) -
    index=0 est tolere dans ce cas (equivalent a "le seul/premier match")
    car la spec ne distingue pas toujours explicitement single/multi."""
    method = getattr(ctx, field, None)
    if method is None or not callable(method):
        raise AttributeError(f"{type(ctx).__name__} n'a pas d'accesseur {field!r}")
    accepts_index = len(inspect.signature(method).parameters) > 0
    if index is not None:
        if not accepts_index:
            if index == 0:
                return method()
            raise TypeError(f"{field} sur {type(ctx).__name__} n'accepte pas d'index (accesseur single)")
        if index < 0:
            # Index Python (-1 = dernier element) : l'accesseur ANTLR genere
            # (getTypedRuleContext(s)/getToken(s)) ne comprend QUE des index
            # positifs 0-bases (retourne None sans erreur pour un index
            # negatif, jamais traduit en "depuis la fin") - passer par la
            # liste complete pour beneficier du slicing Python normal.
            # Bug trouve via un vrai .ili (setConstraint.Constraint, corpus
            # models.geo.admin.ch/DMAV_Bodenbedeckung_V1_0.ili) : index: -1
            # renvoyait toujours None, jamais la derniere expression() reelle.
            values = method()
            values = list(values) if values is not None else []
            return values[index] if -len(values) <= index < len(values) else None
        return method(index)
    return method()


def call_list(ctx: Any, field: str) -> list:
    """Version liste uniforme : un accesseur "multi" sans index retourne
    deja sa liste complete (0..N elements) ; un accesseur "single" retourne
    0 ou 1 element - normalise en liste dans les deux cas."""
    method = getattr(ctx, field, None)
    if method is None or not callable(method):
        raise AttributeError(f"{type(ctx).__name__} n'a pas d'accesseur {field!r}")
    accepts_index = len(inspect.signature(method).parameters) > 0
    if accepts_index:
        result = method()
        return list(result) if result is not None else []
    result = method()
    return [] if result is None else [result]


def is_present(ctx: Any, field: str, index: int | None = None) -> bool:
    """CORRIGE (Lot 45, grammaire) : `call(ctx, field, None)` sur un
    accesseur "multi" (i.e. qui accepte un parametre `i=None`) sans index
    explicite renvoie sa LISTE COMPLETE via `method()` (`getTokens`/
    `getTypedRuleContexts`) - TOUJOURS une liste, JAMAIS `None`, meme
    quand elle est VIDE (aucune occurrence). L'ancien test `is not None`
    la traitait alors a tort comme "presente" inconditionnellement des
    qu'un accesseur devenait multi - jusqu'ici jamais un souci en pratique
    (aucun binding `presence: true` ne visait un accesseur DEJA multi sans
    index), mais devenu un bug reel des que le Lot 45 (Properties<>
    corrigees en listes separees par virgule dans la grammaire, ex.
    `CLASS X (ABSTRACT,FINAL)`) a fait passer ABSTRACT/FINAL/TRANSIENT/...
    de simple a multi (repetable au sein du groupe `(COMMA ...)*`) -
    `Abstract`/`Final`/`Transient` (`presence: true` sans index,
    03_classes_and_structures.yml/04_attributes.yml/etc.) valaient alors
    `True` INCONDITIONNELLEMENT, confirme reel sur models/IlisMeta16.ili
    (diff massif, aucune classe de ce fichier n'utilise pourtant
    ABSTRACT/FINAL/TRANSIENT en combinaison)."""
    result = call(ctx, field, index)
    if isinstance(result, list):
        return len(result) > 0
    return result is not None


def text(ctx: Any, field: str, index: int | None = None) -> str | None:
    """Texte brut d'un accesseur (token ou regle) - .getText() sur le noeud
    retourne, ou None si absent. Les litteraux STRING INTERLIS conservent
    leurs guillemets dans getText() ; le decouillemetage est la
    responsabilite de l'appelant (source_resolver), pas de cette couche
    generique."""
    node = call(ctx, field, index)
    return None if node is None else node.getText()
