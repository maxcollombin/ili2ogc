"""PROACTIVE header-vs-resolved completeness check for `interlis validate`
(previously lazy: only models ACTUALLY referenced by an attribute/role
actually encountered were loaded - a header model never exercised by the
data stayed invisible, available or not). Reuses the fixtures
`tests/fixtures/multi_file/` (Base available, BrokenSyntax indexed but
failed, NoSuchModel absent), same corpus as `test_model_builder_multi_file.py`."""

from pathlib import Path

from conftest import build_from_file

from interlis.builder.repository import ModelRepository
from interlis.xtf.model_resolution import header_completeness
from interlis.xtf.parse import XtfModelRef, XtfTransfer

FIXTURES_DIR = Path(__file__).parent / "fixtures/multi_file"


def _transfer(names: list[str]) -> XtfTransfer:
    return XtfTransfer(
        sender=None,
        ili_version=None,
        models=[XtfModelRef(name=n, version="2026-08-07", uri="https://example.org") for n in names],
        baskets=[],
    )


def test_header_completeness_without_repository_is_no_repo():
    statuses = header_completeness(_transfer(["Base"]), repository=None)
    assert [s.status for s in statuses] == ["no_repo"]


def test_header_completeness_covers_all_four_states():
    repository = ModelRepository([FIXTURES_DIR])
    # bind_builder_factory necessite un InterlisModelBuilder deja construit
    # avec ce repository (meme mecanisme que test_model_builder_multi_file.py) -
    # gaps de spec connus, hors de portee de ce test (warnings suppressed by build_from_file)
    build_from_file(FIXTURES_DIR / "importer.ili", repository=repository)

    statuses = header_completeness(
        _transfer(["INTERLIS", "Base", "NoSuchModel", "BrokenSyntax"]),
        repository=repository,
    )
    by_name = {s.name: s.status for s in statuses}
    assert by_name == {
        "INTERLIS": "builtin",
        "Base": "available",
        "NoSuchModel": "missing",
        "BrokenSyntax": "indexed_but_failed",
    }
