"""Cross-check the attributes the builder extracts from real MGDM models against the frozen geodienste WFS schemas.

The geodienste WFS `DescribeFeatureType` for a theme is the denormalised,
published shape of that MGDM - ground truth for what a derived VIEW would
have to expose. This freezes, per (theme, feature type): which base class
best matches the published fields, which published fields map 1:1 to an
INTERLIS attribute, and which do not (a JOIN across classes, a
code/label split of a catalogue reference, or a GIS-computed field).

A drift here means the builder's attribute extraction changed on a real
production model - review it, then regenerate:

    UPDATE_WFS_CROSSCHECK=1 uv run pytest tests/test_view_wfs_crosscheck.py
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import build_from_text

from interlis.xtf.schema import attributes_of

ROOT = Path(__file__).resolve().parent.parent
MGDM = Path(__file__).resolve().parent / "fixtures" / "mgdm"
WFS = Path(__file__).resolve().parent / "fixtures" / "wfs-schemas"
EXPECTED = MGDM / "wfs_crosscheck_expected.json"

# (theme <theme>.xsd, base model file). Same correspondences as
# docs/corpus-reproduction.md; the subset whose base model is vendored.
CASES = [
    ("fruchtfolgeflaechen", "Fruchtfolgeflaechen_V1.ili"),
    ("waldreservate", "Waldreservate_V2_0.ili"),
    ("wildruhezonen_v2_1_1", "Wildruhezonen_V2_1.ili"),
    ("npl_laermempfindlichkeitsstufen", "Laermempfindlichkeitsstufen_V1_2.ili"),
]

_NORM = re.compile(r"[^a-z0-9]")
# Fields every geodienste layer adds at publication or computes in GIS -
# never expected to map to a model attribute.
_PUBLICATION_FIELDS = {"wkbgeometry", "tid", "kanton"}


def _norm(name: str) -> str:
    return _NORM.sub("", name.lower())


def _wfs_feature_types(xsd_path: Path) -> dict[str, list[str]]:
    root = ET.parse(xsd_path).getroot()  # noqa: S314 - a frozen local fixture
    xsd = "{http://www.w3.org/2001/XMLSchema}"
    out: dict[str, list[str]] = {}
    for ct in root.iter(f"{xsd}complexType"):
        name = ct.get("name") or ""
        if name.endswith("Type"):
            out[name[: -len("Type")]] = [el.get("name") for el in ct.iter(f"{xsd}element") if el.get("name")]
    return out


def _classes(model_file: str) -> dict[str, object]:
    builder = build_from_text((MGDM / model_file).read_text(encoding="utf-8"))
    return {
        getattr(i, "Name", ""): i
        for i in builder.symbol_table.all_registered()
        if getattr(i, "_qualified_class", "").rsplit(".", 1)[-1] == "Class" and getattr(i, "Kind", None) == "Class"
    }


def _crosscheck(theme: str, model_file: str) -> dict:
    classes = _classes(model_file)
    result: dict[str, dict] = {}
    for ftype, fields in _wfs_feature_types(WFS / f"{theme}.xsd").items():
        published = [f for f in fields if _norm(f) not in _PUBLICATION_FIELDS]
        best_name, best_matched = "", []
        for cname, cls in classes.items():
            attr_norms = {_norm(k) for k in attributes_of(cls)}
            matched = [f for f in published if _norm(f) in attr_norms]
            if len(matched) > len(best_matched):
                best_name, best_matched = cname, matched
        matched_norms = {_norm(f) for f in best_matched}
        result[ftype] = {
            "best_class": best_name,
            "matched": sorted(best_matched),
            "missing_in_model": sorted(f for f in published if _norm(f) not in matched_norms),
        }
    return result


def test_wfs_crosscheck_partition_is_frozen() -> None:
    produced = {theme: _crosscheck(theme, model_file) for theme, model_file in CASES}

    if os.environ.get("UPDATE_WFS_CROSSCHECK"):
        EXPECTED.write_text(json.dumps(produced, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        pytest.skip("UPDATE_WFS_CROSSCHECK: wrote wfs_crosscheck_expected.json")

    assert EXPECTED.exists(), "missing wfs_crosscheck_expected.json - regenerate with UPDATE_WFS_CROSSCHECK=1"
    assert produced == json.loads(EXPECTED.read_text(encoding="utf-8")), (
        "the builder's attribute extraction drifted on a real MGDM model. Review, then regenerate "
        "with UPDATE_WFS_CROSSCHECK=1 and commit the diff."
    )
