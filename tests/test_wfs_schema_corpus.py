"""The frozen geodienste.ch WFS `DescribeFeatureType` corpus (`tests/fixtures/wfs-schemas/`).

A published cantonal geoservice's schema is the ground-truth oracle for
which attributes a derived-VIEW `.ili` should expose.
Unlike the transfer data, a `DescribeFeatureType` response is a small,
stable XSD - so it lives in the repo as a frozen fixture. Captured (and
refreshed) by the overlay's `scripts/fetch_wfs_schemas.py`; reproduction
without that script is `docs/corpus-reproduction.md`.

These tests only guard the fixture's integrity and give one reusable
parser; the actual VIEW-vs-service cross-check is
`scripts/crosscheck_view_attributes.py`.
"""

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures/wfs-schemas"
MANIFEST = FIXTURE_DIR / "_manifest.json"
_XSD = "{http://www.w3.org/2001/XMLSchema}"


def feature_types(xsd_path: Path) -> dict[str, dict[str, str]]:
    """Parse a MapServer `DescribeFeatureType` XSD into `{featureType: {attribute: xsd_type}}`."""
    root = ET.parse(xsd_path).getroot()  # noqa: S314 - a vendored, frozen fixture file
    complex_types: dict[str, dict[str, str]] = {}
    for ct in root.findall(f"{_XSD}complexType"):
        name = (ct.get("name") or "").removesuffix("Type")
        attrs = {el.get("name"): el.get("type", "") for el in ct.iter(f"{_XSD}element") if el.get("name")}
        if name:
            complex_types[name] = attrs
    return complex_types


def _manifest() -> dict[str, dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_and_files_agree() -> None:
    manifest = _manifest()
    on_disk = {p.stem for p in FIXTURE_DIR.glob("*.xsd")}
    kept = {theme for theme, row in manifest.items() if row.get("ok")}
    assert on_disk == kept, f"manifest 'ok' set {kept} != .xsd files {on_disk}"
    # A theme that has no WFS (raster-only, e.g. `luftbild`) is recorded,
    # not silently absent - so the corpus stays self-describing.
    assert any(not row.get("ok") for row in manifest.values()) or not manifest, "expected the skipped-theme record"


@pytest.mark.parametrize("theme", sorted(t for t, r in _manifest().items() if r.get("ok")))
def test_each_schema_parses_matches_its_hash_and_has_feature_types(theme: str) -> None:
    row = _manifest()[theme]
    body = (FIXTURE_DIR / f"{theme}.xsd").read_bytes()
    assert hashlib.sha256(body).hexdigest() == row["sha256"], "fixture changed without a manifest refresh"
    assert len(body) == row["byte_size"]
    types = feature_types(FIXTURE_DIR / f"{theme}.xsd")
    assert types, f"{theme}: no feature type in the DescribeFeatureType schema"
    assert all(attrs for attrs in types.values()), f"{theme}: a feature type with no attributes"


def test_feature_types_parser_on_a_known_theme() -> None:
    types = feature_types(FIXTURE_DIR / "npl_nutzungsplanung.xsd")
    assert "grundnutzung" in types
    assert "rechtsstatus" in types["grundnutzung"]
    assert types["grundnutzung"]["wkb_geometry"] == "gml:GeometryPropertyType"
