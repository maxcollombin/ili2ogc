"""SEMANTIC layer: cross-references structural XTF objects with the schema.

Cross-references structural XTF objects (parse.py) with the schema already
built by InterlisModelBuilder (Class/AttrOrParam/Type), so validate.py can
interpret each attribute by its DECLARED type rather than its raw XML
shape alone.

Reuses the SymbolTable/ModelRepository already built by the ModelBuilder
directly (see builder/forward_refs.py, builder/repository.py) - a Class
instance is already registered under its full qualified name
(Model.Topic.ClassName, via InterlisModelBuilder._qualify_name), which
matches XtfObject.qualified_class exactly (same naming convention).
"""

from dataclasses import dataclass

from interlis.builder.repository import ModelRepository
from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance


def resolve_class(
    qualified_class: str, *, symbol_table: SymbolTable, repository: ModelRepository | None
) -> MetaInstance | None:
    """Find the IlisMeta16.ModelData.Class instance for a qualified_class.

    Looks up an XtfObject.qualified_class ("Model.Topic.ClassName"). Checks
    the root model's table first (local class, or already resolved via
    import), then - if absent and a repository is given - the model named
    by the qualified name's FIRST segment (same mechanism as the
    ModelBuilder's own cross-reference resolution, ForwardRefResolver).
    """
    found = symbol_table.resolve(qualified_class, kind_hint=["Class"])
    if isinstance(found, MetaInstance):
        return found
    if repository is not None and "." in qualified_class:
        model_name = qualified_class.split(".", 1)[0]
        found = repository.resolve_external(model_name, qualified_class, kind_hint=["Class"])
        if isinstance(found, MetaInstance):
            return found
    return None


def home_symbol_table(
    qualified_class: str, *, symbol_table: SymbolTable, repository: ModelRepository | None
) -> SymbolTable:
    """Return the symbol table that actually declares `qualified_class`.

    The root model's table if it already registers this class there,
    otherwise the table of the model named by the qualified name's first
    segment, via `ModelRepository.symbol_table_for` (same resolution
    mechanism as `resolve_class` above - never duplicated/reinvented).

    Needed for `embedded_roles_of` (below) to look up embedding
    associations in the RIGHT table: it used to always search the root
    table, regardless of the class's real owning model - confirmed real on
    `2021-01-12_SectoralPlanForRoadInfrastructure_LV95.xtf` (770
    occurrences): the XTF basket is qualified under the model that EXTENDS
    the topic (`SectoralPlanForRoadInfrastructure_LV95_V1_4`, via `TOPIC
    ... EXTENDS Base.Topic`, no association of its own), while EACH
    individual object stays qualified under the BASE model
    (`BaseModel_SectoralPlans_LV95_V1_4`, which declares the
    `Object_SP`/`Document_Object`/`Facility_Object`/`Measure_Facility`
    associations that actually embed roles on those same classes) - the
    root table (extension model) never contains them, only the base
    model's table does. Falls back silently to the root table if the model
    isn't resolvable (same degradation as `resolve_class`: the class is
    then simply not found further down).
    """
    found = symbol_table.resolve(qualified_class, kind_hint=["Class"])
    if isinstance(found, MetaInstance):
        return symbol_table
    if repository is not None and "." in qualified_class:
        model_name = qualified_class.split(".", 1)[0]
        table = repository.symbol_table_for(model_name)
        if table is not None:
            return table
    return symbol_table


def _own_attributes_of(class_instance: MetaInstance) -> dict[str, MetaInstance]:
    """Map attribute name -> AttrOrParam instance, own attributes only.

    Attributes declared on THIS class only (ClassAttr association,
    ClassAttribute role - see spec/grammar/mapping/04_attributes.yml,
    attributeDef.parent) - does NOT include attributes inherited via
    EXTENDS, see attributes_of.
    """
    return {
        a.Name: a
        for a in (getattr(class_instance, "ClassAttribute", None) or [])
        if isinstance(a, MetaInstance) and getattr(a, "Name", None)
    }


