"""IlisMeta16 `Class` -> SQL `Table`: columns, `BAG`/`LIST OF` child tables, ABSTRACT structure subclass tables,
UNIQUE/CHECK/FOREIGN KEY constraints.

`build_tables` is the public entry point - see its own docstring and
`mappings/ilismeta16-to-sql-rules.yml` for the full per-concept scope.
"""

from __future__ import annotations

from interlis.builder.forward_refs import SymbolTable
from interlis.convert.jsonschema import _is_structure
from interlis.diagnostic_ids import note as _diag
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.schema import (
    ResolvedAttribute,
    attributes_of,
    concrete_structure_subclasses,
    reference_target_class,
    resolve_attribute,
    schema_members_of,
)

from .expressions import _MAX_STRUCT_FLATTEN_DEPTH, _expression_to_sql, _UnsupportedCheckExpression
from .identifiers import OID_COLUMN, _dedup_name, _sql_identifier, _truncate_identifier
from .model import CheckConstraint, Column, ForeignKey, Table, UniqueConstraint
from .types import _GEOMETRY_KINDS, _geometry_column_info, _scalar_sql_type


def _avoid_identity_collision(columns: list[Column]) -> dict[str, str]:
    """Rename any column literally named `OID_COLUMN` ("id") to `"id_attr"` (or `"id_attr_2"`, ... on a further
    collision), IN PLACE - returns the `{old_name: new_name}` rename map.

    Real corpus case (found via a live SQLite run,
    `ili_corpus/WasserBase_V1_1.ili`: `ID : MANDATORY TEXT*25;` -
    "duplicate column name: id"): a genuine INTERLIS attribute literally
    named `Id`/`ID` lowercases to the SAME name this module reserves for
    the synthetic identity column (`OID_COLUMN`) - renaming the ATTRIBUTE's
    own column here rather than the reserved one, which every FOREIGN KEY
    and every GDAL `-append` already depends on matching exactly. The
    caller MUST also apply the returned rename map to any `UniqueConstraint`
    built from the SAME attribute set (its own column list is computed
    independently, straight from `PathEl.Ref`, and would otherwise still
    reference the OLD, no-longer-existing name).
    """
    used = {c.name for c in columns}
    renamed: dict[str, str] = {}
    for column in columns:
        if column.name != OID_COLUMN:
            continue
        base_name = f"{OID_COLUMN}_attr"
        new_name = base_name
        suffix = 2
        while new_name in used:
            new_name = f"{base_name}_{suffix}"
            suffix += 1
        used.discard(OID_COLUMN)
        used.add(new_name)
        column.name = new_name
        renamed[OID_COLUMN] = new_name
    return renamed


