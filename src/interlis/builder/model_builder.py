"""Moteur generique du ModelBuilder.

InterlisModelBuilder(InterlisParserVisitor) : AUCUNE methode visitXxx
specifique a une regle. visit(ctx) generique derive le nom de regle depuis
type(ctx).__name__, charge l'entree spec/grammar/mapping/*.yml
correspondante, et applique une des 4 strategies d'execution selon `kind`
(voir le plan de conception du ModelBuilder)."""
from pathlib import Path
from typing import Any

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNode

from interlis.antlr.InterlisParser import InterlisParser
from interlis.antlr.InterlisParserVisitor import InterlisParserVisitor
from interlis.builder import context_access as ca
from interlis.builder.attach import AttachmentResolver
from interlis.builder.errors import BuildError
from interlis.builder.forward_refs import ForwardRef, ForwardRefResolver, SymbolTable
from interlis.builder.repository import ModelRepository
from interlis.builder.source_resolver import _alt_matches, resolve_source
from interlis.metamodel.instance import MetaInstance
from interlis.metamodel.registry import MetamodelRegistry
from interlis.metamodel.uml_schema import MetamodelSchema
from interlis.spec.models import SpecEntry
from interlis.spec.spec_index import load_spec

class InterlisModelBuilder(InterlisParserVisitor):
    def __init__(self, mappings_dir: Path, spec_dir: Path, *, repository: ModelRepository | None = None):
        schema = MetamodelSchema.load(mappings_dir)
        registry = MetamodelRegistry.build(schema)
        spec: dict[str, SpecEntry] = load_spec(spec_dir)
        attachment = AttachmentResolver(schema.uml)
        self._init_shared(schema, registry, spec, attachment, repository)

    @classmethod
    def _from_shared(cls, schema, registry, spec, attachment, repository) -> "InterlisModelBuilder":
        """Construit un sous-builder pour un fichier importe (voir
        ModelRepository) en REUTILISANT les composants partages de la
        session plutot que de les recharger depuis mappings_dir/spec_dir -
        critique pour que les classes Pydantic dynamiques (MetamodelRegistry)
        soient des objets Python identiques entre fichiers, pas une classe
        distincte par fichier malgre le meme qualified_name."""
        self = cls.__new__(cls)
        self._init_shared(schema, registry, spec, attachment, repository)
        return self

    def _init_shared(self, schema, registry, spec, attachment, repository) -> None:
        self.schema = schema
        self.registry = registry
        self.spec = spec
        self.attachment = attachment
        # Un ModelRepository "nu" (aucun repertoire de recherche) sert
        # toujours de support au modele INTERLIS predefini (voir
        # repository.py, _BUILTIN_SOURCES) - ce n'est pas de la resolution
        # cross-fichier a proprement parler (aucun disque consulte au-dela
        # du texte integre), donc reste actif meme sans --repo/repository=...
        # explicite. La resolution cross-fichier REELLE (repertoires fournis
        # par l'appelant) demeure opt-in comme avant.
        self.repository = repository if repository is not None else ModelRepository([])
        self.symbol_table = SymbolTable()
        self.forward_refs = ForwardRefResolver(self.symbol_table)
        self.parser_symbolic_names = InterlisParser.symbolicNames
        self._construction_stack: list[dict] = []
        self._parent_stack: list[MetaInstance] = []
        self.repository.bind_builder_factory(self._make_sub_builder)

    def _make_sub_builder(self) -> "InterlisModelBuilder":
        return InterlisModelBuilder._from_shared(self.schema, self.registry, self.spec, self.attachment, self.repository)

    # ------------------------------------------------------------------
    # Point d'entree public
    # ------------------------------------------------------------------
    def build(self, tree: ParserRuleContext) -> Any:
        result = self.visit(tree)
        self.forward_refs.resolve_all(repository=self.repository)
        return result

    # ------------------------------------------------------------------
    # Dispatch generique (remplace le patron visitXxx d'ANTLR)
    # ------------------------------------------------------------------
    def visit(self, ctx: ParserRuleContext) -> Any:
        if ctx is None:
            return None
        rule_name = self._rule_name(ctx)
        entry = self.spec.get(rule_name)
        if entry is None:
            return super().visitChildren(ctx)

        if entry.kind in ("Instance", "ValueObject"):
            return self._build_instance(ctx, rule_name, entry)
        if entry.kind == "Conditional":
            return self._build_conditional(ctx, rule_name, entry)
        if entry.kind in ("Container", "Dispatcher"):
            return self._relay(ctx, rule_name, entry)
        if entry.kind == "Reference":
            return self._resolve_or_defer(ctx, rule_name, entry)
        raise BuildError(f"kind inconnu {entry.kind!r}", rule=rule_name, ctx=ctx)

    @staticmethod
    def _rule_name(ctx: ParserRuleContext) -> str:
        name = type(ctx).__name__
        if name.endswith("Context"):
            name = name[: -len("Context")]
        return name[0].lower() + name[1:] if name else name

    # ------------------------------------------------------------------
    # Strategie 1 : BuildInstance (kind Instance / ValueObject)
    # ------------------------------------------------------------------
    def _build_instance(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        if isinstance(entry.target, list):
            return self._build_multi_target(ctx, rule_name, entry)

        if rule_name == "modeldef":
            self._register_unqualified_imports(ctx)

        instance = self.registry.new_instance(entry.target)
        instance._source_ctx = ctx

        outer_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()

        if entry.discriminant:
            value = self._resolve_discriminant(ctx, rule_name, entry.discriminant, outer_ctx, consumed)
            if value is not None:
                setattr(instance, entry.discriminant.attribute, value)

        # Pousse AVANT de capturer le contexte utilise par les
        # attribute_bindings de CETTE regle : sinon une reference a
        # `context_key: <rule_name>` (l'instance en cours de construction
        # elle-meme, ex. modeldef.imports.ImportingP) ne trouverait que le
        # contexte de la regle ENGLOBANTE, jamais celui-ci - voir mecanisme
        # field: null.
        self._push_construction_context(rule_name, instance)
        construction_ctx = self._construction_stack[-1]
        self._parent_stack.append(instance)
        try:
            if entry.attribute_bindings:
                self._apply_bindings(instance, ctx, rule_name, entry.attribute_bindings, construction_ctx, consumed)
            sweep_results = self._sweep_unclaimed_children(ctx, consumed)
        finally:
            self._parent_stack.pop()
            self._pop_construction_context()
        self._attach_unclaimed_results(instance, sweep_results, rule_name)

        self._maybe_register_symbol(instance)

        if entry.parent and self._parent_stack:
            self.attachment.attach(
                self._parent_stack[-1], entry.parent.role, instance,
                association=entry.parent.association, role=entry.parent.role, rule=rule_name,
            )

        return instance

    def _register_unqualified_imports(self, ctx: ParserRuleContext) -> None:
        """Detecte, sur le ModeldefContext brut, quels noms de la boucle
        `IMPORTS` sont precedes du modificateur `UNQUALIFIED` (ex. `IMPORTS
        UNQUALIFIED INTERLIS;`) - alimente symbol_table.unqualified_imports,
        consulte par ForwardRefResolver._resolve_one pour autoriser une
        reference NON qualifiee a resoudre vers un modele importe. Pas
        exprimable via le mecanisme generique for_each/attribute_bindings
        (voir modeldef.imports, spec/grammar/mapping/02_packages.yml) : le
        metamodele Import n'a lui-meme aucun attribut pour UNQUALIFIED
        (confirme metamodel.txt) - ceci reste un detail interne au moteur de
        resolution, jamais persiste sur une instance MetaInstance.

        UNQUALIFIED precede toujours immediatement le nom qu'il modifie dans
        la grammaire ('IMPORTS UNQUALIFIED? (Name|INTERLIS) (COMMA
        UNQUALIFIED? (Name|INTERLIS))* SEMI' - confirme
        spec/grammar/mapping/02_packages.yml, note modeldef.imports) : un
        simple parcours positionnel de ctx.children suffit, pas besoin d'une
        correlation plus complexe."""
        if not ca.has_accessor(ctx, "UNQUALIFIED"):
            return
        children = list(ctx.children or [])
        for node in ca.call_list(ctx, "UNQUALIFIED"):
            idx = children.index(node)
            if idx + 1 < len(children):
                nxt = children[idx + 1]
                if isinstance(nxt, TerminalNode):
                    self.symbol_table.unqualified_imports.add(nxt.getText())

    def _build_multi_target(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        """Cas topicDef uniquement (deux targets lies : SubModel + DataUnit).
        Pas de generalisation a N targets - un seul cas existe dans les 121
        regles (voir plan de conception)."""
        instances: dict[str, MetaInstance] = {}
        for target in entry.target:
            short = target.rsplit(".", 1)[-1]
            inst = self.registry.new_instance(target)
            inst._source_ctx = ctx
            instances[short] = inst

        construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()

        prefixed_bindings: dict[str, dict[str, Any]] = {short: {} for short in instances}
        for key, binding in (entry.attribute_bindings or {}).items():
            if "." in key:
                prefix, sub_key = key.split(".", 1)
                if prefix in prefixed_bindings:
                    prefixed_bindings[prefix][sub_key] = binding
                    continue
            # cle non prefixee : appliquee a la premiere instance (rare, defensif)
            prefixed_bindings[next(iter(instances))][key] = binding

        submodel = instances.get("SubModel")
        if submodel is not None:
            self._push_construction_context(rule_name, submodel)
            self._parent_stack.append(submodel)
            try:
                self._apply_bindings(submodel, ctx, rule_name, prefixed_bindings.get("SubModel", {}), construction_ctx, consumed)
            finally:
                self._parent_stack.pop()
                self._pop_construction_context()
            self._maybe_register_symbol(submodel)

        for short, inst in instances.items():
            if short == "SubModel":
                continue
            self._push_construction_context(rule_name, inst)
            self._parent_stack.append(inst)
            try:
                self._apply_bindings(inst, ctx, rule_name, prefixed_bindings.get(short, {}), construction_ctx, consumed)
            finally:
                self._parent_stack.pop()
                self._pop_construction_context()

        if submodel is not None:
            for short, inst in instances.items():
                if inst is not submodel:
                    submodel._twin = inst
                    inst._twin = submodel

        self._sweep_unclaimed_children(ctx, consumed)

        if entry.parent and self._parent_stack:
            for inst in instances.values():
                self.attachment.attach(
                    self._parent_stack[-1], entry.parent.role, inst,
                    association=entry.parent.association, role=entry.parent.role, rule=rule_name,
                )

        return submodel if submodel is not None else next(iter(instances.values()))

    # ------------------------------------------------------------------
    # Cas special : multi_declaration (domainDef uniquement - voir _relay)
    # ------------------------------------------------------------------
    def _build_multi_declaration(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        """domainDef() est le SEUL des 121 regles a boucler grammaticalement
        sur N declarations independantes partageant un seul mot-cle DOMAIN
        (ex. "DOMAIN Code = 0..255; MultRange = 0..2147483647; LengthRange
        EXTENDS MultRange = 1..2147483647;" - confirme sur le code genere
        reel, InterlisParser.py, domainDef() : boucle "while _alt != 2",
        chaque iteration Name (...)? EQ MANDATORY? (type_()|numeric()|
        enumeration()|STRING DOTDOT STRING|CLASS RESTRICTION(...)) SEMI).
        Chaque occurrence de SEMI delimite une declaration - decoupe
        ctx.children en segments par position plutot que par nom d'accesseur
        (aucun mecanisme generique existant, ex. for_each, ne correle
        plusieurs accesseurs DIFFERENTS - Name/type_/numeric/enumeration - a
        la MEME position parmi N occurrences)."""
        children = list(ctx.children or [])
        segments: list[list[Any]] = []
        current: list[Any] = []
        for child in children:
            current.append(child)
            if isinstance(child, TerminalNode) and child.symbol.type == InterlisParser.SEMI:
                segments.append(current)
                current = []
        if current:
            segments.append(current)

        results = []
        for segment in segments:
            name_node = next(
                (c for c in segment if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.Name), None
            )
            if name_node is None:
                continue  # segment sans Name (ex. residu avant le 1er token) - rien a construire
            mandatory = any(
                isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.MANDATORY for c in segment
            )
            content_node = next(
                (c for c in segment if isinstance(c, ParserRuleContext) and self._rule_name(c) in ("type", "numeric", "enumeration")),
                None,
            )
            if content_node is None:
                class_token = next(
                    (c for c in segment if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.CLASS), None,
                )
                if class_token is None:
                    continue  # STRING DOTDOT STRING - pas encore mappe (voir note domainDef)
                instance = self._build_domain_class_restriction(segment, rule_name)
                if not isinstance(instance, MetaInstance):
                    continue
                if getattr(instance, "Name", None) is None:
                    instance.Name = name_node.getText()
                    self._maybe_register_symbol(instance)
                if mandatory and getattr(instance, "Mandatory", None) is None:
                    instance.Mandatory = True
                if entry.parent and self._parent_stack:
                    self.attachment.attach(
                        self._parent_stack[-1], entry.parent.role, instance,
                        association=entry.parent.association, role=entry.parent.role, rule=rule_name,
                    )
                results.append(instance)
                continue
            content_rule = self._rule_name(content_node)
            if content_rule == "type":
                # `_rule_name` derive "type" de `TypeContext` (regle grammaticale
                # `type_()`, methode Python renommee pour eviter le builtin
                # `type` - `_rule_name` ne connait que le nom de CLASSE ANTLR,
                # jamais renomme lui). Cas confirme sur BasketOID/MetaElemOID/
                # LanguageCode (models/IlisMeta16.ili, 1er bloc DOMAIN du
                # fichier) - sans cette correspondance, aucun des 3 domaines
                # (tous via type_(), ex. OID TEXT / TEXT*5) n'etait construit.
                instance = self.visit(content_node)
            else:
                target = "IlisMeta16.ModelData.NumType" if content_rule == "numeric" else "IlisMeta16.ModelData.EnumType"
                instance = self.visit_wrapped(content_node, target, rule_name)
            if not isinstance(instance, MetaInstance):
                continue
            if getattr(instance, "Name", None) is None:
                instance.Name = name_node.getText()
                self._maybe_register_symbol(instance)
            if mandatory and getattr(instance, "Mandatory", None) is None:
                instance.Mandatory = True
            if entry.parent and self._parent_stack:
                self.attachment.attach(
                    self._parent_stack[-1], entry.parent.role, instance,
                    association=entry.parent.association, role=entry.parent.role, rule=rule_name,
                )
            results.append(instance)

        if not results:
            return None
        return results[0] if len(results) == 1 else results

    def _build_domain_class_restriction(self, segment: list[Any], rule_name: str) -> MetaInstance:
        """DOMAIN X = CLASS RESTRICTION(A; B; ...); - la 5e alternative de
        domainDef(), inlinee directement dans son corps ANTLR (CLASS
        (RESTRICTION LPAR classOrAssociationRef (SEMI classOrAssociationRef)*
        RPAR)?, PAS un appel a une sous-regle dediee - confirme sur le code
        genere reel) et jusqu'ici jamais construite (Lot 28, trouve sur
        RoadTrafficCensus_V1_1.ili : "CHCantonCode_Extended = CLASS
        RESTRICTION(sAbroadCode; sCHCantonCode);"). RULE #4 : correspond a
        l'alternative "ClassType" du manuel (eCH-0031 V2.1.0, "ClassType =
        'CLASS' ['RESTRICTION' '(' ViewableRef {';' ViewableRef} ')'] | ...")
        - metaclasse cible non "ClassType" (absente d'ilismeta16-*.yml) mais
        IlisMeta16.ModelData.ReferenceType, MEME target que referenceAttr()
        (REFERENCE TO ...) : Class EXTENDS Type (confirme), donc utilisable
        directement comme role Type de AttrOrParamType, mais CETTE
        alternative grammaticale n'a pas de clause EXTERNAL (confirme sur le
        code ANTLR - contrairement a referenceAttr), donc External=False
        inconditionnellement. BaseClass attache via la MEME association que
        referenceAttr.BaseClass/roleDef.BaseClass (ClassRelatedType). Ne
        couvre que la 1ere classOrAssociationRef (base non restreinte) -
        meme limite deja documentee pour ces deux bindings."""
        instance = self.registry.new_instance("IlisMeta16.ModelData.ReferenceType")
        instance.External = False
        first_ref = next(
            (c for c in segment if isinstance(c, ParserRuleContext) and self._rule_name(c) == "classOrAssociationRef"),
            None,
        )
        if first_ref is not None:
            value = self.visit(first_ref)
            if value is not None:
                self.attachment.attach(
                    instance, "BaseClass", value, association="BaseClass", role="BaseClass", rule=rule_name,
                )
                if isinstance(value, ForwardRef):
                    self.forward_refs.register_pending(value, instance, "BaseClass")
        return instance

    def _build_enumeration_tree(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry) -> None:
        """Construit correctement l'arbre EnumNode d'un `enumeration()`
        (Lot 33 - bug reel trouve en elargissant le corpus XTF a
        ID65.1_KGS_PBC_V2_2 : "KGS_Kategorie : MANDATORY (A (A,
        verstaerkter_Schutz), B);" produisait TopNode=A/A.Sub=[B], perdant
        totalement les vrais enfants de A et confondant B - un FRERE de A
        au niveau racine - avec un enfant. Root cause documentee sur
        `enumElement` (spec/grammar/mapping/06_types.yml) : l'ancien
        mecanisme (chaque enumElement s'auto-attachant via
        `parent: {association: TopNode, role: TopNode}`) ne pouvait
        represent qu'UNE CHAINE LINEAIRE, jamais un arbre a embranchements,
        et ignorait la convention documentee dans `models/IlisMeta16.ili`
        (commentaire sur EnumNode) : "MetaElement.Name := 'TOP' for
        topnode" - un noeud racine SYNTHETIQUE, jamais un element reel.

        Distingue le niveau par la classe du PARENT courant
        (`_parent_stack[-1]`, deja pousse par `visit_wrapped` pour l'appel
        SOMMET, ou par `_build_instance` de l'enumElement englobant pour un
        appel IMBRIQUE - Sub-Enumeration) :
        - Appel SOMMET (parent = EnumType) : cree le noeud TOP synthetique,
          l'attache comme EnumType.TopNode, y attache CHAQUE enumElement de
          la liste plate comme enfant direct (role Node, association
          SubNode) - Order/Final s'appliquent a l'EnumType lui-meme.
        - Appel IMBRIQUE (parent = EnumNode, l'enumElement englobant) :
          attache directement les enumElement de cette Sub-Enumeration comme
          enfants de CE noeud (pas de TOP synthetique supplementaire -
          l'enumElement englobant sert deja de racine locale) - Final
          s'applique a ce noeud lui-meme (marque non extensible), Order est
          `not_applicable` a ce niveau (deja documente ainsi)."""
        parent_context = self._parent_stack[-1] if self._parent_stack else None
        is_top_level = isinstance(parent_context, MetaInstance) and parent_context._qualified_class == "IlisMeta16.ModelData.EnumType"

        if is_top_level:
            top = self.registry.new_instance("IlisMeta16.ModelData.EnumNode")
            top.Name = "TOP"
            self.attachment.attach(
                parent_context, "TopNode", top, association="TopNode", role="TopNode", rule=rule_name,
            )
            children_target = top
            final_target = parent_context
        else:
            children_target = parent_context
            final_target = parent_context

        if children_target is not None and ca.has_accessor(ctx, "enumElement"):
            for el_ctx in ca.call_list(ctx, "enumElement"):
                node = self.visit(el_ctx)
                if isinstance(node, MetaInstance):
                    self.attachment.attach(
                        children_target, "Node", node, association="SubNode", role="Node", rule=rule_name,
                    )

        construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()
        final_binding = entry.attribute_bindings.get("Final") if entry.attribute_bindings else None
        if final_target is not None and isinstance(final_binding, dict):
            final_value = self._resolve_binding_value(ctx, rule_name, "Final", final_binding, construction_ctx, consumed)
            if final_value:
                self.attachment.attach(final_target, "Final", final_value, rule=rule_name)
        order_binding = entry.attribute_bindings.get("Order") if entry.attribute_bindings else None
        if is_top_level and isinstance(order_binding, dict):
            order_value = self._resolve_binding_value(ctx, rule_name, "Order", order_binding, construction_ctx, consumed)
            if order_value:
                self.attachment.attach(parent_context, "Order", order_value, rule=rule_name)
        return None

    # ------------------------------------------------------------------
    # Strategie 2 : Conditional
    # ------------------------------------------------------------------
    def _build_conditional(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        for token_or_rule, branch in (entry.when_present or {}).items():
            if not ca.has_accessor(ctx, token_or_rule):
                continue
            node = ca.call(ctx, token_or_rule)
            if node is None:
                continue
            instance = self.registry.new_instance(branch.target)
            instance._source_ctx = ctx
            if branch.discriminant:
                setattr(instance, branch.discriminant.attribute, branch.discriminant.model_extra.get("value"))

            construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
            consumed: set[int] = set()
            self._push_construction_context(rule_name, instance)
            self._parent_stack.append(instance)
            try:
                if isinstance(node, ParserRuleContext):
                    # `token_or_rule` designe une VRAIE regle grammaticale
                    # (pas un simple token), avec son propre contenu structure
                    # decrit par SA PROPRE entree spec (ex. oIDType.numeric ->
                    # numeric(), Min/Max/Circular/Clockwise/Unit) - la visiter
                    # et fusionner son bag sur `instance`, meme mecanisme que
                    # visit_wrapped (wrap: explicite). CORRIGE (Lot 25) :
                    # avant, ce contenu n'etait jamais recupere du tout.
                    self._merge_bag_into_instance(instance, self.visit(node), rule_name)
                if entry.attribute_bindings:
                    self._apply_bindings(instance, ctx, rule_name, entry.attribute_bindings, construction_ctx, consumed)
            finally:
                self._parent_stack.pop()
                self._pop_construction_context()
            return instance

        # aucune branche when_present prise : pass-through pur (ex. term ->
        # term0 sans EQ GT, predicate -> factor sans NOT/DEFINED). Ne PAS
        # reutiliser _relay/son mecanisme de bag generique ici : les
        # attribute_bindings d'une regle Conditional (ex. term2.SubExpressions
        # = [predicate(0), predicate(1)] "multi: true") decrivent le contenu
        # de la branche MATCHED (deja traitee ci-dessus), pas une recette
        # generique de pass-through - les appliquer quand meme construisait
        # un bag residuel {'SubExpressions': [predicate(0)], '_relation_child':
        # None} au lieu de relayer purement vers l'unique enfant reel (trouve
        # sur models/IlisMeta16.ili, chaine term/term0/term1/term2 sans
        # operateur). Balayage pur des enfants non reclames, comme le fait
        # deja predicate -> factor avec succes (le seul enfant reel restant
        # non consomme est celui a relayer).
        consumed: set[int] = set()
        sweep_results = self._sweep_unclaimed_children(ctx, consumed)
        real_results = [v for _rule, v in sweep_results if v is not None]
        if real_results:
            return real_results[0] if len(real_results) == 1 else real_results
        return None

    # ------------------------------------------------------------------
    # Strategie 3 : Relay (Container / Dispatcher)
    # ------------------------------------------------------------------
    def _relay(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        # multi_declaration : cas particulier confirme UNIQUE parmi les 121
        # regles (comme topicDef/_build_multi_target, l'autre cas particulier
        # deja cable) - domainDef() boucle grammaticalement sur N
        # declarations de domaine partageant un seul mot-cle DOMAIN (confirme
        # sur le code genere reel, InterlisParser.py) ; aucun mecanisme
        # generique existant (bag/for_each) ne correle plusieurs accesseurs
        # differents (Name/type_/numeric/enumeration) a la MEME position -
        # voir _build_multi_declaration.
        if entry.multi_declaration:
            return self._build_multi_declaration(ctx, rule_name, entry)

        # enumeration() : 3e et dernier cas special parmi les 121 regles
        # (Lot 33, meme famille que multi_declaration/_build_multi_target) -
        # voir _build_enumeration_tree pour le detail du bug corrige.
        if rule_name == "enumeration":
            return self._build_enumeration_tree(ctx, rule_name, entry)

        # children: dispatch multi-visite (ex. definitions -> classDef*,
        # topicDef*, ...) - toutes les occurrences comptent, pas une seule
        # alternative.
        if entry.children:
            for child_rule in entry.children:
                if not ca.has_accessor(ctx, child_rule):
                    continue
                for node in ca.call_list(ctx, child_rule):
                    self.visit(node)
            return None

        # dispatches_to: une seule alternative grammaticale reellement
        # prise parmi les regles nommees (ex. classOrStructureDef). Si
        # AUCUNE ne matche, ne pas s'arreter la : certaines regles (ex.
        # attrTypeDef, dispatches_to=[attrType, lineType]) ont AUSSI leurs
        # propres alternatives directes decrites dans attribute_bindings
        # (numeric/enumeration/NUMERIC bare) - tomber dans le traitement
        # bag ci-dessous plutot que de renvoyer None prematurement.
        if entry.dispatches_to:
            for name in entry.dispatches_to:
                if ca.has_accessor(ctx, name):
                    node = ca.call(ctx, name)
                    if node is not None:
                        return self.visit(node) if isinstance(node, ParserRuleContext) else node.getText()

        # Ni children ni dispatches_to matche : calcule les attribute_bindings
        # propres (le cas echeant - notes seules type _dispatch ne
        # produisent rien), les rend disponibles au contexte de
        # construction (mecanisme field: null, ex. interlis2def.iliVersion
        # -> modeldef.iliVersion), PUIS balaie les enfants non reclames
        # (ex. interlis2def -> modeldef, jamais dans ses propres bindings).
        bag: dict[str, Any] = {}
        consumed: set[int] = set()
        construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
        if entry.attribute_bindings:
            for key, binding in entry.attribute_bindings.items():
                if not isinstance(binding, dict):
                    continue
                if "target" in binding:
                    bag[key] = self._build_nested(ctx, rule_name, key, binding, construction_ctx, consumed)
                    continue
                if not isinstance(binding.get("source"), dict):
                    continue  # note seule (ex. _dispatch) : rien a calculer
                bag[key] = self._resolve_binding_value(ctx, rule_name, key, binding, construction_ctx, consumed)

        real_values = {k: v for k, v in bag.items() if v is not None}

        # Une cle du bag peut elle-meme DEJA porter une instance concrete
        # (ex. binding `wrap:` sur une alternative numeric()/enumeration()
        # nue, voir domainDef._domain_content) plutot qu'une simple valeur
        # scalaire - dans ce cas c'est CETTE instance qui est le contenu reel
        # de la regle (les autres cles du bag, ex. Name/Mandatory, ne font
        # que la decrire) : memes regles d'application que pour un resultat
        # trouve par balayage (voir _apply_sibling_bag_values ci-dessous),
        # mais sans attendre le balayage puisque le noeud a deja ete consomme
        # par le calcul du binding lui-meme.
        # CORRIGE (audit NumType.Min/Max, Lot 25) : exclut les instances
        # "hollow" (ex. numeric()._refsys_clause -> NumsRefSys construite
        # inconditionnellement par _build_nested meme quand la clause de
        # reference n'est PAS presente dans le source, tous ses champs
        # restant None) - sans ce filtre, une telle instance creuse gagnait
        # a tort ce fast-path et etait renvoyee TELLE QUELLE au lieu du bag
        # dict, empechant visit_wrapped (qui a deja sa PROPRE logique de
        # filtrage des valeurs hollow, voir plus bas) de jamais l'atteindre -
        # perdant silencieusement Min/Max/Circular/Clockwise/Unit de TOUT
        # domaine/type numerique, y compris sur models/IlisMeta16.ili
        # lui-meme (ex. "Code = 0..255;").
        wrapped = [v for v in real_values.values() if isinstance(v, MetaInstance) and not self._is_hollow(v)]
        if len(wrapped) == 1:
            self._apply_sibling_bag_values(wrapped[0], real_values)
            return wrapped[0]

        if real_values:
            self._construction_stack.append({**construction_ctx, **real_values})
        try:
            sweep_results = self._sweep_unclaimed_children(ctx, consumed)
        finally:
            if real_values:
                self._construction_stack.pop()

        # Un enfant non reclame visite (ex. interlis2def -> modeldef) porte
        # generalement LE contenu reel de cette regle - prioritaire sur le
        # bag de valeurs propres (qui ne sert alors souvent qu'a alimenter
        # le contexte de construction, ex. iliVersion).
        real_results = [v for _rule, v in sweep_results if v is not None]
        if real_results:
            result = real_results[0] if len(real_results) == 1 else real_results
            if isinstance(result, MetaInstance) and real_values:
                self._apply_sibling_bag_values(result, real_values)
            return result

        if len(bag) == 1:
            (only_value,) = bag.values()
            return only_value
        if bag:
            return bag
        return None

    # ------------------------------------------------------------------
    # Strategie 4 : Reference (produit un ForwardRef, jamais resolu immediatement)
    # ------------------------------------------------------------------
    def _resolve_or_defer(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        # Extension propre au projet, deja documentee (voir 04_attributes.yml,
        # restrictedStructureRef._inline_type, status: not_applicable) :
        # restrictedStructureRef() a une alternative grammaticale type_()
        # ABSENTE du manuel officiel (type inline anonyme, ex. "TEXT*50")
        # en plus de structureRef()/ANYSTRUCTURE - ce n'est PAS un nom a
        # resoudre (ForwardRef sur ctx.getText() echouait toujours avec
        # BuildError "non resolue et non attribuable a un import" pour toute
        # occurrence reelle, ex. Holznutzungsbewilligung_V1_0.ili "TEXT*50").
        # Relais direct vers la construction du type inline plutot que la
        # resolution par nom.
        has_structure_ref = ca.has_accessor(ctx, "structureRef") and bool(ca.call_list(ctx, "structureRef"))
        if ca.has_accessor(ctx, "type_") and not has_structure_ref:
            node = ca.call(ctx, "type_")
            if node is not None:
                return self.visit(node)
        name = ctx.getText()
        # `resolves_to` est deja un nom court (ex. "Class") ; `target` (repli)
        # est un nom qualifie complet (ex. "IlisMeta16.ModelData.Class") - la
        # SymbolTable compare toujours contre le nom court de la classe
        # metamodele reelle de l'instance (_qualified_class), donc normaliser
        # ici plutot que de propager un format incoherent selon la source.
        hint = entry.resolves_to or entry.target
        if isinstance(hint, list):
            hint = [h.rsplit(".", 1)[-1] for h in hint]
        elif hint:
            hint = [hint.rsplit(".", 1)[-1]]
        else:
            hint = []
        expanded: list[str] = []
        for h in hint:
            expanded.extend(self._expand_kind_hint(h))
        return ForwardRef(
            name=name, resolves_to_hint=expanded or None, rule=rule_name, home_model=self._current_model_name(),
        )

    def _current_model_name(self) -> str | None:
        """Nom du MODEL englobant la construction en cours (parcourt
        `_parent_stack` depuis la fin) - utilise pour desambiguiser un nom
        court QUAND MEME cible present dans plusieurs modeles d'un fichier
        multi-MODEL (Lot 33 : "PointStructure" declare separement dans
        BaseModel_SectoralPlans_LV03_V1_4 ET _LV95_V1_4, meme fichier,
        MEME symbol_table depuis le fix multi-modeles du Lot 28 -
        auparavant invisible car un seul modele par fichier n'etait jamais
        construit)."""
        for inst in reversed(self._parent_stack):
            if inst._qualified_class == "IlisMeta16.ModelData.Model":
                return getattr(inst, "Name", None)
        return None

    def _expand_kind_hint(self, short_name: str) -> list[str]:
        """Un hint (`resolves_to`/`target`) peut nommer une classe metamodele
        ABSTRAITE (ex. "DomainType", isAbstract=true dans le XMI - AUCUNE
        instance n'est jamais litteralement de cette classe, toujours une
        sous-classe concrete comme EnumType/NumType/TextType) - la
        comparaison stricte `_qualified_class == hint` dans
        `SymbolTable.resolve` ne matchait donc JAMAIS pour `domainRef`
        (hint="DomainType"), rendant sa levee d'ambiguite totalement inerte
        (trouve sur models.geo.admin.ch : "Bodenbedeckungsart:
        Bodenbedeckungsart;", l'attribut ET son domaine EnumType partagent
        le meme nom court, jamais desambiguise). Expanse un hint abstrait en
        la liste de ses sous-classes concretes reelles ; un hint deja
        concret (ex. "Class") est retourne inchange."""
        qualified = next((qn for qn in self.schema.uml.qualified if qn.rsplit(".", 1)[-1] == short_name), None)
        if qualified is None:
            return [short_name]
        element = self.schema.uml.qualified[qualified]
        if not element.get("abstract"):
            return [short_name]
        descendants = [
            qn.rsplit(".", 1)[-1] for qn, el in self.schema.uml.qualified.items()
            if qualified in (el.get("all_superclasses") or [])
        ]
        return descendants or [short_name]

    # ------------------------------------------------------------------
    # Application des attribute_bindings
    # ------------------------------------------------------------------
    def _apply_bindings(
        self,
        instance: MetaInstance,
        ctx: ParserRuleContext,
        rule_name: str,
        bindings: dict[str, Any],
        construction_ctx: dict,
        consumed: set[int],
    ) -> None:
        for key, binding in bindings.items():
            if not isinstance(binding, dict):
                continue
            if binding.get("status") in ("not_applicable", "unresolved") and "source" not in binding and "target" not in binding:
                continue

            try:
                self._apply_one_binding(instance, ctx, rule_name, key, binding, construction_ctx, consumed)
            except BuildError:
                source = binding.get("source") if isinstance(binding.get("source"), dict) else {}
                if source.get("optional") or binding.get("optional"):
                    # Binding marque optionnel dont la resolution/attachement
                    # a echoue (ex. modeldef.imports : construction Import
                    # par nom, hors perimetre du moteur generique actuel -
                    # voir plan de conception, "pas ferme pour toujours").
                    # Ne bloque pas le reste de la construction.
                    continue
                raise

    def _apply_one_binding(
        self,
        instance: MetaInstance,
        ctx: ParserRuleContext,
        rule_name: str,
        key: str,
        binding: dict,
        construction_ctx: dict,
        consumed: set[int],
    ) -> None:
        if "for_each" in binding:
            self._apply_for_each_binding(instance, ctx, rule_name, key, binding, construction_ctx, consumed)
            return

        if "target" in binding:
            nested = self._build_nested(ctx, rule_name, key, binding, construction_ctx, consumed)
            if nested is None:
                return
            self.attachment.attach(
                instance, key, nested,
                association=binding.get("association"), role=binding.get("role"), rule=rule_name,
            )
            return

        value = self._resolve_binding_value(ctx, rule_name, key, binding, construction_ctx, consumed)
        if key.startswith("_"):
            # Cle interne/documentaire (convention du projet, ex. _dispatch :
            # decrit les alternatives grammaticales pour la lisibilite du
            # mapping mais chaque alternative reelle s'auto-construit via sa
            # propre regle/binding - jamais un vrai nom d'attribut/role.
            # Le calcul ci-dessus est conserve (effets de bord eventuels,
            # ex. propagation par contexte de construction), seul l'attach
            # litteral est evite - meme convention que _attach_unclaimed_results
            # (trouve en corrigeant `factor.dispatch` sur models/IlisMeta16.ili :
            # tentait d'attacher sous la cle 'dispatch', absente du metamodele).
            return
        if value is None:
            # Rien a attacher - soit un binding sans source (note seule),
            # soit une valeur absente (optional), soit une regle visitee
            # pour ses seuls effets de bord (ex. topicDef.definitions :
            # chaque classDef/etc. s'auto-attache via SON PROPRE parent:,
            # cette cle-ci n'a rien a recevoir en retour - voir sa note).
            return
        if isinstance(value, list) and not value:
            # Liste multi vide (rien present dans le .ili) : rien a attacher.
            return
        if isinstance(value, ForwardRef):
            self.attachment.attach(
                instance, key, value,
                association=binding.get("association"), role=binding.get("role"), rule=rule_name,
            )
            self.forward_refs.register_pending(value, instance, self._resolved_field_name(instance, key, binding))
            return
        self.attachment.attach(
            instance, key, value,
            association=binding.get("association"), role=binding.get("role"), rule=rule_name,
        )

    def _resolved_field_name(self, instance: MetaInstance, key: str, binding: dict) -> str:
        role = binding.get("role")
        if role:
            return role
        element = self.schema.uml.qualified.get(instance._qualified_class, {})
        if self.schema.uml.attribute_exists(element, key):
            return key
        found = self.attachment._find_association_for_role(instance._qualified_class, key)
        return found[1] if found else key

    def _build_nested(
        self, ctx: ParserRuleContext, rule_name: str, key: str, binding: dict, construction_ctx: dict, consumed: set[int]
    ) -> MetaInstance | None:
        target = binding["target"]
        sub_bindings = {
            k: v for k, v in binding.items()
            if k not in ("target", "note", "association", "role") and isinstance(v, dict) and "source" in v
        }
        if not sub_bindings:
            # target: sans aucun sous-binding source: reel (ex.
            # modeldef/topicDef.default_class_oid) : la note associee
            # documente explicitement une logique procedurale non couverte
            # par le moteur generique (ex. "appliquer apres coup a chaque
            # Class du topic qui ne definit pas son propre OID") - pas une
            # construction imbriquee standard, on ne fabrique pas
            # d'instance creuse pour ne pas l'attacher a tort.
            return None
        instance = self.registry.new_instance(target)
        instance._source_ctx = ctx
        self._parent_stack.append(instance)
        try:
            self._apply_bindings(instance, ctx, rule_name, sub_bindings, construction_ctx, consumed)
        except BuildError:
            # Une construction imbriquee (ex. attrTypeDef._collection ->
            # MultiValue) represente UNE alternative grammaticale parmi
            # d'autres, pas une structure toujours presente - si un de ses
            # sous-bindings requis (marqueur discriminant, ex. BAG|LIST)
            # echoue, c'est le signe que cette alternative n'a simplement
            # pas ete prise ici, pas une vraie erreur. Abandonne toute la
            # construction imbriquee plutot que de la laisser remonter
            # (meme logique que Conditional : branche non applicable ->
            # rien construit).
            return None
        finally:
            self._parent_stack.pop()
        return instance

    def _apply_for_each_binding(
        self,
        instance: MetaInstance,
        ctx: ParserRuleContext,
        rule_name: str,
        key: str,
        binding: dict,
        construction_ctx: dict,
        consumed: set[int],
    ) -> None:
        """Construit UNE instance de `target` par element d'une liste
        resolue (`for_each:`), au lieu d'une seule instance - necessaire
        pour les regles ou une boucle grammaticale (ex. modeldef.imports :
        une clause IMPORTS repetee) doit produire N instances liees
        distinctes (ex. N associations Import), pas une seule construction
        imbriquee standard (voir _build_nested, qui suppose une seule
        instance par binding).

        Chaque sous-binding peut lire l'element courant de la boucle via
        `source: {field: null, context_key: '__item__'}` (mecanisme
        field: null generique, juste sous une cle synthetique dediee a
        cette boucle - meme principe que le contexte de construction
        propage ailleurs, ex. interlis2def.iliVersion)."""
        items = self._resolve_binding_value(ctx, rule_name, key, {"source": binding["for_each"]}, construction_ctx, consumed)
        if items is None:
            return
        if not isinstance(items, list):
            items = [items]
        if not items:
            return

        target = binding["target"]
        sub_bindings = {
            k: v for k, v in binding.items()
            if k not in ("for_each", "target", "note", "association", "role") and isinstance(v, dict)
        }
        for item in items:
            item_ctx = {**construction_ctx, "__item__": item}
            built = self.registry.new_instance(target)
            built._source_ctx = ctx
            self._apply_bindings(built, ctx, rule_name, sub_bindings, item_ctx, consumed)
            if binding.get("association"):
                self.attachment.attach(
                    instance, key, built,
                    association=binding.get("association"), role=binding.get("role"), rule=rule_name,
                )
            else:
                # Pas de role de collection cote metamodele (ex. Import :
                # association pure ImportingP<->ImportedP, aucune association
                # inverse "Model.Imports" declaree dans ilismeta16-associations.yml -
                # confirme par recherche exhaustive) : conserve quand meme les
                # instances construites, sous la cle brute du binding, pour
                # qu'elles restent atteignables (sinon perdues au garbage
                # collector, aucune autre reference ne les retient).
                current = getattr(instance, key, None)
                if current is None:
                    current = []
                    setattr(instance, key, current)
                current.append(built)

    def _resolve_binding_value(
        self, ctx: ParserRuleContext, rule_name: str, key: str, binding: dict, construction_ctx: dict, consumed: set[int]
    ) -> Any:
        source = binding.get("source")
        if not isinstance(source, dict):
            return None
        # Ne pre-marquer "consomme" que si l'alternative numerotee (le cas
        # echeant) s'applique reellement a CE ctx : plusieurs alternatives
        # grammaticales numerotees d'une meme regle Conditional peuvent
        # partager le meme nom d'accesseur pour des usages differents (ex.
        # predicate : alt1 = factor nu, alt3 = DEFINED LPAR factor RPAR -
        # meme accesseur "factor"). Sans ce garde, l'alternative NON prise
        # marquait quand meme le noeud comme consomme, l'excluant a tort du
        # balayage des enfants non reclames (_sweep_unclaimed_children) et
        # laissant un bag residuel {SubExpression: None, _defined_factor:
        # None} remonter a la place de la vraie instance (trouve sur un
        # predicate nu dans models/IlisMeta16.ili).
        alt = source.get("alt")
        if alt is None or _alt_matches(ctx, alt):
            for name in self._accessor_names_in_source(source):
                if ca.has_accessor(ctx, name):
                    for node in ca.call_list(ctx, name):
                        consumed.add(id(node))
        rule_map = binding.get("rule") if isinstance(binding.get("rule"), dict) else binding.get("mapping")
        wrap_map = binding.get("wrap") if isinstance(binding.get("wrap"), dict) else None
        return resolve_source(
            ctx, source, rule=rule_name, construction_context=construction_ctx, builder=self,
            rule_map=rule_map, binding_key=key, wrap_map=wrap_map,
        )

    def visit_wrapped(self, node: ParserRuleContext, target: str, rule_name: str) -> MetaInstance:
        """Visite une regle-bag (Container/ValueObject, ex. numeric()/
        enumeration()) en l'enveloppant dans une instance typee du
        metamodele (ex. NumType/EnumType) - utilise quand une regle
        englobante precise (ex. attrTypeDef.Type) sait quelle classe cible
        ce bag represente vraiment.

        L'instance est creee et poussee sur la pile de construction AVANT
        la visite, pas apres : certains enfants de la regle-bag s'auto-
        attachent via leur PROPRE `parent:` PENDANT la visite (ex.
        enumElement -> association TopNode/SubNode). Envelopper apres coup
        les aurait attaches au mauvais parent (celui deja au sommet de la
        pile a ce moment, ex. l'AttrOrParam englobant) plutot qu'a cette
        nouvelle instance."""
        instance = self.registry.new_instance(target)
        self._parent_stack.append(instance)
        try:
            bag = self.visit(node)
        finally:
            self._parent_stack.pop()

        self._merge_bag_into_instance(instance, bag, rule_name)
        return instance

    def _merge_bag_into_instance(self, instance: MetaInstance, bag: Any, rule_name: str) -> None:
        """Fusionne un bag dict (resultat de `_relay` sur une regle Container,
        ex. numeric()/enumeration()) sur `instance` - attache chaque champ non
        vide/non hollow, resout les ForwardRef en attente. Facteur commun
        entre `visit_wrapped` (wrap: explicite) et `_build_conditional`
        (branche `when_present` dont le token/regle EST elle-meme une regle
        avec son propre contenu structure, ex. oIDType.numeric -> numeric()) -
        CORRIGE (audit NumType.Min/Max, Lot 25) : avant, seul `visit_wrapped`
        appliquait ce filtre hollow ; `_build_conditional` ne visitait jamais
        le noeud de la branche matched du tout, perdant tout le contenu
        propre de numeric()/textType() (Min/Max/Circular/Clockwise/Unit) pour
        toute regle OID numerique/textuelle (ex. `I32OID = OID
        0..2147483647;`, namespace INTERLIS predefini)."""
        if not isinstance(bag, dict):
            return
        for field, value in bag.items():
            if field == "Elements":
                continue  # deja attache via enumElement.parent: pendant la visite ci-dessus
            if value is None or (isinstance(value, list) and not value) or self._is_hollow(value):
                # None/liste vide/"hollow" (instance imbriquee dont tous les
                # champs sont None/vides, ex. numeric._refsys_clause quand
                # aucune clause de reference n'est presente) : rien a attacher.
                continue
            if isinstance(value, ForwardRef):
                self.attachment.attach(instance, field, value, rule=rule_name)
                self.forward_refs.register_pending(value, instance, self._resolved_field_name(instance, field, {}))
                continue
            self.attachment.attach(instance, field, value, rule=rule_name)

    @staticmethod
    def _is_hollow(value: Any) -> bool:
        if not isinstance(value, MetaInstance):
            return False
        fields = {**value.__dict__, **(value.model_extra or {})}
        return all(v is None or v == [] for k, v in fields.items() if not k.startswith("_"))
        return instance

    @staticmethod
    def _accessor_names_in_source(source: dict) -> list[str]:
        names: list[str] = []
        field = source.get("field")
        if field:
            names.extend(n.strip() for n in field.split("|"))
        anchor = source.get("anchor")
        if anchor:
            names.append(anchor)
        if "join" in source:
            for k, v in source.items():
                if v is None and k not in ("field", "optional"):
                    names.append(k)
        return names

    def _resolve_discriminant(self, ctx, rule_name, discriminant, construction_ctx, consumed) -> Any:
        extra = discriminant.model_extra or {}
        if "value" in extra and not isinstance(extra["value"], dict):
            return extra["value"]
        if isinstance(extra.get("value"), dict):
            nested = extra["value"]
            source = nested.get("source")
            if isinstance(source, dict):
                for name in self._accessor_names_in_source(source):
                    if ca.has_accessor(ctx, name):
                        for node in ca.call_list(ctx, name):
                            consumed.add(id(node))
                rule_map = nested.get("mapping") or nested.get("rule")
                return resolve_source(
                    ctx, source, rule=rule_name, construction_context=construction_ctx, builder=self, rule_map=rule_map,
                )
        if extra.get("dynamic") and isinstance(extra.get("rule"), dict):
            for token_or_rule, value in extra["rule"].items():
                if ca.has_accessor(ctx, token_or_rule) and ca.is_present(ctx, token_or_rule):
                    return value
        return None

    # ------------------------------------------------------------------
    # Contexte de construction propage (mecanisme field: null)
    # ------------------------------------------------------------------
    def _push_construction_context(self, rule_name: str, instance: MetaInstance) -> None:
        ctx_dict = dict(self._construction_stack[-1]) if self._construction_stack else {}
        ctx_dict[rule_name] = instance
        name = getattr(instance, "Name", None)
        if name is not None:
            ctx_dict[f"{rule_name}.Name"] = name
        self._construction_stack.append(ctx_dict)

    def _pop_construction_context(self) -> None:
        self._construction_stack.pop()

    # ------------------------------------------------------------------
    # Table de symboles
    # ------------------------------------------------------------------
    def _maybe_register_symbol(self, instance: MetaInstance) -> None:
        element = self.schema.uml.qualified.get(instance._qualified_class, {})
        if not self.schema.uml.attribute_exists(element, "Name"):
            return
        name = getattr(instance, "Name", None)
        if not name:
            return
        qualified = self._qualify_name(name)
        self.symbol_table.register(qualified, instance)

    def _qualify_name(self, name: str) -> str:
        parts = [getattr(inst, "Name", None) for inst in self._parent_stack if getattr(inst, "Name", None)]
        parts.append(name)
        return ".".join(parts)

    def _apply_sibling_bag_values(self, result: MetaInstance, real_values: dict) -> None:
        """CORRIGE (audit corpus reel models.geo.admin.ch, 2026-08-03) : une
        regle Container avec ses PROPRES attribute_bindings (ex.
        domainDef.Name/Mandatory) dont le contenu reel est une AUTRE valeur
        du meme bag (ex. domainDef._domain_content, wrap: sur numeric()/
        enumeration() nus) ou vient d'un enfant non reclame (ex.
        enumerationType -> EnumType, via le dispatcher type_()) perdait ces
        valeurs - poussees seulement dans le contexte de construction
        (mecanisme field: null), jamais appliquees sur l'instance retournee
        elle-meme, meme quand cette instance HERITE reellement le champ
        concerne (ex. EnumType.Name via MetaElement - confirme
        ilismeta16-classes.yml, DomainType.attributes.inherited.Name).
        Consequence concrete trouvee : un DOMAIN nomme via une enumeration
        nue (ex. "DOMAIN CodeWeekDayType = (MON, TUE, ...);", sans le
        mot-cle ENUM) produisait un EnumType SANS Name -> jamais enregistre
        dans la SymbolTable -> toute reference ulterieure par nom
        (domainRef/restrictedStructureRef) echouait avec BuildError "non
        resolue et non attribuable a un import". N'applique que les cles
        reellement own/inherited de la classe metamodele de `result`,
        jamais deja renseignees."""
        element = self.schema.uml.qualified.get(result._qualified_class, {})
        registered_name = False
        for key, value in real_values.items():
            if value is result:
                continue
            if getattr(result, key, None) is not None:
                continue
            if not self.schema.uml.attribute_exists(element, key):
                continue
            setattr(result, key, value)
            if key == "Name":
                registered_name = True
        if registered_name:
            self._maybe_register_symbol(result)

    # ------------------------------------------------------------------
    # Balayage des enfants non reclames par attribute_bindings (ex.
    # modeldef -> definitions, jamais mentionne dans les bindings de
    # modeldef - voir plan de conception, mecanisme necessaire car la
    # relation structurelle passe par `parent:` sur la regle enfant, pas
    # par un binding explicite sur la regle englobante).
    # ------------------------------------------------------------------
    def _sweep_unclaimed_children(self, ctx: ParserRuleContext, consumed: set[int]) -> list[tuple[str, Any]]:
        results = []
        for child in ctx.children or []:
            if isinstance(child, ParserRuleContext) and id(child) not in consumed:
                results.append((self._rule_name(child), self.visit(child)))
        return results

    def _attach_unclaimed_results(self, instance: MetaInstance, sweep_results: list[tuple[str, Any]], rule_name: str) -> None:
        """Un enfant non reclame par attribute_bindings dont la regle n'a
        PAS de `parent:` propre (donc ne s'est pas deja auto-attache) mais
        produit un resultat concret : tenter de le rattacher via
        l'association qui relie les deux classes connues (ex. attrTypeDef
        -> AttrOrParamType.Type, voir AttachmentResolver.find_association_connecting).
        Best-effort : silencieux si aucune association ne relie les deux
        classes (le resultat reste alors seulement un effet de bord deja
        produit, ex. enregistrement dans la table de symboles)."""
        for child_rule, value in sweep_results:
            if value is None:
                continue
            child_entry = self.spec.get(child_rule)
            if child_entry is not None and child_entry.parent:
                continue  # deja auto-attache via son propre parent:
            if isinstance(value, dict):
                # La regle non reclamee est un bag (Container/Dispatcher a
                # plusieurs cles, ex. attrTypeDef -> {Mandatory, Type,
                # _collection}) : chaque cle non vide est attachee
                # individuellement sur `instance` par son propre nom (meme
                # mecanisme que wrap_bag_as_instance, mais sur l'instance
                # ENGLOBANTE existante plutot que sur une instance neuve).
                # Si une cle ne s'attache pas sur `instance` (ex. Mandatory,
                # documente comme ne concernant PAS AttrOrParam mais le Type
                # concret produit a cote dans le meme bag - attrTypeDef.
                # Mandatory), retenter sur une valeur MetaInstance soeur du
                # meme bag avant d'abandonner.
                siblings = [v for v in value.values() if isinstance(v, MetaInstance)]
                has_unresolved_sibling = any(isinstance(v, ForwardRef) for v in value.values())
                for key, sub_value in value.items():
                    if sub_value is None or (isinstance(sub_value, list) and not sub_value) or self._is_hollow(sub_value):
                        continue
                    if key.startswith("_"):
                        # Cle interne (convention du projet, ex. _collection,
                        # _refsys_clause) : jamais un vrai nom d'attribut/role,
                        # ne PAS tenter attach(instance, "_collection", ...)
                        # (echouerait toujours par construction) - chercher
                        # directement l'association reliant les deux classes
                        # CONNUES (meme mecanisme que pour une MetaInstance
                        # non reclamee au niveau superieur). Best-effort,
                        # silencieux si aucune association ne convient.
                        if isinstance(sub_value, MetaInstance):
                            found = self.attachment.find_association_connecting(
                                instance._qualified_class, sub_value._qualified_class
                            )
                            if found is not None:
                                _assoc_name, role, upper = found
                                self.attachment._set_field(instance, role, sub_value, upper)
                        continue
                    attached_on = None
                    attached_field = key
                    try:
                        self.attachment.attach(instance, key, sub_value, rule=rule_name)
                        attached_on = instance
                        attached_field = self._resolved_field_name(instance, key, {})
                    except BuildError:
                        for sibling in siblings:
                            if sibling is sub_value:
                                continue
                            try:
                                self.attachment.attach(sibling, key, sub_value, rule=rule_name)
                                attached_on = sibling
                                attached_field = self._resolved_field_name(sibling, key, {})
                                break
                            except BuildError:
                                continue
                        if attached_on is None:
                            # CORRIGE (Lot 29, regression trouvee sur models/
                            # IlisMeta16.ili apres le fix attrTypeDef.dispatches_to
                            # ci-dessus - "[roleDef] impossible d'attacher 'Type'
                            # sur Role") : `attach()` (ci-dessus) exige un role
                            # litteralement nomme `key` (ici "Type") sur une
                            # association connectant les deux classes - or
                            # roleDef() appelle aussi attrTypeDef() (role type
                            # inline), et AUCUNE association Role<->(Class ou
                            # DomainType) ne s'appelle "Type" (role nomme
                            # differemment, ex. BaseClass). Repli generique par
                            # CLASSE CONNUE (meme mecanisme que le bloc
                            # `isinstance(value, MetaInstance)`/`ForwardRef` plus
                            # bas dans cette methode, pour un enfant non reclame
                            # BRUT) - applique ici pour une valeur nichee dans un
                            # bag, pour un `sub_value` MetaInstance OU ForwardRef
                            # (hint de classe cible via resolves_to_hint).
                            candidate_classes: list[str] = []
                            if isinstance(sub_value, MetaInstance):
                                candidate_classes = [sub_value._qualified_class]
                            elif isinstance(sub_value, ForwardRef):
                                hints = sub_value.resolves_to_hint if isinstance(sub_value.resolves_to_hint, list) else (
                                    [sub_value.resolves_to_hint] if sub_value.resolves_to_hint else []
                                )
                                candidate_classes = [
                                    qn for hint in hints for qn in self.schema.uml.qualified
                                    if qn.rsplit(".", 1)[-1] == hint
                                ]
                            for qualified_class in candidate_classes:
                                found = self.attachment.find_association_connecting(instance._qualified_class, qualified_class)
                                if found is not None:
                                    _assoc_name, role, upper = found
                                    self.attachment._set_field(instance, role, sub_value, upper)
                                    attached_on = instance
                                    attached_field = role
                                    break
                        if attached_on is None:
                            if has_unresolved_sibling and not isinstance(sub_value, ForwardRef):
                                # Best-effort (docstring de cette methode) :
                                # une cle soeur du bag (ex. attrTypeDef.Mandatory,
                                # destinee au Type produit a cote) n'a nulle part
                                # ou s'attacher CE ctx-ci parce que ce Type est
                                # encore un ForwardRef non resolu (ex. reference
                                # a un DOMAIN nomme existant, potentiellement
                                # PARTAGE entre plusieurs usages de l'attribut -
                                # ex. "Owner: MANDATORY Owner;" - domaine et
                                # attribut homonymes, Lot 29). Appliquer Mandatory
                                # sur l'instance PARTAGEE une fois resolue serait
                                # incertain (quel usage aurait raison si
                                # plusieurs different ?) - abandonne silencieusement
                                # plutot que de faire planter tout le build pour
                                # une info secondaire non critique. Un `sub_value`
                                # qui est LUI-MEME le ForwardRef non resolu (ex.
                                # Type) doit en revanche toujours lever - une
                                # reference cassee ne doit jamais etre masquee
                                # (RULE #5), voir le `raise` ci-dessous.
                                continue
                            raise
                    if attached_on is not None and isinstance(sub_value, ForwardRef):
                        # CORRIGE (Lot 29) : un ForwardRef niche DANS le bag
                        # (ex. attrTypeDef.Type quand attrType() dispatche vers
                        # domainRef(), reference a un domaine nomme existant)
                        # n'etait jamais enregistre pour resolution differee -
                        # contrairement au cas ForwardRef "nu" (non imbrique
                        # dans un dict, deja gere plus bas dans cette methode) -
                        # il restait un ForwardRef littéral, jamais remplace par
                        # l'instance reelle ni par UnresolvedNamedReference,
                        # silencieusement, `resolve_all()` ne le voyant jamais.
                        self.forward_refs.register_pending(sub_value, attached_on, attached_field)
                continue
            if isinstance(value, list):
                continue
            if isinstance(value, ForwardRef):
                # CORRIGE (audit multi-fichiers, 2026-08-03) : une regle
                # Reference-kind (ex. domainRef/classRef) visitee comme enfant
                # non reclame (ex. attrTypeDef -> attrType -> domainRef,
                # jamais capture par un binding explicite sur attributeDef -
                # confirme, attributeDef.attribute_bindings n'a pas de cle
                # Type) produit un ForwardRef, pas encore une MetaInstance -
                # le controle `isinstance(value, MetaInstance)` ci-dessous
                # l'ignorait silencieusement, perdant l'attribut Type de
                # TOUTE reference de domaine/classe utilisee comme type
                # d'attribut (ex. "Kind: MANDATORY Base.PersonKind;"), locale
                # ou cross-fichier. Meme mecanisme d'association-par-classe
                # que pour une MetaInstance ci-dessous, mais via le hint de
                # classe cible du ForwardRef (resolves_to_hint, nom court -
                # retrouve son nom qualifie complet dans le schema) puisque
                # la vraie classe n'est pas encore connue avant resolution.
                hints = value.resolves_to_hint if isinstance(value.resolves_to_hint, list) else (
                    [value.resolves_to_hint] if value.resolves_to_hint else []
                )
                for hint in hints:
                    qualified_hint = next(
                        (qn for qn in self.schema.uml.qualified if qn.rsplit(".", 1)[-1] == hint), None
                    )
                    if qualified_hint is None:
                        continue
                    found = self.attachment.find_association_connecting(instance._qualified_class, qualified_hint)
                    if found is not None:
                        _assoc_name, role, upper = found
                        self.attachment._set_field(instance, role, value, upper)
                        self.forward_refs.register_pending(value, instance, role)
                        break
                continue
            if not isinstance(value, MetaInstance):
                continue
            found = self.attachment.find_association_connecting(instance._qualified_class, value._qualified_class)
            if found is not None:
                _assoc_name, role, upper = found
                self.attachment._set_field(instance, role, value, upper)