def single_own_attribute(class_instance: MetaInstance) -> MetaInstance | None:
    """Return `class_instance`'s single own attribute, if it has EXACTLY ONE.

    A recurring structural pattern: a wrapper STRUCTURE with exactly 1
    attribute. `None` otherwise (0 or several). Reused by
    `reference_external_status` (the `MandatoryCatalogueReference`
    pattern) AND by `restriction_candidates`/the XTF validator (3rd
    encoding form, `CLASS RESTRICTION(A; B; C)` over 1-attribute
    STRUCTUREs, see docs/xtf-transfer-encoding-notes.md).
    """
    own = _own_attributes_of(class_instance)
    if len(own) == 1:
        return next(iter(own.values()))
    return None


def attributes_of(class_instance: MetaInstance) -> dict[str, MetaInstance]:
    """Map attribute name -> AttrOrParam instance, own then inherited.

    Own attributes first, then attributes inherited via the `EXTENDS`
    chain (walks the `Inheritance` association/`Super` role, fed by
    classDef()/structureDef(), spec/grammar/mapping/03_classes_and_structures.yml
    - previously never built at all, not just unwalked). An own attribute
    wins over an inherited one of the same name (redeclaration/restriction
    in the subclass) - not confirmed on a real example so far, but
    consistent with the language's general EXTENDS semantics rather than
    assuming no collision can happen.

    Stops gracefully (no crash on already-known limits):
    - `Super` absent (root of the chain, or a terminal abstract class):
      the loop simply ends.
    - `Super` still a `ForwardRef`/`UnresolvedNamedReference` (parent class
      in a model not loaded via `--repo`, e.g.
      `CatalogueObjects_V1.Catalogues.Item`, confirmed real on
      RoadTrafficCensus_V1_1): the inheritance chain is truncated there,
      no error - attributes inherited beyond that point simply stay
      invisible, the same category of limit already known for any partial
      cross-model resolution.
    - anti-cycle guard (`seen`, by identity): no real cycle is known in
      the corpus (a circular EXTENDS would be a model error anyway), but
      cheap protection against an infinite loop if one is ever
      encountered.
    """
    merged: dict[str, MetaInstance] = {}
    seen: set[int] = set()
    current: MetaInstance | None = class_instance
    chain: list[dict[str, MetaInstance]] = []
    while isinstance(current, MetaInstance) and id(current) not in seen:
        seen.add(id(current))
        chain.append(_own_attributes_of(current))
        current = getattr(current, "Super", None)
    for level in reversed(chain):
        merged.update(level)
    return merged


def _class_related_base_class(instance: MetaInstance) -> MetaInstance | None:
    """Return `instance`'s `.BaseClass` (association BaseClass, role CRT<->BaseClass).

    Works for any `ClassRelatedType` instance - `Role` (roleDef.BaseClass)
    AND `ReferenceType` (referenceAttr.BaseClass, same generic
    association, reused by `reference_target_class` below).
    """
    base = getattr(instance, "BaseClass", None)
    if isinstance(base, list):
        base = base[0] if base else None
    return base if isinstance(base, MetaInstance) else None


def _all_class_related_base_classes(instance: MetaInstance) -> list[MetaInstance]:
    """Return the LIST version (not just the 1st) of `_class_related_base_class`.

    Needed for `restriction_candidates`: `CLASS RESTRICTION(A; B; C)` now
    attaches ALL its candidates onto `BaseClass` (see
    `InterlisModelBuilder._build_domain_class_restriction`), not just the
    first one as before that change.
    """
    base = getattr(instance, "BaseClass", None)
    if base is None:
        return []
    if isinstance(base, list):
        return [b for b in base if isinstance(b, MetaInstance)]
    return [base] if isinstance(base, MetaInstance) else []