def _columns_for_class(
    cls: MetaInstance,
    symbol_table: SymbolTable | None,
    *,
    prefix: str = "",
    depth: int = 0,
) -> tuple[
    list[Column],
    list[ForeignKey],
    list[str],
    list[tuple[str, MetaInstance]],
    dict[str, list[list[str]]],
    list[tuple[str, MetaInstance, bool, bool]],
]:
    """Return `(columns, foreign_keys, notes, child_specs, local_unique, abstract_specs)` for `cls`'s own+inherited
    members, flattening up to `_MAX_STRUCT_FLATTEN_DEPTH` levels of STRUCTURE nesting inline.

    `prefix` is non-empty on the recursive call flattening a STRUCTURE
    attribute (`"<attr>_"`, or `"<attr>_<subattr>_"` at the second level) -
    it builds the flattened column names. `depth` counts how many STRUCTURE
    levels have already been entered; a STRUCTURE found at
    `depth == _MAX_STRUCT_FLATTEN_DEPTH` is refused with a `-- NOTE` rather
    than flattened into an ever-deeper column name (RULE #7: bounded, see
    mappings/ilismeta16-to-sql-rules.yml's StructureNesting entry).

    `child_specs` is `[(label, MultiValue instance), ...]` for every
    `BAG`/`LIST OF` member found at the TOP level or while flattening a
    STRUCTURE (`label` carries the full `"<struct_attr>_"` /
    `"<struct_attr>_<sub_attr>_"` prefix in the nested case, e.g.
    `"zustaendige_behoerde_entries"`) - built into a related child table by
    `_build_child_table` (called from `build_tables`, which alone knows the
    already-used table names to disambiguate against). A `BAG`/`LIST OF`
    reached through one or two flattened STRUCTURE levels still becomes one
    child table keyed by the parent's OID.

    `local_unique` is the SAME shape `_local_unique_constraints_for_class`
    returns, merged up from any nested STRUCTURE's OWN `Kind=LocalU`
    `UniqueConstraint` (the real corpus idiom, e.g. `LocalisationCH_V1.
    MultilingualText` wraps a `BAG`/`LIST` AND declares `UNIQUE (LOCAL)` on
    itself, not on the embedding Class) - keys qualified with the SAME
    `"<struct_attr>_"` prefix as the matching `child_specs` label, so
    `build_tables` can look them up together without knowing they came
    from a nested STRUCTURE at all.

    `abstract_specs` is `[(label, abstract STRUCTURE class, ordered,
    from_multivalue), ...]` for an attribute whose (element) type is an
    ABSTRACT structure - `build_tables` emits one child table per concrete
    subclass found in the symbol table (`concrete_structure_subclasses`),
    mirroring the JSON Schema pipeline's `anyOf`. `from_multivalue`
    distinguishes a `BAG`/`LIST OF <abstract>` from a single-valued
    ABSTRACT structure attribute, only to pick the right `-- NOTE` id when
    no concrete subclass is in the conversion.
    """
    columns: list[Column] = []
    foreign_keys: list[ForeignKey] = []
    notes: list[str] = []
    child_specs: list[tuple[str, MetaInstance]] = []
    local_unique: dict[str, list[list[str]]] = {}
    abstract_specs: list[tuple[str, MetaInstance, bool, bool]] = []
    members = schema_members_of(cls, symbol_table) if symbol_table is not None else attributes_of(cls)
    for name, attr in members.items():
        resolved = resolve_attribute(attr)
        label = f"{prefix}{name}"
        col_name = _sql_identifier(label)

        if resolved.type_kind == "MultiValue":
            multi_value = resolved.type_instance
            if not isinstance(multi_value, MetaInstance):
                notes.append(
                    _diag(
                        "SQL-BAGLIST-ELEMENT-UNRESOLVED",
                        f"{label}: BAG/LIST OF element type not resolved - provide its model via --repo",
                    )
                )
                continue
            element = getattr(multi_value, "BaseType", None)
            is_abstract_struct = (
                isinstance(element, MetaInstance)
                and _is_structure(element)
                and bool(getattr(element, "Abstract", False))
            )
            if is_abstract_struct:
                abstract_specs.append((label, element, bool(getattr(multi_value, "Ordered", False)), True))
                continue
            child_specs.append((label, multi_value))
            continue

        if resolved.type_kind == "Class" and _is_structure(resolved.type_instance):
            if depth >= _MAX_STRUCT_FLATTEN_DEPTH:
                notes.append(
                    _diag(
                        "SQL-STRUCT-NESTED-DEEP",
                        f"{label}: STRUCTURE nested more than {_MAX_STRUCT_FLATTEN_DEPTH} levels deep - not flattened",
                    )
                )
                continue
            if bool(getattr(resolved.type_instance, "Abstract", False)):
                abstract_specs.append((label, resolved.type_instance, False, False))
                continue
            sub_columns, sub_fks, sub_notes, sub_child_specs, sub_local_unique, sub_abstract_specs = _columns_for_class(
                resolved.type_instance,
                symbol_table,
                prefix=f"{label}_",
                depth=depth + 1,
            )
            columns.extend(sub_columns)
            foreign_keys.extend(sub_fks)
            notes.extend(sub_notes)
            child_specs.extend(sub_child_specs)
            abstract_specs.extend(sub_abstract_specs)
            for nested_key, nested_groups in sub_local_unique.items():
                local_unique.setdefault(nested_key, []).extend(nested_groups)
            struct_local_unique, struct_local_unique_notes = _local_unique_constraints_for_class(resolved.type_instance)
            for role_attr, groups in struct_local_unique.items():
                local_unique.setdefault(f"{label}_{role_attr}", []).extend(groups)
            notes.extend(f"{label}: {note}" for note in struct_local_unique_notes)
            continue

        if resolved.type_kind in ("Class", "ReferenceType") and resolved.type_instance is not None:
            target = reference_target_class(resolved)
            if target is None:
                notes.append(
                    _diag(
                        "SQL-REF-TARGET-UNRESOLVED",
                        f"{label}: reference target not resolved - pass its model's directory "
                        f"to --repo, or the model file to --catalog",
                    )
                )
                continue
            target_table = _sql_identifier(getattr(target, "Name", None) or "")
            columns.append(Column(col_name, "text", nullable=not resolved.mandatory))
            fk_name = _truncate_identifier(_sql_identifier(f"fk_{getattr(cls, 'Name', '')}_{label}"))
            foreign_keys.append(ForeignKey(fk_name, [col_name], target_table, [OID_COLUMN]))
            continue

        if resolved.type_kind in _GEOMETRY_KINDS:
            sfa_type, srid, reason = _geometry_column_info(resolved)
            if sfa_type is None:
                notes.append(
                    _diag("SQL-GEOM-NO-CRS", f"{label}: {reason} - provide the geometry base model via --repo")
                )
                continue
            columns.append(
                Column(
                    col_name,
                    sql_type="",
                    nullable=not resolved.mandatory,
                    geometry_type=sfa_type,
                    srid=srid,
                )
            )
            continue

        scalar_type = _scalar_sql_type(resolved)
        if scalar_type is not None:
            columns.append(Column(col_name, scalar_type, nullable=not resolved.mandatory))
            continue

        if resolved.type_kind is None:
            notes.append(
                _diag(
                    "BUILD-TYPE-UNRESOLVED",
                    f"{label}: attribute type not resolved by the model builder - "
                    f"provide the imported model via --repo",
                )
            )
        else:
            notes.append(_diag("SQL-ATTR-TYPE-UNMAPPED", f"{label}: unsupported type {resolved.type_kind!r}"))
    return columns, foreign_keys, notes, child_specs, local_unique, abstract_specs


