"""Generic ModelBuilder engine.

InterlisModelBuilder(InterlisParserVisitor) has NO rule-specific visitXxx
method. The generic visit(ctx) derives the rule name from
type(ctx).__name__, loads the matching spec/grammar/mapping/*.yml entry,
and applies one of 4 execution strategies depending on `kind`.

VIEW/OID/TRANSLATION OF are mixed in from view_mixin.py/oid_mixin.py/
translation_mixin.py - this file stays one unit by necessity.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNode

from interlis.antlr.InterlisParser import InterlisParser
from interlis.antlr.InterlisParserVisitor import InterlisParserVisitor
from interlis.builder import context_access as ca
from interlis.builder.attach import AttachmentResolver
from interlis.builder.context_mixin import _ContextMixin
from interlis.builder.errors import BuildError
from interlis.builder.forward_refs import ForwardRef, ForwardRefResolver, SymbolTable
from interlis.builder.oid_mixin import _OidMixin
from interlis.builder.repository import ModelRepository
from interlis.builder.source_resolver import resolve_source
from interlis.builder.translation_mixin import _TranslationMixin
from interlis.builder.view_mixin import _ViewBuildingMixin
from interlis.metamodel.instance import MetaInstance
from interlis.metamodel.registry import MetamodelRegistry
from interlis.metamodel.uml_schema import MetamodelSchema
from interlis.spec.models import SpecEntry
from interlis.spec.spec_index import load_spec


class InterlisModelBuilder(_ViewBuildingMixin, _OidMixin, _TranslationMixin, _ContextMixin, InterlisParserVisitor):
    def __init__(self, mappings_dir: Path, spec_dir: Path, *, repository: ModelRepository | None = None):
        schema = MetamodelSchema.load(mappings_dir)
        registry = MetamodelRegistry.build(schema)
        spec: dict[str, SpecEntry] = load_spec(spec_dir)
        attachment = AttachmentResolver(schema.uml)
        self._init_shared(schema, registry, spec, attachment, repository)

    @classmethod
    def _from_shared(cls, schema, registry, spec, attachment, repository) -> "InterlisModelBuilder":
        """Build a sub-builder for an imported file (see ModelRepository).

        Reuses the session's shared components rather than reloading -
        critical for the dynamic Pydantic classes (MetamodelRegistry) to
        stay the same Python objects across files.
        """
        self = cls.__new__(cls)
        self._init_shared(schema, registry, spec, attachment, repository)
        return self

    def _init_shared(self, schema, registry, spec, attachment, repository) -> None:
        self.schema = schema
        self.registry = registry
        self.spec = spec
        self.attachment = attachment
        # A "bare" ModelRepository (no search directory) always backs the
        # predefined INTERLIS model (see repository.py, _BUILTIN_SOURCES) -
        # not really cross-file resolution (no disk consulted beyond the
        # embedded text), so it stays active even without an explicit
        # --repo/repository=... REAL cross-file resolution (search
        # directories supplied by the caller) stays opt-in as before.
        self.repository = repository if repository is not None else ModelRepository([])
        self.symbol_table = SymbolTable()
        self.forward_refs = ForwardRefResolver(self.symbol_table)
        self.parser_symbolic_names = InterlisParser.symbolicNames
        self._construction_stack: list[dict] = []
        self._parent_stack: list[MetaInstance] = []
        # eCH-0117 meta-attribute capture (see build()/
        # _attach_pending_meta_attributes) - empty unless build() is
        # called with meta_attributes=... (root file only; an imported
        # file's own comments aren't captured yet, see build()'s docstring).
        self._pending_meta_attributes: list[tuple[int, str, str]] = []
        self._meta_attribute_index = 0
        # `MANDATORY <named domain>;` (e.g. "Geom: MANDATORY Coord2D;") -
        # see _apply_pending_mandatory_overrides. AttrOrParam instances
        # whose own MANDATORY couldn't attach directly because their Type
        # was still an unresolved ForwardRef to a (possibly SHARED) named
        # domain at construction time - resolved AFTER
        # forward_refs.resolve_all(), same "record during construction,
        # apply after resolution" pattern as _pending_view_all_of below.
        self._pending_mandatory_overrides: list[MetaInstance] = []
        # "ATTRIBUTE ALL OF <Name>;" (see _expand_view_all_of) needs
        # RenamedBaseView.BaseView already resolved to a real Class -
        # still a ForwardRef at construction time whenever the base is
        # itself forward-referenced (a later TOPIC, or an imported model
        # resolved via ModelRepository), since forward_refs.resolve_all()
        # only runs once at the END of build(). Recorded here during
        # construction, actually expanded in build() AFTER resolve_all().
        self._pending_view_all_of: list[tuple[MetaInstance, ParserRuleContext]] = []
        # "Name := expression" view attributes (see
        # _build_view_bare_attributes) build their AttrOrParam/Derivates
        # eagerly (no forward-ref involved), but resolving their Type needs
        # RenamedBaseView.BaseView already resolved - same ForwardRef
        # timing issue as _pending_view_all_of above, same deferred fix.
        self._pending_view_bare_attrs: list[tuple[MetaInstance, MetaInstance, MetaInstance]] = []
        # `MODEL X (fr) ... TRANSLATION OF Y ["ver"]` - the grammar parses
        # the clause but the metamodel `Model` has no own field for it.
        # Recorded here during construction, aligned against the resolved
        # base model in build() AFTER resolve_all() - see
        # _apply_pending_translations.
        self._pending_translations: list[tuple[MetaInstance, str]] = []
        # Real IlisMeta16 instances for the five predefined INTERLIS types
        # whose names are reserved lexer tokens, so they can never be
        # declared as a `DOMAIN` and reach the builder only as an opaque
        # sentinel (a bare `"INTERLIS.HALIGNMENT"` string from
        # `alignmentType`, or an `INTERLIS.<token>` `structureRef`
        # ForwardRef that resolves to nothing). Materialised on demand by
        # `_predefined_type`, one shared instance per token per builder.
        self._predefined_type_cache: dict[str, MetaInstance] = {}
        self.repository.bind_builder_factory(self._make_sub_builder)

    def _make_sub_builder(self) -> "InterlisModelBuilder":
        return InterlisModelBuilder._from_shared(
            self.schema, self.registry, self.spec, self.attachment, self.repository
        )

    # ------------------------------------------------------------------
    # Point d'entree public
    # ------------------------------------------------------------------
    def build(self, tree: ParserRuleContext, *, meta_attributes: list[tuple[int, str, str]] | None = None) -> Any:
        """Build `tree`, optionally capturing eCH-0117 `!!@Name=Value` comments.

        `meta_attributes` is attached to the built instances via
        `_attach_pending_meta_attributes`, per eCH-0117's "first following
        language construct" rule. Only `tree` itself is covered by THIS
        call - an imported model gets the same treatment independently,
        via its own sub-builder's `build()` call.
        """
        self._pending_meta_attributes = sorted(meta_attributes or [], key=lambda triple: triple[0])
        self._meta_attribute_index = 0
        result = self.visit(tree)
        self.forward_refs.resolve_all(repository=self.repository)
        self._apply_pending_mandatory_overrides()
        self._apply_pending_view_all_of()
        self._apply_pending_view_bare_attr_types()
        self._apply_pending_translations()
        return result

    def _apply_pending_mandatory_overrides(self) -> None:
        """Give each attribute queued in `_pending_mandatory_overrides` its OWN, Mandatory=True `Type` clone.

        `DomainType.Mandatory` is the ONLY place `MANDATORY` can attach -
        correct for an INLINE type but wrong for a NAMED domain
        reference, since every attribute referencing it resolves to the
        SAME registered instance (real corpus-wide, 292 occurrences).
        Runs AFTER `resolve_all()`: a shallow clone with `Mandatory`
        forced `True` replaces `instance.Type`, leaving the original
        SHARED instance untouched for every other attribute.
        """
        for instance in self._pending_mandatory_overrides:
            resolved = getattr(instance, "Type", None)
            if not isinstance(resolved, MetaInstance) or bool(getattr(resolved, "Mandatory", False)):
                continue
            fields = {
                k: v for k, v in {**resolved.__dict__, **(resolved.model_extra or {})}.items() if not k.startswith("_")
            }
            fields["Mandatory"] = True
            instance.Type = self.registry.new_instance(resolved._qualified_class, **fields)

    # Reference Manual eCH-0031 V2.1.0 Annex A / §3.8: the predefined
    # INTERLIS types whose names are reserved lexer tokens.
    #   HALIGNMENT (FINAL) = (Left, Center, Right) ORDERED;
    #   VALIGNMENT (FINAL) = (Top, Cap, Half, Base, Bottom) ORDERED;
    #   BOOLEAN    (FINAL) = (false, true) ORDERED;   -> BooleanType, matching
    #                        this builder's own handling of the bare BOOLEAN keyword
    #   URI     (FINAL) = TEXT*1023;                  -> TextType Kind=Uri
    #   UUIDOID EXTENDS ANYOID = OID TEXT*36;         -> TextType (36 chars)
    _PREDEFINED_ENUM_ELEMENTS: dict[str, tuple[str, ...]] = {
        "HALIGNMENT": ("Left", "Center", "Right"),
        "VALIGNMENT": ("Top", "Cap", "Half", "Base", "Bottom"),
    }

    def _predefined_type(self, token: str) -> MetaInstance | None:
        """Return the shared IlisMeta16 instance for a predefined `INTERLIS.<token>` type.

        One instance per token per builder - `Mandatory` is never set
        here; an attribute needing it gets a private clone via
        `_pending_mandatory_overrides`.
        """
        cached = self._predefined_type_cache.get(token)
        if cached is not None:
            return cached
        instance: MetaInstance | None = None
        if token in self._PREDEFINED_ENUM_ELEMENTS:
            instance = self.registry.new_instance("IlisMeta16.ModelData.EnumType")
            instance.Name = token
            instance.Order = "Ordered"
            top = self.registry.new_instance("IlisMeta16.ModelData.EnumNode")
            top.Name = "TOP"
            self.attachment.attach(
                instance,
                "TopNode",
                top,
                association="TopNode",
                role="TopNode",
                rule="<predefined>",
            )
            for element_name in self._PREDEFINED_ENUM_ELEMENTS[token]:
                node = self.registry.new_instance("IlisMeta16.ModelData.EnumNode")
                node.Name = element_name
                self.attachment.attach(
                    top,
                    "Node",
                    node,
                    association="SubNode",
                    role="Node",
                    rule="<predefined>",
                )
        elif token == "BOOLEAN":
            instance = self.registry.new_instance("IlisMeta16.ModelData.BooleanType")
            instance.Name = "BOOLEAN"
        elif token == "URI":
            instance = self.registry.new_instance("IlisMeta16.ModelData.TextType")
            instance.Name = "URI"
            instance.Kind = "Uri"
        elif token == "UUIDOID":
            instance = self.registry.new_instance("IlisMeta16.ModelData.TextType")
            instance.Name = "UUIDOID"
            instance.Kind = "Text"
            instance.MaxLength = 36
        if instance is not None:
            self._predefined_type_cache[token] = instance
        return instance

    def _resolve_predefined_type_sentinel(self, instance: MetaInstance, bag: dict[str, Any]) -> None:
        """Replace a predefined-`INTERLIS`-token sentinel in an `attrTypeDef` bag with a real Type.

        `bag["Type"]` reaches `_attach_unclaimed_results` as an opaque
        string or a `structureRef` ForwardRef that resolves to nothing.
        Swaps in the materialised type; queues
        `_pending_mandatory_overrides` for a leading `MANDATORY`.
        """
        type_value = bag.get("Type")
        token: str | None = None
        if isinstance(type_value, str) and type_value in ("INTERLIS.HALIGNMENT", "INTERLIS.VALIGNMENT"):
            token = type_value.split(".", 1)[1]
        elif isinstance(type_value, ForwardRef) and type_value.name in (
            "INTERLIS.BOOLEAN",
            "INTERLIS.URI",
            "INTERLIS.UUIDOID",
        ):
            token = type_value.name.split(".", 1)[1]
        if token is None:
            return
        synthesized = self._predefined_type(token)
        if synthesized is None:
            return
        bag["Type"] = synthesized
        if bag.pop("Mandatory", False) is True:
            self._pending_mandatory_overrides.append(instance)

    @staticmethod
    def _ctx_line(ctx: Any) -> int | None:
        """Return the source line of a ParserRuleContext OR a bare Token."""
        start = getattr(ctx, "start", None)
        return getattr(start if start is not None else ctx, "line", None)

    def _attach_pending_meta_attributes(self, instance: MetaInstance, ctx: Any) -> None:
        """Attach every pending meta-attribute comment up to `ctx`'s own line.

        eCH-0117 §3: "the meta-attribute applies to the first following
        language construct" - built instances are visited depth-first in
        source order, and `_pending_meta_attributes` is consumed by a
        single monotonic index, so the first instance whose own line is
        >= a pending comment's line IS that construct.
        """
        line = self._ctx_line(ctx)
        if line is None or self._meta_attribute_index >= len(self._pending_meta_attributes):
            return
        while self._meta_attribute_index < len(self._pending_meta_attributes):
            comment_line, name, value = self._pending_meta_attributes[self._meta_attribute_index]
            if comment_line > line:
                break
            meta = self.registry.new_instance("IlisMeta16.ModelData.MetaAttribute")
            meta.Name = name
            meta.Value = value
            self.attachment.attach(
                instance,
                "MetaAttribute",
                meta,
                association="MetaAttributes",
                role="MetaAttribute",
                rule="metaAttribute",
            )
            self._meta_attribute_index += 1

    # ------------------------------------------------------------------
    # Generic dispatch (replaces ANTLR's visitXxx pattern)
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
        raise BuildError(f"unknown kind {entry.kind!r}", rule=rule_name, ctx=ctx)

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
        self._attach_pending_meta_attributes(instance, ctx)

        outer_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()

        if entry.discriminant:
            value = self._resolve_discriminant(ctx, rule_name, entry.discriminant, outer_ctx, consumed)
            if value is not None:
                setattr(instance, entry.discriminant.attribute, value)

        # Push BEFORE capturing the context used by THIS rule's
        # attribute_bindings: otherwise a reference to `context_key:
        # <rule_name>` (the instance currently being built itself, e.g.
        # modeldef.imports.ImportingP) would only find the ENCLOSING rule's
        # context, never this one - see the field: null mechanism.
        with self._pushed_context(rule_name, instance):
            construction_ctx = self._construction_stack[-1]
            if entry.attribute_bindings:
                self._apply_bindings(instance, ctx, rule_name, entry.attribute_bindings, construction_ctx, consumed)
            sweep_results = self._sweep_unclaimed_children(ctx, consumed)
        self._attach_unclaimed_results(instance, sweep_results, rule_name)

        self._maybe_register_symbol(instance)

        if rule_name == "classDef":
            self._attach_class_oid(instance, ctx)
            self._fix_class_extended_super(instance, ctx)
        elif rule_name == "modeldef":
            self._register_translation_of(instance, ctx)
        elif rule_name == "unitDef":
            self._register_unit_alias(instance, ctx)
        elif rule_name == "viewDef":
            self._set_view_formation_kind(instance, ctx)
            self._expand_view_all_of(instance, ctx)
            self._build_view_bare_attributes(instance, ctx)
        elif rule_name == "constant":
            self._normalize_enumeration_const_value(instance)
        elif rule_name == "pathEl":
            self._set_path_el_kind(instance, ctx)
        elif rule_name == "existenceConstraint":
            self._fix_existence_constraint_attr(instance)
        elif rule_name == "contextDef":
            self._build_context_domain_pairs(instance, ctx)
        elif rule_name == "metaDataBasketDef":
            self._build_metadata_basket_members(instance, ctx)

        if entry.parent and self._parent_stack:
            self.attachment.attach(
                self._parent_stack[-1],
                entry.parent.role,
                instance,
                association=entry.parent.association,
                role=entry.parent.role,
                rule=rule_name,
            )

        return instance

    def _register_unqualified_imports(self, ctx: ParserRuleContext) -> None:
        """Detect which `IMPORTS` names are prefixed with `UNQUALIFIED`.

        Feeds `symbol_table.unqualified_imports`, used by
        `ForwardRefResolver._resolve_one` to let an unqualified reference
        resolve into an imported model. Not expressible via the generic
        `for_each`/`attribute_bindings` mechanism: `Import` has no
        attribute of its own for `UNQUALIFIED`. It always immediately
        precedes the name it modifies, so a positional walk suffices.
        """
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
        """Build the two linked targets of a topicDef: SubModel + DataUnit.

        Not generalized to N targets - topicDef is the only one of the 121
        rules that needs this.
        """
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
            # unprefixed key: applied to the first instance (rare, defensive)
            prefixed_bindings[next(iter(instances))][key] = binding

        submodel = instances.get("SubModel")
        if submodel is not None:
            # eCH-0117 SS5: a TopicDef's meta-attributes belong to the
            # SCHEMA side (SubModel), never DataUnit - the two twins share
            # the same source ctx, so this must be scoped explicitly
            # rather than left to the generic _build_instance hook (which
            # _build_multi_target bypasses entirely).
            self._attach_pending_meta_attributes(submodel, ctx)
            with self._pushed_context(rule_name, submodel):
                self._apply_bindings(
                    submodel, ctx, rule_name, prefixed_bindings.get("SubModel", {}), construction_ctx, consumed
                )
            self._maybe_register_symbol(submodel)

        for short, inst in instances.items():
            if short == "SubModel":
                continue
            with self._pushed_context(rule_name, inst):
                self._apply_bindings(inst, ctx, rule_name, prefixed_bindings.get(short, {}), construction_ctx, consumed)

        if submodel is not None:
            for short, inst in instances.items():
                if inst is not submodel:
                    submodel._twin = inst
                    inst._twin = submodel

        self._sweep_unclaimed_children(ctx, consumed)

        self._apply_topic_oid_clauses(ctx, instances)

        if entry.parent and self._parent_stack:
            for inst in instances.values():
                self.attachment.attach(
                    self._parent_stack[-1],
                    entry.parent.role,
                    inst,
                    association=entry.parent.association,
                    role=entry.parent.role,
                    rule=rule_name,
                )

        return submodel if submodel is not None else next(iter(instances.values()))

    # ------------------------------------------------------------------
    # Cas special : multi_declaration (domainDef uniquement - voir _relay)
    # ------------------------------------------------------------------
    def _build_multi_declaration(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        """Split a domainDef() into its N independent declarations.

        `domainDef()` is the only one of the 121 rules that grammatically
        loops over N independent declarations sharing a single `DOMAIN`
        keyword. Each SEMI delimits a declaration - splits `ctx.children`
        into segments by position (no existing generic mechanism
        correlates several DIFFERENT accessors at the SAME position).
        `RESTRICTION(...)` contains its own INTERNAL SEMIs (separators
        between candidates, not declaration terminators) - this tracks
        LPAR/RPAR depth so a SEMI only delimits a declaration outside any
        open parenthesis.
        """
        children = list(ctx.children or [])
        segments: list[list[Any]] = []
        current: list[Any] = []
        paren_depth = 0
        for child in children:
            current.append(child)
            if isinstance(child, TerminalNode):
                if child.symbol.type == InterlisParser.LPAR:
                    paren_depth += 1
                elif child.symbol.type == InterlisParser.RPAR:
                    paren_depth -= 1
                elif child.symbol.type == InterlisParser.SEMI and paren_depth == 0:
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
                continue  # segment with no Name (e.g. leftover before the 1st token) - nothing to build
            mandatory = any(isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.MANDATORY for c in segment)
            content_node = next(
                (
                    c
                    for c in segment
                    if isinstance(c, ParserRuleContext) and self._rule_name(c) in ("type", "numeric", "enumeration")
                ),
                None,
            )
            if content_node is None:
                class_token = next(
                    (c for c in segment if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.CLASS),
                    None,
                )
                if class_token is None:
                    # Unreachable in practice: a bare "STRING DOTDOT STRING" domainDef
                    # alternative parses via type_'s own alternative instead (ANTLR
                    # resolves the ambiguity there - see type.text_range_alt,
                    # spec/grammar/mapping/06_types.yml). A defensive no-op, not a real gap.
                    continue
                instance = self._build_domain_class_restriction(segment, rule_name)
                if not isinstance(instance, MetaInstance):
                    continue
                # _build_domain_class_restriction builds its instance
                # directly (not via _build_instance/visit_wrapped), so it
                # never got a _source_ctx - fall back to this segment's own
                # Name token (a bare Token, not a ParserRuleContext -
                # _ctx_line handles both shapes).
                instance._source_ctx = instance._source_ctx or name_node.symbol
                self._attach_pending_meta_attributes(instance, instance._source_ctx)
                self._attach_domain_extends(instance, segment, rule_name)
                if getattr(instance, "Name", None) is None:
                    instance.Name = name_node.getText()
                    self._maybe_register_symbol(instance)
                if mandatory and getattr(instance, "Mandatory", None) is None:
                    instance.Mandatory = True
                if entry.parent and self._parent_stack:
                    self.attachment.attach(
                        self._parent_stack[-1],
                        entry.parent.role,
                        instance,
                        association=entry.parent.association,
                        role=entry.parent.role,
                        rule=rule_name,
                    )
                results.append(instance)
                continue
            content_rule = self._rule_name(content_node)
            if content_rule == "type":
                # `_rule_name` derives "type" from `TypeContext` (grammar rule
                # `type_()`, the Python method renamed to avoid the `type`
                # builtin - `_rule_name` only knows the ANTLR CLASS name,
                # which is never renamed). Confirmed on BasketOID/MetaElemOID/
                # LanguageCode (models/IlisMeta16.ili, the file's 1st DOMAIN
                # block) - without this match, none of the 3 domains (all via
                # type_(), e.g. OID TEXT / TEXT*5) was ever built.
                instance = self.visit(content_node)
            else:
                target = (
                    "IlisMeta16.ModelData.NumType" if content_rule == "numeric" else "IlisMeta16.ModelData.EnumType"
                )
                instance = self.visit_wrapped(content_node, target, rule_name)
            if not isinstance(instance, MetaInstance):
                continue
            self._attach_domain_extends(instance, segment, rule_name)
            if getattr(instance, "Name", None) is None:
                instance.Name = name_node.getText()
                self._maybe_register_symbol(instance)
            if mandatory and getattr(instance, "Mandatory", None) is None:
                instance.Mandatory = True
            if entry.parent and self._parent_stack:
                self.attachment.attach(
                    self._parent_stack[-1],
                    entry.parent.role,
                    instance,
                    association=entry.parent.association,
                    role=entry.parent.role,
                    rule=rule_name,
                )
            results.append(instance)

        if not results:
            return None
        return results[0] if len(results) == 1 else results

    def _build_metadata_basket_members(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Materialize `MetaObjectDef` children for each `OBJECTS OF <Class>: <Name>(, <Name>)*` clause.

        `metaDataBasketDef()`'s generic `attribute_bindings` cannot express
        this (see `objects_of_clauses` in `spec/grammar/mapping/07_constraints.yml`):
        `Name()` returns every `Name` token flattened - the basket's own
        name, then each clause's Class-Name and Member-Name(s), all mixed
        together with no grouping. Clause boundaries only exist in the raw
        token stream (grammar: `(OBJECTS OF Name COLON (Name (COMMA
        Name)*) SEMI?)+`, each clause starting with its own `OBJECTS`
        token) - same "walk `ctx.children` by hand" approach as
        `_build_multi_declaration`. Without this, a `Sign := {name}`
        (`signParamAssignment.metaobjectref_alt`) referencing a SIGN
        BASKET member never resolves: no `MetaObjectDef` was ever
        registered under that name - confirmed via `BuildError` on real
        corpus data (`RoadsExgm2ien.ili`: `Sign := {Building}`;
        `modelPond.ili`: `Sign := {goodhealth}`).
        """
        # `OBJECTS OF SurfaceSign: ...` names a Class UNQUALIFIED, resolved
        # against the basket's OWN `~ topicRef` scope (e.g. "StandardSymbology.
        # StandardSigns"), not the enclosing model's `IMPORTS UNQUALIFIED`
        # fallback (real corpus basket defs never use UNQUALIFIED imports) -
        # reuses `ForwardRef.topic_extends_hint`'s existing "try
        # <hint>.<name> first" resolution, same mechanism a `TOPIC EXTENDS`
        # namespace already relies on.
        topic_ref_ctx = ctx.topicRef()
        basket_topic_hint = topic_ref_ctx.getText() if topic_ref_ctx is not None else None
        clauses: list[list[Any]] = []
        current: list[Any] = []
        for child in ctx.children or []:
            if isinstance(child, TerminalNode) and child.symbol.type == InterlisParser.OBJECTS:
                if current:
                    clauses.append(current)
                current = []
            current.append(child)
        if current:
            clauses.append(current)
        for clause in clauses:
            if not (isinstance(clause[0], TerminalNode) and clause[0].symbol.type == InterlisParser.OBJECTS):
                continue  # leading segment before the 1st OBJECTS token (basket name/EXTENDS/TILDE topicRef)
            names = [c for c in clause if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.Name]
            if len(names) < 2:
                continue  # need a Class-Name plus at least one Member-Name
            class_name, member_names = names[0].getText(), names[1:]
            for member_token in member_names:
                metaobj = self.registry.new_instance("IlisMeta16.ModelData.MetaObjectDef")
                metaobj.Name = member_token.getText()
                metaobj.IsRefSystem = getattr(instance, "Kind", None) == "RefSystemB"
                self._maybe_register_symbol(metaobj)
                self.attachment.attach(
                    instance,
                    "Member",
                    metaobj,
                    association="MetaBasketMembers",
                    role="Member",
                    rule="metaDataBasketDef",
                )
                class_ref = ForwardRef(
                    name=class_name,
                    resolves_to_hint="Class",
                    rule="metaDataBasketDef",
                    home_model=self._current_model_name(),
                    topic_extends_hint=basket_topic_hint,
                )
                self.attachment.attach(
                    metaobj, "Class", class_ref, association="MetaObjectClass", role="Class", rule="metaDataBasketDef"
                )
                self.forward_refs.register_pending(class_ref, metaobj, "Class")

    def _attach_domain_extends(self, instance: MetaInstance, segment: list[Any], rule_name: str) -> None:
        """Attach `Super` for a `DOMAIN X EXTENDS Y = ...;` declaration.

        `domainDef()`'s optional `(EXTENDS domainRef)?` clause sits just
        before `EQ`, inside the segment `_build_multi_declaration` already
        split out. Previously never attached - content construction only
        handled its own content alternative, never this optional prefix.
        Needed for `schema.line_coord_type` to walk the EXTENDS chain of a
        `LineType` with no `VERTEX` of its own. `graceful=True`, same
        policy as `classDef`/`structureDef.Super`.
        """
        extends_idx = next(
            (
                i
                for i, c in enumerate(segment)
                if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.EXTENDS
            ),
            None,
        )
        if extends_idx is None:
            return
        domain_ref_node = next(
            (
                c
                for c in segment[extends_idx + 1 :]
                if isinstance(c, ParserRuleContext) and self._rule_name(c) == "domainRef"
            ),
            None,
        )
        if domain_ref_node is None:
            return
        value = self.visit(domain_ref_node)
        if not isinstance(value, ForwardRef):
            return
        value.graceful = True
        self.attachment.attach(instance, "Super", value, association="Inheritance", role="Super", rule=rule_name)
        self.forward_refs.register_pending(value, instance, "Super")

    def _build_domain_class_restriction(self, segment: list[Any], rule_name: str) -> MetaInstance:
        """Build `DOMAIN X = CLASS RESTRICTION(A; B; ...);`.

        `domainDef()`'s 5th alternative, inlined directly in its ANTLR
        body - previously never built (found on
        `RoadTrafficCensus_V1_1.ili`). Target metaclass is
        `IlisMeta16.ModelData.ReferenceType`, same as `referenceAttr()`,
        but this alternative has no `EXTERNAL` clause, so
        `External=False` unconditionally. Attaches ALL
        `classOrAssociationRef` in the segment (not just the first),
        needed to interpret the 3rd XTF encoding form - `CLASS
        RESTRICTION(A; B; C)` with several real candidates (see
        `xtf/schema.py::restriction_candidates`).
        """
        instance = self.registry.new_instance("IlisMeta16.ModelData.ReferenceType")
        instance.External = False
        refs = [
            c for c in segment if isinstance(c, ParserRuleContext) and self._rule_name(c) == "classOrAssociationRef"
        ]
        for ref_ctx in refs:
            value = self.visit(ref_ctx)
            if value is None:
                continue
            self.attachment.attach(
                instance,
                "BaseClass",
                value,
                association="BaseClass",
                role="BaseClass",
                rule=rule_name,
            )
            if isinstance(value, ForwardRef):
                self.forward_refs.register_pending(value, instance, "BaseClass")
        return instance

    def _build_type_string_range(self, ctx: ParserRuleContext) -> MetaInstance | None:
        """Build type()'s 3rd alternative: a bare `STRING DOTDOT STRING` domain.

        E.g. `Angle_DMS_90 EXTENDS Angle_DMS = "-90:00:00.000" ..
        "90:00:00.000";` (`CoordSys-20151124.ili`). Maps to
        `IlisMeta16.ModelData.FormattedType`, by analogy with
        `formattedType()`'s own alternative. Built by hand because
        `ctx.getAltNumber()` is unconditionally 0 for unlabeled-alternative
        rules in the vendored grammar.
        """
        strings = [
            c for c in (ctx.children or []) if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.STRING
        ]
        if len(strings) != 2:
            return None
        instance = self.registry.new_instance("IlisMeta16.ModelData.FormattedType")
        instance.Min = strings[0].getText()
        instance.Max = strings[1].getText()
        return instance

    def _build_control_points_ref(self, ctx: ParserRuleContext, rule_name: str) -> ForwardRef | None:
        """Build a ForwardRef from `controlPoints()` (`VERTEX Name(DOT Name)*`).

        E.g. "VERTEX Coord2" - its declarative binding used to produce
        only a LIST of Name token texts, never attached anywhere
        (`LineType.CoordType` stayed `None`) - the generic
        `_resolve_or_defer` doesn't fit either, since `ctx.getText()`
        would capture the `VERTEX` token along with the qualified name.
        Rebuilds the name by hand from the `Name` tokens only.
        """
        names = [n.getText() for n in ca.call_list(ctx, "Name")]
        if not names:
            return None
        hints = self._expand_kind_hint("CoordType")
        return ForwardRef(
            name=".".join(names),
            resolves_to_hint=hints or None,
            rule=rule_name,
            home_model=self._current_model_name(),
            topic_extends_hint=self._current_topic_extends_hint(ctx),
        )

    def _build_local_uniqueness_def(self, ctx: ParserRuleContext) -> dict[str, Any]:
        """Build `{Kind: "LocalU", UniqueDef: [PathOrInspFactor, ...]}` for `localUniqueness()`.

        Real corpus usage (confirmed against every `UNIQUE (LOCAL)`
        occurrence in `ili_corpus/`) is always the single shape
        `RoleName: AttrName`. Can't be a generic `attribute_bindings`
        entry: `ctx.Name()` returns EVERY `Name` token in the rule, and
        ANTLR's own decision for the leading `(Name COLON)?` is genuinely
        AMBIGUOUS for `Entries: Code` and resolves to the WRONG one for
        this project's real usage - re-derived here directly from the
        raw children, ignoring ANTLR's internal branch choice.
        """
        children = list(ctx.getChildren())[3:]  # skip LPAR LOCAL RPAR
        colon_index = next(
            (
                i
                for i, c in enumerate(children)
                if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.COLON
            ),
            None,
        )
        role_tokens = children[:colon_index] if colon_index is not None else children
        attr_tokens = children[colon_index + 1 :] if colon_index is not None else []

        def path_el(name: str) -> MetaInstance:
            el = self.registry.new_instance("IlisMeta16.ModelData.PathEl")
            el.Kind = "ReferenceAttr"
            el.Ref = name
            return el

        role_names = [
            t.getText() for t in role_tokens if isinstance(t, TerminalNode) and t.symbol.type == InterlisParser.Name
        ]
        attr_names = [
            t.getText() for t in attr_tokens if isinstance(t, TerminalNode) and t.symbol.type == InterlisParser.Name
        ]

        def factor(names: list[str]) -> MetaInstance:
            instance = self.registry.new_instance("IlisMeta16.ModelData.PathOrInspFactor")
            instance.PathEls = [
                path_el(name) for name in names
            ]  # a fresh PathEl per factor - never shared across UniqueDef entries
            return instance

        if not role_names:
            return {"Kind": "LocalU"}
        if not attr_names:
            return {"Kind": "LocalU", "UniqueDef": [factor(role_names)]}
        return {"Kind": "LocalU", "UniqueDef": [factor([*role_names, name]) for name in attr_names]}

    def _build_enumeration_tree(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry) -> None:
        """Build the EnumNode tree of an `enumeration()` correctly.

        Each `enumElement` self-attaching via its own `parent:` (the naive
        per-element approach) can only represent a linear chain, never a
        branching tree - for a nested definition like `"KGS_Kategorie :
        MANDATORY (A (A, verstaerkter_Schutz), B);"` it would lose A's
        real children entirely. Distinguishes TOP vs NESTED by the
        current PARENT's class (`_parent_stack[-1]`): TOP creates a
        synthetic `TOP` node (`models/IlisMeta16.ili`'s documented
        convention); NESTED attaches directly to the enclosing
        `enumElement`, which already serves as local root.
        """
        parent_context = self._parent_stack[-1] if self._parent_stack else None
        is_top_level = (
            isinstance(parent_context, MetaInstance)
            and parent_context._qualified_class == "IlisMeta16.ModelData.EnumType"
        )

        if is_top_level:
            top = self.registry.new_instance("IlisMeta16.ModelData.EnumNode")
            top.Name = "TOP"
            self.attachment.attach(
                parent_context,
                "TopNode",
                top,
                association="TopNode",
                role="TopNode",
                rule=rule_name,
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
                        children_target,
                        "Node",
                        node,
                        association="SubNode",
                        role="Node",
                        rule=rule_name,
                    )

        construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()
        final_binding = entry.attribute_bindings.get("Final") if entry.attribute_bindings else None
        if final_target is not None and isinstance(final_binding, dict):
            final_value = self._resolve_binding_value(
                ctx, rule_name, "Final", final_binding, construction_ctx, consumed
            )
            if final_value:
                self.attachment.attach(final_target, "Final", final_value, rule=rule_name)
        order_binding = entry.attribute_bindings.get("Order") if entry.attribute_bindings else None
        if is_top_level and isinstance(order_binding, dict):
            order_value = self._resolve_binding_value(
                ctx, rule_name, "Order", order_binding, construction_ctx, consumed
            )
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
            if node is None or (isinstance(node, list) and not node):
                # A MULTI token accessor (e.g. term0's OR, term1's AND/MUL/
                # DIV - each can repeat via the grammar's `*`, so ANTLR
                # generates `getTokens`, never `getToken`) returns an EMPTY
                # LIST, never `None`, when the operator is absent - `node is
                # None` alone never caught this, so term0/term1 ALWAYS took
                # this branch (even for a single term2/term1 with no
                # operator at all), wrapping it in a spurious CompoundExpr
                # with exactly 1 SubExpressions entry instead of the correct
                # pure pass-through. Confirmed empirically (a plain `KBfrei
                # == #false` constraint produced a phantom
                # CompoundExpr(Operation='And', SubExpressions=[<1 item>])
                # wrapping the real Relation CompoundExpr).
                continue
            instance = self.registry.new_instance(branch.target)
            instance._source_ctx = ctx
            if branch.discriminant:
                value = branch.discriminant.model_extra.get("value")
                if rule_name == "term2" and token_or_rule == "relation":
                    # CompoundExpr.Operation=Relation is a PARENT kind-value
                    # with its own children (Equal/NotEqual/Less/Greater/
                    # LessOrEqual/GreaterOrEqual, ilismeta16-kind-values.yml)
                    # - the real sub-value relation() matched, never the
                    # literal group name "Relation" itself. relation()'s OWN
                    # attribute_bindings.RelationKind never actually worked
                    # (composed field names like "EQ+EQ"/"LT+GT" don't match
                    # any real ANTLR accessor - confirmed empirically,
                    # always resolved to `None`), so this is resolved here
                    # directly against RelationContext's real accessors
                    # instead (`_relation_kind`, same "read the raw ctx
                    # directly" pattern as `_set_view_formation_kind`/
                    # `_set_join_or_null`).
                    value = self._relation_kind(node) or value
                setattr(instance, branch.discriminant.attribute, value)

            construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
            consumed: set[int] = set()
            with self._pushed_context(rule_name, instance):
                if isinstance(node, ParserRuleContext):
                    # `token_or_rule` designates a REAL grammar rule (not a
                    # plain token), with its own structured content described
                    # by ITS OWN spec entry (e.g. oIDType.numeric -> numeric(),
                    # Min/Max/Circular/Clockwise/Unit) - visit it and merge
                    # its bag into `instance`, same mechanism as visit_wrapped
                    # (explicit wrap:). This content used to never be picked
                    # up at all.
                    self._merge_bag_into_instance(instance, self.visit(node), rule_name)
                if entry.attribute_bindings:
                    self._apply_bindings(instance, ctx, rule_name, entry.attribute_bindings, construction_ctx, consumed)
                if rule_name == "predicate" and token_or_rule == "DEFINED":
                    self._set_defined_subexpression(instance, ctx)
                if rule_name == "factor" and token_or_rule == "INTERLIS":
                    self._set_predefined_function_call(instance, ctx)
            return instance

        # No when_present branch matched: pure pass-through (e.g. term ->
        # term0 without EQ GT, predicate -> factor without NOT/DEFINED). Do
        # NOT reuse _relay/its generic bag mechanism here: a Conditional
        # rule's attribute_bindings (e.g. term2.SubExpressions =
        # [predicate(0), predicate(1)] "multi: true") describe the content of
        # the MATCHED branch (already handled above), not a generic
        # pass-through recipe - applying them anyway built a residual bag
        # {'SubExpressions': [predicate(0)], '_relation_child': None} instead
        # of purely relaying to the single real child (found on
        # models/IlisMeta16.ili, a term/term0/term1/term2 chain with no
        # operator). Plain sweep of unclaimed children instead, the same way
        # predicate -> factor already does successfully (the only remaining
        # real unconsumed child is the one to relay to).
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
        # multi_declaration: a special case confirmed unique among the 121
        # rules (like topicDef/_build_multi_target, the other already-wired
        # special case) - domainDef() grammatically loops over N domain
        # declarations sharing a single DOMAIN keyword; no existing generic
        # mechanism (bag/for_each) correlates several different accessors
        # (Name/type_/numeric/enumeration) at the SAME position - see
        # _build_multi_declaration.
        if entry.multi_declaration:
            return self._build_multi_declaration(ctx, rule_name, entry)

        # enumeration(): 3rd special case among the 121 rules (same family
        # as multi_declaration/_build_multi_target) - see
        # _build_enumeration_tree for the fixed bug's detail.
        if rule_name == "enumeration":
            return self._build_enumeration_tree(ctx, rule_name, entry)

        # type(): 4th and last special case - its 3rd bare grammar
        # alternative "STRING DOTDOT STRING" (neither baseType() nor
        # lineType() present) has neither a sub-rule to dispatch to nor a
        # usable declarative binding (see _build_type_string_range for the
        # full detail, including a note on `alt:` being inert throughout
        # the whole mapping).
        if rule_name == "type" and not ca.is_present(ctx, "baseType") and not ca.is_present(ctx, "lineType"):
            instance = self._build_type_string_range(ctx)
            if instance is not None:
                return instance

        # controlPoints(): 5th special case (XTF geometry) - see
        # _build_control_points_ref for the full detail (joining Name tokens
        # while ignoring VERTEX, a ForwardRef then attached generically by
        # lineType.CoordType).
        if rule_name == "controlPoints":
            return self._build_control_points_ref(ctx, rule_name)

        # localUniqueness(): 6th special case - see _build_local_uniqueness_def
        # for why UniqueDef needs raw-children handling (a genuine grammar
        # ambiguity, not just a missing accessor).
        if rule_name == "localUniqueness":
            return self._build_local_uniqueness_def(ctx)

        # children: multi-visit dispatch (e.g. definitions -> classDef*,
        # topicDef*, ...) - all occurrences count, not a single
        # alternative.
        if entry.children:
            for child_rule in entry.children:
                if not ca.has_accessor(ctx, child_rule):
                    continue
                for node in ca.call_list(ctx, child_rule):
                    self.visit(node)
            return None

        # dispatches_to: only one grammar alternative is actually taken
        # among the named rules (e.g. classOrStructureDef). If NONE
        # matches, don't stop there: some rules (e.g. attrTypeDef,
        # dispatches_to=[attrType, lineType]) ALSO have their own direct
        # alternatives described in attribute_bindings (numeric/enumeration/
        # bare NUMERIC) - fall through to the bag processing below instead
        # of returning None prematurely.
        if entry.dispatches_to:
            for name in entry.dispatches_to:
                if ca.has_accessor(ctx, name):
                    node = ca.call(ctx, name)
                    if node is not None:
                        return self.visit(node) if isinstance(node, ParserRuleContext) else node.getText()

        # Neither children nor dispatches_to matched: compute this rule's
        # own attribute_bindings (if any - notes-only entries like
        # _dispatch produce nothing), make them available to the
        # construction context (the field: null mechanism, e.g.
        # interlis2def.iliVersion -> modeldef.iliVersion), THEN sweep the
        # unclaimed children (e.g. interlis2def -> modeldef, never in its
        # own bindings).
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
                    continue  # notes-only entry (e.g. _dispatch): nothing to compute
                bag[key] = self._resolve_binding_value(ctx, rule_name, key, binding, construction_ctx, consumed)

        real_values = {k: v for k, v in bag.items() if v is not None}

        # A bag key can itself ALREADY carry a concrete instance (e.g. a
        # `wrap:` binding on a bare numeric()/enumeration() alternative, see
        # domainDef._domain_content) rather than a plain scalar value - in
        # that case THIS instance is the rule's real content (the bag's
        # other keys, e.g. Name/Mandatory, only describe it): same
        # application rules as a result found by sweeping (see
        # _apply_sibling_bag_values below), but without waiting for the
        # sweep since the node was already consumed by the binding's own
        # computation.
        # Excludes "hollow" instances (e.g. numeric()._refsys_clause ->
        # NumsRefSys built unconditionally by _build_nested even when the
        # reference clause is NOT present in the source, all its fields
        # staying None) - without this filter, such a hollow instance
        # wrongly won this fast-path and was returned AS-IS instead of the
        # bag dict, preventing visit_wrapped (which already has its OWN
        # hollow-value filtering logic, see below) from ever reaching it -
        # silently losing Min/Max/Circular/Clockwise/Unit for EVERY numeric
        # domain/type, including on models/IlisMeta16.ili itself (e.g.
        # "Code = 0..255;").
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

        # A visited unclaimed child (e.g. interlis2def -> modeldef) usually
        # carries THE rule's real content - takes priority over the bag of
        # own values (which then often only feeds the construction context,
        # e.g. iliVersion).
        real_results = [v for _rule, v in sweep_results if v is not None]
        if real_results:
            result = real_results[0] if len(real_results) == 1 else real_results
            if isinstance(result, MetaInstance) and real_values:
                self._apply_sibling_bag_values(result, real_values)
            return result

        if len(bag) == 1:
            ((only_key, only_value),) = bag.items()
            if only_key.startswith("_") or not entry.feeds_into:
                # Unwrap to the bare value in the common case: either a
                # single notes-only/relay key (e.g. attributePath's own
                # `_dispatch`, pure pass-through to objectOrAttributePath -
                # which IS this rule's real content once the internal-
                # marker wrapper is stripped), OR a rule with no
                # `feeds_into:` at all (e.g. textConst's `Value`, whose
                # single key exists only for readability in the spec - the
                # PARENT rule's own binding, e.g. constant.Value's
                # `alt_rule` dispatch, consumes the bare resolved value
                # directly via `_resolve_node`, never a {key: value} dict).
                return only_value
            # A single REAL (non-underscore) attribute_bindings key on a
            # rule that DOES declare `feeds_into:` (e.g.
            # objectOrAttributePath's `PathEls`, feeds_into: PathOrInspFactor)
            # must stay a {key: value} bag, not be unwrapped to a bare
            # value: a Conditional branch merges this rule's result via
            # `_merge_bag_into_instance`, which requires a dict (`if not
            # isinstance(bag, dict): return`) - unwrapping here silently
            # discarded it, the real root cause of
            # `PathOrInspFactor.PathEls` staying empty for every bare
            # attribute-name factor (e.g. `KBfrei` in `KBfrei == #false`),
            # confirmed empirically (no `attach(key='PathEls', ...)` call
            # ever fired) before this fix.
        if bag:
            return bag
        return None

    # ------------------------------------------------------------------
    # Strategy 4: Reference (produces a ForwardRef, never resolved immediately)
    # ------------------------------------------------------------------
    def _resolve_or_defer(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        # A project-specific extension, already documented (see
        # 04_attributes.yml, restrictedStructureRef._inline_type,
        # status: not_applicable): restrictedStructureRef() has a type_()
        # grammar alternative ABSENT from the official manual (an anonymous
        # inline type, e.g. "TEXT*50") in addition to structureRef()/
        # ANYSTRUCTURE - this is NOT a name to resolve (a ForwardRef on
        # ctx.getText() always failed with a BuildError "unresolved and not
        # attributable to an import" for every real occurrence, e.g.
        # Holznutzungsbewilligung_V1_0.ili "TEXT*50"). Relay directly to
        # building the inline type instead of name resolution.
        has_structure_ref = ca.has_accessor(ctx, "structureRef") and bool(ca.call_list(ctx, "structureRef"))
        if ca.has_accessor(ctx, "type_") and not has_structure_ref:
            node = ca.call(ctx, "type_")
            if node is not None:
                return self.visit(node)
        name = ctx.getText()
        # `resolves_to` is already a short name (e.g. "Class"); `target`
        # (fallback) is a full qualified name (e.g.
        # "IlisMeta16.ModelData.Class") - SymbolTable always compares
        # against the instance's real metamodel class short name
        # (_qualified_class), so normalize here rather than propagating an
        # inconsistent format depending on the source.
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
            name=name,
            resolves_to_hint=expanded or None,
            rule=rule_name,
            home_model=self._current_model_name(),
            topic_extends_hint=self._current_topic_extends_hint(ctx),
        )

    def _current_topic_extends_hint(self, ctx: ParserRuleContext) -> str | None:
        """Return the raw text of ctx's enclosing TOPIC's 1st topicRef(), if any.

        Only if that TOPIC carries an EXTENDS (see
        `ForwardRef.topic_extends_hint`, `forward_refs.py`). Walks
        `ctx.parentCtx` up to the first `TopicDefContext` found. Reads
        raw text rather than consulting already-built state
        (`DataUnit.Super`): avoids depending on ForwardRef resolution
        order.
        """
        node = ctx.parentCtx
        while node is not None and type(node).__name__ != "TopicDefContext":
            node = node.parentCtx
        if node is None or node.EXTENDS() is None:
            return None
        refs = node.topicRef()
        if not refs:
            return None
        return refs[0].getText()

    def _current_model_name(self) -> str | None:
        """Return the name of the MODEL enclosing the current construction.

        Walks `_parent_stack` from the end. Used to disambiguate a short
        name still present in several models of a multi-MODEL file (real
        case: "PointStructure" declared separately in two models of the
        SAME file/symbol_table).
        """
        for inst in reversed(self._parent_stack):
            if inst._qualified_class == "IlisMeta16.ModelData.Model":
                return getattr(inst, "Name", None)
        return None

    def _expand_kind_hint(self, short_name: str) -> list[str]:
        """Expand an abstract metamodel class hint into its concrete subclasses.

        A hint can name an ABSTRACT metamodel class (e.g. "DomainType" -
        no instance is ever literally of that class, always a concrete
        subclass like EnumType/NumType/TextType) - the strict
        `_qualified_class == hint` comparison in `SymbolTable.resolve`
        never matched for `domainRef`, making disambiguation inert (real
        corpus case: `"Bodenbedeckungsart: Bodenbedeckungsart;"`, the
        attribute and its domain sharing the same short name).
        """
        qualified = next((qn for qn in self.schema.uml.qualified if qn.rsplit(".", 1)[-1] == short_name), None)
        if qualified is None:
            return [short_name]
        element = self.schema.uml.qualified[qualified]
        if not element.get("abstract"):
            return [short_name]
        descendants = [
            qn.rsplit(".", 1)[-1]
            for qn, el in self.schema.uml.qualified.items()
            if qualified in (el.get("all_superclasses") or [])
        ]
        return descendants or [short_name]

    # ------------------------------------------------------------------
    # Applying attribute_bindings
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
            if (
                binding.get("status") in ("not_applicable", "unresolved")
                and "source" not in binding
                and "target" not in binding
            ):
                continue

            try:
                self._apply_one_binding(instance, ctx, rule_name, key, binding, construction_ctx, consumed)
            except BuildError:
                source = binding.get("source") if isinstance(binding.get("source"), dict) else {}
                if source.get("optional") or binding.get("optional"):
                    # A binding marked optional whose resolution/attachment
                    # failed (e.g. modeldef.imports: Import construction by
                    # name, outside the current generic engine's scope).
                    # Doesn't block the rest of the construction.
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
                instance,
                key,
                nested,
                association=binding.get("association"),
                role=binding.get("role"),
                rule=rule_name,
            )
            return

        value = self._resolve_binding_value(ctx, rule_name, key, binding, construction_ctx, consumed)
        if key.startswith("_"):
            # Internal/documentation-only key (project convention, e.g.
            # _dispatch: describes the grammar alternatives for the
            # mapping's readability, but each real alternative
            # self-constructs via its own rule/binding - never a real
            # attribute/role name. The computation above is still kept
            # (possible side effects, e.g. construction-context
            # propagation), only the literal attach is skipped - same
            # convention as _attach_unclaimed_results (needed for e.g.
            # `factor.dispatch`, which has no 'dispatch' attribute/role on
            # the metamodel to attach under).
            return
        if value is None:
            # Nothing to attach - either a binding with no source
            # (notes-only), or an absent value (optional), or a rule
            # visited only for its side effects (e.g. topicDef.definitions:
            # each classDef/etc. self-attaches via ITS OWN parent:, this key
            # has nothing to receive back - see its note).
            return
        if isinstance(value, list) and not value:
            # Empty multi-value list (nothing present in the .ili): nothing to attach.
            return
        if isinstance(value, ForwardRef):
            if binding.get("association") == "Inheritance" and binding.get("role") == "Super":
                # EXTENDS: never fatal if unresolved, see
                # ForwardRef.graceful (symmetric to domainRef/BaseClass).
                value.graceful = True
                hint = value.resolves_to_hint
                hints = hint if isinstance(hint, list) else ([hint] if hint else [])
                if "SubModel" in hints:
                    # TOPIC EXTENDS: only this case resolves to a SubModel
                    # rather than directly to the right ExtendableME
                    # subclass (classDef/structureDef/domainDef already
                    # resolve to Class/DomainType) - see
                    # ForwardRef.resolve_via_twin.
                    value.resolve_via_twin = True
            if binding.get("association") == "LineCoord" and binding.get("role") == "CoordType":
                # VERTEX (XTF geometry): a named CoordType can live in an
                # imported model not loaded via --repo (same category of
                # limitation as BaseClass/EXTENDS) - never fatal.
                value.graceful = True
            self.attachment.attach(
                instance,
                key,
                value,
                association=binding.get("association"),
                role=binding.get("role"),
                rule=rule_name,
            )
            self.forward_refs.register_pending(value, instance, self._resolved_field_name(instance, key, binding))
            return
        self.attachment.attach(
            instance,
            key,
            value,
            association=binding.get("association"),
            role=binding.get("role"),
            rule=rule_name,
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
        self,
        ctx: ParserRuleContext,
        rule_name: str,
        key: str,
        binding: dict,
        construction_ctx: dict,
        consumed: set[int],
    ) -> MetaInstance | None:
        target = binding["target"]
        sub_bindings = {
            k: v
            for k, v in binding.items()
            if k not in ("target", "note", "association", "role") and isinstance(v, dict) and "source" in v
        }
        if not sub_bindings:
            # target: with no real source: sub-binding (e.g.
            # modeldef/topicDef.default_class_oid): the associated note
            # explicitly documents procedural logic not covered by the
            # generic engine (e.g. "apply afterwards to each Class in the
            # topic that doesn't define its own OID") - not a standard
            # nested construction, so no hollow instance is built to avoid
            # wrongly attaching it.
            return None
        instance = self.registry.new_instance(target)
        instance._source_ctx = ctx
        with self._pushed_parent(instance):
            try:
                self._apply_bindings(instance, ctx, rule_name, sub_bindings, construction_ctx, consumed)
            except BuildError:
                # A nested construction (e.g. attrTypeDef._collection ->
                # MultiValue) represents ONE grammar alternative among others,
                # not an always-present structure - if one of its required
                # sub-bindings (a discriminant marker, e.g. BAG|LIST) fails,
                # that's a sign this alternative simply wasn't taken here, not a
                # real error. Discards the whole nested construction rather than
                # letting it propagate (same logic as Conditional: inapplicable
                # branch -> nothing built).
                return None
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
        """Build one `target` instance per element of a resolved list.

        For `for_each:` bindings, instead of a single instance - needed
        for rules where a grammatical loop (e.g. `modeldef.imports`) must
        produce N distinct linked instances, not a single standard nested
        construction (`_build_nested` assumes one instance per binding).
        Each sub-binding can read the loop's current element via
        `source: {field: null, context_key: '__item__'}`.
        """
        items = self._resolve_binding_value(
            ctx, rule_name, key, {"source": binding["for_each"]}, construction_ctx, consumed
        )
        if items is None:
            return
        if not isinstance(items, list):
            items = [items]
        if not items:
            return

        target = binding["target"]
        sub_bindings = {
            k: v
            for k, v in binding.items()
            if k not in ("for_each", "target", "note", "association", "role") and isinstance(v, dict)
        }
        for item in items:
            item_ctx = {**construction_ctx, "__item__": item}
            built = self.registry.new_instance(target)
            built._source_ctx = ctx
            self._apply_bindings(built, ctx, rule_name, sub_bindings, item_ctx, consumed)
            if binding.get("association"):
                self.attachment.attach(
                    instance,
                    key,
                    built,
                    association=binding.get("association"),
                    role=binding.get("role"),
                    rule=rule_name,
                )
            else:
                # No collection role on the metamodel side (e.g. Import: a
                # pure ImportingP<->ImportedP association, no inverse
                # "Model.Imports" association declared in
                # ilismeta16-associations.yml) - still keeps the built
                # instances, under the binding's raw key, so they stay
                # reachable (otherwise lost to the garbage collector, no
                # other reference holds them).
                current = getattr(instance, key, None)
                if current is None:
                    current = []
                    setattr(instance, key, current)
                current.append(built)

    def _resolve_binding_value(
        self,
        ctx: ParserRuleContext,
        rule_name: str,
        key: str,
        binding: dict,
        construction_ctx: dict,
        consumed: set[int],
    ) -> Any:
        source = binding.get("source")
        if not isinstance(source, dict):
            return None
        # This guard used to mark "consumed" only if
        # `_alt_matches(ctx, source["alt"])` - a mechanism removed from the
        # whole mapping (see source_resolver.py: `ctx.getAltNumber()` always
        # returns 0 for this grammar, so `alt: <int>` was ALWAYS false and
        # this guard NEVER marked a node shared between alternatives as
        # consumed - harmless only because resolve_source itself also
        # always returned None for these same bindings). The 15 affected
        # bindings (e.g. predicate: alt1 = bare factor, alt3 = DEFINED LPAR
        # factor RPAR - same "factor" accessor) now rely on their
        # accessor's NATURAL presence (already exclusive to the grammar
        # alternative actually taken, verified case by case): consuming
        # unconditionally is therefore correct, this guard is no longer
        # needed.
        for name in self._accessor_names_in_source(source):
            if ca.has_accessor(ctx, name):
                for node in ca.call_list(ctx, name):
                    consumed.add(id(node))
        rule_map = binding.get("rule") if isinstance(binding.get("rule"), dict) else binding.get("mapping")
        wrap_map = binding.get("wrap") if isinstance(binding.get("wrap"), dict) else None
        return resolve_source(
            ctx,
            source,
            rule=rule_name,
            construction_context=construction_ctx,
            builder=self,
            rule_map=rule_map,
            binding_key=key,
            wrap_map=wrap_map,
        )

    def visit_wrapped(self, node: ParserRuleContext, target: str, rule_name: str) -> MetaInstance:
        """Visit a bag rule, wrapped into a typed metamodel instance.

        For a Container/ValueObject rule (e.g. `numeric()`/`enumeration()`),
        used when an enclosing rule knows which class this bag actually
        represents. The instance is pushed onto the construction stack
        BEFORE the visit: some children of the bag rule self-attach via
        their OWN `parent:` DURING the visit - wrapping afterward would
        attach them to the wrong parent.
        """
        instance = self.registry.new_instance(target)
        instance._source_ctx = node
        self._attach_pending_meta_attributes(instance, node)
        with self._pushed_parent(instance):
            bag = self.visit(node)

        self._merge_bag_into_instance(instance, bag, rule_name)
        return instance

    def _merge_bag_into_instance(self, instance: MetaInstance, bag: Any, rule_name: str) -> None:
        """Merge a bag dict onto `instance`.

        The bag is the result of `_relay` on a Container rule - attaches
        every non-empty/non-hollow field, resolves pending ForwardRefs.
        Shared by `visit_wrapped` and `_build_conditional`'s
        `when_present` branch - previously `_build_conditional` never
        visited the matched branch's node at all, losing all of
        `numeric()`/`textType()`'s own content for any numeric/text OID
        rule (e.g. `I32OID = OID 0..2147483647;`).
        """
        if not isinstance(bag, dict):
            return
        for field, value in bag.items():
            if field == "Elements":
                continue  # already attached via enumElement.parent: during the visit above
            if value is None or (isinstance(value, list) and not value) or self._is_hollow(value):
                # None/empty list/"hollow" (a nested instance whose fields
                # are all None/empty, e.g. numeric._refsys_clause when no
                # reference clause is present): nothing to attach.
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
                    ctx,
                    source,
                    rule=rule_name,
                    construction_context=construction_ctx,
                    builder=self,
                    rule_map=rule_map,
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

    @contextmanager
    def _pushed_parent(self, instance: MetaInstance) -> Iterator[MetaInstance]:
        """Push `instance` onto `_parent_stack` for the scope of the `with` block, popping it on any exit."""
        self._parent_stack.append(instance)
        try:
            yield instance
        finally:
            self._parent_stack.pop()

    @contextmanager
    def _pushed_context(self, rule_name: str, instance: MetaInstance) -> Iterator[MetaInstance]:
        """Push both the construction context AND `_parent_stack` for `instance`, popping both on any exit.

        Same pairing as 4 call sites used to duplicate by hand (push both, pop `_parent_stack` first).
        """
        self._push_construction_context(rule_name, instance)
        self._parent_stack.append(instance)
        try:
            yield instance
        finally:
            self._parent_stack.pop()
            self._pop_construction_context()

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

    def _register_unit_alias(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Register a UNIT's bracketed short-name alias under the SAME instance too.

        `ctx.Name(1)` is the optional bracketed alias (e.g.
        `CubicMeterPerSecond [m3sec] = ...;`). The metamodel's `Unit`
        class has no dedicated ShortName attribute, so without this the
        alias is never registered anywhere - confirmed on real corpus
        data: a later `[m3sec]` unitRef elsewhere raised a `BuildError`.
        """
        names = ctx.Name()
        if len(names) < 2:
            return
        alias = names[1].getText()
        self.symbol_table.register(self._qualify_name(alias), instance)

    def _fix_existence_constraint_attr(self, instance: MetaInstance) -> None:
        """Re-wrap `ExistenceConstraint.Attr` as a real `PathOrInspFactor` instance.

        Same class of gap as `_stash_aggregation_key`/`_stash_inspection_path`:
        the declarative `Attr` binding fires and sets `instance.Attr`, but
        only with the raw Container bag - the wrapping into a proper
        instance normally happens in `factor()`'s own `Conditional` branch
        merge, which `existenceConstraint`'s grammar never routes through.
        """
        bag = getattr(instance, "Attr", None)
        if not isinstance(bag, dict):
            return
        factor = self.registry.new_instance("IlisMeta16.ModelData.PathOrInspFactor")
        self._merge_bag_into_instance(factor, bag, "existenceConstraint")
        instance.Attr = factor

    def _fix_class_extended_super(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Resolve `Super` for `CLASS X (EXTENDED)` reopening a same-named class from the enclosing TOPIC's base.

        Real corpus gap (`ISOS_V2.ili`): `TOPIC ISOS EXTENDS
        ISOS_V2.ISOSBase = CLASS Ortsbild (EXTENDED) = ...` - eCH-0031
        §3.5.2: there is only ONE class C, not two. `classDef.Super` only
        fires for an EXPLICIT `EXTENDS classOrStructureRef` clause -
        `(EXTENDED)` has no such clause (its target is implicit: the
        same-named class in the topic named by the ENCLOSING topic's own
        `EXTENDS`), so it was silently left with no `Super` at all.

        Approximates "one class C" as ordinary single inheritance rather
        than a true merged instance, reusing the existing Super-chain
        walk. The `Super` reference is built ALREADY FULLY QUALIFIED
        (`f"{hint}.{name}"`) rather than a bare short name: a bare name is
        deliberately AMBIGUOUS here (two classes are genuinely named
        that), and `SymbolTable.resolve`'s short-name fallback could
        resolve straight back to THIS SAME instance - confirmed
        empirically as a real self-loop before this fix.
        """
        if getattr(instance, "Super", None) is not None:
            return  # an explicit `EXTENDS classOrStructureRef` already won - mutually exclusive per spec
        if not ca.is_present(ctx, "EXTENDED"):
            return
        name = getattr(instance, "Name", None)
        hint = self._current_topic_extends_hint(ctx)
        if not name or not hint or "." not in hint:
            return
        ref = ForwardRef(
            name=f"{hint}.{name}",
            resolves_to_hint=["Class"],
            rule="classDef",
            graceful=True,
        )
        self.attachment.attach(instance, "Super", ref, association="Inheritance", role="Super", rule="classDef")
        self.forward_refs.register_pending(ref, instance, "Super")

    _RELATION_KIND_LT_GT = {(True, False): "Less", (False, True): "Greater", (True, True): "NotEqual"}

    def _relation_kind(self, ctx: ParserRuleContext) -> str | None:
        """Resolve `relation()`'s matched alternative to its `CompoundExpr.Operation` child value.

        No real ANTLR accessor for a composed multi-token alternative like
        "EQ EQ"/"LT GT" - reads `RelationContext`'s real accessors
        directly. `EQ()` is a MULTI accessor (`>= 2` distinguishes "==").
        `LT()`/`GT()` are each used standalone AND together ("<>") - only
        checking whether BOTH are present disambiguates it.
        """
        if len(ca.call_list(ctx, "EQ")) >= 2:
            return "Equal"
        if ca.call(ctx, "NOT_EQ") is not None:
            return "NotEqual"
        if ca.call(ctx, "LTEQ") is not None:
            return "LessOrEqual"
        if ca.call(ctx, "GTEQ") is not None:
            return "GreaterOrEqual"
        has_lt = ca.call(ctx, "LT") is not None
        has_gt = ca.call(ctx, "GT") is not None
        return self._RELATION_KIND_LT_GT.get((has_lt, has_gt))

    def _set_path_el_kind(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Resolve `pathEl()`'s matched alternative to its `PathEl.Kind` value.

        6 of the 9 alternatives share the same leading `Name` token, so no
        composed field name matches a real ANTLR accessor. Checks the
        unambiguous alternatives first, then disambiguates the 3
        remaining bare-`Name` ones via `COLON` (Role), a double `EQ`
        (MetaObject) or `LSBR` (ViewBase) - anything else is
        ReferenceAttr.
        """
        if ca.call(ctx, "THIS") is not None:
            instance.Kind = "This"
        elif ca.call(ctx, "THISAREA") is not None:
            instance.Kind = "ThisArea"
        elif ca.call(ctx, "THATAREA") is not None:
            instance.Kind = "ThatArea"
        elif ca.call(ctx, "PARENT") is not None:
            instance.Kind = "Parent"
        elif ca.call(ctx, "associationPath") is not None:
            instance.Kind = "AssocPath"
        elif ca.call(ctx, "attributeRef") is not None:
            instance.Kind = "Attribute"
        elif ca.call(ctx, "COLON") is not None:
            instance.Kind = "Role"
        elif len(ca.call_list(ctx, "EQ")) >= 2:
            instance.Kind = "MetaObject"
        elif ca.call(ctx, "LSBR") is not None:
            instance.Kind = "ViewBase"
        else:
            instance.Kind = "ReferenceAttr"

    def _normalize_enumeration_const_value(self, instance: MetaInstance) -> None:
        """Join a `Constant(Type=Enumeration)`'s raw `enumerationConst` bag into a plain dotted-path string.

        `constant()`'s generic `Value` binding assigns whichever sub-rule
        matched AS-IS - correct for every OTHER alternative, but
        `enumerationConst` is a Container with 2 REAL keys (`Value`: the
        Name segments, `Others`: a bool), never unwrapped to a bare value
        by `_relay`. Without this, `Constant.Value` for e.g. `#false` was
        the raw bag instead of the plain dotted-path string the metamodel
        declares.
        """
        value = getattr(instance, "Value", None)
        if not isinstance(value, dict):
            return
        segments = value.get("Value") or []
        if isinstance(segments, str):
            segments = [segments]
        if value.get("Others"):
            segments = [*segments, "OTHERS"]
        instance.Value = ".".join(segments)

    def _set_defined_subexpression(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Set `UnaryExpr(Operation=Defined).SubExpression` from `DEFINED LPAR factor RPAR`'s own factor.

        `factor` has no direct role/attribute name to bind against via
        the generic mechanism. `UnaryExpr.SubExpression` is typed
        `Expression`, and `factor()` builds a `Factor` (`Factor EXTENDS
        Expression`), so a direct assignment is enough.
        """
        factor_node = ca.call(ctx, "factor")
        if factor_node is None:
            return
        value = self.visit(factor_node)
        if value is not None:
            instance.SubExpression = value

    def _set_predefined_function_call(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Set `FunctionCall.Function`/`.Arguments` from `factor`'s alt4 (predefined functions, e.g.
        `INTERLIS.len(...)`).

        `Function` needs 2 tokens joined (`INTERLIS` + the matched
        `Name`/`URI`/`UUIDOID`), and `Arguments` needs each bare
        `expression()` wrapped in a synthetic `ActualArgument` - this alt
        never goes through the `argument()` rule, so neither has a
        matching direct accessor.
        """
        names = ctx.Name()
        name_token = names[0] if names else (ctx.URI() or ctx.UUIDOID())
        if name_token is None:
            return
        instance.Function = f"INTERLIS.{name_token.getText()}"
        arguments = []
        for expr_ctx in ctx.expression():
            value = self.visit(expr_ctx)
            if value is None:
                continue
            argument = self.registry.new_instance("IlisMeta16.ModelData.ActualArgument")
            argument.Kind = "Expression"
            argument.Expression = value
            arguments.append(argument)
        instance.Arguments = arguments

    def _qualify_name(self, name: str) -> str:
        parts = [getattr(inst, "Name", None) for inst in self._parent_stack if getattr(inst, "Name", None)]
        parts.append(name)
        return ".".join(parts)

    def _apply_sibling_bag_values(self, result: MetaInstance, real_values: dict) -> None:
        """Apply sibling bag values that `result` actually inherits/owns.

        A Container rule whose real content is ANOTHER value from the
        same bag (e.g. `domainDef._domain_content`, `wrap:` on bare
        `numeric()`/`enumeration()`) used to lose these values - pushed
        only into the construction context, never applied onto the
        returned instance itself. Concrete consequence found: a DOMAIN
        named via a bare enumeration produced an `EnumType` with no
        `Name` -> never registered in the SymbolTable -> any later
        reference by name failed with a `BuildError`.
        """
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
    # Sweep of children not claimed by attribute_bindings (e.g.
    # modeldef -> definitions, never mentioned in modeldef's bindings -
    # needed because the structural relationship goes through `parent:` on
    # the child rule, not through an explicit binding on the enclosing
    # rule).
    # ------------------------------------------------------------------
    def _sweep_unclaimed_children(self, ctx: ParserRuleContext, consumed: set[int]) -> list[tuple[str, Any]]:
        results = []
        for child in ctx.children or []:
            if isinstance(child, ParserRuleContext) and id(child) not in consumed:
                results.append((self._rule_name(child), self.visit(child)))
        return results

    def _attach_unclaimed_results(
        self, instance: MetaInstance, sweep_results: list[tuple[str, Any]], rule_name: str
    ) -> None:
        """Try to attach unclaimed children that produced a concrete result.

        For a child not claimed by attribute_bindings whose rule has NO
        `parent:` of its own but produces a concrete result: tries to
        attach it via the association connecting the two known classes
        (`AttachmentResolver.find_association_connecting`). Best-effort:
        silent if no association connects the two classes.
        """
        for child_rule, value in sweep_results:
            if value is None:
                continue
            if child_rule == "constraintDef":
                # constraintDef is a Container dispatching to
                # mandatoryConstraint/uniquenessConstraint/setConstraint/...
                # - each of THOSE declares its own `parent:`
                # (ClassConstraint/Constraint) and has already self-attached
                # the built constraint. constraintDef itself has no
                # `parent:`, so without this the same constraint gets
                # attached a SECOND time below via
                # find_association_connecting (View/Class <-> Constraint is a
                # real association). A `classDef` never hits this - its
                # `constraintDef`s sit under the `classOrStructureDef`
                # wrapper - but `viewDef` has `constraintDef` as a direct
                # child (a real corpus case: every DMAV `*_Gueltig` VIEW
                # carries a view-level `UNIQUE CHxxxxxx:`).
                continue
            if child_rule == "formationDef":
                # viewDef-only: a pure dispatcher relaying to projection()/
                # join()/union()/aggregation()/inspection() (spec/grammar/
                # mapping/09_views_graphics.yml, "No direct IlisMeta16
                # instance expected"), none of which declare their OWN
                # `parent:` either (they're Containers too) - so the
                # `child_entry.parent` check below can't detect that
                # whatever bubbles up (a RenamedBaseView or list thereof)
                # was already self-attached several levels down by
                # renamedViewableRef's OWN `parent:` (BaseViewDef). Without
                # this, a single-base formationDef (PROJECTION OF) got its
                # one RenamedBaseView re-attached a SECOND time here
                # (find_association_connecting silently succeeds, since
                # View<->RenamedBaseView is a real association) - a
                # multi-base one (JOIN OF/UNION OF) never hit this because
                # its bubbled-up value is a list, not a MetaInstance, so the
                # dict/instance branches below never matched it anyway.
                continue
            child_entry = self.spec.get(child_rule)
            if child_entry is not None and child_entry.parent:
                continue  # already self-attached via its own parent:
            if isinstance(value, dict):
                # The unclaimed rule is a bag (Container/Dispatcher with
                # several keys, e.g. attrTypeDef -> {Mandatory, Type,
                # _collection}): each non-empty key is attached
                # individually onto `instance` under its own name (same
                # mechanism as wrap_bag_as_instance, but onto the existing
                # ENCLOSING instance rather than a new one). If a key
                # doesn't attach onto `instance` (e.g. Mandatory, documented
                # as NOT concerning AttrOrParam but the concrete Type
                # produced alongside it in the same bag - attrTypeDef.
                # Mandatory), retry on a sibling MetaInstance value from the
                # same bag before giving up.
                if child_rule == "attrTypeDef":
                    self._resolve_predefined_type_sentinel(instance, value)
                siblings = [v for v in value.values() if isinstance(v, MetaInstance)]
                has_unresolved_sibling = any(isinstance(v, ForwardRef) for v in value.values())
                for key, sub_value in value.items():
                    if (
                        sub_value is None
                        or (isinstance(sub_value, list) and not sub_value)
                        or self._is_hollow(sub_value)
                    ):
                        continue
                    if key.startswith("_"):
                        # Internal key (project convention, e.g. _collection,
                        # _refsys_clause): never a real attribute/role name,
                        # do NOT try attach(instance, "_collection", ...)
                        # (would always fail by construction) - look
                        # directly for the association connecting the two
                        # KNOWN classes (same mechanism as for an unclaimed
                        # MetaInstance at the top level). Best-effort,
                        # silent if no association fits.
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
                            # `attach()` (above) requires a role literally
                            # named `key` (here "Type") on an association
                            # connecting the two classes - but roleDef() also
                            # calls attrTypeDef() (inline type role), and NO
                            # Role<->(Class or DomainType) association is
                            # named "Type" (the role is named differently,
                            # e.g. BaseClass). Generic fallback by KNOWN
                            # CLASS (same mechanism as the
                            # `isinstance(value, MetaInstance)`/`ForwardRef`
                            # block further below in this method, for a RAW
                            # unclaimed child) - applied here for a value
                            # nested in a bag, for a `sub_value` that is a
                            # MetaInstance OR a ForwardRef (target class hint
                            # via resolves_to_hint).
                            candidate_classes: list[str] = []
                            if isinstance(sub_value, MetaInstance):
                                candidate_classes = [sub_value._qualified_class]
                            elif isinstance(sub_value, ForwardRef):
                                hints = (
                                    sub_value.resolves_to_hint
                                    if isinstance(sub_value.resolves_to_hint, list)
                                    else ([sub_value.resolves_to_hint] if sub_value.resolves_to_hint else [])
                                )
                                candidate_classes = [
                                    qn
                                    for hint in hints
                                    for qn in self.schema.uml.qualified
                                    if qn.rsplit(".", 1)[-1] == hint
                                ]
                            for qualified_class in candidate_classes:
                                found = self.attachment.find_association_connecting(
                                    instance._qualified_class, qualified_class
                                )
                                if found is not None:
                                    _assoc_name, role, upper = found
                                    self.attachment._set_field(instance, role, sub_value, upper)
                                    attached_on = instance
                                    attached_field = role
                                    break
                        if attached_on is None:
                            # `key`'s own attach failed (e.g. "Mandatory",
                            # not a real AttrOrParam attribute) - a sibling
                            # bag key (e.g. attrTypeDef.Type, meant to be
                            # built alongside it) has nowhere for THIS key
                            # to land. Two distinct, non-critical cases
                            # (never a `sub_value` that IS ITSELF the broken
                            # reference - see the `raise` below, unchanged
                            # for that case):
                            if not isinstance(sub_value, ForwardRef) and has_unresolved_sibling:
                                # Case 1: this bag's Type is still an
                                # unresolved ForwardRef (e.g. a reference to
                                # an existing, possibly SHARED, named DOMAIN
                                # - "Geom: MANDATORY Coord2D;"). `Mandatory`
                                # specifically is queued in `_pending_mandatory_overrides`,
                                # resolved by
                                # `_apply_pending_mandatory_overrides` AFTER
                                # `forward_refs.resolve_all()` - a private,
                                # Mandatory=True clone of the resolved Type
                                # replaces `instance.Type`, never mutating
                                # the shared domain instance every OTHER
                                # attribute referencing it might also
                                # resolve to (applying it directly on the
                                # shared instance here, before resolution,
                                # would be wrong regardless of timing - which
                                # use would be right if several differ?).
                                # Anything else in this situation still has
                                # nowhere principled to land - silently
                                # skipped, same as before this fix.
                                if key == "Mandatory" and sub_value is True:
                                    self._pending_mandatory_overrides.append(instance)
                                continue
                            if not isinstance(sub_value, ForwardRef) and not siblings:
                                # Case 2: NO sibling MetaInstance/ForwardRef
                                # exists in this bag AT ALL to even attempt
                                # an attach - confirmed real on `HAli:
                                # MANDATORY HALIGNMENT;`
                                # (CHBase_Part6_GRAPHICANNOTATIONS_V1/V2.ili):
                                # `alignmentType()` (grammar - "HALIGNMENT"/
                                # "VALIGNMENT" are RESERVED tokens,
                                # grammatically impossible to declare via
                                # domainDef - confirmed, `DOMAIN HALIGNMENT
                                # = ...` is a syntax rejection, not a
                                # missing binding) deliberately builds NO
                                # instance at all (target: null, same
                                # category as booleanType/oIDType for
                                # BOOLEAN/ANYOID/UUIDOID) - so its bag only
                                # contains an opaque TEXT value
                                # ("resolved_type"), never an instance for
                                # Mandatory to land on. Before the original
                                # fix for this case, `has_unresolved_sibling`
                                # stayed FALSE (no ForwardRef, just an inert
                                # dict) and the code fell into the `raise` -
                                # `interlis build`/`validate` crashed
                                # entirely as soon as an attribute typed
                                # HALIGNMENT/VALIGNMENT MANDATORY, even
                                # though this type otherwise stays
                                # deliberately uninterpreted (same
                                # documented limitation as BOOLEAN) -
                                # silently dropping Mandatory (secondary
                                # info) rather than a total crash for an
                                # otherwise valid attribute.
                                continue
                            raise
                    if attached_on is not None and isinstance(sub_value, ForwardRef):
                        # A ForwardRef nested INSIDE the bag (e.g.
                        # attrTypeDef.Type when attrType() dispatches to
                        # domainRef(), a reference to an existing named
                        # domain) used to never be registered for deferred
                        # resolution - unlike the "bare" ForwardRef case (not
                        # nested in a dict, already handled further below in
                        # this method) - it stayed a literal ForwardRef,
                        # never replaced by the real instance or by
                        # UnresolvedNamedReference, silently, `resolve_all()`
                        # never seeing it.
                        self.forward_refs.register_pending(sub_value, attached_on, attached_field)
                continue
            if isinstance(value, list):
                continue
            if isinstance(value, ForwardRef):
                # A Reference-kind rule (e.g. domainRef/classRef) visited as
                # an unclaimed child (e.g. attrTypeDef -> attrType ->
                # domainRef, never captured by an explicit binding on
                # attributeDef - attributeDef.attribute_bindings has no Type
                # key) produces a ForwardRef, not yet a MetaInstance - the
                # `isinstance(value, MetaInstance)` check below silently
                # ignored it, losing the Type attribute for EVERY domain/
                # class reference used as an attribute type (e.g. "Kind:
                # MANDATORY Base.PersonKind;"), local or cross-file. Same
                # association-by-class mechanism as for a MetaInstance
                # below, but via the ForwardRef's target class hint
                # (resolves_to_hint, a short name - its full qualified name
                # is looked up in the schema) since the real class isn't
                # known yet before resolution.
                hints = (
                    value.resolves_to_hint
                    if isinstance(value.resolves_to_hint, list)
                    else ([value.resolves_to_hint] if value.resolves_to_hint else [])
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