def _role_is_multi(role: MetaInstance) -> bool:
    """Check whether the role's cardinality is > 1 (Multiplicity.Max == '*').

    Min/Max are TEXT strings (ilismeta16-classes.yml, Multiplicity.own.Max:
    type TEXT), never absent when a cardinality() clause is present;
    `Multiplicity is None` (no clause in the .ili) means the default
    cardinality, never > 1 - e.g. a roleDef with no explicit cardinality(),
    such as `rMeasurementLocation -<#> MeasurementLocation;`.
    """
    mult = getattr(role, "Multiplicity", None)
    return isinstance(mult, MetaInstance) and getattr(mult, "Max", None) == "*"


def embedded_roles_of(class_instance: MetaInstance, symbol_table: SymbolTable) -> dict[str, MetaInstance]:
    """Map role name -> Role instance, for EMBEDDED association roles.

    Embedded association roles are transferred as pseudo-attributes of
    THIS class in the XTF (e.g. `rMeasurementLocation` on `Indicator`) -
    not regular ClassAttr attributes (see attributes_of).

    Algorithm confirmed against the Reference Manual eCH-0031 V2.1.0
    §4.3.9 "Codierung von Beziehungen":
    - An association with EXACTLY 2 roles is ALWAYS embedded, except cases
      currently out of scope (>2 roles, an explicit OID on the
      association, some inter-topic relations - not handled here, the
      association is then simply skipped rather than misclassified).
    - If ONLY ONE of the 2 roles has a max cardinality > 1: embedded on
      that role's TARGET class; the embedded pseudo-attribute's NAME is
      the OTHER role's (§4.3.9.1: "for RoleName, the name of the role
      pointing to the OPPOSITE object must be given").
    - If both roles have max cardinality <= 1: embedded on the SECOND
      declared role's target class (.ili file order); pseudo-attribute
      named after the FIRST role.
    - If both roles have max cardinality > 1: NOT embedded (transferred as
      a separate class instance, §4.3.9.2) - absent from the result.

    Known limitation (out of scope): the manual's "same Topic as the
    association" nuance (which can force an association to NOT embed if
    the target classes are in a different topic) isn't checked - every
    resolved association is treated as if it shared its target classes'
    topic (by far the most common case in the real corpus). Only searches
    within the `symbol_table` given by the caller -
    `schema_members_of`/`_validate_object` now passes the table of the
    model that ACTUALLY declares the class (via `home_symbol_table`
    above), not systematically the root model's: covers an association
    defined in a model IMPORTED by the root model (e.g. a class embedded
    via `TOPIC EXTENDS`, or more generally any class resolved via
    `ModelRepository`).

    Compares target classes via `is_class_compatible(class_instance,
    embed_on)` (defined further below in this module, the same
    compatibility relation already used for a resolved reference's real
    target) rather than strict identity: true if `class_instance` IS
    `embed_on`, OR a subclass (direct or indirect) via the `Super` chain -
    otherwise a role embedded on an ABSTRACT base class would never be
    recognized for its concrete transferred subclasses.
    """
    result: dict[str, MetaInstance] = {}
    for candidate in symbol_table.all_registered():
        if not isinstance(candidate, MetaInstance) or candidate._qualified_class.rsplit(".", 1)[-1] != "Class":
            continue
        if getattr(candidate, "Kind", None) != "Association":
            continue
        roles = [r for r in (getattr(candidate, "Role", None) or []) if isinstance(r, MetaInstance)]
        if len(roles) != 2:
            continue
        role_a, role_b = roles
        target_a, target_b = _class_related_base_class(role_a), _class_related_base_class(role_b)
        if target_a is None or target_b is None:
            continue
        multi_a, multi_b = _role_is_multi(role_a), _role_is_multi(role_b)
        if multi_a and multi_b:
            continue
        if multi_a:
            embed_on, embedded_role = target_a, role_b
        elif multi_b:
            embed_on, embedded_role = target_b, role_a
        else:
            embed_on, embedded_role = target_b, role_a
        if is_class_compatible(class_instance, embed_on) and getattr(embedded_role, "Name", None):
            result[embedded_role.Name] = embedded_role
    return result