def _build_child_table(
    parent_table: str,
    attr_name: str,
    multi_value: MetaInstance,
    symbol_table: SymbolTable | None,
) -> tuple[Table | None, dict[str, str], str | None, list[tuple[str, MetaInstance]]]:
    """Return `(child_table, renamed, None, nested_child_specs)` on success or `(None, {}, reason, [])` on failure,
    for one `BAG`/`LIST OF` attribute.

    Companion to `convert/jsonfg.py`'s `include_child_rows` synthetic
    Features - GDAL loads them into
    the table this returns via the SAME `ogr2ogr -append` call that loads
    the parent data, routed by `"featureType"`. Schema: a `<parent>_fk` `FOREIGN KEY` back to the
    parent (own `id` identity column added by the renderer, like every
    table) +
    `seq` (only when `Ordered=True` - `LIST` is order-significant, `BAG`
    is not) + the element's own value column(s), dispatched the SAME way
    as a plain attribute: scalar/geometry -> one `value` column;
    `STRUCTURE` -> its own columns (reusing `_columns_for_class` directly
    with no prefix, since THIS table already represents one structure
    instance). A `BAG`/`LIST OF` found INSIDE that structure - a nested
    multi-value, not flattenable into a column - is returned as
    `nested_child_specs` rather than built here: the caller (`build_tables`)
    turns each into its own `<this table>_<subattr>` child table, one
    level deeper (`build_tables`'s own recursion bound, mirroring
    `_MAX_STRUCT_FLATTEN_DEPTH`). The `ReferenceType`/non-structure `Class`
    branch below is DEFENSIVE only -
    verified against `attrTypeDef`'s real ANTLR bytecode (RULE #2bis)
    that `BAG`/`LIST OF REFERENCE TO X` is NOT actually constructible by
    this project's vendored grammar at all (`attrTypeDef`'s `(BAG|LIST)
    OF` alternative only ever calls `restrictedStructureRef()` - a named
    `STRUCTURE`, `ANYSTRUCTURE`, or a bare scalar `type_()`, never
    `referenceAttr()`) - contrary to what the abstract eCH-0031 EBNF
    alone would suggest, and confirmed absent from the real corpus too.
    An unmapped `BaseType` kind returns `(None, {}, reason, [])` - the caller
    keeps the pre-existing "-- NOTE" on the PARENT table instead of
    creating an empty/broken child table.
    """
    base_type = getattr(multi_value, "BaseType", None)
    if not isinstance(base_type, MetaInstance):
        return None, {}, "BaseType not resolved", []
    base_kind = base_type._qualified_class.rsplit(".", 1)[-1]

    child_table_name = _sql_identifier(f"{parent_table}_{attr_name}")
    fk_column = _sql_identifier(f"{parent_table}_fk")
    columns: list[Column] = [Column(fk_column, "text", nullable=False)]
    foreign_keys: list[ForeignKey] = [
        ForeignKey(
            _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_{fk_column}")),
            [fk_column],
            parent_table,
            [OID_COLUMN],
        )
    ]
    notes: list[str] = []

    if bool(getattr(multi_value, "Ordered", False)):
        columns.append(Column("seq", "integer", nullable=False))

    nested_child_specs: list[tuple[str, MetaInstance]] = []
    if base_kind == "Class" and _is_structure(base_type):
        if bool(getattr(base_type, "Abstract", False)):
            # An abstract element is routed to `abstract_specs` by `_columns_for_class`
            # (one child table per concrete subclass) - never reaches here.
            return None, {}, "BAG/LIST OF an ABSTRACT structure - subclass polymorphism not mapped to a table", []
        sub_columns, sub_fks, sub_notes, sub_child_specs, _sub_local_unique, _sub_abstract = _columns_for_class(
            base_type, symbol_table
        )
        columns.extend(sub_columns)
        foreign_keys.extend(sub_fks)
        notes.extend(sub_notes)
        nested_child_specs = sub_child_specs
    elif base_kind in ("Class", "ReferenceType"):
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        target = reference_target_class(synthetic)
        if target is None:
            return None, {}, "reference target not resolved - pass its model to --repo or --catalog", []
        target_table = _sql_identifier(getattr(target, "Name", None) or "")
        columns.append(Column("value", "text", nullable=True))
        foreign_keys.append(
            ForeignKey(
                _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_value")),
                ["value"],
                target_table,
                [OID_COLUMN],
            )
        )
    elif base_kind in _GEOMETRY_KINDS:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        sfa_type, srid, reason = _geometry_column_info(synthetic)
        if sfa_type is None:
            return None, {}, reason, []
        columns.append(Column("value", sql_type="", nullable=False, geometry_type=sfa_type, srid=srid))
    else:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        scalar_type = _scalar_sql_type(synthetic)
        if scalar_type is None:
            return None, {}, f"unsupported element type {base_kind!r}", []
        columns.append(Column("value", scalar_type, nullable=False))

    renamed = _avoid_identity_collision(columns)
    table = Table(name=child_table_name, columns=columns, foreign_keys=foreign_keys, notes=notes)
    return table, renamed, None, nested_child_specs


