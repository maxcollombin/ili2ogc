"""Lot 1-3 (backlog item 5, second stage) - .xtf -> JSON-FG, Feature/FeatureCollection + single-attribute geometry.

See docs/jsonfg-conversion-strategy.md for the design decision and scope
(JSON-FG "core" + "types-schemas" requirements classes only).
"""

import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.jsonfg import (
    CONF_CIRCULAR_ARCS,
    CONF_CORE,
    CONF_TYPES_SCHEMAS,
    _child_row_features,
    object_to_feature,
    transfer_to_feature_collection,
)
from interlis.runtime.parse import meta_attribute_comments, parse_text
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"

_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Age : 0 .. 130;
      Height : 0.000 .. 999.999;
      Code : TEXT*20;
      Active : BOOLEAN;
      Ref : REFERENCE TO A;
    END A;
  END T;
END Foo.
"""


def _build(src: str, *, capture_meta: bool = False):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if capture_meta:
            builder.build(tree, meta_attributes=meta_attribute_comments(src))
        else:
            builder.build(tree)
    return builder


def _resolved_class(builder, name: str):
    return builder.symbol_table.resolve(name)


def _node(tag: str, text: str) -> RawNode:
    return RawNode(tag=tag, text=text, attrib={}, children=[])


def _ref_node(tag: str, target: str) -> RawNode:
    return RawNode(tag=tag, text=None, attrib={"REF": target}, children=[])


def _wrap(tag: str, *children: RawNode) -> RawNode:
    return RawNode(tag=tag, text=None, attrib={}, children=list(children))


def _coord(c1: str, c2: str) -> RawNode:
    return _wrap("COORD", _node("C1", c1), _node("C2", c2))


_REF_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Item =
      Code : TEXT*10;
    END Item;
    CLASS Location =
      Name : TEXT*20;
    END Location;
    CLASS Indicator =
      Value : TEXT*20;
    END Indicator;
    ASSOCIATION Location_Indicator =
      rLocation (EXTERNAL) -<#> Location;
      rIndicator -- {0..*} Indicator;
    END Location_Indicator;
    STRUCTURE ItemRef =
      Reference : MANDATORY REFERENCE TO Item;
    END ItemRef;
    STRUCTURE PlainWrapper =
      Sub : TEXT*10;
    END PlainWrapper;
    CLASS Holder =
      DirectRef : REFERENCE TO Item;
      CatalogRef : ItemRef;
      NoRefStruct : PlainWrapper;
    END Holder;
  END T;
END Foo.
"""

_MULTI_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE LocalisedText =
      Language : TEXT*2;
      Text : TEXT*100;
    END LocalisedText;
    STRUCTURE MultilingualText =
      LocalisedText : BAG {1..*} OF LocalisedText;
    END MultilingualText;
    CLASS Facility =
      Codes : BAG {0..*} OF TEXT*5;
      Name : MultilingualText;
    END Facility;
  END T;
END Foo.
"""


_GEOM_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    !!@CRS=EPSG:2056
    Coord2D = COORD 2000000.000 .. 3000000.000, 1000000.000 .. 1400000.000;
    !!@CRS=EPSG:2056
    MultiCoord2D = MULTICOORD 2000000.000 .. 3000000.000, 1000000.000 .. 1400000.000;
    NoCrsCoord = COORD 0.000 .. 1000.000, 0.000 .. 1000.000;
    !!@CRS=EPSG:21781
    Coord2DOther = COORD 400000.000 .. 900000.000, 0.000 .. 400000.000;
    Line = POLYLINE WITH (STRAIGHTS, ARCS) VERTEX Coord2D;
    MultiLine = MULTIPOLYLINE WITH (STRAIGHTS, ARCS) VERTEX Coord2D;
    Poly = SURFACE WITH (STRAIGHTS) VERTEX Coord2D WITHOUT OVERLAPS > 0.001;
    PolyArc = SURFACE WITH (STRAIGHTS, ARCS) VERTEX Coord2D WITHOUT OVERLAPS > 0.001;
    MultiPolyArc = MULTISURFACE WITH (STRAIGHTS, ARCS) VERTEX Coord2D WITHOUT OVERLAPS > 0.001;
  TOPIC T =
    CLASS APoint =
      Geom : MANDATORY Coord2D;
    END APoint;
    CLASS AMultiPoint =
      Geom : MANDATORY MultiCoord2D;
    END AMultiPoint;
    CLASS ALine =
      Geom : MANDATORY Line;
    END ALine;
    CLASS AMultiLine =
      Geom : MANDATORY MultiLine;
    END AMultiLine;
    CLASS APoly =
      Geom : MANDATORY Poly;
    END APoly;
    CLASS APolyArc =
      Geom : MANDATORY PolyArc;
    END APolyArc;
    CLASS AMultiPolyArc =
      Geom : MANDATORY MultiPolyArc;
    END AMultiPolyArc;
    CLASS ANoCrs =
      Geom : MANDATORY NoCrsCoord;
    END ANoCrs;
    CLASS ATwoGeoms =
      Point : MANDATORY Coord2D;
      Area : Poly;
    END ATwoGeoms;
    CLASS ATwoGeomsDiffCrs =
      PointA : MANDATORY Coord2D;
      PointB : MANDATORY Coord2DOther;
    END ATwoGeomsDiffCrs;
  END T;
END Foo.
"""


