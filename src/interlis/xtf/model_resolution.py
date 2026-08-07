"""Resolution du/des modele(s) racine requis pour valider un transfert XTF,
pilotee EXCLUSIVEMENT par le contenu du fichier lui-meme (Lot 34 - decision
d'architecture documentee dans docs/model-resolution-strategy.md : jamais de
Model Repository interrogeable en direct façon iliValidator, toujours les
references explicites du transfert + un corpus `.ili` prealablement
telecharge en local, RULE de reproductibilite deja etablie pour IMPORTS
depuis le Lot 22 - "jamais de reseau pendant un build()").

Deux sources d'information distinctes, RULE #1 (ne pas deviner - verifier) :
- `HEADERSECTION/MODELS` (`XtfTransfer.models`, parse.py) : declare quels
  modeles ET QUELLES VERSIONS ont ete utilises pour produire CE transfert -
  la source canonique pour savoir QUOI telecharger (nom+version+URI exacts,
  voir scripts/fetch_ili_models.py qui l'utilise deja pour ca).
- `DATASECTION` (les paniers reellement presents, `XtfBasket.qualified_topic`)
  : la source canonique pour savoir quel(s) modele(s) sont REELLEMENT le(s)
  modele(s) "metier" (racine) de CE transfert precis - le header liste
  TOUJOURS aussi des modeles "core" (Units/CoordSys/geometrie/etc.) qui ne
  produisent jamais de panier propre (utilises seulement comme TYPES), donc
  ne peuvent pas servir de point d'entree a `InterlisModelBuilder.build()`."""
from interlis.xtf.parse import XtfTransfer


def root_model_names(transfer: XtfTransfer) -> list[str]:
    """Noms de modele (premier segment qualifie) ayant au moins un panier
    dans la DATASECTION - ordre de premiere apparition, dedupliques. Un seul
    de ces modeles suffit comme racine de `InterlisModelBuilder.build()` :
    les classes des AUTRES modeles requis (racine restants, ou core importe)
    resolvent deja via `ModelRepository.resolve_external` (voir
    xtf/schema.py:resolve_class, mecanisme existant depuis le Lot 22/30) des
    lors que leur fichier est indexe par le meme `--repo`."""
    seen: dict[str, None] = {}
    for basket in transfer.baskets:
        name = basket.qualified_topic.split(".", 1)[0]
        seen.setdefault(name, None)
    return list(seen)


def header_model_lookup(transfer: XtfTransfer) -> dict[str, "list[str]"]:
    """Nom de modele -> [version, uri] tel que declare en HEADERSECTION/
    MODELS, pour produire un message d'erreur actionnable (RULE #5 : ne
    jamais degrader silencieusement) quand un modele racine requis par la
    DATASECTION est absent des repertoires `--repo` fournis - donne a
    l'utilisateur/session suivante EXACTEMENT quoi aller chercher (et ou),
    sans avoir a re-parser le fichier a la main."""
    return {m.name: [m.version or "?", m.uri or "?"] for m in transfer.models}