def _build_nested_child_tables(
    parent_child_table: Table,
    child_specs: list[tuple[str, MetaInstance]],
    symbol_table: SymbolTable | None,
    used_table_names: set[str],
) -> list[Table]:
    """Build one `<parent_child_table>_<attr>` table per `BAG`/`LIST OF` attribute found one level inside it.

    `INSPECTION OF <class> -> a -> b` (an indirect/multi-hop path) needs
    exactly this chain of tables to exist (`views.py::_build_inspection_view`
    resolves it by walking `<base>_a_b`); a `BAG`/`LIST OF` attribute
    nested this way was previously discarded silently by
    `_build_child_table` (`_sub_child_specs`, now `nested_child_specs`).
    Bounded to ONE level (no recursive call back into this function): no
    real corpus evidence of a THIRD nesting level, and the immediate
    `UNIQUE (LOCAL)`/`struct_global_unique` bookkeeping `build_tables`
    does for a first-level child table doesn't apply here (a `UNIQUE
    (LOCAL)` this deep has no observed real-world case either) - a
    further-nested `BAG`/`LIST OF` inside one of these tables is simply
    noted, not built, same "no real corpus evidence, no crash" stance as
    `_MAX_STRUCT_FLATTEN_DEPTH` for STRUCTURE flattening.
    """
    out: list[Table] = []
    for attr_name, multi_value in child_specs:
        child_table, _renamed, reason, deeper_specs = _build_child_table(
            parent_child_table.name, attr_name, multi_value, symbol_table
        )
        if child_table is None:
            rule = (
                "SQL-BAGLIST-ELEMENT-UNRESOLVED"
                if reason and "not resolved" in reason
                else "SQL-BAGLIST-ELEMENT-UNMAPPED"
            )
            parent_child_table.notes.append(_diag(rule, f"{attr_name}: BAG/LIST OF - {reason}"))
            continue
        base_name = child_table.name
        name = base_name
        suffix = 2
        while name in used_table_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_table_names.add(name)
        child_table.name = name
        if deeper_specs:
            child_table.notes.append(
                _diag(
                    "SQL-BAGLIST-ELEMENT-UNMAPPED",
                    "a further nested BAG/LIST OF attribute is not built as a table (2 levels of nesting is the bound)",
                )
            )
        out.append(child_table)
    return out


def _structure_child_table(
    parent_table: str,
    table_name: str,
    struct_cls: MetaInstance,
    symbol_table: SymbolTable | None,
    *,
    ordered: bool,
) -> tuple[Table, dict[str, str]]:
    """One child table holding instances of a concrete STRUCTURE `struct_cls`.

    Used for an ABSTRACT structure attribute's concrete subclasses (one
    table per subclass, `concrete_structure_subclasses`) - same shape as
    `_build_child_table`'s own STRUCTURE branch (a `<parent>_fk` back to the
    parent, `seq` when `ordered`, then the structure's own flattened
    columns), factored out so `build_tables` can call it per subclass.
    """
    fk_column = _sql_identifier(f"{parent_table}_fk")
    columns: list[Column] = [Column(fk_column, "text", nullable=False)]
    foreign_keys: list[ForeignKey] = [
        ForeignKey(
            _truncate_identifier(_sql_identifier(f"fk_{table_name}_{fk_column}")),
            [fk_column],
            parent_table,
            [OID_COLUMN],
        )
    ]
    if ordered:
        columns.append(Column("seq", "integer", nullable=False))
    sub_columns, sub_fks, sub_notes, _sub_child_specs, _sub_local_unique, _sub_abstract = _columns_for_class(
        struct_cls, symbol_table
    )
    columns.extend(sub_columns)
    foreign_keys.extend(sub_fks)
    renamed = _avoid_identity_collision(columns)
    return Table(name=table_name, columns=columns, foreign_keys=foreign_keys, notes=list(sub_notes)), renamed


def _check_constraints_for_class(
    cls: MetaInstance,
    table_name: str,
    column_names: set[str],
    renamed: dict[str, str],
) -> tuple[list[CheckConstraint], list[str]]:
    """Return `(constraints, notes)` for `cls`'s own row-local `MANDATORY CONSTRAINT`s - same scope as
    `constraint_eval.py`'s `check_feature_constraints`.

    `UniqueConstraint` is handled by `_unique_constraints_for_class`/
    `_local_unique_constraints_for_class`. `SetConstraint`/
    `ExistenceConstraint` and the percentage-based plausibility form
    (`SimpleConstraint` with `Percentage`, or `Kind` `LowPercC`/`HighPercC`)
    are population/basket-level checks a single-row `CHECK` cannot express -
    same exclusion as `check_feature_constraints`, not attempted here
    either, but each is surfaced as a `-- NOTE` rather than dropped
    silently (RULE #5 - `SET`/`EXISTENCE` touch ~3%/~8% of the real
    corpus).
    """
    result: list[CheckConstraint] = []
    notes: list[str] = []
    counter = 0
    for constraint in getattr(cls, "Constraint", None) or []:
        qname = constraint._qualified_class.rsplit(".", 1)[-1]
        label = repr(getattr(constraint, "Name", None)) if getattr(constraint, "Name", None) else "<unnamed>"
        if qname == "UniqueConstraint":
            continue  # handled by _unique_constraints_for_class / _local_unique_constraints_for_class
        if qname == "ExistenceConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-EXISTENCE",
                    f"EXISTENCE CONSTRAINT {label}: a check that a value also occurs in another class - "
                    "no single-row SQL CHECK can express it",
                )
            )
            continue
        if qname == "SetConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-SET",
                    f"SET CONSTRAINT {label}: a whole-population check - no single-row SQL CHECK can express it",
                )
            )
            continue
        if qname != "SimpleConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-NOT-ROWLOCAL",
                    f"CONSTRAINT {label} ({qname}): not a row-local MANDATORY CONSTRAINT - no CHECK generated",
                )
            )
            continue
        if (
            getattr(constraint, "Kind", None) not in (None, "MandC")
            or getattr(constraint, "Percentage", None) is not None
        ):
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-PLAUSIBILITY",
                    f"CONSTRAINT {label}: percentage-based plausibility form (Kind="
                    f"{getattr(constraint, 'Kind', None)!r}) - a population ratio no single-row SQL CHECK can express",
                )
            )
            continue
        expr = getattr(constraint, "LogicalExpression", None)
        if expr is None:
            continue
        counter += 1
        name = getattr(constraint, "Name", None)
        try:
            sql_expr = _expression_to_sql(expr, column_names, renamed)
        except _UnsupportedCheckExpression as exc:
            notes.append(
                _diag(
                    "SQL-CHECK-EXPR-UNSUPPORTED",
                    f"MANDATORY CONSTRAINT {name or f'#{counter}'!r}: {exc} - CHECK not generated",
                )
            )
            continue
        constraint_name = _truncate_identifier(
            _sql_identifier(f"chk_{table_name}_{name}" if name else f"chk_{table_name}_{counter}")
        )
        result.append(CheckConstraint(constraint_name, sql_expr))
    return result, notes


