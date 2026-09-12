"""`convert/cartosym.py` - each `StandardSymbology` interface's Symbolizer, verified through pycartosym's SLD writer.

Values below are taken from real corpus data (`Point_Graphics_
Signatures.xtf`'s `Color`/`SymbolSign` - `github.com/MediaComem/FGDM4GS` -
and `RoadsExgm2ien.ili`'s `PolylineSign`/`SurfaceSign` PARAMETER
assignments), not invented numbers.
"""

import pycartosym
from pycartosym.models.styles import Style, StylingRule, Symbolizer

from interlis.convert.cartosym import (
    color_to_rgb,
    polyline_sign_to_stroke,
    surface_sign_to_fill,
    text_sign_to_label,
)

_SLD = pycartosym.get_codec("sld")


def _write(symbolizer: Symbolizer) -> str:
    out = _SLD.write(Style(styling_rules=[StylingRule(name="r", symbolizer=symbolizer)]))
    return out if isinstance(out, str) else out.decode()


def test_black_color_from_point_graphics_signatures_xtf():
    """`Point_Graphics_Signatures.xtf`'s `Color ili:tid="2"` (Name=black, L=0/C=0/H=0/T=1.0)."""
    rgb = color_to_rgb(0.0, 0.0, 0.0)
    assert (rgb.r, rgb.g, rgb.b) == (0, 0, 0)


def test_white_color_from_point_graphics_signatures_xtf():
    """Same fixture's `Color ili:tid="1"` (Name=white, L=100/C=0/H=0)."""
    rgb = color_to_rgb(100.0, 0.0, 0.0)
    assert (rgb.r, rgb.g, rgb.b) == (255, 255, 255)


def test_polyline_sign_solid_stroke_writes_valid_sld():
    """`RoadsExgm2ien.ili`'s `Street_precise`/`_unprecise` (`PolylineSign`, `Sign := {continuous}`/`{dotted}`)."""
    black = color_to_rgb(0.0, 0.0, 0.0)
    stroke = polyline_sign_to_stroke(color=black, opacity=1.0, width=1.0, join="miter", cap="round")
    xml = _write(Symbolizer(stroke=stroke))
    assert "<se:LineSymbolizer>" in xml
    assert '"stroke">#000000' in xml
    assert '"stroke-linejoin">miter' in xml
    assert '"stroke-linecap">round' in xml


def test_polyline_sign_dashed_stroke_carries_dash_pattern():
    stroke = polyline_sign_to_stroke(color=color_to_rgb(0.0, 0.0, 0.0), width=1.0, dash_pattern=[4.0, 2.0])
    xml = _write(Symbolizer(stroke=stroke))
    assert '"stroke-dasharray">4 2<' in xml


def test_surface_sign_fill_from_roadsexgm2ien_building():
    """`RoadsExgm2ien.ili`'s `Building OF ... SurfaceSign: WHERE Type == #building (Sign := {Building}; ...)`."""
    fill = surface_sign_to_fill(fill_color=color_to_rgb(50.0, 20.0, 90.0), opacity=1.0)
    xml = _write(Symbolizer(fill=fill))
    assert "<se:PolygonSymbolizer>" in xml
    assert '"fill-opacity">1' in xml


def test_text_sign_label_from_roadsexgm2ien_streetname():
    """`RoadsExgm2ien.ili`'s `StreetName OF ... TextSign: (Txt := Street -> Name; Rotation := NamOri; ...)`."""
    label = text_sign_to_label(
        text="Main Street", font_face="Arial", height=3.0, h_alignment="Center", v_alignment="Half"
    )
    xml = _write(Symbolizer(label=label))
    assert "<se:TextSymbolizer>" in xml
    assert "<se:Label>Main Street</se:Label>" in xml
    assert "<se:AnchorPointX>0.5</se:AnchorPointX>" in xml
    assert "<se:AnchorPointY>0.5</se:AnchorPointY>" in xml


def test_text_sign_cap_and_base_valignment_approximate_to_top_and_bottom():
    """`VALIGNMENT`'s 5 levels (`Top`/`Cap`/`Half`/`Base`/`Bottom`) collapse onto pycartosym's 3 (top/middle/bottom)."""
    cap_xml = _write(Symbolizer(label=text_sign_to_label(text="x", v_alignment="Cap")))
    base_xml = _write(Symbolizer(label=text_sign_to_label(text="x", v_alignment="Base")))
    assert "<se:AnchorPointY>1</se:AnchorPointY>" in cap_xml
    assert "<se:AnchorPointY>0</se:AnchorPointY>" in base_xml
