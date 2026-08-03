"""Moteur generique du ModelBuilder.

InterlisModelBuilder(InterlisParserVisitor) : AUCUNE methode visitXxx
specifique a une regle. visit(ctx) generique derive le nom de regle depuis
type(ctx).__name__, charge l'entree spec/grammar/mapping/*.yml
correspondante, et applique une des 4 strategies d'execution selon `kind`
(voir le plan de conception du ModelBuilder)."""
from pathlib import Path
from typing import Any

from antlr4 import ParserRuleContext

from interlis.antlr.InterlisParser import InterlisParser
from interlis.antlr.InterlisParserVisitor import InterlisParserVisitor
from interlis.builder import context_access as ca
from interlis.builder.attach import AttachmentResolver
from interlis.builder.errors import BuildError
from interlis.builder.forward_refs import ForwardRef, ForwardRefResolver, SymbolTable
from interlis.builder.source_resolver import resolve_source
from interlis.metamodel.instance import MetaInstance
from interlis.metamodel.registry import MetamodelRegistry
from interlis.metamodel.uml_schema import MetamodelSchema
from interlis.spec.models import SpecEntry
from interlis.spec.spec_index import load_spec

class InterlisModelBuilder(InterlisParserVisitor):
    def __init__(self, mappings_dir: Path, spec_dir: Path):
        self.schema = MetamodelSchema.load(mappings_dir)
        self.registry = MetamodelRegistry.build(self.schema)
        self.spec: dict[str, SpecEntry] = load_spec(spec_dir)
        self.attachment = AttachmentResolver(self.schema.uml)
        self.symbol_table = SymbolTable()
        self.forward_refs = ForwardRefResolver(self.symbol_table)
        self.parser_symbolic_names = InterlisParser.symbolicNames
        self._construction_stack: list[dict] = []
        self._parent_stack: list[MetaInstance] = []

    # ------------------------------------------------------------------
    # Point d'entree public
    # ------------------------------------------------------------------
    def build(self, tree: ParserRuleContext) -> Any:
        result = self.visit(tree)
        self.forward_refs.resolve_all()
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
    # Strategie 2 : Conditional
    # ------------------------------------------------------------------
    def _build_conditional(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        for token_or_rule, branch in (entry.when_present or {}).items():
            if not ca.has_accessor(ctx, token_or_rule):
                continue
            if not ca.is_present(ctx, token_or_rule):
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
                if entry.attribute_bindings:
                    self._apply_bindings(instance, ctx, rule_name, entry.attribute_bindings, construction_ctx, consumed)
            finally:
                self._parent_stack.pop()
                self._pop_construction_context()
            return instance

        # aucune branche when_present prise : pass-through pur (ex. term ->
        # term0 sans EQ GT, predicate -> factor sans NOT/DEFINED).
        return self._relay(ctx, rule_name, entry)

    # ------------------------------------------------------------------
    # Strategie 3 : Relay (Container / Dispatcher)
    # ------------------------------------------------------------------
    def _relay(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
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
            return real_results[0] if len(real_results) == 1 else real_results

        if len(bag) == 1:
            (only_value,) = bag.values()
            return only_value
        if bag:
            return bag
        return None

    # ------------------------------------------------------------------
    # Strategie 4 : Reference (produit un ForwardRef, jamais resolu immediatement)
    # ------------------------------------------------------------------
    def _resolve_or_defer(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry) -> ForwardRef:
        name = ctx.getText()
        return ForwardRef(name=name, resolves_to_hint=entry.resolves_to or entry.target, rule=rule_name)

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

        if not isinstance(bag, dict):
            return instance
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
        return instance

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
                for key, sub_value in value.items():
                    if sub_value is None or (isinstance(sub_value, list) and not sub_value) or self._is_hollow(sub_value):
                        continue
                    try:
                        self.attachment.attach(instance, key, sub_value, rule=rule_name)
                    except BuildError:
                        for sibling in siblings:
                            if sibling is sub_value:
                                continue
                            try:
                                self.attachment.attach(sibling, key, sub_value, rule=rule_name)
                                break
                            except BuildError:
                                continue
                        else:
                            raise
                continue
            if isinstance(value, list):
                continue
            if not isinstance(value, MetaInstance):
                continue
            found = self.attachment.find_association_connecting(instance._qualified_class, value._qualified_class)
            if found is not None:
                _assoc_name, role, upper = found
                self.attachment._set_field(instance, role, value, upper)