def _unique_constraints_for_class(
    cls: MetaInstance, table_name: str
) -> tuple[list[UniqueConstraint], list[str], dict[str, list[list[str]]]]:
    """Return `(constraints, notes, struct_global_unique)` for `cls`'s own `Kind=GlobalU` `UniqueConstraint`s.

    `constraints`: plain `UNIQUE (...)` over own columns (every path a single
    own-attribute hop).

    `struct_global_unique`: `{struct attr: [[sub-attr column, ...], ...]}` for a
    `UNIQUE X->Y` (or compound `UNIQUE X->Y, X->Z`) where `X` is this class's
    own `BAG`/`LIST OF STRUCTURE` attribute - the global (not `(LOCAL)`, so no
    per-parent scoping) counterpart of `_local_unique_constraints_for_class`.
    `build_tables` turns each group into a `UNIQUE` on the `<parent>_<attr>`
    child table, WITHOUT the `<parent>_fk` prefix. A first hop that never
    matches a child table is a real `->` reference/role navigation and gets a
    `SQL-UNIQUE-CROSS-REF` note there.

    `Kind=LocalU` is handled by `_local_unique_constraints_for_class` (skipped
    here, never noted twice). `Kind` on every `PathEl` is always
    "ReferenceAttr" regardless of whether the hop is a role or a structure
    step (confirmed empirically), so path LENGTH plus the child-
    table match in `build_tables` are the only real signals.
    """
    result: list[UniqueConstraint] = []
    notes: list[str] = []
    struct_global_unique: dict[str, list[list[str]]] = {}
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("UniqueConstraint"):
            continue
        kind = getattr(constraint, "Kind", None)
        if kind == "LocalU":
            continue
        path_defs = getattr(constraint, "UniqueDef", None) or []
        if kind != "GlobalU" or not path_defs:
            notes.append(_diag("SQL-UNIQUE-BASKET", f"UNIQUE ({kind}): basket-scoped UNIQUE not supported yet"))
            continue
        own_columns: list[str] = []
        two_hop: list[tuple[str, str]] = []  # (raw first-hop attr name, sql sub-column)
        bad = False
        for path in path_defs:
            path_els = getattr(path, "PathEls", None) or []
            if any(getattr(pe, "Kind", None) not in ("ReferenceAttr", "Attribute") for pe in path_els):
                bad = True
                break
            refs = [getattr(pe, "Ref", None) or "" for pe in path_els]
            if len(refs) == 1:
                own_columns.append(_sql_identifier(refs[0]))
            elif len(refs) == 2:
                two_hop.append((refs[0], _sql_identifier(refs[1])))
            else:
                bad = True
                break
        if bad or (own_columns and two_hop):
            # 3+ hops, an unexpected PathEl kind, or a mix of an own column
            # and a navigated one in the SAME constraint - not one plain
            # UNIQUE and not one child-table UNIQUE either.
            notes.append(
                _diag(
                    "SQL-UNIQUE-CROSS-REF",
                    "UNIQUE across a '->' reference - not expressible as a plain SQL table constraint",
                )
            )
            continue
        if two_hop:
            first_hops = {h[0] for h in two_hop}
            if len(first_hops) != 1:
                notes.append(
                    _diag(
                        "SQL-UNIQUE-CROSS-REF",
                        "UNIQUE mixing several '->' navigations in one constraint - not expressible in SQL",
                    )
                )
                continue
            struct_global_unique.setdefault(two_hop[0][0], []).append([h[1] for h in two_hop])
            continue
        if own_columns:
            name = _truncate_identifier(_sql_identifier(f"uq_{table_name}_{'_'.join(own_columns)}"))
            result.append(UniqueConstraint(name, own_columns))
    return result, notes, struct_global_unique