def schema_members_of(class_instance: MetaInstance, symbol_table: SymbolTable) -> dict[str, MetaInstance]:
    """Return the union of attributes_of and embedded_roles_of.

    The full view of pseudo-attributes an XTF object of this class can
    carry: own/inherited ClassAttr attributes plus embedded association
    roles.
    """
    members = dict(attributes_of(class_instance))
    members.update(embedded_roles_of(class_instance, symbol_table))
    return members


@dataclass
class ResolvedAttribute:
    """A schema attribute ready to be interpreted/validated.

    Holds its AttrOrParam OR Role instance (embedded association roles are
    handled uniformly, via BaseClass instead of Type), its resolved
    Type/target class (can be None if unresolved - an external reference
    out of scope, see docs/xtf-transfer-encoding-notes.md / README Known
    limitations), and the short name of the Type's concrete metamodel
    class (e.g. "TextType", "NumType", "EnumType", "ReferenceType",
    "Class" - never the abstract "DomainType").
    """

    attr: MetaInstance
    type_instance: MetaInstance | None
    type_kind: str | None
    mandatory: bool


def resolve_attribute(attr: MetaInstance) -> ResolvedAttribute:
    if attr._qualified_class.rsplit(".", 1)[-1] == "Role":
        # Role EXTENDS ReferenceType EXTENDS ClassRelatedType EXTENDS
        # DomainType (ilismeta16-classes.yml): carries its OWN
        # Mandatory (inherited from DomainType) - unlike AttrOrParam,
        # there's no separate Type field, the target class comes from
        # BaseClass (attached via the BaseClass association, same
        # mechanism as any other ClassRelatedType - like ReferenceType).
        target = _class_related_base_class(attr)
        return ResolvedAttribute(
            attr=attr,
            type_instance=target,
            type_kind="Class" if target is not None else None,
            mandatory=bool(getattr(attr, "Mandatory", False)),
        )
    type_instance = getattr(attr, "Type", None)
    type_instance = type_instance if isinstance(type_instance, MetaInstance) else None
    type_kind = type_instance._qualified_class.rsplit(".", 1)[-1] if type_instance is not None else None
    mandatory = bool(getattr(type_instance, "Mandatory", False)) if type_instance is not None else False
    return ResolvedAttribute(attr=attr, type_instance=type_instance, type_kind=type_kind, mandatory=mandatory)


def reference_target_class(resolved: ResolvedAttribute) -> MetaInstance | None:
    """Return the Class DECLARED as a reference/role's target.

    Used to check class compatibility between a resolved reference and its
    declared target. For `type_kind == "Class"` (an embedded association
    role, OR `restrictedClassOrAssRef`/`restrictedStructureRef` resolving
    DIRECTLY to a Class): `resolved.type_instance` IS ALREADY that class
    (see `resolve_attribute`, both forms share `type_kind="Class"`). For
    `type_kind == "ReferenceType"` (a plain `REFERENCE TO X`):
    `resolved.type_instance` is the `ReferenceType` WRAPPER itself, not
    the target class - that lives in its `.BaseClass` (the same generic
    `BaseClass` association as `Role`, confirmed on `referenceAttr()` -
    spec/grammar/mapping/04_attributes.yml).
    """
    if resolved.type_instance is None:
        return None
    if resolved.type_kind == "ReferenceType":
        return _class_related_base_class(resolved.type_instance)
    if resolved.type_kind == "Class":
        return resolved.type_instance
    return None


def is_class_compatible(actual: MetaInstance, declared: MetaInstance) -> bool:
    """Check whether `actual` is `declared`, or one of its subclasses.

    True if `actual` IS `declared`, or a SUBCLASS (direct or indirect, via
    the `Inheritance`/`Super` chain) of `declared` - standard INTERLIS
    polymorphism for a reference: a reference declared toward a class
    (often abstract) must accept any concrete subclass as its real target,
    not just `declared` itself. Compares by Python IDENTITY (`is`), not by
    qualified name - valid as long as `actual`/`declared` come from the
    same `symbol_table`/`ModelRepository` (always true within a single
    `validate_transfer`, which reuses the SAME SymbolTable/ModelRepository
    throughout - the same guarantee already relied on by `attributes_of`).
    """
    seen: set[int] = set()
    current: MetaInstance | None = actual
    while isinstance(current, MetaInstance) and id(current) not in seen:
        if current is declared:
            return True
        seen.add(id(current))
        current = getattr(current, "Super", None)
    return False


