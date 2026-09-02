"""VIEW ... INSPECTION OF ... (grammar rule inspection): AREA is OPTIONAL.

Real corpus bug (LWB_Nutzungsflaechen_V3_0.ili, models.geo.admin.ch):
`VIEW InspectionOfProgramm INSPECTION OF LNF_Nutzung_Programm ~ ... ;` (no
AREA) raised `BuildError: [inspection] 'AREA' missing (not optional) on
InspectionContext` - the spec/grammar/mapping/09_views_graphics.yml
binding for inspection's `_kind` read AREA via a plain field+index
accessor with no `optional`/`presence` flag, even though the grammar
itself (InterlisParser.py's inspection()) only matches AREA inside an
"if" lookahead guard - not unconditionally. Fixed by switching to the
same `presence: true` dispatch pattern already used for
viewDef.Abstract/Final/Transient.
"""

from conftest import build_from_text as _build


def _model(area: str) -> str:
    return f"""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Attr : TEXT*10;
    END A;
    VIEW MyView
      {area}INSPECTION OF A -> Attr;
    = END MyView;
  END T;
END Foo.
"""


def _inspection_view(builder):
    return next(
        inst
        for inst in builder.symbol_table.all_registered()
        if getattr(inst, "_qualified_class", None) == "IlisMeta16.ModelData.View"
    )


def test_inspection_without_area_does_not_raise():
    # AREA vs. its absence is not preserved as a distinct field anywhere in the
    # built object graph (the `inspection` Container's `_kind` binding has no
    # downstream consumer - grep confirms no converter reads it) - the only
    # content assertion available is that the View still comes out well-formed,
    # not just that construction didn't crash.
    view = _inspection_view(_build(_model("")))
    assert view.FormationKind == "Inspection"
    assert view._inspection_path == ["Attr"]


def test_inspection_with_area_does_not_raise():
    view = _inspection_view(_build(_model("AREA ")))
    assert view.FormationKind == "Inspection"
    assert view._inspection_path == ["Attr"]
