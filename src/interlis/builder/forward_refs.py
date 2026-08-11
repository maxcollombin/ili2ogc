"""Forward-reference resolution: single ANTLR visit pass, deferred lookup.

Resolution happens via a symbol table, not immediately during the visit.

Each Reference-kind rule (classRef, structureRef, associationRef,
domainRef, unitRef, viewRef, graphicRef, topicRef, metaDataBasketRef,
metaObjectRef, viewableRef) produces a ForwardRef placed directly in the
relevant field via AttachmentResolver, instead of resolving immediately.
A final pass (resolve_all) replaces each ForwardRef with the real instance
found in the SymbolTable (current file), or - if a ModelRepository is
provided (multi-file resolution, see repository.py) - in the matching
imported model's table, loaded on demand. Without a repository (or if the
model remains unfindable), the name becomes a documented
UnresolvedNamedReference instead of an exception.
"""
from dataclasses import dataclass, field
from typing import Any

from interlis.builder.errors import BuildError, UnresolvedNamedReference


@dataclass
class ForwardRef:
    """Placeholder set on a field, replaced by the final resolution pass."""

    name: str
    resolves_to_hint: str | list[str] | None = None
    rule: str = ""
    # True for references whose VERY NATURE guarantees they always target
    # an imported model (e.g. modeldef.imports - IMPORTS by definition
    # names ANOTHER model, never a local declaration) - bypasses the
    # generic has_prefix heuristic (designed for references that COULD be
    # local, e.g. classRef/associationRef).
    always_external: bool = False
    # Name of the MODEL enclosing the construction that produced this
    # ForwardRef - disambiguates a short name present in SEVERAL models of
    # the same multi-MODEL file (e.g. "PointStructure" declared separately
    # in BaseModel_SectoralPlans_LV03_V1_4 AND _LV95_V1_4, same file, SAME
    # symbol_table for both). None if not determinable (e.g. the
    # predefined INTERLIS model).
    home_model: str | None = None
    # Raw text of the enclosing TOPIC's first topicRef(), ONLY if that
    # TOPIC has an EXTENDS (e.g. "CatalogueObjects_V1.Catalogues" for a
    # reference produced inside `TOPIC AxisCatalogs EXTENDS
    # CatalogueObjects_V1.Catalogues = ...`) - None if the enclosing TOPIC
    # has no EXTENDS, or if no TOPIC encloses this reference. Direct
    # citation, eCH-0031 V2.1.0 §3.5.4 "Namensraume": "Erweitert ein
    # Modellierungselement ein anderes, werden seinen Namensraumen alle
    # Namen des Basis-Modellierungselementes zugefuegt" (when a modeling
    # element extends another, all names of the base element are added to
    # its namespace) - a TOPIC B EXTENDS TOPIC A makes all short names
    # declared in A visible inside B, WITHOUT an IMPORTS UNQUALIFIED.
    # Used as a last resort by `ForwardRefResolver._resolve_one` for an
    # unqualified name not found locally, BEFORE the existing IMPORTS
    # UNQUALIFIED fallback.
    topic_extends_hint: str | None = None
    # True for a ForwardRef carried by the Super role of the Inheritance
    # association (classDef/structureDef/topicDef EXTENDS) - an unresolved
    # EXTENDS must NEVER crash the whole build, symmetric to the existing
    # policy for domainRef/BaseClass: degrades to UnresolvedNamedReference
    # rather than BuildError, including when `topic_extends_hint` was
    # tried and failed (e.g. the external model is absent from the given
    # `--repo`). Set by InterlisModelBuilder._apply_one_binding, never by
    # _resolve_or_defer (which doesn't yet know the target role/association
    # at that stage).
    graceful: bool = False
    # True ONLY for the Super of a `TOPIC EXTENDS topicRef`: `topicRef`
    # resolves to a SubModel (the "Topic" in the Package sense, confirmed
    # in ilismeta16-classes.yml - does NOT extend ExtendableME), while the
    # Inheritance association (Sub/Super) targets ExtendableME - only that
    # SubModel's TWIN DataUnit (same topicDef, see
    # `InterlisModelBuilder._build_multi_target`/`_twin`) is a real
    # instance of it. `resolve_all` substitutes this twin AFTER name
    # resolution, rather than storing the resolved SubModel directly
    # (incompatible type for the target attribute).
    resolve_via_twin: bool = False