def restriction_candidates(resolved: ResolvedAttribute) -> list[MetaInstance]:
    """Return every candidate class of a `CLASS RESTRICTION(A; B; C)`.

    XTF's 3rd encoding form, see docs/xtf-transfer-encoding-notes.md.
    Length > 1 ONLY for this construct (e.g. `Owner = CLASS
    RESTRICTION(sCHOwnerCode; sCHCantonCode; sCHMunicipalityCode)`,
    RoadTrafficCensus_V1_1.ili); length 0 or 1 for a plain `REFERENCE
    TO`/role (already covered by `reference_target_class`, still the
    function to use for the simple case - this one is SPECIFICALLY for
    the multi-candidate case).
    """
    if resolved.type_kind != "ReferenceType" or resolved.type_instance is None:
        return []
    return _all_class_related_base_classes(resolved.type_instance)


def coord_axes(coord_type: MetaInstance | None) -> list[MetaInstance]:
    """Return the ORDERED list of a `CoordType`'s `NumType` instances.

    Association `AxisSpec`, role `Axis`, `{1..3} NumType ORDERED`
    (confirmed in ilismeta16-associations.yml/coordinateType binding,
    spec/grammar/mapping/06_types.yml) - each potentially carries
    Min/Max/Unit (own TEXT, absent for a bare `NUMERIC` axis with no
    range). Returns an EMPTY list (not an error) if `coord_type` is
    `None`, or if `Axis` wasn't resolved (e.g. an external domain not
    loaded via `--repo`) - the XTF validator must then fall back to a
    numeric PARSEABILITY check only, never a range check, on the affected
    components (see validate.py).
    """
    if coord_type is None:
        return []
    axes = getattr(coord_type, "Axis", None)
    if isinstance(axes, list):
        return [a for a in axes if isinstance(a, MetaInstance)]
    return [axes] if isinstance(axes, MetaInstance) else []


def line_coord_type(line_type: MetaInstance | None) -> MetaInstance | None:
    """Return the `CoordType` linked to a `LineType` via `LineCoord`.

    `LineType <-> CoordType, 0..1`, role `CoordType`, fed by the
    `lineType.CoordType`/`controlPoints()` binding ('VERTEX Name', see
    InterlisModelBuilder._build_control_points_ref). `None` if `line_type`
    is `None`, or if no `LineType` in the `Super` chain (`Inheritance`
    association, own THEN inherited via `EXTENDS`, same principle as
    `attributes_of` for classes) carries a VERTEX clause of its own.

    Walks the EXTENDS chain: `DirectedLine EXTENDS Line = DIRECTED
    POLYLINE;` (real, from CHBase) has NO VERTEX clause of its own - its
    CoordType lives on `Line`, the base domain. This required
    `domainDef()` to actually attach `Super` for every EXTENDS clause
    first (`InterlisModelBuilder._attach_domain_extends`, previously a
    total gap - NO domainDef() EXTENDS clause was attached before that
    change, regardless of domain type).

    Graceful stop (same category of limit as `attributes_of`): `Super`
    absent, or still an `UnresolvedNamedReference` (base domain in a model
    not loaded via `--repo`) - the chain is simply truncated at that
    point, no error. Anti-cycle guard (`seen`, by identity) - same
    precaution as `attributes_of`/`is_class_compatible`.
    """
    seen: set[int] = set()
    current: MetaInstance | None = line_type
    while isinstance(current, MetaInstance) and id(current) not in seen:
        seen.add(id(current))
        ct = getattr(current, "CoordType", None)
        if isinstance(ct, MetaInstance):
            return ct
        current = getattr(current, "Super", None)
    return None