def test_scalar_properties_id_and_metadata():
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(
        tid="obj-1",
        qualified_class="Foo.T.A",
        attributes={
            "Age": [_node("Age", "42")],
            "Height": [_node("Height", "1.75")],
            "Code": [_node("Code", "hello")],
            "Active": [_node("Active", "true")],
        },
    )
    feature = object_to_feature(obj, cls)
    assert feature["type"] == "Feature"
    assert feature["id"] == "obj-1"
    assert feature["featureType"] == "A"
    assert feature["geometry"] is None
    assert feature["conformsTo"] == [CONF_CORE, CONF_TYPES_SCHEMAS]
    assert feature["properties"] == {"Age": 42, "Height": 1.75, "Code": "hello", "Active": True}


def test_boolean_false_and_integer_vs_number_typing():
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(
        tid="obj-2",
        qualified_class="Foo.T.A",
        attributes={
            "Age": [_node("Age", "7")],
            "Height": [_node("Height", "2")],
            "Active": [_node("Active", "false")],
        },
    )
    feature = object_to_feature(obj, cls)
    assert feature["properties"]["Age"] == 7
    assert isinstance(feature["properties"]["Age"], int)
    assert feature["properties"]["Height"] == 2.0
    assert isinstance(feature["properties"]["Height"], float)
    assert feature["properties"]["Active"] is False


def test_missing_tid_omits_id():
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(tid=None, qualified_class="Foo.T.A", attributes={})
    feature = object_to_feature(obj, cls)
    assert "id" not in feature