@dataclass
class _Pending:
    ref: ForwardRef
    container: Any
    field: str
    is_list_item: bool = False


class SymbolTable:
    """Maps a qualified (or short, if unambiguous) name to a built instance.

    Populated on the fly for every Instance-kind instance built whose class
    has a Name attribute (direct or inherited).
    """

    def __init__(self):
        self._qualified: dict[str, Any] = {}
        self._by_short_name: dict[str, list[Any]] = {}
        # Names of models imported via `IMPORTS UNQUALIFIED X` in THIS file
        # (e.g. {"INTERLIS"}) - never persisted on the metamodel side
        # (Import has no attribute of its own for UNQUALIFIED - see
        # spec/grammar/mapping/02_packages.yml, _imports_unqualified), but
        # needed here: only an explicitly UNQUALIFIED import lets an
        # unqualified reference resolve into an imported model instead of
        # staying strictly local to the current file (Reference Manual
        # eCH-0031 V2.1.0 §3.5.1). Populated by
        # InterlisModelBuilder._register_unqualified_imports.
        self.unqualified_imports: set[str] = set()

    def register(self, qualified_name: str, instance: Any) -> None:
        self._qualified[qualified_name] = instance
        short = qualified_name.rsplit(".", 1)[-1]
        self._by_short_name.setdefault(short, []).append(instance)

    def all_registered(self) -> list[Any]:
        """Return all registered instances, deduplicated by identity.

        An alias (e.g. from rekey_model_prefix) can make two different keys
        point to the SAME object. Used by xtf/schema.py to enumerate known
        associations without reaching into private state.
        """
        seen: set[int] = set()
        result = []
        for instance in self._qualified.values():
            if id(instance) not in seen:
                seen.add(id(instance))
                result.append(instance)
        return result

    def resolve(self, name: str, kind_hint: str | list[str] | None = None, home_model: str | None = None) -> Any | None:
        if name in self._qualified:
            return self._qualified[name]
        # A QUALIFIED name whose prefix does NOT designate this table
        # (`has_prefix` False - an imported model, NOT the current file)
        # must NEVER fall back to the short-name fallback below - otherwise
        # a short-name collision with a LOCAL symbol (e.g. `STRUCTURE
        # ModInfo EXTENDS WithLatestModification_V1.ModInfo` - "ModInfo"
        # exists LOCALLY AND in the imported model) silently resolves to
        # the WRONG candidate (e.g. ModInfo.Super pointing to ITSELF, a
        # self-loop).
        # `ForwardRefResolver._resolve_one` calls THIS `resolve()` first
        # and only tries its own cross-file fallback (`has_prefix`/
        # `ModelRepository.resolve_external`) if `resolve()` returns
        # `None` - without this guard, that fallback was therefore NEVER
        # reached once a short-name collision existed. Unqualified names
        # (`"." not in name`) are unaffected (`has_prefix` always returns
        # False for them, out of scope of this guard).
        if "." in name and not self.has_prefix(name):
            return None
        short = name.rsplit(".", 1)[-1]
        candidates = self._by_short_name.get(short, [])
        if kind_hint is not None and len(candidates) > 1:
            # The same short name can designate DIFFERENT metamodel
            # entities (e.g. a CLASS "MetaElement" and an anonymous role
            # (named after its target class) "MetaElement" in another
            # association, or "Localisation" as both a CLASS and an
            # anonymous association role - found on
            # DMAV_AdressesDeBatiments_V1_0.ili) - kind_hint (the
            # Reference-kind binding's target/resolves_to, e.g. "Class", or
            # several possible values e.g. viewableRef: ["Class", "View"])
            # resolves the ambiguity by keeping only instances of the
            # targeted metamodel class(es).
            hints = kind_hint if isinstance(kind_hint, list) else [kind_hint]
            filtered = [c for c in candidates if getattr(c, "_qualified_class", "").rsplit(".", 1)[-1] in hints]
            if len(filtered) == 1:
                return filtered[0]
            candidates = filtered if filtered else candidates
        if len(candidates) > 1 and home_model:
            # The same short name can also be declared in SEVERAL MODELS of
            # the SAME .ili file (multi-MODEL, e.g. "PointStructure" in
            # both BaseModel_SectoralPlans_LV03_V1_4 AND _LV95_V1_4,
            # sharing this SAME symbol_table).
            # kind_hint alone can't resolve this ambiguity (both candidates
            # are the SAME concrete metamodel class, e.g.
            # Class[Kind=Structure]) - fall back to the MODEL enclosing the
            # construction that requested this resolution (see
            # InterlisModelBuilder._current_model_name), looking directly
            # in `_qualified` (candidates from `_by_short_name` don't carry
            # their own qualified name).
            candidate_ids = {id(c) for c in candidates}
            in_model = [
                v for k, v in self._qualified.items()
                if k.startswith(home_model + ".") and k.rsplit(".", 1)[-1] == short and id(v) in candidate_ids
            ]
            if len(in_model) == 1:
                return in_model[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def rekey_model_prefix(self, old_prefix: str, new_prefix: str) -> None:
        """Add an alias `new_prefix.X` -> same instance for every `old_prefix.X` entry.

        The original entry is kept, never removed. Used ONLY by
        ModelRepository for the predefined INTERLIS model (see
        repository.py, _PREDEFINED_INTERLIS_SOURCE): its declared MODEL
        carries an internal name different from the real name documented
        by the manual (lexer constraint - "INTERLIS" is a reserved token,
        not a valid Name); this fixes that up once the model is built so
        that qualified references written by a user (`INTERLIS.I32OID`)
        find an exact match instead of relying only on `resolve()`'s
        short-name fallback. Touches ONLY `_qualified`, never
        `_by_short_name`: the instance is already there from its original
        `register()` call - touching it again would duplicate the entry
        and break `resolve()`'s `len(candidates) == 1` case for an
        otherwise-unambiguous short name.
        """
        prefix = old_prefix + "."
        for qualified, instance in list(self._qualified.items()):
            if qualified.startswith(prefix):
                self._qualified[new_prefix + "." + qualified[len(prefix):]] = instance

    def has_prefix(self, name: str) -> bool:
        """Check whether `name`'s model prefix refers to the current file.

        True if `name` is qualified (has a model prefix) AND that prefix
        designates the current file itself, as opposed to an imported
        model's prefix (entirely absent from this table). Only applies to
        qualified names - an unqualified name follows separate logic, see
        ForwardRefResolver._resolve_one.
        """
        if "." not in name:
            return False
        prefix = name.split(".", 1)[0]
        return any(qn.startswith(prefix + ".") or qn == prefix for qn in self._qualified)


class ForwardRefResolver:
    def __init__(self, symbol_table: SymbolTable):
        self.symbol_table = symbol_table
        self._pending: list[_Pending] = []

    def register_pending(self, ref: ForwardRef, container: Any, field: str) -> None:
        self._pending.append(_Pending(ref, container, field))

    def resolve_all(self, repository=None) -> None:
        for entry in self._pending:
            resolved = self._resolve_one(entry.ref, repository)
            if entry.ref.resolve_via_twin:
                twin = getattr(resolved, "_twin", None)
                if twin is not None:
                    resolved = twin
            current = getattr(entry.container, entry.field, None)
            if isinstance(current, list):
                for i, item in enumerate(current):
                    if item is entry.ref:
                        current[i] = resolved
            else:
                setattr(entry.container, entry.field, resolved)
        self._pending.clear()

    def _resolve_one(self, ref: ForwardRef, repository=None) -> Any:
        kind_hint = ref.resolves_to_hint if isinstance(ref.resolves_to_hint, (str, list)) else None
        if ref.always_external:
            # The name itself (e.g. modeldef.imports.ImportedP) IS the
            # imported model's name, never prefixed - if a ModelRepository
            # is configured, try to load it and resolve it to the real
            # Model instance (it registers under its own bare name, the
            # same mechanism as any other named instance) rather than
            # always falling back to UnresolvedNamedReference.
            if repository is not None:
                found = repository.resolve_external(ref.name, ref.name, kind_hint)
                if found is not None:
                    return found
            return UnresolvedNamedReference(ref.name, reason="external_import")
        found = self.symbol_table.resolve(ref.name, kind_hint=kind_hint, home_model=ref.home_model)
        if found is not None:
            return found
        if "." in ref.name:
            if not self.symbol_table.has_prefix(ref.name):
                # Prefix absent from THIS file: a candidate for cross-file
                # resolution if a ModelRepository is configured (see
                # repository.py) - each loaded model keeps its OWN table,
                # never merged, so no short-name ambiguity is introduced
                # between unrelated models (only a FULL QUALIFIED name
                # match in the explicitly targeted model's table counts).
                if repository is not None:
                    prefix = ref.name.split(".", 1)[0]
                    found = repository.resolve_external(prefix, ref.name, kind_hint)
                    if found is not None:
                        return found
                return UnresolvedNamedReference(ref.name, reason="external_import")
            if ref.graceful:
                # Unresolved EXTENDS: never fatal, symmetric to
                # domainRef/BaseClass.
                return UnresolvedNamedReference(ref.name, reason="unresolved_extends")
            raise BuildError(f"unresolved reference, not attributable to an import: {ref.name!r}", rule=ref.rule)
        # Unqualified name not found locally: try the enclosing TOPIC
        # EXTENDS namespace first, if any (see ForwardRef.topic_extends_hint)
        # - BEFORE the existing IMPORTS UNQUALIFIED fallback, the two
        # mechanisms being independent.
        found = self._resolve_via_topic_extends(ref, kind_hint, repository)
        if found is not None:
            return found
        # Otherwise, only a reference to a model explicitly imported
        # UNQUALIFIED (see SymbolTable.unqualified_imports) can legitimately
        # name it - otherwise it's a real local bug (a name never declared
        # in this file), to be raised as an exception rather than hidden.
        if not self.symbol_table.unqualified_imports:
            if ref.graceful:
                return UnresolvedNamedReference(ref.name, reason="unresolved_extends")
            raise BuildError(
                f"unresolved reference, not attributable to an import: {ref.name!r}", rule=ref.rule
            )
        if repository is not None:
            for model_name in self.symbol_table.unqualified_imports:
                found = repository.resolve_external(model_name, ref.name, kind_hint)
                if found is not None:
                    return found
        return UnresolvedNamedReference(ref.name, reason="external_import")

    def _resolve_via_topic_extends(self, ref: ForwardRef, kind_hint, repository=None) -> Any | None:
        """Try to resolve an unqualified name via the TOPIC EXTENDS namespace.

        See ForwardRef.topic_extends_hint for the rationale. Only a
        QUALIFIED EXTENDS (a topic from ANOTHER model, e.g.
        "CatalogueObjects_V1.Catalogues") needs dedicated handling here: an
        EXTENDS on a topic in the SAME file (unqualified, e.g. "TOPIC
        Countries EXTENDS AdministrativeUnits") is already covered without
        anything special, since the generic short-name resolution
        (`SymbolTable.resolve` earlier in `_resolve_one`) already finds the
        target name in the SAME symbol table, with no notion of per-topic
        scope - retrying a "hint.name" candidate here would just duplicate
        that same short-name fallback for nothing.
        """
        hint = ref.topic_extends_hint
        if not hint or "." not in hint:
            return None
        candidate = f"{hint}.{ref.name}"
        found = self.symbol_table.resolve(candidate, kind_hint=kind_hint)
        if found is not None:
            return found
        if repository is not None:
            prefix = hint.split(".", 1)[0]
            return repository.resolve_external(prefix, candidate, kind_hint)
        return None
