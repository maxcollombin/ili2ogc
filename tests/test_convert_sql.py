"""Backlog item 14, Lot 1 - .ili -> SQL DDL (PostgreSQL + GeoPackage/SQLite): tables, columns, UNIQUE/FOREIGN KEY constraints.

See docs/sql-conversion-strategy.md for the design decision and scope.
"""

import sqlite3
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.sql import Column, UniqueConstraint, build_tables, render_gpkg, render_postgresql
from interlis.runtime.parse import meta_attribute_comments, parse_text

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"


def _build(src: str):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree, meta_attributes=meta_attribute_comments(src))
    return builder


def _resolved_class(builder, name: str):
    return builder.symbol_table.resolve(name)


def _table(tables, name: str):
    return next(t for t in tables if t.name == name)


_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    !!@CRS=EPSG:2056
    Coord2D = COORD 2000000.000 .. 3000000.000, 1000000.000 .. 1400000.000;
  TOPIC T =
    STRUCTURE Addr =
      Street : TEXT*50;
      Number : TEXT*10;
    END Addr;
    CLASS Owner =
      Code : MANDATORY TEXT*20;
      UNIQUE Code;
    END Owner;
    CLASS Parcel =
      ParcelNr : MANDATORY 0 .. 999999;
      Geom : MANDATORY Coord2D;
      Location : Addr;
      Owner : MANDATORY REFERENCE TO Owner;
      Tags : BAG {0..*} OF TEXT*5;
      UNIQUE ParcelNr, Owner->Code;
    END Parcel;
  END T;
