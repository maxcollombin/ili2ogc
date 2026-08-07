"""Interprete une entree `source:` (+ eventuel `rule:`/`mapping:` frere)
d'un binding de spec/grammar/mapping/*.yml contre un ctx ANTLR reel.

Principe central, qui fait que le moteur reste generique meme pour les
constructions imbriquees (ex. attrTypeDef._collection) ou le mecanisme
`path:` : chaque fois qu'un accesseur retourne un noeud, on distingue token
(TerminalNode -> texte brut) de regle (ParserRuleContext -> VISITE
recursivement par le builder, qui applique la logique propre - kind,
attribute_bindings - de CETTE regle). Voir _resolve_node().

Catalogue des formes `source` couvertes, cf. plan de conception du
ModelBuilder (verifie contre les 9 fichiers reels le 2026-08-02, pas ferme
pour toujours) : field+index, field+presence, field+kind:alt_token(+optional),
field+multi(+optional), field+optional, field+alt+index+optional,
field+anchor+optional, field+path+optional, kind:constant+value,
segments composes+join(+index negatif), sequence_pattern, field:null seul
(valeur propagee par le contexte de construction)."""
import warnings
from typing import Any

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNode

from interlis.builder import context_access as ca
from interlis.builder.errors import BuildError
from interlis.builder.forward_refs import ForwardRef

ALT_KINDS = {"alt_token", "alt_rule", "alt_token_or_rule", "alt_token_presence"}


def _resolve_node(node: Any, builder: Any, rule: str) -> Any:
    if node is None:
        return None
    if isinstance(node, TerminalNode):
        return node.getText()
    if isinstance(node, ParserRuleContext):
        return builder.visit(node)
    if isinstance(node, list):
        # Certains accesseurs "multi" retournent leur liste complete meme
        # quand le binding ne demandait pas explicitement une liste (ex.
        # `index: null` appelle l'accesseur sans argument, qui pour un
        # accesseur multi renvoie TOUJOURS la liste, pas un element unique) -
        # resoudre chaque element plutot que de renvoyer la liste brute.
        resolved = [r for r in (_resolve_node(n, builder, rule) for n in node) if r is not None]
        if len(resolved) == 1:
            return resolved[0]
        return resolved if resolved else None
    return node


def _token_name(node: Any, builder: Any) -> str | None:
    symbol = getattr(node, "symbol", None)
    if symbol is None:
        return None
    names = builder.parser_symbolic_names
    idx = symbol.type
    return names[idx] if 0 <= idx < len(names) else None


