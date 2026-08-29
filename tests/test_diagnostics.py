"""Shared diagnostics core: Diagnostic / DiagnosticBag / text + SARIF renderers."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft4Validator

from interlis.diagnostics import (
    Diagnostic,
    DiagnosticBag,
    Location,
    render_sarif,
    render_text,
    severity_for_class,
)

ROOT = Path(__file__).resolve().parent.parent
SARIF_SCHEMA = json.loads((ROOT / "tests/fixtures/sarif-2.1.0-schema.json").read_text())


def _bag() -> DiagnosticBag:
    bag = DiagnosticBag()
    bag.add(
        Diagnostic(
            "warning",
            "SQL-REF-TARGET-UNRESOLVED",
            "Parcel.owner: reference target not resolved",
            Location(file="x.ili", model="Cadastre", element_path="Parcel.owner"),
            help="pass its model's directory to --repo, or the model file to --catalog",
        )
    )
    bag.add(
        Diagnostic(
            "note",
            "SQL-CONSTRAINT-SET",
            "SET CONSTRAINT 'areas': a whole-population check",
            Location(model="Cadastre", element_path="Parcel"),
        )
    )
    return bag


def test_unknown_severity_and_rule_are_rejected():
    with pytest.raises(ValueError):
        Diagnostic("fatal", "SQL-CONSTRAINT-SET", "x")
    with pytest.raises(ValueError):
        Diagnostic("note", "NOT-A-REAL-RULE", "x")


def test_class_and_default_severity():
    d = Diagnostic("warning", "SQL-REF-TARGET-UNRESOLVED", "x")
    assert d.klass == "C"
    assert severity_for_class("C") == "warning"
    assert severity_for_class("A") == "note"


def test_bag_counts_and_exit_codes():
    empty = DiagnosticBag()
    assert empty.exit_code() == 0 and empty.highest_severity() is None

    bag = _bag()
    assert bag.counts() == {"error": 1 - 1, "warning": 1, "note": 1}
    assert bag.highest_severity() == "warning"
    assert bag.exit_code() == 2
    assert bag.exit_code(strict=True) == 1

    bag.add(Diagnostic("error", "SQL-VIEW-NO-ATTRS", "boom"))
    assert bag.exit_code() == 1


def test_render_text_is_ruff_shaped():
    out = render_text(_bag())
    assert "warning SQL-REF-TARGET-UNRESOLVED  x.ili:Cadastre.Parcel.owner  " in out
    assert "    help: pass its model" in out
    assert out.strip().endswith("2 diagnostic(s): 0 errors, 1 warning, 1 note")


def test_render_sarif_validates_against_the_2_1_0_schema():
    log = render_sarif(_bag(), tool_version="9.9.9")
    Draft4Validator(SARIF_SCHEMA).validate(log)
    assert log["version"] == "2.1.0"
    run = log["runs"][0]
    assert run["tool"]["driver"]["version"] == "9.9.9"
    rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
    assert rule_ids == {"SQL-REF-TARGET-UNRESOLVED", "SQL-CONSTRAINT-SET"}
    first = run["results"][0]
    assert first["ruleId"] == "SQL-REF-TARGET-UNRESOLVED"
    assert first["level"] == "warning"
    assert first["properties"]["help"].startswith("pass its model")


def test_empty_bag_sarif_still_valid():
    Draft4Validator(SARIF_SCHEMA).validate(render_sarif(DiagnosticBag()))