END Foo.
"""


def test_scalar_and_mandatory_columns():
    builder = _build(_MODEL)
    owner = _resolved_class(builder, "Foo.T.Owner")
    tables = build_tables([owner])
    table = _table(tables, "owner")
    assert table.columns == [Column("code", "varchar(20)", nullable=False)]


def test_primary_key_and_unique_constraint():
    builder = _build(_MODEL)
    owner = _resolved_class(builder, "Foo.T.Owner")
    tables = build_tables([owner])
    table = _table(tables, "owner")
    ddl = render_postgresql(tables)
    assert '"id" text UNIQUE NOT NULL' in ddl
    assert len(table.unique_constraints) == 1
    assert table.unique_constraints[0].columns == ["code"]
    assert 'CONSTRAINT uq_owner_code UNIQUE ("code")' in ddl


def test_geometry_column_uses_sfa_type_and_resolved_srid():
    builder = _build(_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    table = _table(tables, "parcel")
    geom = next(c for c in table.columns if c.name == "geom")
    assert geom.geometry_type == "Point"
    assert geom.srid == 2056
    # `MANDATORY <named domain>` (e.g. `Geom : MANDATORY Coord2D;`) is a
    # reference to a domain SHARED by every attribute using it - fixed
    # 2026-08-27 (InterlisModelBuilder._apply_pending_mandatory_overrides,
    # see docs/sql-conversion-strategy.md): this attribute gets its OWN
    # private, Mandatory=True clone of Coord2D, never mutating the shared
    # domain instance any OTHER attribute might still reference.
    assert not geom.nullable


def test_structure_attribute_flattened_one_level():
    builder = _build(_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    table = _table(tables, "parcel")
    names = {c.name for c in table.columns}
    assert {"location_street", "location_number"} <= names
    assert "location" not in names


def test_reference_becomes_fk_column_and_constraint():
    builder = _build(_MODEL)
    owner = _resolved_class(builder, "Foo.T.Owner")
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables(
        [parcel, owner]
    )  # owner must ALSO be converted, or the FK gets dropped (see test_foreign_key_dropped_when_target_not_converted)
    table = _table(tables, "parcel")
    owner_col = next(c for c in table.columns if c.name == "owner")
    assert owner_col.sql_type == "text"
    assert not owner_col.nullable
    assert len(table.foreign_keys) == 1
    fk = table.foreign_keys[0]
    assert fk.columns == ["owner"]
    assert fk.ref_table == "owner"
    assert fk.ref_columns == ["id"]
    ddl = render_postgresql(tables)
    assert 'ALTER TABLE "parcel" ADD CONSTRAINT fk_parcel_owner FOREIGN KEY ("owner") REFERENCES "owner" ("id");' in ddl


def test_foreign_key_dropped_when_target_not_converted():
    """A REFERENCE TO target NOT in `classes` (real corpus case: a cross-model reference) keeps its column but drops the FK - never a dangling `REFERENCES` (found via a live PostGIS run, 2026-08-27)."""
    builder = _build(_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])  # owner deliberately NOT included
    table = _table(tables, "parcel")
    assert table.foreign_keys == []
    assert any(c.name == "owner" for c in table.columns)  # column itself is kept
    assert any("belongs to a" in note and "different model" in note for note in table.notes)


def test_multivalue_attribute_never_inlined_becomes_a_child_table():
    """See test_convert_sql.py's dedicated child-table tests (`_CHILD_TABLE_MODEL`) for the full shape - this just confirms `_MODEL`'s own Parcel.Tags isn't left as a plain column or a note."""
    builder = _build(_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    table = _table(tables, "parcel")
    assert not any(c.name == "tags" for c in table.columns)
    assert not any("BAG/LIST" in note for note in table.notes)
    assert any(t.name == "parcel_tags" for t in tables)


def test_unique_with_reference_navigation_gets_a_note_not_a_wrong_constraint():
    """`UNIQUE ParcelNr, Owner->Code;` - the WHOLE constraint is unsupported (RULE #5), not silently reduced to just ParcelNr."""
    builder = _build(_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    table = _table(tables, "parcel")
    assert table.unique_constraints == []
    assert any("->" in note for note in table.notes)


def test_structure_class_itself_is_not_a_table():
    builder = _build(_MODEL)
    addr = _resolved_class(builder, "Foo.T.Addr")
    tables = build_tables([addr])
    assert tables == []


def test_missing_crs_produces_a_note_not_an_untyped_column():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    NoCrsCoord = COORD 0.000 .. 1000.000, 0.000 .. 1000.000;
  TOPIC T =
    CLASS A =
      Geom : MANDATORY NoCrsCoord;
    END A;
  END T;
END Foo.
""")
    a = _resolved_class(builder, "Foo.T.A")
    tables = build_tables([a])
    table = _table(tables, "a")
    assert not any(c.name == "geom" for c in table.columns)
    assert any("no resolved CRS" in note for note in table.notes)


def test_role_becomes_fk_when_symbol_table_given():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Item =
      Code : TEXT*10;
    END Item;
    CLASS Holder =
      Name : TEXT*10;
    END Holder;
    ASSOCIATION Holder_Item =
      rHolder (EXTERNAL) -<#> Holder;
      rItem -- {0..*} Item;
    END Holder_Item;
  END T;
END Foo.
""")
    item = _resolved_class(builder, "Foo.T.Item")
    holder = _resolved_class(builder, "Foo.T.Holder")
    tables = build_tables([item, holder], symbol_table=builder.symbol_table)
    table = _table(tables, "item")
    fk = next(c for c in table.foreign_keys if c.ref_table == "holder")
    assert fk.columns == ["rholder"]


def test_enum_boolean_formatted_blackbox_column_types():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    MyDate = FORMAT INTERLIS.XMLDate "1900-01-01" .. "2999-12-31";
  TOPIC T =
    CLASS A =
      Kategorie : (a, b, c);
      Active : BOOLEAN;
      Created : MyDate;
      Blob : BLACKBOX BINARY;
    END A;
  END T;
END Foo.
""")
    a = _resolved_class(builder, "Foo.T.A")
    tables = build_tables([a])
    table = _table(tables, "a")
    types = {c.name: c.sql_type for c in table.columns}
    assert types["kategorie"] == "text"
    assert types["active"] == "boolean"
    assert types["created"] == "date"
    assert types["blob"] == "text"


def test_name_and_uri_text_kinds_get_exact_bounds():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Ident : NAME;
      Link : URI;
    END A;
  END T;
END Foo.
""")
    a = _resolved_class(builder, "Foo.T.A")
    tables = build_tables([a])
    table = _table(tables, "a")
    types = {c.name: c.sql_type for c in table.columns}
    assert types["ident"] == "varchar(255)"
    assert types["link"] == "varchar(1023)"