def _local_unique_constraints_for_class(cls: MetaInstance) -> tuple[dict[str, list[list[str]]], list[str]]:
    """Return `({BAG/LIST attr name: [[sub-attr column, ...], ...]}, notes)` for `cls`'s own `Kind=LocalU`
    `UniqueConstraint`s.

    Each `UniqueDef` entry's `PathEls` is `[role_hop, sub_attr]` (see
    `InterlisModelBuilder._build_local_uniqueness_def` - real corpus usage
    is always exactly this shape, e.g. `UNIQUE (LOCAL) Entries: Code;` ->
    `PathEls=[('ReferenceAttr','Entries'), ('ReferenceAttr','Code')]`): the
    role path (all but the last hop) must be exactly ONE hop naming the
    `BAG`/`LIST OF` attribute, and every entry of the SAME `UniqueConstraint`
    must share that SAME role hop (one `UNIQUE (LOCAL) X: A, B;`-style
    compound constraint over the SAME `BAG`/`LIST`, matching the grammar's
    own single-role-path-then-attribute-list shape) - a multi-hop role
    path or a mix of role hops is grammatically possible but never seen in
    the real corpus, rejected with a note rather than guessed at (RULE #7).
    `build_tables` attaches the resulting column list as one compound
    `UNIQUE` on the matching child table, prefixed with that table's own
    `<parent>_fk` column (RULE #1: reuses the SAME child-table naming
    `_build_child_table` already establishes, not a parallel convention).
    """
    result: dict[str, list[list[str]]] = {}
    notes: list[str] = []
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("UniqueConstraint"):
            continue
        if getattr(constraint, "Kind", None) != "LocalU":
            continue
        path_defs = getattr(constraint, "UniqueDef", None) or []
        if not path_defs:
            notes.append(
                _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL): role path could not be resolved - not supported")
            )
            continue
        role_attr: str | None = None
        columns: list[str] = []
        supported = True
        for path in path_defs:
            path_els = getattr(path, "PathEls", None) or []
            if len(path_els) < 2 or any(
                getattr(pe, "Kind", None) not in ("ReferenceAttr", "Attribute") for pe in path_els
            ):
                notes.append(_diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL): path shape not supported"))
                supported = False
                break
            *role_hops, sub_attr = path_els
            if len(role_hops) != 1:
                notes.append(
                    _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL) across a multi-hop role path - not supported")
                )
                supported = False
                break
            hop_name = getattr(role_hops[0], "Ref", None) or ""
            if role_attr is None:
                role_attr = hop_name
            elif hop_name != role_attr:
                notes.append(
                    _diag(
                        "SQL-UNIQUE-LOCAL-UNSUPPORTED",
                        "UNIQUE (LOCAL) mixing several BAG/LIST attributes in one constraint - not supported",
                    )
                )
                supported = False
                break
            columns.append(_sql_identifier(getattr(sub_attr, "Ref", None) or ""))
        if supported and role_attr and columns:
            result.setdefault(role_attr, []).append(columns)
    return result, notes


