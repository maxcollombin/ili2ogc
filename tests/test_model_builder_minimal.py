"""End-to-end smoke test of ModelBuilder against a real minimal model, not a
simulation - exercises the generic engine (kind dispatch, discriminant,
`parent:` composition tree, association attachment, symbol table).

Known limitation (a per-rule spec gap, not an engine defect - see
.claude/PROGRESS.md): roleDef's target class reference
(restrictedClassOrAssRef) is not yet wired in spec/grammar/mapping/
(documented "not yet mapped" in its own note), so ForwardRef resolution is
not exercised end-to-end here."""

from pathlib import Path

import pytest
from conftest import build_from_file_with_model

FIXTURE = Path(__file__).parent / "fixtures/minimal_model.ili"


@pytest.fixture(scope="module")
def builder_and_model():
    # known spec gaps, see docstring (warnings suppressed by build_from_file_with_model)
    return build_from_file_with_model(FIXTURE)


@pytest.fixture(scope="module")
def model(builder_and_model):
    return builder_and_model[1]


def test_model_identity(model):
    assert model._qualified_class == "IlisMeta16.ModelData.Model"
    assert model.Name == "MinimalTest"
    assert model.iliVersion == "2.4"


def test_topic_dual_instance(model):
    assert len(model.Element) == 2
    submodel, dataunit = model.Element
    assert submodel._qualified_class == "IlisMeta16.ModelData.SubModel"
    assert submodel.Name == "MainTopic"
    assert dataunit._qualified_class == "IlisMeta16.ModelData.DataUnit"
    assert dataunit.Name == "BASKET"
    assert submodel._twin is dataunit
    assert dataunit._twin is submodel


def test_classes_with_discriminant(model):
    submodel = model.Element[0]
    person, company, worksfor = submodel.Element
    assert (person.Name, person.Kind) == ("Person", "Class")
    assert (company.Name, company.Kind) == ("Company", "Class")
    assert (worksfor.Name, worksfor.Kind) == ("WorksFor", "Association")


def test_attribute_with_text_type(model):
    person = model.Element[0].Element[0]
    attrs = {a.Name: a for a in person.ClassAttribute}
    assert set(attrs) == {"Name", "BirthYear", "Kind"}
    name_attr = attrs["Name"]
    assert name_attr._qualified_class == "IlisMeta16.ModelData.AttrOrParam"
    assert name_attr.Type is not None
    assert name_attr.Type._qualified_class == "IlisMeta16.ModelData.TextType"
    assert name_attr.Type.Kind == "Text"


def test_association_roles_and_strongness(model):
    worksfor = model.Element[0].Element[2]
    roles = {r.Name: r for r in worksfor.Role}
    assert set(roles) == {"Employee", "Employer"}
    assert roles["Employee"].Strongness == "Assoc"
    assert roles["Employer"].Strongness == "Assoc"


def test_symbol_table_registration(builder_and_model):
    builder, model = builder_and_model
    person = model.Element[0].Element[0]
    resolved = builder.symbol_table.resolve("Person")
    assert resolved is person
