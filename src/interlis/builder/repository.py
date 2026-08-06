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

from interlis.runtime.parse import parse_file

_MODEL_NAME_RE = re.compile(r"\b(?:MODEL|REFSYSTEM)\s+([A-Za-z_][A-Za-z0-9_]*)")


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
        path = self._index.get(model_name)
        if path is None or self._make_sub_builder is None:
            self._cache[model_name] = None
            return None
        tree, syntax_errors = parse_file(path)
        if syntax_errors:
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
        builder.build(tree)
        return builder.symbol_table