def resolve_source(
    ctx: Any,
    source: dict,
    *,
    rule: str,
    construction_context: dict,
    builder: Any,
    rule_map: dict | None = None,
    binding_key: str | None = None,
    wrap_map: dict | None = None,
) -> Any:
    if not source:
        return None

    if source.get("kind") == "constant":
        return source.get("value")

    field = source.get("field")
    optional = bool(source.get("optional"))

    # field: null seul -> valeur propagee par la regle englobante, sous la
    # MEME cle d'attribut (ex. interlis2def.iliVersion -> modeldef.iliVersion,
    # meme nom des deux cotes - mecanisme minoritaire mais impossible a
    # eviter sans changer la spec, cable explicitement plutot qu'une
    # solution generique cachee, voir plan de conception).
    if field is None:
        key = source.get("context_key") or binding_key or rule
        value = construction_context.get(key)
        if source.get("as_forward_ref") and isinstance(value, str):
            # Enveloppe une valeur brute (ex. nom importe via
            # modeldef.imports, boucle for_each - voir InterlisModelBuilder)
            # en reference nommee, resolue plus tard comme n'importe quelle
            # autre (SymbolTable, ou UnresolvedNamedReference si hors du
            # fichier courant - decision de perimetre V1 IMPORTS).
            return ForwardRef(name=value, rule=rule, always_external=bool(source.get("always_external_ref")))
        return value

    kind = source.get("kind")

    # --- multi : liste, chaque element resolu (token -> texte, regle ->
    # visite). Verifie AVANT le cas alt/'|' ci-dessous : un champ compose
    # (ex. 'Name|INTERLIS') combine a multi:true doit lister TOUTES les
    # occurrences de chaque alternative, pas s'arreter a la premiere
    # (sinon ca.call(ctx, name) sur un accesseur "multi" appele sans index
    # renverrait sa LISTE complete, pas un noeud unique - bug latent). -----
    if source.get("multi"):
        names = [n.strip() for n in field.split("|")] if "|" in field else [field]
        nodes: list[Any] = []
        for name in names:
            if ca.has_accessor(ctx, name):
                nodes.extend(ca.call_list(ctx, name))
        between = source.get("between")
        if between and nodes:
            # Meme accesseur (ex. Name) reutilise a plusieurs positions
            # grammaticales distinctes dans la MEME regle (ex. modeldef :
            # son propre nom, un nom de langue optionnel, les noms importes,
            # le nom de fermeture apres END - tous des 'Name') : ne garder
            # que les occurrences situees ENTRE deux tokens-ancres (ex.
            # ['IMPORTS', 'END'] pour modeldef.imports), par position dans
            # ctx.children plutot que par index plat fixe.
            start_name, end_name = between
            children = list(ctx.children or [])
            start_positions = [children.index(n) for n in ca.call_list(ctx, start_name) if n in children]
            end_positions = [children.index(n) for n in ca.call_list(ctx, end_name) if n in children]
            if start_positions and end_positions:
                lo, hi = min(start_positions), min(end_positions)
                nodes = [n for n in nodes if lo < children.index(n) < hi]
            else:
                nodes = []
        return [_resolve_node(n, builder, rule) for n in nodes]

    # --- alternatives (alt_token / alt_rule / alt_token_or_rule /
    # alt_token_presence) : plusieurs noms de champ separes par '|', on
    # retient le premier present. La cle de mapping (rule_map) est le NOM
    # de champ qui a matche (pas son texte), ou "absent" si aucun. ----------
    if kind in ALT_KINDS or "|" in field:
        names = [n.strip() for n in field.split("|")]
        alt_index = source.get("index")
        for name in names:
            if not ca.has_accessor(ctx, name):
                continue
            # index (ex. Min: index 0, Max: index 1 sur le MEME groupe
            # d'alternatives 'Number|PosNumber|Dec') selectionne QUELLE
            # occurrence du nom matche, pas seulement sa presence - ignore
            # ca aurait donne la liste complete (ou le seul/premier element)
            # au lieu de l'operande voulu. Mais un index peut ne pas
            # s'appliquer a TOUTES les alternatives du groupe (ex.
            # cardinality.Max: 'MUL|PosNumber', index:1 - MUL/'*' est un
            # accesseur single, l'index ne concerne que la branche PosNumber) -
            # retomber sur l'accesseur sans index plutot que TypeError.
            try:
                node = ca.call(ctx, name, alt_index)
            except TypeError:
                node = ca.call(ctx, name)
            if isinstance(node, list):
                # `name` est un accesseur "multi" sur CE ctx (peut apparaitre
                # plusieurs fois - ex. numeric()/enumeration() sur
                # DomainDefContext, qui boucle sur N declarations de domaine,
                # meme quand une seule alternative n'est en jeu par
                # declaration) et aucun index explicite n'a matche (alt_index
                # est None ou ne s'applique pas ici) : `ca.call` sans index
                # renvoie alors la LISTE COMPLETE, jamais None, meme quand
                # elle est VIDE - sans ce garde, une liste vide etait
                # faussement traitee comme "alternative presente" (trouve sur
                # domainDef._domain_content : "numeric" matchait toujours en
                # premier avec une liste vide avant que "enumeration", la
                # vraie alternative presente, ne soit jamais essayee).
                node = node[0] if node else None
            if node is not None:
                if kind == "alt_token_presence":
                    value = name
                elif wrap_map is not None and name in wrap_map and isinstance(node, ParserRuleContext):
                    # L'alternative matchee est une regle-bag (Container/
                    # ValueObject, ex. numeric()/enumeration()) a promouvoir
                    # en instance typee du metamodele (ex. NumType/EnumType).
                    # L'instance doit exister et etre au sommet de la pile
                    # de construction AVANT la visite (pas apres) : certains
                    # enfants de la regle-bag s'auto-attachent via leur
                    # propre `parent:` PENDANT la visite (ex. enumElement ->
                    # TopNode/SubNode) - visiter d'abord puis envelopper
                    # ensuite les attacherait au mauvais parent (celui
                    # deja au sommet de la pile a ce moment, pas la nouvelle
                    # instance). Voir InterlisModelBuilder.visit_wrapped.
                    return builder.visit_wrapped(node, wrap_map[name], rule)
                else:
                    value = _resolve_node(node, builder, rule)
                if rule_map is not None:
                    return rule_map.get(name, value)
                return value
        if rule_map is not None and "absent" in rule_map:
            return rule_map["absent"]
        # Aucune alternative presente et pas de cle "absent" pour un defaut
        # explicite : traite comme optionnel par defaut, `optional: true`
        # ou pas. Justification (audit smoke test ModelBuilder, 2026-08-02) :
        # tous les alt_token/alt_rule reels examines sont soit dotes d'un
        # "absent" dans rule_map (cas ou une valeur par defaut existe), soit
        # documentes comme optionnels dans leur note sans que le flag
        # structure `optional: true` suive systematiquement (plusieurs
        # occurrences trouvees et corrigees au cas par cas, ex.
        # numeric.Clockwise - mais le motif se repete plus qu'il n'est
        # commode de corriger un par un) - aucun cas reel trouve ou une
        # alternative manquante doit etre une erreur bloquante.
        warnings.warn(f"[{rule}] aucune alternative presente parmi {names!r} - traite comme absent/None")
        return None

    # --- sequence_pattern : reconnaissance d'une sequence exacte de tokens
    # consecutifs a partir de `field` (ex. roleDef.Strongness : '--' vs
    # '-<>' vs '-<#>' -> Assoc/Aggr/Comp). La cle de mapping est la
    # sequence de NOMS de token (ex. "MINUS LT GT"), pas le texte. ----------
    if source.get("sequence_pattern"):
        if rule_map is None:
            raise BuildError("sequence_pattern sans rule:/mapping: associe", rule=rule, ctx=ctx)
        return _match_sequence_pattern(ctx, field, rule_map, builder, rule, optional)

    # --- anchor : position relative a un token ancre (pas un index plat -
    # necessaire quand plusieurs occurrences du meme type de champ existent
    # et que seule celle suivant un token precis nous interesse). ----------
    if "anchor" in source:
        return _resolve_anchor(ctx, field, source["anchor"], optional=optional, builder=builder, rule=rule)

    # --- path : delegue a une sous-regle (Container/ValueObject, visitee
    # via _resolve_node -> retourne un bag dict), extrait une cle. ----------
    if "path" in source:
        if not ca.has_accessor(ctx, field):
            raise BuildError(f"accesseur {field!r} introuvable sur {type(ctx).__name__}", rule=rule, ctx=ctx)
        node = ca.call(ctx, field)
        if node is None:
            if optional:
                return None
            raise BuildError(f"{field!r} absent (path={source['path']!r})", rule=rule, ctx=ctx)
        bag = _resolve_node(node, builder, rule)
        if not isinstance(bag, dict):
            raise BuildError(
                f"path={source['path']!r} attendu sur un bag (dict), obtenu {type(bag).__name__}", rule=rule, ctx=ctx
            )
        return bag.get(source["path"])

    # --- presence : booleen (le champ apparait-il ou non). ------------------
    if source.get("presence"):
        present = ca.is_present(ctx, field, source.get("index"))
        if rule_map is not None:
            key = f"{field}_present" if present else f"{field}_absent"
            if key in rule_map:
                return rule_map[key]
        return present

    # --- segments composes + join : concatenation de plusieurs sous-valeurs
    # nommees comme cles freres de `field` (valeur null = "presence de ce
    # segment tel quel"), avec index Python (-1 = dernier) supporte. --------
    if "join" in source:
        return _resolve_join(ctx, source, builder, rule)

    # --- cas standard : field (+ index) (+ optional). -------------------
    # CORRIGE (Lot 38) : ce chemin acceptait aussi un filtre `alt: <entier>`
    # (+ forme "N|M"), cense ne s'appliquer qu'a UNE alternative grammaticale
    # numerotee via `ctx.getAltNumber()`. Retire entierement - RULE #1,
    # verifie empiriquement que `getAltNumber()` renvoie INCONDITIONNELLEMENT
    # 0 pour toute regle de `InterlisParser.g4` (aucune alternative
    # labellisee nulle part dans la grammaire vendee), rendant ce filtre
    # TOUJOURS faux et les 15 bindings qui l'utilisaient TOUJOURS None (bug
    # confirme sur formattedType.Format/Min/Max, entre autres). Chacun des
    # 15 cas reels a ete reverifie contre la grammaire (RULE #1/#2) : le nom
    # d'accesseur (`field:`) qu'ils filtraient etait deja NATURELLEMENT
    # exclusif a l'alternative visee par construction grammaticale (ex.
    # `formattedType` alt1 a un accesseur `Name` direct qu'aucune autre
    # alternative n'expose ; `pathEl` n'expose `Name` directement que dans
    # ses alternatives 5/6/9, jamais 1-4/7/8) - le filtre `alt:` etait donc
    # une securite redondante plutot qu'une necessite, jamais indispensable
    # a la bonne resolution une fois retire. Voir PROGRESS.md (Lot 37/38)
    # pour le detail complet de l'investigation.
    if not ca.has_accessor(ctx, field):
        if optional:
            return None
        raise BuildError(f"accesseur {field!r} introuvable sur {type(ctx).__name__}", rule=rule, ctx=ctx)

    node = ca.call(ctx, field, source.get("index"))
    if node is None:
        if optional:
            return None
        raise BuildError(f"{field!r} absent (non optionnel) sur {type(ctx).__name__}", rule=rule, ctx=ctx)
    value = _resolve_node(node, builder, rule)
    if rule_map is not None:
        return rule_map.get(value, value)
    return value