def build_tables(
    classes: list[MetaInstance],
    symbol_table: SymbolTable | None = None,
    *,
    class_symbol_tables: dict[int, SymbolTable] | None = None,
    class_table_names: dict[int, str] | None = None,
) -> list[Table]:
    """Convert every `Class(Kind=Class)` in `classes` into a `Table` - the dialect-neutral IR every renderer consumes.

    Unlike `convert/jsonschema.py`'s `model_to_json_schema`, this performs
    NO reachability discovery beyond `classes` itself: a STRUCTURE-typed
    attribute is flattened INLINE (`_columns_for_class`), never a separate
    `Table`, so there is nothing beyond the given roots to discover
    (see mappings/ilismeta16-to-sql-rules.yml).

    `class_table_names` (`id(cls) -> str`, optional out-param) is filled
    with the final table name chosen for every class - `build_views` uses
    it to map a View's base classes to their tables by identity rather
    than by re-deriving a possibly-disambiguated name.

    `class_symbol_tables` (`id(cls) -> SymbolTable`, optional) overrides
    `symbol_table` for one specific class when looking up its embedded
    association roles (`_columns_for_class` -> `schema_members_of` ->
    `embedded_roles_of`) - needed for a class that belongs to a DIFFERENT
    model than `symbol_table` (e.g. `cli.cmd_convert_sql`'s `--catalog`
    classes), whose embedding association may be declared in that OTHER
    model's own table, never in `symbol_table`. Deliberately NOT
    `xtf/schema.py`'s `home_symbol_table` (used by `xtf/validate.py` for
    the analogous problem): that helper DISCOVERS the right table from a
    bare qualified-name string via `ModelRepository`, which here would
    return a table built by a SEPARATE parse of the same model file - a
    different Python object graph than the one `cls` itself belongs to,
    breaking `is_class_compatible`'s identity comparison
    (`embedded_roles_of`'s `Super`-chain walk). The caller (`cli.py`)
    already knows, by construction, the exact `SymbolTable` each class
    came from (one `InterlisModelBuilder` per `--catalog` file) - passing
    it directly keeps the class and the table it's queried against in the
    SAME identity graph, which discovery-via-repository cannot guarantee.

    Two real bugs found and fixed by executing the generated DDL against a
    real SQLite engine, not just eyeballing the text - neither
    was specific to one renderer, both affect PostgreSQL too:
    1. A short `Class.Name` collision across TOPICs (real corpus cases,
       e.g. two different `Item` classes) produced two `CREATE TABLE item`
       statements - disambiguated the SAME way as
       `convert/jsonschema.py`'s `_assign_keys` (`_2`/`_3` suffix).
    2. `UNIQUE <attr>;` on an attribute whose type never resolved to a
       mapped column (e.g. `INTERLIS.UUIDOID`, real corpus case
       `ili_corpus/Axis_V1_1.ili`) still built a `UNIQUE` constraint
       naming that (never-created) column - `CONSTRAINT ... UNIQUE
       (databaseid)` referencing a column that plain doesn't exist.
       Filtered out here (RULE #5: a note, not a crash-only-at-DDL-time
       surprise) by cross-checking against the columns actually built.
    """
    tables = []
    used_table_names: set[str] = set()
    # For abstract-STRUCTURE subclass discovery: the root table plus every
    # distinct per-class table (--catalog / folded-in models) - a concrete
    # subclass can be registered in a different model's table than the
    # abstract base it extends.
    _distinct_catalog_tables = {id(t): t for t in (class_symbol_tables or {}).values()}.values()
    scan_symbol_tables: list[SymbolTable] = [st for st in [symbol_table, *_distinct_catalog_tables] if st is not None]
    for cls in classes:
        if getattr(cls, "Kind", None) != "Class":
            continue
        base_name = _sql_identifier(getattr(cls, "Name", None) or "")
        table_name = base_name
        suffix = 2
        while table_name in used_table_names:
            table_name = f"{base_name}_{suffix}"
            suffix += 1
        used_table_names.add(table_name)
        if class_table_names is not None:
            class_table_names[id(cls)] = table_name

        home_table = (class_symbol_tables or {}).get(id(cls), symbol_table)
        columns, foreign_keys, notes, child_specs, nested_local_unique, abstract_specs = _columns_for_class(
            cls, home_table
        )
        renamed = _avoid_identity_collision(columns)
        unique_constraints, unique_notes, struct_global_unique = _unique_constraints_for_class(cls, table_name)
        for unique in unique_constraints:
            unique.columns = [renamed.get(c, c) for c in unique.columns]
        column_names = {c.name for c in columns}
        valid_unique_constraints = []
        for unique in unique_constraints:
            missing = [c for c in unique.columns if c not in column_names]
            if missing:
                unique_notes.append(
                    _diag(
                        "SQL-UNIQUE-COL-UNMAPPED",
                        f"UNIQUE ({', '.join(unique.columns)}): column(s) {missing} have no mapped SQL type",
                    )
                )
                continue
            valid_unique_constraints.append(unique)
        check_constraints, check_notes = _check_constraints_for_class(cls, table_name, column_names, renamed)
        local_unique, local_unique_notes = _local_unique_constraints_for_class(cls)
        for attr_name, groups in nested_local_unique.items():
            local_unique.setdefault(attr_name, []).extend(groups)

        parent_table = Table(
            name=table_name,
            columns=columns,
            unique_constraints=valid_unique_constraints,
            foreign_keys=foreign_keys,
            check_constraints=check_constraints,
            notes=notes + unique_notes + check_notes + local_unique_notes,
        )
        tables.append(parent_table)

        for attr_name, multi_value in child_specs:
            child_table, child_renamed, reason, nested_specs = _build_child_table(
                table_name, attr_name, multi_value, home_table
            )
            if child_table is None:
                rule = (
                    "SQL-BAGLIST-ELEMENT-UNRESOLVED"
                    if reason and "not resolved" in reason
                    else "SQL-BAGLIST-ELEMENT-UNMAPPED"
                )
                tables[-1].notes.append(_diag(rule, f"{attr_name}: BAG/LIST OF - {reason}"))
                continue
            child_base_name = child_table.name
            child_name = child_base_name
            suffix = 2
            while child_name in used_table_names:
                child_name = f"{child_base_name}_{suffix}"
                suffix += 1
            used_table_names.add(child_name)
            child_table.name = child_name

            fk_column = _sql_identifier(f"{table_name}_fk")
            child_column_names = {c.name for c in child_table.columns}
            for group_columns in local_unique.pop(attr_name, []):
                remapped = [child_renamed.get(c, c) for c in group_columns]
                full_columns = [fk_column, *remapped]
                missing = [c for c in full_columns if c not in child_column_names]
                if missing:
                    child_table.notes.append(
                        _diag(
                            "SQL-UNIQUE-COL-UNMAPPED",
                            f"UNIQUE (LOCAL) {attr_name}: column(s) {missing} have no mapped SQL type",
                        )
                    )
                    continue
                name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(full_columns)}"))
                child_table.unique_constraints.append(UniqueConstraint(name, full_columns))

            # `UNIQUE <this attr>->Sub` (global, no per-parent scoping): a
            # plain UNIQUE over the child table's own sub-attribute columns.
            for group_columns in struct_global_unique.pop(attr_name, []):
                remapped = [child_renamed.get(c, c) for c in group_columns]
                missing = [c for c in remapped if c not in child_column_names]
                if missing:
                    cols = ", ".join(group_columns)
                    child_table.notes.append(
                        _diag(
                            "SQL-UNIQUE-COL-UNMAPPED",
                            f"UNIQUE {attr_name}->({cols}): column(s) {missing} have no mapped SQL type",
                        )
                    )
                    continue
                name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(remapped)}"))
                child_table.unique_constraints.append(UniqueConstraint(name, remapped))

            tables.append(child_table)
            if nested_specs:
                tables.extend(_build_nested_child_tables(child_table, nested_specs, home_table, used_table_names))

        # An ABSTRACT structure attribute (single-valued or BAG/LIST OF):
        # one child table per concrete subclass reachable in the symbol
        # table, mirroring the JSON Schema pipeline's `anyOf`. The table
        # name (`<parent>_<attr>_<subclass>`) is the discriminant - no
        # extra `kind` column. No subclass in the conversion -> a `-- NOTE`.
        for attr_name, abstract_cls, ordered, from_multivalue in abstract_specs:
            subclasses = concrete_structure_subclasses(abstract_cls, home_table, *scan_symbol_tables)
            if not subclasses:
                if from_multivalue:
                    rule, kind = "SQL-BAGLIST-ELEMENT-UNMAPPED", "BAG/LIST OF an ABSTRACT structure"
                else:
                    rule, kind = "SQL-STRUCT-ABSTRACT", "ABSTRACT structure"
                parent_table.notes.append(
                    _diag(
                        rule,
                        f"{attr_name}: {kind} - no concrete subclass in this conversion to build a "
                        f"table per subtype (provide the model that defines them via --repo/--catalog)",
                    )
                )
                continue
            groups_for_attr = local_unique.pop(attr_name, [])
            fk_column = _sql_identifier(f"{table_name}_fk")
            for sub in subclasses:
                sub_name = _sql_identifier(getattr(sub, "Name", None) or "")
                child_name = _dedup_name(_sql_identifier(f"{table_name}_{attr_name}_{sub_name}"), used_table_names)
                child_table, child_renamed = _structure_child_table(
                    table_name, child_name, sub, home_table, ordered=ordered
                )
                child_column_names = {c.name for c in child_table.columns}
                for group_columns in groups_for_attr:
                    full_columns = [fk_column, *(child_renamed.get(c, c) for c in group_columns)]
                    missing = [c for c in full_columns if c not in child_column_names]
                    if missing:
                        child_table.notes.append(
                            _diag(
                                "SQL-UNIQUE-COL-UNMAPPED",
                                f"UNIQUE (LOCAL) {attr_name}: column(s) {missing} have no mapped SQL type",
                            )
                        )
                        continue
                    name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(full_columns)}"))
                    child_table.unique_constraints.append(UniqueConstraint(name, full_columns))
                tables.append(child_table)

        # Any UNIQUE (LOCAL) whose role hop never matched a real BAG/LIST OF
        # attribute on this class (typo, or a role path this project's
        # grammar mapping doesn't reach) - never silently dropped (RULE #5).
        for attr_name in local_unique:
            parent_table.notes.append(
                _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", f"UNIQUE (LOCAL) {attr_name}: no matching BAG/LIST OF attribute")
            )

        # `UNIQUE X->Y` whose first hop `X` is not a `BAG`/`LIST OF STRUCTURE`
        # attribute of this class: a real `REFERENCE TO`/role navigation to
        # another table's column (or an abstract structure split across
        # subtype tables). No single table/index constraint expresses it -
        # a BEFORE INSERT/UPDATE trigger would.
        for attr_name, groups in struct_global_unique.items():
            for group_columns in groups:
                parent_table.notes.append(
                    _diag(
                        "SQL-UNIQUE-CROSS-REF",
                        f"UNIQUE {attr_name}->({', '.join(group_columns)}): navigates a '->' reference to another "
                        "table - needs a trigger, not a table constraint",
                    )
                )

    # A real bug found the same way (PostgreSQL, live `psycopg`-free
    # verification against a real `postgis/postgis` container):
    # a `REFERENCE TO`/Role target belonging to a DIFFERENT model (real
    # corpus case, `ili_corpus/LWB_Bewirtschaftungseinheiten_V3_0.ili`'s
    # `Zone_Ausland` -> `LWB_Landwirtschaftliche_Zonengrenzen_Kataloge_V2_0.
    # LZ_Kataloge.LZ_Katalog_TypRef`) resolves to a REAL Class via
    # `reference_target_class` (repository-loaded, so not caught by the
    # "unresolved reference" check in `_columns_for_class`) but that class
    # is NOT among `classes` - this function deliberately converts ONE
    # model's OWN classes only (no cross-model reachability discovery,
    # unlike `convert/jsonschema.py`'s `_discover_classes`). The FK column itself
    # (a valid OID string either way) is kept; only the now-dangling
    # `FOREIGN KEY` constraint - which would `ALTER TABLE ... REFERENCES` a
    # table this conversion never creates - is dropped.
    final_table_names = {t.name for t in tables}
    for table in tables:
        kept_fks = []
        for fk in table.foreign_keys:
            if fk.ref_table not in final_table_names:
                table.notes.append(
                    _diag(
                        "SQL-FK-CROSS-MODEL-DROPPED",
                        f"FOREIGN KEY ({', '.join(fk.columns)}): target table {fk.ref_table!r} belongs to a "
                        "different model, not created by this conversion - pass that model via --catalog "
                        "(constraint dropped, column kept)",
                    )
                )
                continue
            kept_fks.append(fk)
        table.foreign_keys = kept_fks
    return tables