def test_render_postgresql_foreign_keys_come_after_every_create_table():
    builder = _build(_MODEL)
    owner = _resolved_class(builder, "Foo.T.Owner")
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    ddl = render_postgresql(build_tables([parcel, owner]))
    assert ddl.index('CREATE TABLE "parcel"') < ddl.index('ALTER TABLE "parcel"')
    assert ddl.index('CREATE TABLE "owner"') < ddl.index('ALTER TABLE "parcel"')


def test_render_gpkg_inline_unique_and_foreign_key():
    """Unlike PostgreSQL, GPKG/SQLite declares everything inline - no ALTER TABLE at all (see docs/sql-conversion-strategy.md, SQLite can't add constraints post-hoc)."""
    builder = _build(_MODEL)
    owner = _resolved_class(builder, "Foo.T.Owner")
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    ddl = render_gpkg(build_tables([parcel, owner]))
    assert "ALTER TABLE" not in ddl
    assert 'CONSTRAINT uq_owner_code UNIQUE ("code")' in ddl
    assert 'CONSTRAINT fk_parcel_owner FOREIGN KEY ("owner") REFERENCES "owner" ("id")' in ddl


def test_render_gpkg_geometry_column_and_metadata_rows():
    builder = _build(_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    ddl = render_gpkg(build_tables([parcel]))
    assert '"geom" POINT NOT NULL' in ddl
    assert (
        "INSERT INTO gpkg_contents (table_name, data_type, identifier, srs_id) VALUES ('parcel', 'features', 'parcel', 2056);"
        in ddl
    )
    assert (
        "INSERT INTO gpkg_geometry_columns (table_name, column_name, geometry_type_name, srs_id, z, m) "
        "VALUES ('parcel', 'geom', 'POINT', 2056, 0, 0);"
    ) in ddl
    assert "INSERT OR IGNORE INTO gpkg_spatial_ref_sys" in ddl
    assert "EPSG:2056" in ddl


def test_render_gpkg_epsg_4326_skipped_as_pre_registered():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    !!@CRS=EPSG:4326
    Coord2D = COORD -180.0 .. 180.0, -90.0 .. 90.0;
  TOPIC T =
    CLASS A =
      Geom : MANDATORY Coord2D;
    END A;
  END T;
END Foo.
""")
    a = _resolved_class(builder, "Foo.T.A")
    ddl = render_gpkg(build_tables([a]))
    assert "gpkg_spatial_ref_sys" not in ddl


def test_render_gpkg_non_spatial_table_registered_as_attributes():
    builder = _build(_MODEL)
    owner = _resolved_class(builder, "Foo.T.Owner")
    ddl = render_gpkg(build_tables([owner]))
    assert (
        "INSERT INTO gpkg_contents (table_name, data_type, identifier) VALUES ('owner', 'attributes', 'owner');" in ddl
    )


def test_duplicate_class_name_across_topics_gets_disambiguated():
    """Real corpus case (multiple files): two different classes named "Item" in different TOPICs - found via a live SQLite/PostgreSQL run, 2026-08-27 ("table already exists")."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T1 =
    CLASS Item =
      Code1 : TEXT*10;
    END Item;
  END T1;
  TOPIC T2 =
    CLASS Item =
      Code2 : TEXT*10;
    END Item;
  END T2;
END Foo.
""")
    item1 = _resolved_class(builder, "Foo.T1.Item")
    item2 = _resolved_class(builder, "Foo.T2.Item")
    tables = build_tables([item1, item2])
    names = [t.name for t in tables]
    assert names == ["item", "item_2"]
    assert len(names) == len(set(names))


def test_predefined_uuidoid_becomes_a_column_and_keeps_its_unique():
    """`INTERLIS.UUIDOID` (a reserved-token predefined type) is now materialised as a real TEXT*36 column, so `UNIQUE DatabaseId;` stays a valid constraint instead of being dropped - real corpus case ili_corpus/Axis_V1_1.ili."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      DatabaseId : MANDATORY INTERLIS.UUIDOID;
      HAli       : MANDATORY HALIGNMENT;
      Flag       : INTERLIS.BOOLEAN;
      Site       : INTERLIS.URI;
      UNIQUE DatabaseId;
    END A;
  END T;
END Foo.
""")
    a = _resolved_class(builder, "Foo.T.A")
    tables = build_tables([a])
    table = _table(tables, "a")
    by_name = {c.name: c for c in table.columns}
    assert by_name["databaseid"].sql_type == "varchar(36)"
    assert not by_name["databaseid"].nullable
    assert by_name["hali"].sql_type == "text" and not by_name["hali"].nullable
    assert by_name["flag"].sql_type == "boolean" and by_name["flag"].nullable
    assert by_name["site"].sql_type == "varchar(1023)"
    assert [u.columns for u in table.unique_constraints] == [["databaseid"]]
    assert not any("no mapped SQL type" in note for note in table.notes)
    assert not any("BUILD-TYPE-UNRESOLVED" in note or "SQL-ATTR-TYPE-UNMAPPED" in note for note in table.notes)


def test_reserved_keyword_class_name_gets_quoted():
    """A real class named after a SQL reserved word (real corpus cases: "Union", "Index") - unquoted, both PostgreSQL and SQLite reject `CREATE TABLE union (...)`."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Union =
      Code : TEXT*10;
    END Union;
  END T;
END Foo.
""")
    union = _resolved_class(builder, "Foo.T.Union")
    tables = build_tables([union])
    pg_ddl = render_postgresql(tables)
    gpkg_ddl = render_gpkg(tables)
    assert 'CREATE TABLE "union" (' in pg_ddl
    assert 'CREATE TABLE "union" (' in gpkg_ddl


def test_attribute_literally_named_id_gets_renamed_not_the_identity_column():
    """Real corpus case (ili_corpus/WasserBase_V1_1.ili): `ID : MANDATORY TEXT*25;` lowercases to the SAME name as the reserved identity column - found via a live SQLite run ("duplicate column name: id"), 2026-08-27."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      ID : MANDATORY TEXT*25;
      UNIQUE ID;
    END A;
  END T;
END Foo.
""")
    a = _resolved_class(builder, "Foo.T.A")
    tables = build_tables([a])
    table = _table(tables, "a")
    names = [c.name for c in table.columns]
    assert names == ["id_attr"]  # never a second "id" - that name is reserved for the identity column
    assert table.unique_constraints == [UniqueConstraint("uq_a_id", ["id_attr"])]
    ddl = render_postgresql(tables)
    assert '"id" text UNIQUE NOT NULL' in ddl
    assert '"id_attr" varchar(25) NOT NULL' in ddl
    assert 'CONSTRAINT uq_a_id UNIQUE ("id_attr")' in ddl


_CHILD_TABLE_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE LocalisedText =
      Language : TEXT*2;
      Text : MANDATORY TEXT*100;
    END LocalisedText;
    CLASS Parcel =
      ParcelNr : MANDATORY 0 .. 999999;
      Tags : BAG {0..*} OF TEXT*5;
      Names : LIST {0..*} OF LocalisedText;
    END Parcel;
  END T;
END Foo.
"""


def test_bag_of_scalar_becomes_a_child_table_with_a_value_column():
    builder = _build(_CHILD_TABLE_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    child = _table(tables, "parcel_tags")
    names = {c.name: c for c in child.columns}
    assert names["parcel_fk"].sql_type == "text"
    assert not names["parcel_fk"].nullable
    assert names["value"].sql_type == "varchar(5)"
    assert not names["value"].nullable
    assert not any(c.name == "seq" for c in child.columns)  # BAG - no ordering column
    fk = child.foreign_keys[0]
    assert fk.columns == ["parcel_fk"]
    assert fk.ref_table == "parcel"
    assert fk.ref_columns == ["id"]
    assert not any(c.name == "tags" for c in _table(tables, "parcel").columns)  # never inlined on the parent


def test_list_of_structure_child_table_has_seq_and_flattened_columns():
    builder = _build(_CHILD_TABLE_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    child = _table(tables, "parcel_names")
    names = {c.name: c for c in child.columns}
    assert (
        "seq" in names and names["seq"].sql_type == "integer" and not names["seq"].nullable
    )  # LIST - ordering matters
    assert names["language"].sql_type == "varchar(2)" and names["language"].nullable
    assert names["text"].sql_type == "varchar(100)" and not names["text"].nullable


def test_render_gpkg_child_table_inline_fk_and_no_topological_sort_needed():
    builder = _build(_CHILD_TABLE_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    ddl = render_gpkg(build_tables([parcel]))
    assert "ALTER TABLE" not in ddl
    assert 'CONSTRAINT fk_parcel_tags_parcel_fk FOREIGN KEY ("parcel_fk") REFERENCES "parcel" ("id")' in ddl


def test_child_table_name_collision_gets_disambiguated_like_any_other_table():
    """A real class literally named "<Parent>_<attr>" would collide with the synthesized child table name - defensive coverage, not seen in the real corpus."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Parcel_Tags =
      X : TEXT*5;
    END Parcel_Tags;
    CLASS Parcel =
      Tags : BAG {0..*} OF TEXT*5;
    END Parcel;
  END T;
END Foo.
""")
    collider = _resolved_class(builder, "Foo.T.Parcel_Tags")
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([collider, parcel])
    names = [t.name for t in tables]
    assert names == ["parcel_tags", "parcel", "parcel_tags_2"]


_CHECK_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE Addr =
      Street : TEXT*50;
      Number : TEXT*10;
    END Addr;
    CLASS Parcel =
      ParcelNr : MANDATORY 0 .. 999999;
      Status : MANDATORY TEXT*10;
      Loc : Addr;
      MANDATORY CONSTRAINT ParcelNr >= 0;
      MANDATORY CONSTRAINT NamedCheck: (Status == "Active" OR Status == "Closed") AND DEFINED(Loc->Street);
      MANDATORY CONSTRAINT Impl: Status == "Closed" => DEFINED(Loc->Number);
      MANDATORY CONSTRAINT NotClosed: NOT (Status == "Closed");
    END Parcel;
  END T;
END Foo.
"""


def test_mandatory_constraint_becomes_an_inline_check_constraint():
    builder = _build(_CHECK_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    pg_ddl = render_postgresql(tables)
    gpkg_ddl = render_gpkg(tables)
    assert 'CONSTRAINT chk_parcel_1 CHECK (("parcelnr" >= 0))' in pg_ddl
    assert (
        'CONSTRAINT chk_parcel_1 CHECK (("parcelnr" >= 0))' in gpkg_ddl
    )  # inline in BOTH dialects - CHECK has no forward-reference ordering issue, unlike FOREIGN KEY
    assert "ALTER TABLE" not in pg_ddl.split("CREATE TABLE")[0]  # CHECK never needs the FK's separate ALTER TABLE pass


def test_check_constraint_and_or_defined_over_a_flattened_struct_path():
    builder = _build(_CHECK_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    ddl = render_postgresql(build_tables([parcel]))
    assert (
        "CONSTRAINT chk_parcel_namedcheck CHECK "
        '(((("status" = \'Active\') OR ("status" = \'Closed\')) AND ("loc_street" IS NOT NULL)))'
    ) in ddl


def test_check_constraint_implication_and_not():
    builder = _build(_CHECK_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    ddl = render_postgresql(build_tables([parcel]))
    assert 'CONSTRAINT chk_parcel_impl CHECK ((NOT ("status" = \'Closed\') OR ("loc_number" IS NOT NULL)))' in ddl
    assert "CONSTRAINT chk_parcel_notclosed CHECK ((NOT (\"status\" = 'Closed')))" in ddl


def test_check_constraint_executes_against_real_sqlite_and_enforces_the_rule():
    """Not just text assembly - the generated CHECK must actually be enforceable SQL (same discipline as the UNIQUE/FOREIGN KEY live-engine checks elsewhere in this file)."""
    builder = _build(_CHECK_MODEL)
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    ddl = render_gpkg(build_tables([parcel]))
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl.split("INSERT INTO gpkg_contents")[0])
    conn.execute(
        "INSERT INTO parcel (id, parcelnr, status, loc_street, loc_number) VALUES (?,?,?,?,?)",
        ("1", 5, "Active", "Main St", None),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO parcel (id, parcelnr, status, loc_street, loc_number) VALUES (?,?,?,?,?)",
            ("2", -1, "Active", "Main St", None),
        )


def test_unsupported_constraint_expression_becomes_a_note_not_a_wrong_check():
    """Real corpus bug (2026-08-27, `ili_corpus/Naturereigniskataster_MGDM_V1.ili`): a `factor` alt this project's grammar mapping used to lose entirely (`INTERLIS.len(...)`, see spec/grammar/mapping/07_constraints.yml's `factor.INTERLIS` entry) collapsed to a bare attribute path - `INTERLIS.len(ParcelNr) == 3` would have silently built (and rendered a CHECK for) the wrong condition `ParcelNr == 3`. Fixed at construction (a real `FunctionCall` node now), so this must surface as an unsupported note - never a column comparison."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Parcel =
      Code : MANDATORY TEXT*20;
      MANDATORY CONSTRAINT (INTERLIS.len(Code)) == 3;
    END Parcel;
  END T;
END Foo.
""")
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    table = _table(tables, "parcel")
    assert table.check_constraints == []
    assert any("CHECK not generated" in note for note in table.notes)
    ddl = render_postgresql(tables)
    assert '"code" = ' not in ddl  # never a wrong CHECK comparing the raw column instead of len(...)


_LOCAL_UNIQUE_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE CountryName =
      Code : MANDATORY TEXT*10;
    END CountryName;
    CLASS CountryNamesTranslation =
      Entries : LIST {0..*} OF CountryName;
      UNIQUE (LOCAL) Entries: Code;
    END CountryNamesTranslation;
  END T;
END Foo.
"""


def test_unique_local_becomes_a_compound_unique_on_the_child_table():
    """Real corpus shape (`ili_corpus/CHBase_Part4_ADMINISTRATIVEUNITS_V2.ili`): `UNIQUE (LOCAL) Entries: Code;` scopes uniqueness to Code WITHIN one parent's own Entries list, not across the whole table - UNIQUE (parent_fk, code) on the child table, never a plain UNIQUE (code)."""
    builder = _build(_LOCAL_UNIQUE_MODEL)
    cls = _resolved_class(builder, "Foo.T.CountryNamesTranslation")
    tables = build_tables([cls])
    parent = _table(tables, "countrynamestranslation")
    child = _table(tables, "countrynamestranslation_entries")
    assert parent.unique_constraints == []  # the constraint belongs to the CHILD table, not the parent
    assert parent.notes == []
    assert child.unique_constraints == [
        UniqueConstraint(
            "uq_countrynamestranslation_entries_countrynamestranslation_fk_c",  # truncated to 63 chars, same as every other identifier in this module
            ["countrynamestranslation_fk", "code"],
        )
    ]


def test_unique_local_executes_against_real_sqlite_and_enforces_per_parent_scope():
    """Not just text assembly - same live-engine discipline as every other constraint in this file."""
    builder = _build(_LOCAL_UNIQUE_MODEL)
    cls = _resolved_class(builder, "Foo.T.CountryNamesTranslation")
    ddl = render_gpkg(build_tables([cls]))
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl.split("INSERT INTO gpkg_contents")[0])
    conn.execute("INSERT INTO countrynamestranslation (id) VALUES (?)", ("p1",))
    conn.execute("INSERT INTO countrynamestranslation (id) VALUES (?)", ("p2",))
    conn.execute(
        "INSERT INTO countrynamestranslation_entries (id, countrynamestranslation_fk, seq, code) VALUES (?,?,?,?)",
        ("e1", "p1", 0, "CH"),
    )
    conn.execute(  # same code, DIFFERENT parent - must be allowed (that's the whole point of "LOCAL")
        "INSERT INTO countrynamestranslation_entries (id, countrynamestranslation_fk, seq, code) VALUES (?,?,?,?)",
        ("e2", "p2", 0, "CH"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(  # same code, SAME parent - must be rejected
            "INSERT INTO countrynamestranslation_entries (id, countrynamestranslation_fk, seq, code) VALUES (?,?,?,?)",
            ("e3", "p1", 1, "CH"),
        )


def test_unique_local_on_a_structure_nested_one_level_into_a_class():
    """Dominant real corpus idiom (`ili_corpus/KbS_V1_5.ili`'s MultilingualUri/MultilingualText pattern): a STRUCTURE wraps the BAG/LIST AND declares UNIQUE (LOCAL) on itself, embedded one level into a Class as an ordinary attribute - the child table is qualified with the STRUCTURE attribute's own name (`parcel_name_entries`, not `parcel_entries`), with its UNIQUE (LOCAL) applied exactly like the direct-on-Class case."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE LocalisedText =
      Language : TEXT*2;
      Text : MANDATORY TEXT*50;
    END LocalisedText;
    STRUCTURE MultilingualText =
      Entries : BAG {1..*} OF LocalisedText;
      UNIQUE (LOCAL) Entries: Language;
    END MultilingualText;
    CLASS Parcel =
      Name : MANDATORY MultilingualText;
    END Parcel;
  END T;
END Foo.
""")
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    tables = build_tables([parcel])
    child = _table(tables, "parcel_name_entries")
    assert {c.name for c in child.columns} == {"parcel_fk", "language", "text"}
    assert any(u.columns == ["parcel_fk", "language"] for u in child.unique_constraints)
    assert child.foreign_keys[0].columns == ["parcel_fk"]
    assert child.foreign_keys[0].ref_table == "parcel"


def test_unique_local_on_a_nested_structure_executes_against_real_sqlite_and_enforces_per_parent_scope():
    """Not just text assembly - same live-engine discipline as the direct-on-Class case above."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE LocalisedText =
      Language : TEXT*2;
      Text : MANDATORY TEXT*50;
    END LocalisedText;
    STRUCTURE MultilingualText =
      Entries : BAG {1..*} OF LocalisedText;
      UNIQUE (LOCAL) Entries: Language;
    END MultilingualText;
    CLASS Parcel =
      Name : MANDATORY MultilingualText;
    END Parcel;
  END T;
END Foo.
""")
    parcel = _resolved_class(builder, "Foo.T.Parcel")
    ddl = render_gpkg(build_tables([parcel]))
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl.split("INSERT INTO gpkg_contents")[0])
    conn.execute("INSERT INTO parcel (id) VALUES (?)", ("p1",))
    conn.execute("INSERT INTO parcel (id) VALUES (?)", ("p2",))
    conn.execute(
        "INSERT INTO parcel_name_entries (id, parcel_fk, language, text) VALUES (?,?,?,?)",
        ("e1", "p1", "de", "Parzelle"),
    )
    conn.execute(  # same language, DIFFERENT parent - must be allowed (that's the whole point of "LOCAL")
        "INSERT INTO parcel_name_entries (id, parcel_fk, language, text) VALUES (?,?,?,?)",
        ("e2", "p2", "de", "Parzelle"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(  # same language, SAME parent - must be rejected
            "INSERT INTO parcel_name_entries (id, parcel_fk, language, text) VALUES (?,?,?,?)",
            ("e3", "p1", "de", "Parcelle"),
        )