def reference_external_status(resolved: ResolvedAttribute) -> bool | None:
    """Return the status of the optional `REFERENCE TO (EXTERNAL) X` clause.

    Tri-state (`True`/`False`/`None`, not bool): `None` means "this
    validator can't confidently identify a REFERENCE TO here" - distinct
    from `False` ("confirmed NOT-EXTERNAL"). Used to distinguish, when an
    extracted REF resolves to no object in the transfer, a legitimate
    EXTERNAL reference (separate catalogue/basket) from a more likely sign
    of bad data.

    3 confirmed forms (True/False, never None): a direct `ReferenceType`
    attribute; a 1-attribute Structure wrapping a `ReferenceType`
    (the `MandatoryCatalogueReference` pattern); and an embedded
    association role's own `(EXTERNAL)` clause on `roleDef()`. `None` for
    everything else (a genuinely undetermined status, not "assumed
    False"). Full reasoning for each form, with real corpus examples:
    docs/dev-notes/reference-external-status-investigation.md.
    """
    if resolved.type_kind == "ReferenceType" and resolved.type_instance is not None:
        return bool(getattr(resolved.type_instance, "External", False))
    if resolved.attr._qualified_class.rsplit(".", 1)[-1] == "Role":
        return bool(getattr(resolved.attr, "EmbeddedTransfer", None))
    if resolved.type_kind == "Class" and resolved.type_instance is not None:
        # `_own_attributes_of` (NOT `attributes_of`): this checks a
        # structural pattern on the STRUCTURE ITSELF (a wrapper with a
        # single OWN attribute) - an attribute inherited via EXTENDS would
        # wrongly add a 2nd entry and break detection of the
        # MandatoryCatalogueReference pattern, unrelated to what's being
        # checked here.
        wrapped = _own_attributes_of(resolved.type_instance)
        if len(wrapped) == 1:
            inner = resolve_attribute(next(iter(wrapped.values())))
            if inner.type_kind == "ReferenceType" and inner.type_instance is not None:
                return bool(getattr(inner.type_instance, "External", False))
    return None


def enum_values(enum_type: MetaInstance) -> set[str]:
    """Return every valid DOTTED PATH for this EnumType.

    Reference Manual eCH-0031 V2.1.0 §4.3.11.3: "EnumValue =
    (EnumElement-Name {'.' EnumElement-Name}) | 'OTHERS'." - a
    HIERARCHICAL enum (an EnumNode with children) transfers as the FULL
    PATH from the root, not just the leaf node's name - e.g. for
    KGS_PBC_V2_2.KGS_Kategorie: "A (A, verstaerkter_Schutz), B", the
    transferred value for node "A" nested under the same-named root node
    "A" is "A.A", not just "A". "For encoding ... the syntax is applied
    REGARDLESS of whether the value domain covers only leaves or also
    nodes" - so EVERY node contributes its own path, not just leaves.
    EnumType.TopNode is a SYNTHETIC root node (Name="TOP", never a real
    value - confirmed by `models/IlisMeta16.ili`'s comment on EnumNode:
    "MetaElement.Name := 'TOP' for topnode", and built as such, see
    InterlisModelBuilder._build_enumeration_tree) - excluded from the
    returned paths, only ITS children (the real top-level values) start a
    path. Recurses through EnumNode.Node (association SubNode, role Node).
    """
    values: set[str] = set()

    def walk(node: MetaInstance | None, prefix: str, *, is_synthetic_root: bool) -> None:
        if node is None:
            return
        name = getattr(node, "Name", None)
        if not name:
            return
        if is_synthetic_root:
            path = ""
        else:
            path = f"{prefix}.{name}" if prefix else name
            values.add(path)
        for child in getattr(node, "Node", None) or []:
            walk(child, path, is_synthetic_root=False)

    walk(getattr(enum_type, "TopNode", None), "", is_synthetic_root=True)
    return values
