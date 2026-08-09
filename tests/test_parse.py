"""Tests pour src/interlis/runtime/parse.py (parse_file/parse_text) -
Lot 52 : repli ISO-8859-1 pour les fichiers .ili non-UTF-8 (RULE #1,
confirme reel sur des variantes obsolete du corpus large models.geo.admin.ch,
ex. obsolete/CHBase_Part1_GEOMETRY_V1_o0.ili)."""
from pathlib import Path

from interlis.runtime.parse import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_file_utf8_still_works():
    tree, errors = parse_file(FIXTURES / "minimal_model.ili")
    assert not errors
    assert tree is not None


def test_parse_file_falls_back_to_latin1():
    """tests/fixtures/latin1_model.ili est encode en ISO-8859-1 (contient
    'für', invalide en UTF-8 - confirme via decode() a la construction du
    fixture) - doit parser sans lever d'exception ni d'erreur de syntaxe."""
    tree, errors = parse_file(FIXTURES / "latin1_model.ili")
    assert not errors
    assert tree is not None
