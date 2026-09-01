"""Tests for OID clause wiring (classDef's own `OID AS .../NO OID`, topicDef's
`BASKET OID AS .../OID AS ...`) - see InterlisModelBuilder._scan_oid_clauses.

Fixture (tests/fixtures/oid_model.ili) covers, one TOPIC per case:
- NoClause: neither clause anywhere - no Class/DataUnit ever gets an Oid.
- BasketOnly: `BASKET OID AS MyOid;` only - DataUnit.Oid is set, but its
  Class (no clause of its own) gets no default (none was declared).
- ClassDefault: bare `OID AS MyOid;` (the class-default clause) plus 3
  classes exercising the 3 ways a class can interact with it: inherit it
  (Inherits), override it with its own domain (OwnDomain, `OID AS
  INTERLIS.UUIDOID;` -> AnyOIDType), opt out entirely (Opted, `NO OID;`).
- Both: `BASKET OID AS MyOid; OID AS INTERLIS.UUIDOID;` together (the
  reference manual's citation order, eCH-0031 V2.1.0 §3.5.2) - basket and
  class-default resolve independently.
"""

from pathlib import Path

import pytest
from conftest import build_from_file_with_model

FIXTURE = Path(__file__).parent / "fixtures/oid_model.ili"


@pytest.fixture(scope="module")
def model():
    _builder, model = build_from_file_with_model(FIXTURE)
    return model


def _topic(model, name):
    return next(
        sm for sm in model.Element if sm._qualified_class == "IlisMeta16.ModelData.SubModel" and sm.Name == name
    )


def _class(submodel, name):
    return next(c for c in submodel.Element if getattr(c, "Kind", None) == "Class" and c.Name == name)


def test_no_clause_gives_no_oid_anywhere(model):
    topic = _topic(model, "NoClause")
    plain = _class(topic, "Plain")
    assert getattr(plain, "Oid", None) is None
    assert getattr(topic._twin, "Oid", None) is None


def test_basket_only_sets_dataunit_oid_but_not_class_default(model):
    topic = _topic(model, "BasketOnly")
    du_oid = getattr(topic._twin, "Oid", None)
    assert du_oid is not None
    assert du_oid._qualified_class == "IlisMeta16.ModelData.TextType"
    assert du_oid.Name == "MyOid"
    # No bare `OID AS` clause was declared for this topic - its class must
    # NOT receive a default just because BASKET OID AS is present.
    no_own_oid = _class(topic, "NoOwnOid")
    assert getattr(no_own_oid, "Oid", None) is None


def test_class_default_is_inherited_by_a_class_with_no_own_clause(model):
    topic = _topic(model, "ClassDefault")
    inherits = _class(topic, "Inherits")
    oid = getattr(inherits, "Oid", None)
    assert oid is not None
    assert oid._qualified_class == "IlisMeta16.ModelData.TextType"
    assert oid.Name == "MyOid"


def test_class_default_does_not_override_a_class_with_its_own_domain(model):
    topic = _topic(model, "ClassDefault")
    own_domain = _class(topic, "OwnDomain")
    oid = getattr(own_domain, "Oid", None)
    assert oid is not None
    # `OID AS INTERLIS.UUIDOID;` -> AnyOIDType (reserved lexer token, never
    # a name-resolved domain), not the topic's MyOid default.
    assert oid._qualified_class == "IlisMeta16.ModelData.AnyOIDType"


def test_class_default_does_not_override_no_oid(model):
    topic = _topic(model, "ClassDefault")
    opted = _class(topic, "Opted")
    assert getattr(opted, "Oid", None) is None


def test_class_default_topic_has_no_basket_oid(model):
    topic = _topic(model, "ClassDefault")
    assert getattr(topic._twin, "Oid", None) is None


def test_basket_and_class_default_resolve_independently(model):
    topic = _topic(model, "Both")
    du_oid = getattr(topic._twin, "Oid", None)
    assert du_oid is not None and du_oid.Name == "MyOid"
    combined = _class(topic, "Combined")
    cls_oid = getattr(combined, "Oid", None)
    assert cls_oid is not None
    assert cls_oid._qualified_class == "IlisMeta16.ModelData.AnyOIDType"