def test_out_of_scope_attribute_gets_marker_not_dropped():
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(
        tid="obj-3",
        qualified_class="Foo.T.A",
        attributes={"Ref": [_node("Ref", "obj-1")]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["properties"]["Ref"] == {"x-unsupported": "ReferenceType"}


def test_unknown_attribute_name_skipped():
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(
        tid="obj-4",
        qualified_class="Foo.T.A",
        attributes={"NotInSchema": [_node("NotInSchema", "x")]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["properties"] == {}


def test_coord_attribute_becomes_place_point_with_crs():
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "APoint")
    obj = XtfObject(
        tid="p-1",
        qualified_class="Foo.T.APoint",
        attributes={"Geom": [_wrap("Geom", _coord("2600000.0", "1200000.0"))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {"type": "Point", "coordinates": [2600000.0, 1200000.0]}
    assert feature["coordRefSys"] == "http://www.opengis.net/def/crs/EPSG/0/2056"
    assert "Geom" not in feature["properties"]
    assert feature["geometry"] is None


def test_multicoord_attribute_becomes_place_multipoint():
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "AMultiPoint")
    obj = XtfObject(
        tid="mp-1",
        qualified_class="Foo.T.AMultiPoint",
        attributes={
            "Geom": [
                _wrap(
                    "Geom",
                    _wrap(
                        "MULTICOORD",
                        _coord("2600000.0", "1200000.0"),
                        _coord("2600100.0", "1200100.0"),
                    ),
                )
            ]
        },
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {
        "type": "MultiPoint",
        "coordinates": [[2600000.0, 1200000.0], [2600100.0, 1200100.0]],
    }
    assert "Geom" not in feature["properties"]


def test_polyline_attribute_becomes_place_linestring():
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ALine")
    obj = XtfObject(
        tid="l-1",
        qualified_class="Foo.T.ALine",
        attributes={
            "Geom": [
                _wrap(
                    "Geom",
                    _wrap(
                        "POLYLINE",
                        _coord("2600000.0", "1200000.0"),
                        _coord("2600100.0", "1200100.0"),
                    ),
                )
            ]
        },
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {
        "type": "LineString",
        "coordinates": [[2600000.0, 1200000.0], [2600100.0, 1200100.0]],
    }


def test_surface_attribute_becomes_place_polygon_outer_ring_first():
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "APoly")
    outer = _wrap(
        "BOUNDARY",
        _wrap(
            "POLYLINE",
            _coord("0.0", "0.0"),
            _coord("10.0", "0.0"),
            _coord("10.0", "10.0"),
            _coord("0.0", "0.0"),
        ),
    )
    hole = _wrap(
        "BOUNDARY",
        _wrap(
            "POLYLINE",
            _coord("1.0", "1.0"),
            _coord("2.0", "1.0"),
            _coord("2.0", "2.0"),
            _coord("1.0", "1.0"),
        ),
    )
    obj = XtfObject(
        tid="s-1",
        qualified_class="Foo.T.APoly",
        attributes={"Geom": [_wrap("Geom", _wrap("SURFACE", outer, hole))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"]["type"] == "Polygon"
    rings = feature["place"]["coordinates"]
    assert len(rings) == 2
    assert rings[0][0] == [0.0, 0.0]  # outer ring listed first, eCH-0031 order-only convention
    assert rings[1][0] == [1.0, 1.0]


def test_polyline_pure_arc_becomes_bare_circular_string():
    """A POLYLINE = COORD then a single ARC, no other straight segment -> a bare `CircularString`, not wrapped in
    `CompoundCurve`.
    """
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ALine")
    arc = _wrap(
        "ARC", _node("C1", "2600100.0"), _node("C2", "1200100.0"), _node("A1", "2600050.0"), _node("A2", "1200050.0")
    )
    obj = XtfObject(
        tid="l-2",
        qualified_class="Foo.T.ALine",
        attributes={"Geom": [_wrap("Geom", _wrap("POLYLINE", _coord("2600000.0", "1200000.0"), arc))]},
    )
    feature = object_to_feature(obj, cls)
    assert "Geom" not in feature["properties"]
    assert feature["place"] == {
        "type": "CircularString",
        "coordinates": [[2600000.0, 1200000.0], [2600050.0, 1200050.0], [2600100.0, 1200100.0]],
    }
    assert CONF_CIRCULAR_ARCS in feature["conformsTo"]


def test_polyline_straight_then_arc_then_straight_becomes_compound_curve():
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ALine")
    arc = _wrap(
        "ARC", _node("C1", "2600100.0"), _node("C2", "1200100.0"), _node("A1", "2600050.0"), _node("A2", "1200050.0")
    )
    obj = XtfObject(
        tid="l-4",
        qualified_class="Foo.T.ALine",
        attributes={
            "Geom": [
                _wrap(
                    "Geom",
                    _wrap(
                        "POLYLINE",
                        _coord("2600000.0", "1200000.0"),
                        arc,
                        _coord("2600200.0", "1200200.0"),
                    ),
                )
            ]
        },
    )
    feature = object_to_feature(obj, cls)
    place = feature["place"]
    assert place["type"] == "CompoundCurve"
    assert place["geometries"] == [
        {
            "type": "CircularString",
            "coordinates": [[2600000.0, 1200000.0], [2600050.0, 1200050.0], [2600100.0, 1200100.0]],
        },
        {"type": "LineString", "coordinates": [[2600100.0, 1200100.0], [2600200.0, 1200200.0]]},
    ]
    assert CONF_CIRCULAR_ARCS in feature["conformsTo"]


def test_custom_line_form_segment_falls_back_to_unsupported_property():
    """A POLYLINE segment that's neither COORD nor ARC (a custom LINE FORM) stays out of scope.

    RULE #5, never guessed.
    """
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ALine")
    custom = _wrap("CustomForm", _node("X", "1"))
    obj = XtfObject(
        tid="l-5",
        qualified_class="Foo.T.ALine",
        attributes={"Geom": [_wrap("Geom", _wrap("POLYLINE", _coord("2600000.0", "1200000.0"), custom))]},
    )
    feature = object_to_feature(obj, cls)
    assert "place" not in feature
    assert feature["properties"]["Geom"] == {"x-unsupported": "LineType"}


def test_polyline_two_chained_arcs_become_one_circular_string():
    """Two consecutive ARC segments share their common endpoint - one 5-position `CircularString`, JSON-FG SS7.5.1."""
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ALine")
    arc1 = _wrap("ARC", _node("C1", "2.0"), _node("C2", "0.0"), _node("A1", "1.0"), _node("A2", "1.0"))
    arc2 = _wrap("ARC", _node("C1", "4.0"), _node("C2", "0.0"), _node("A1", "3.0"), _node("A2", "1.0"))
    obj = XtfObject(
        tid="l-6",
        qualified_class="Foo.T.ALine",
        attributes={"Geom": [_wrap("Geom", _wrap("POLYLINE", _coord("0.0", "0.0"), arc1, arc2))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {
        "type": "CircularString",
        "coordinates": [[0.0, 0.0], [1.0, 1.0], [2.0, 0.0], [3.0, 1.0], [4.0, 0.0]],
    }


def test_surface_arc_boundary_becomes_curve_polygon():
    """A SURFACE whose (sole) boundary mixes an ARC with straight segments -> a `CurvePolygon` wrapping a
    `CompoundCurve` ring.
    """
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "APolyArc")
    arc = _wrap("ARC", _node("C1", "10.0"), _node("C2", "0.0"), _node("A1", "5.0"), _node("A2", "5.0"))
    boundary = _wrap("BOUNDARY", _wrap("POLYLINE", _coord("0.0", "0.0"), arc, _coord("0.0", "0.0")))
    obj = XtfObject(
        tid="s-2",
        qualified_class="Foo.T.APolyArc",
        attributes={"Geom": [_wrap("Geom", _wrap("SURFACE", boundary))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {
        "type": "CurvePolygon",
        "geometries": [
            {
                "type": "CompoundCurve",
                "geometries": [
                    {"type": "CircularString", "coordinates": [[0.0, 0.0], [5.0, 5.0], [10.0, 0.0]]},
                    {"type": "LineString", "coordinates": [[10.0, 0.0], [0.0, 0.0]]},
                ],
            },
        ],
    }
    assert CONF_CIRCULAR_ARCS in feature["conformsTo"]


def test_multipolyline_with_one_arc_part_becomes_multi_curve():
    """One straight + one curved part in a MULTIPOLYLINE -> `MultiCurve`.

    Every part is a full geometry object (SS7.5.4).
    """
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "AMultiLine")
    straight = _wrap("POLYLINE", _coord("0.0", "0.0"), _coord("1.0", "0.0"))
    arc = _wrap("ARC", _node("C1", "12.0"), _node("C2", "10.0"), _node("A1", "11.0"), _node("A2", "11.0"))
    curved = _wrap("POLYLINE", _coord("10.0", "10.0"), arc)
    obj = XtfObject(
        tid="ml-1",
        qualified_class="Foo.T.AMultiLine",
        attributes={"Geom": [_wrap("Geom", _wrap("MULTIPOLYLINE", straight, curved))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {
        "type": "MultiCurve",
        "geometries": [
            {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 0.0]]},
            {"type": "CircularString", "coordinates": [[10.0, 10.0], [11.0, 11.0], [12.0, 10.0]]},
        ],
    }
    assert CONF_CIRCULAR_ARCS in feature["conformsTo"]


def test_multisurface_with_one_curved_part_becomes_multi_surface():
    """One straight `Polygon` + one curved `CurvePolygon` part in a MULTISURFACE -> `MultiSurface` (SS7.5.5)."""
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "AMultiPolyArc")
    straight_boundary = _wrap(
        "BOUNDARY",
        _wrap(
            "POLYLINE",
            _coord("0.0", "0.0"),
            _coord("1.0", "0.0"),
            _coord("1.0", "1.0"),
            _coord("0.0", "0.0"),
        ),
    )
    arc = _wrap("ARC", _node("C1", "20.0"), _node("C2", "10.0"), _node("A1", "15.0"), _node("A2", "15.0"))
    curved_boundary = _wrap("BOUNDARY", _wrap("POLYLINE", _coord("10.0", "10.0"), arc, _coord("10.0", "10.0")))
    straight_surface = _wrap("SURFACE", straight_boundary)
    curved_surface = _wrap("SURFACE", curved_boundary)
    obj = XtfObject(
        tid="ms-1",
        qualified_class="Foo.T.AMultiPolyArc",
        attributes={"Geom": [_wrap("Geom", _wrap("MULTISURFACE", straight_surface, curved_surface))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {
        "type": "MultiSurface",
        "geometries": [
            {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]},
            {
                "type": "CurvePolygon",
                "geometries": [
                    {
                        "type": "CompoundCurve",
                        "geometries": [
                            {"type": "CircularString", "coordinates": [[10.0, 10.0], [15.0, 15.0], [20.0, 10.0]]},
                            {"type": "LineString", "coordinates": [[20.0, 10.0], [10.0, 10.0]]},
                        ],
                    },
                ],
            },
        ],
    }
    assert CONF_CIRCULAR_ARCS in feature["conformsTo"]


def test_missing_crs_meta_falls_back_to_unsupported_property():
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ANoCrs")
    obj = XtfObject(
        tid="nc-1",
        qualified_class="Foo.T.ANoCrs",
        attributes={"Geom": [_wrap("Geom", _coord("100.0", "200.0"))]},
    )
    feature = object_to_feature(obj, cls)
    assert "place" not in feature
    assert feature["properties"]["Geom"] == {"x-unsupported": "CoordType"}


def test_multi_geometry_class_gets_geometry_collection_place():
    """A class with 2 resolvable geometry attributes (real corpus shape: `Station`, point + area) bundles both into one
    `GeometryCollection` - no "primary" is picked.
    """
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ATwoGeoms")
    outer = _wrap(
        "BOUNDARY",
        _wrap(
            "POLYLINE",
            _coord("0.0", "0.0"),
            _coord("10.0", "0.0"),
            _coord("10.0", "10.0"),
            _coord("0.0", "0.0"),
        ),
    )
    obj = XtfObject(
        tid="tg-1",
        qualified_class="Foo.T.ATwoGeoms",
        attributes={
            "Point": [_wrap("Point", _coord("2600000.0", "1200000.0"))],
            "Area": [_wrap("Area", _wrap("SURFACE", outer))],
        },
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Point", "coordinates": [2600000.0, 1200000.0]},
            {"type": "Polygon", "coordinates": [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 0.0]]]},
        ],
    }
    assert feature["coordRefSys"] == "http://www.opengis.net/def/crs/EPSG/0/2056"
    assert "Point" not in feature["properties"]
    assert "Area" not in feature["properties"]


def test_multi_geometry_class_with_only_one_attribute_populated():
    """Only one of the two geometry-typed attributes has a wire value - unchanged single-geometry behaviour, no
    collection wrapping.
    """
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ATwoGeoms")
    obj = XtfObject(
        tid="tg-2",
        qualified_class="Foo.T.ATwoGeoms",
        attributes={"Point": [_wrap("Point", _coord("2600000.0", "1200000.0"))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["place"] == {"type": "Point", "coordinates": [2600000.0, 1200000.0]}
    assert "Area" not in feature["properties"]


def test_multi_geometry_class_with_mismatched_crs_gets_no_place():
    """No real corpus evidence of this ever occurring.

    Defensive coverage only (RULE #5: never guess which CRS wins).
    """
    builder = _build(_GEOM_MODEL, capture_meta=True)
    cls = _resolved_class(builder, "ATwoGeomsDiffCrs")
    obj = XtfObject(
        tid="tg-3",
        qualified_class="Foo.T.ATwoGeomsDiffCrs",
        attributes={
            "PointA": [_wrap("PointA", _coord("2600000.0", "1200000.0"))],
            "PointB": [_wrap("PointB", _coord("600000.0", "200000.0"))],
        },
    )
    feature = object_to_feature(obj, cls)
    assert "place" not in feature
    assert "coordRefSys" not in feature
    assert feature["properties"]["PointA"] == {"x-unsupported": "CoordType"}
    assert feature["properties"]["PointB"] == {"x-unsupported": "CoordType"}


def test_without_meta_capture_crs_is_unresolved():
    """`meta_attributes` is opt-in (Lot 3, .ili side) - without it, even a CRS-carrying domain yields no place."""
    builder = _build(_GEOM_MODEL, capture_meta=False)
    cls = _resolved_class(builder, "APoint")
    obj = XtfObject(
        tid="p-2",
        qualified_class="Foo.T.APoint",
        attributes={"Geom": [_wrap("Geom", _coord("2600000.0", "1200000.0"))]},
    )
    feature = object_to_feature(obj, cls)
    assert "place" not in feature
    assert feature["properties"]["Geom"] == {"x-unsupported": "CoordType"}


def test_standalone_false_omits_conforms_to():
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(tid="obj-5", qualified_class="Foo.T.A", attributes={})
    feature = object_to_feature(obj, cls, standalone=False)
    assert "conformsTo" not in feature
    assert feature["type"] == "Feature"
    assert feature["featureType"] == "A"


def test_schema_url_populates_feature_schema_as_string():
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(tid="obj-6", qualified_class="Foo.T.A", attributes={})
    feature = object_to_feature(obj, cls, schema_url="schema.json")
    assert feature["featureSchema"] == "schema.json#/$defs/A"


def test_without_schema_url_omits_feature_schema():
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(tid="obj-7", qualified_class="Foo.T.A", attributes={})
    feature = object_to_feature(obj, cls)
    assert "featureSchema" not in feature


def test_standalone_false_omits_feature_schema_even_with_schema_url():
    """The "featureSchema" member stays root-object-only here, same stance as "conformsTo" - never duplicated per nested
    Feature.
    """
    builder = _build(_MODEL)
    cls = _resolved_class(builder, "A")
    obj = XtfObject(tid="obj-8", qualified_class="Foo.T.A", attributes={})
    feature = object_to_feature(obj, cls, standalone=False, schema_url="schema.json")
    assert "featureSchema" not in feature


def test_transfer_to_feature_collection_wraps_features_without_per_feature_conforms_to():
    builder = _build(_MODEL)
    basket = XtfBasket(
        bid="b1",
        qualified_topic="Foo.T",
        kind=None,
        endstate=None,
        objects=[
            XtfObject(tid="c-1", qualified_class="Foo.T.A", attributes={"Age": [_node("Age", "1")]}),
            XtfObject(tid="c-2", qualified_class="Foo.T.A", attributes={"Age": [_node("Age", "2")]}),
        ],
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table)
    assert collection["type"] == "FeatureCollection"
    assert collection["conformsTo"] == [CONF_CORE, CONF_TYPES_SCHEMAS]
    assert len(collection["features"]) == 2
    assert all("conformsTo" not in f for f in collection["features"])
    assert {f["id"] for f in collection["features"]} == {"c-1", "c-2"}
    # homogeneous collection (clause 13 Recommendation A): featureType hoisted too
    assert collection["featureType"] == "A"


def test_transfer_to_feature_collection_skips_unresolvable_class():
    builder = _build(_MODEL)
    basket = XtfBasket(
        bid="b1",
        qualified_topic="Foo.T",
        kind=None,
        endstate=None,
        objects=[XtfObject(tid="u-1", qualified_class="Foo.T.DoesNotExist", attributes={})],
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table)
    assert collection["features"] == []
    assert "featureType" not in collection


def test_transfer_to_feature_collection_no_hoisted_featuretype_when_heterogeneous():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Age : 0 .. 130;
    END A;
    CLASS B =
      Age : 0 .. 130;
    END B;
  END T;
END Foo.
""")
    basket = XtfBasket(
        bid="b1",
        qualified_topic="Foo.T",
        kind=None,
        endstate=None,
        objects=[
            XtfObject(tid="a-1", qualified_class="Foo.T.A", attributes={}),
            XtfObject(tid="b-1", qualified_class="Foo.T.B", attributes={}),
        ],
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table)
    assert "featureType" not in collection
    assert {f["featureType"] for f in collection["features"]} == {"A", "B"}


def test_transfer_to_feature_collection_schema_url_homogeneous_is_a_string():
    builder = _build(_MODEL)
    basket = XtfBasket(
        bid="b1",
        qualified_topic="Foo.T",
        kind=None,
        endstate=None,
        objects=[
            XtfObject(tid="c-1", qualified_class="Foo.T.A", attributes={}),
            XtfObject(tid="c-2", qualified_class="Foo.T.A", attributes={}),
        ],
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table, schema_url="schema.json")
    assert collection["featureSchema"] == "schema.json#/$defs/A"
    assert all("featureSchema" not in f for f in collection["features"])


def test_transfer_to_feature_collection_schema_url_heterogeneous_is_an_object():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Age : 0 .. 130;
    END A;
    CLASS B =
      Age : 0 .. 130;
    END B;
  END T;
END Foo.
""")
    basket = XtfBasket(
        bid="b1",
        qualified_topic="Foo.T",
        kind=None,
        endstate=None,
        objects=[
            XtfObject(tid="a-1", qualified_class="Foo.T.A", attributes={}),
            XtfObject(tid="b-1", qualified_class="Foo.T.B", attributes={}),
        ],
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table, schema_url="schema.json")
    assert collection["featureSchema"] == {"A": "schema.json#/$defs/A", "B": "schema.json#/$defs/B"}


def test_transfer_to_feature_collection_without_schema_url_omits_feature_schema():
    builder = _build(_MODEL)
    basket = XtfBasket(
        bid="b1",
        qualified_topic="Foo.T",
        kind=None,
        endstate=None,
        objects=[XtfObject(tid="c-1", qualified_class="Foo.T.A", attributes={})],
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table)
    assert "featureSchema" not in collection


def test_plain_reference_to_becomes_string_oid():
    builder = _build(_REF_MODEL)
    cls = _resolved_class(builder, "Holder")
    obj = XtfObject(
        tid="h-1",
        qualified_class="Foo.T.Holder",
        attributes={"DirectRef": [_wrap("DirectRef", _ref_node("Item", "tgt-1"))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["properties"]["DirectRef"] == "tgt-1"


def test_catalog_reference_structure_becomes_string_oid():
    """1-own-attribute STRUCTURE wrapping a REFERENCE TO.

    Real corpus pattern, RoadTrafficAccidentLocation_V2's AccidentType/
    RoadType/... attributes - the REF is findable 2 levels deep, same
    _extract_reference search as a plain REFERENCE TO, no special-casing
    needed.
    """
    builder = _build(_REF_MODEL)
    cls = _resolved_class(builder, "Holder")
    obj = XtfObject(
        tid="h-2",
        qualified_class="Foo.T.Holder",
        attributes={"CatalogRef": [_wrap("CatalogRef", _wrap("ItemRefWrapper", _ref_node("Reference", "tgt-2")))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["properties"]["CatalogRef"] == "tgt-2"


def test_genuine_structure_without_ref_recurses_into_nested_object():
    builder = _build(_REF_MODEL)
    cls = _resolved_class(builder, "Holder")
    obj = XtfObject(
        tid="h-3",
        qualified_class="Foo.T.Holder",
        attributes={"NoRefStruct": [_wrap("NoRefStruct", _wrap("PlainWrapperWrapper", _node("Sub", "hello")))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["properties"]["NoRefStruct"] == {"Sub": "hello"}


def test_embedded_role_absent_without_symbol_table():
    builder = _build(_REF_MODEL)
    cls = _resolved_class(builder, "Indicator")
    obj = XtfObject(
        tid="i-1",
        qualified_class="Foo.T.Indicator",
        attributes={"Value": [_node("Value", "v")], "rLocation": [_ref_node("rLocation", "loc-1")]},
    )
    feature = object_to_feature(obj, cls)
    assert set(feature["properties"]) == {"Value"}


def test_embedded_role_becomes_string_oid_with_symbol_table():
    builder = _build(_REF_MODEL)
    cls = _resolved_class(builder, "Indicator")
    obj = XtfObject(
        tid="i-2",
        qualified_class="Foo.T.Indicator",
        attributes={"Value": [_node("Value", "v")], "rLocation": [_ref_node("rLocation", "loc-1")]},
    )
    feature = object_to_feature(obj, cls, symbol_table=builder.symbol_table)
    assert feature["properties"]["rLocation"] == "loc-1"
    assert feature["properties"]["Value"] == "v"


def test_multivalue_of_scalars_becomes_array():
    builder = _build(_MULTI_MODEL)
    cls = _resolved_class(builder, "Facility")
    obj = XtfObject(
        tid="f-1",
        qualified_class="Foo.T.Facility",
        attributes={"Codes": [_wrap("Codes", _node("Item", "AA"), _node("Item", "BB"))]},
    )
    feature = object_to_feature(obj, cls)
    assert feature["properties"]["Codes"] == ["AA", "BB"]


def test_multivalue_of_structures_becomes_array_of_nested_objects():
    """Real corpus pattern: Facility.Name (MultilingualText) -> BAG OF LocalisedText.

    Each occurrence itself a structure with own scalar attributes -
    2-level recursion, already_unwrapped correctly distinguishing the
    container level from the occurrence level.
    """
    builder = _build(_MULTI_MODEL)
    cls = _resolved_class(builder, "Facility")
    localised_en = _wrap("LocalisedText", _node("Language", "en"), _node("Text", "Hello"))
    localised_fr = _wrap("LocalisedText", _node("Language", "fr"), _node("Text", "Bonjour"))
    name_wire = _wrap("Name", _wrap("MultilingualText", _wrap("LocalisedText", localised_en, localised_fr)))
    obj = XtfObject(tid="f-2", qualified_class="Foo.T.Facility", attributes={"Name": [name_wire]})
    feature = object_to_feature(obj, cls)
    assert feature["properties"]["Name"] == {
        "LocalisedText": [
            {"Language": "en", "Text": "Hello"},
            {"Language": "fr", "Text": "Bonjour"},
        ],
    }


_CHILD_ROWS_MODEL = """INTERLIS 2.4;
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


def test_child_row_features_scalar_bag_gets_a_value_property_and_parent_fk():
    """Matches convert/sql.py's parcel_tags child table exactly (see tests/test_convert_sql.py's _CHILD_TABLE_MODEL,
    same shape).
    """
    builder = _build(_CHILD_ROWS_MODEL)
    cls = _resolved_class(builder, "Parcel")
    obj = XtfObject(
        tid="p-1",
        qualified_class="Foo.T.Parcel",
        attributes={
            "ParcelNr": [_node("ParcelNr", "42")],
            "Tags": [_wrap("Tags", _node("TEXT", "AA"), _node("TEXT", "BB"))],
        },
    )
    rows = _child_row_features(obj, cls, symbol_table=builder.symbol_table)
    tags = [r for r in rows if r["featureType"] == "parcel_tags"]
    assert [r["properties"] for r in tags] == [
        {"parcel_fk": "p-1", "value": "AA"},
        {"parcel_fk": "p-1", "value": "BB"},
    ]
    assert tags[0]["id"] == "p-1_Tags_0"
    assert tags[0]["geometry"] is None
    assert "seq" not in tags[0]["properties"]  # BAG - no ordering


def test_child_row_features_list_of_structure_spreads_members_and_gets_seq():
    builder = _build(_CHILD_ROWS_MODEL)
    cls = _resolved_class(builder, "Parcel")
    en = _wrap("LocalisedText", _node("Language", "en"), _node("Text", "Hello"))
    fr = _wrap("LocalisedText", _node("Language", "fr"), _node("Text", "Bonjour"))
    obj = XtfObject(
        tid="p-2",
        qualified_class="Foo.T.Parcel",
        attributes={
            "ParcelNr": [_node("ParcelNr", "1")],
            "Names": [_wrap("Names", en, fr)],
        },
    )
    rows = _child_row_features(obj, cls, symbol_table=builder.symbol_table)
    names = [r for r in rows if r["featureType"] == "parcel_names"]
    assert [r["properties"] for r in names] == [
        {"parcel_fk": "p-2", "seq": 0, "Language": "en", "Text": "Hello"},
        {"parcel_fk": "p-2", "seq": 1, "Language": "fr", "Text": "Bonjour"},
    ]


def test_child_row_features_never_produced_by_default():
    """`include_child_rows` is opt-in - zero behavior change for every existing caller (RULE: additive, not a silent
    behavior shift).
    """
    builder = _build(_CHILD_ROWS_MODEL)
    obj = XtfObject(
        tid="p-3",
        qualified_class="Foo.T.Parcel",
        attributes={
            "ParcelNr": [_node("ParcelNr", "1")],
            "Tags": [_wrap("Tags", _node("TEXT", "AA"))],
        },
    )
    basket = XtfBasket(bid="b1", qualified_topic="Foo.T", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table)
    assert len(collection["features"]) == 1
    assert collection["features"][0]["featureType"] == "Parcel"


def test_transfer_to_feature_collection_include_child_rows_appends_them():
    builder = _build(_CHILD_ROWS_MODEL)
    obj = XtfObject(
        tid="p-4",
        qualified_class="Foo.T.Parcel",
        attributes={
            "ParcelNr": [_node("ParcelNr", "1")],
            "Tags": [_wrap("Tags", _node("TEXT", "AA"), _node("TEXT", "BB"))],
        },
    )
    basket = XtfBasket(bid="b1", qualified_topic="Foo.T", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table, include_child_rows=True)
    feature_types = [f["featureType"] for f in collection["features"]]
    assert feature_types == ["Parcel", "parcel_tags", "parcel_tags"]
    # Heterogeneous featureTypes - collection-level "featureType" correctly omitted
    # (matches the pre-existing rule, not a new one).
    assert "featureType" not in collection
    # No longer duplicated on the parent too - convert/sql.py's "parcel" table has no
    # "tags" column to receive it anyway.
    assert "Tags" not in collection["features"][0]["properties"]


def test_transfer_to_feature_collection_hoists_uniform_coord_ref_sys():
    """Matches the JSON-FG Standard's own official example verbatim.

    core/examples/airports.json (opengeospatial/ogc-feat-geo-json):
    "coordRefSys" declared once on the collection, entirely absent from
    each nested Feature - not just an optimization, what
    /req/core/same-crs actually requires.
    """
    builder = _build(_GEOM_MODEL, capture_meta=True)
    basket = XtfBasket(
        bid="b1",
        qualified_topic="Foo.T",
        kind=None,
        endstate=None,
        objects=[
            XtfObject(
                tid="p-1",
                qualified_class="Foo.T.APoint",
                attributes={"Geom": [_wrap("Geom", _coord("2600000.0", "1200000.0"))]},
            ),
            XtfObject(
                tid="p-2",
                qualified_class="Foo.T.APoint",
                attributes={"Geom": [_wrap("Geom", _coord("2650000.0", "1250000.0"))]},
            ),
        ],
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table)
    assert collection["coordRefSys"] == "http://www.opengis.net/def/crs/EPSG/0/2056"
    assert all("coordRefSys" not in f for f in collection["features"])
    assert all(f["place"]["type"] == "Point" for f in collection["features"])