def _resolve_anchor(ctx: Any, field: str, anchor: str, *, optional: bool, builder: Any, rule: str) -> Any:
    if not ca.has_accessor(ctx, anchor):
        if optional:
            return None
        raise BuildError(f"ancre {anchor!r} introuvable sur {type(ctx).__name__}", rule=rule, ctx=ctx)
    anchor_node = ca.call(ctx, anchor)
    if anchor_node is None:
        if optional:
            return None
        raise BuildError(f"ancre {anchor!r} absente", rule=rule, ctx=ctx)
    children = list(ctx.children or [])
    try:
        anchor_index = children.index(anchor_node)
    except ValueError:
        raise BuildError(f"ancre {anchor!r} introuvable dans ctx.children", rule=rule, ctx=ctx)
    for node in ca.call_list(ctx, field):
        try:
            idx = children.index(node)
        except ValueError:
            continue
        if idx > anchor_index:
            return _resolve_node(node, builder, rule)
    if optional:
        return None
    raise BuildError(f"{field!r} absent apres l'ancre {anchor!r}", rule=rule, ctx=ctx)


def _resolve_join(ctx: Any, source: dict, builder: Any, rule: str) -> str:
    separator = source["join"]
    field = source.get("field")
    index = source.get("index")
    segments: list[str] = []
    if field is not None:
        if index is not None:
            nodes = ca.call_list(ctx, field)
            if not nodes:
                raise BuildError(f"{field!r} vide (join, index={index})", rule=rule, ctx=ctx)
            segments.append(_resolve_node(nodes[index], builder, rule))
        else:
            segments.append(_resolve_node(ca.call(ctx, field), builder, rule))
    for key, value in source.items():
        if key in ("field", "index", "join", "optional") or value is not None:
            continue
        if not ca.has_accessor(ctx, key):
            continue
        node = ca.call(ctx, key)
        if node is not None:
            segments.append(_resolve_node(node, builder, rule))
    return separator.join(s for s in segments if s is not None)


def _match_sequence_pattern(ctx: Any, field: str, rule_map: dict, builder: Any, rule: str, optional: bool = False) -> Any:
    if not ca.has_accessor(ctx, field):
        raise BuildError(f"accesseur {field!r} introuvable sur {type(ctx).__name__}", rule=rule, ctx=ctx)
    # `field` peut lui-meme etre un accesseur "multi" (ex. '--' = deux
    # tokens MINUS) : l'ancre est TOUJOURS la 1ere occurrence, la sequence
    # se lit a partir de sa position dans ctx.children. Absent est possible
    # de facon legitime (ex. roleDef a une 2e forme grammaticale - restriction
    # de type sur un role EXISTANT par son nom - qui n'utilise aucun symbole
    # de force de relation du tout).
    candidates = ca.call_list(ctx, field)
    if not candidates:
        if optional:
            return None
        raise BuildError(f"{field!r} absent (sequence_pattern)", rule=rule, ctx=ctx)
    anchor_node = candidates[0]
    children = list(ctx.children or [])
    try:
        start = children.index(anchor_node)
    except ValueError:
        raise BuildError(f"{field!r} introuvable dans ctx.children", rule=rule, ctx=ctx)

    max_len = max(len(pattern.split()) for pattern in rule_map)
    window = children[start:start + max_len]
    names = [_token_name(n, builder) for n in window]
    for length in range(max_len, 0, -1):
        candidate = " ".join(n for n in names[:length] if n)
        if candidate in rule_map:
            return rule_map[candidate]
    raise BuildError(
        f"aucune sequence de tokens ne correspond a {sorted(rule_map)} a partir de {field!r} (obtenu {names!r})",
        rule=rule, ctx=ctx,
    )
