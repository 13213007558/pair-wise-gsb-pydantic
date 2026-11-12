"""Differential tests: gateway JSON Schema admission vs. pydantic model entry points.

The gateway admits raw JSON input with the Draft 2020-12 schema exported by pydantic
(``model_json_schema()``, validation mode); only afterwards does the application parse
the same payload through the pydantic model. These tests check whether the exported
schema silently drops an input restriction that the model enforces, which would let a
payload past the gateway that the application later rejects (or, worse, treats
differently).

Agreed input domain (the only range compared here):
    * payloads are plain JSON values (dict/list/str/int/float/bool/None), never Python
      objects such as ``datetime`` or ``Decimal``;
    * the model side is driven in ``strict=True`` mode so lax Python coercion does not
      count as a schema weakness;
    * validation-mode schemas only. Serialization-mode schemas and ``by_alias`` output
      shapes are a different contract and are out of range.

Evidence boundary (see the ``evidence_scope`` tests below):
    * constraints that JSON Schema cannot express (custom validators, lax Python
      coercion) are recorded as such instead of being claimed as full equivalence;
    * a divergence where the gateway is *stricter* than the model (``populate_by_name``
      accepting field names that the schema does not list) is also recorded, because it
      is not a relaxation of the gateway.

Neither the models nor their generated schemas are mutated by the comparison tests.
The mutation-detection tests operate on ``copy.deepcopy`` copies only.
"""

import copy
from dataclasses import dataclass
from typing import Literal, Optional, Union

import pytest
from jsonschema import Draft202012Validator

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Models under test
# ---------------------------------------------------------------------------


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    zip_code: str


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    address: Address


class Envelope(BaseModel):
    """Same ``$defs/Address`` reused once nested and once inside an array."""

    model_config = ConfigDict(extra="forbid")

    user: User
    previous_addresses: list[Address]


class Node(BaseModel):
    """Recursive ``$ref`` so a definition is reused and nested again."""

    model_config = ConfigDict(extra="forbid")

    label: str
    children: list["Node"] = []


Node.model_rebuild()


class Cat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pet_type: Literal["cat"]
    meow_volume: int


class Dog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pet_type: Literal["dog"]
    bark_volume: int


class PetOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    pet: Union[Cat, Dog] = Field(discriminator="pet_type")


class Profile(BaseModel):
    """Required nullable field vs. optional nullable field with a default."""

    model_config = ConfigDict(extra="forbid")

    nickname: Optional[str]
    age: Optional[int] = None


class Aliased(BaseModel):
    """Input aliases: schema properties must carry the alias names."""

    model_config = ConfigDict(extra="forbid")

    user_name: str = Field(alias="userName")
    email_address: str = Field(validation_alias="emailAddress")


# ---------------------------------------------------------------------------
# Comparison machinery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayCase:
    label: str
    payload: object
    accepted: bool


def _model_accepts(model: type[BaseModel], payload: object) -> bool:
    try:
        model.model_validate(payload, strict=True)
    except Exception:
        return False
    return True


def _schema_accepts(schema: dict, payload: object) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(payload))


def _assert_gateway_matches_model(model: type[BaseModel], case: GatewayCase) -> None:
    schema = model.model_json_schema()
    model_says = _model_accepts(model, case.payload)
    gateway_says = _schema_accepts(schema, case.payload)

    assert model_says is case.accepted, f"{case.label}: hand-written expectation is wrong for model"
    assert (
        gateway_says is case.accepted
    ), f"{case.label}: gateway {gateway_says!r} disagrees with model {model_says!r} for {case.payload!r}"


# ---------------------------------------------------------------------------
# 1. additionalProperties=false, including reuse then nesting
# ---------------------------------------------------------------------------

ENVELOPE_CASES = [
    GatewayCase(
        "valid full envelope",
        {
            "user": {"name": "a", "address": {"city": "c", "zip_code": "z"}},
            "previous_addresses": [{"city": "c2", "zip_code": "z2"}],
        },
        True,
    ),
    # earliest relaxation point: the root object itself
    GatewayCase(
        "extra field at root",
        {
            "user": {"name": "a", "address": {"city": "c", "zip_code": "z"}},
            "previous_addresses": [],
            "hacker": 1,
        },
        False,
    ),
    # one level down, through the User $ref
    GatewayCase(
        "extra field on nested user",
        {"user": {"name": "a", "address": {"city": "c", "zip_code": "z"}, "role": "x"}, "previous_addresses": []},
        False,
    ),
    # deepest nested object
    GatewayCase(
        "extra field on nested address",
        {"user": {"name": "a", "address": {"city": "c", "zip_code": "z", "extra": 1}}, "previous_addresses": []},
        False,
    ),
    # the same Address definition reused inside an array must keep the restriction
    GatewayCase(
        "extra field on reused address definition inside array",
        {
            "user": {"name": "a", "address": {"city": "c", "zip_code": "z"}},
            "previous_addresses": [{"city": "c", "zip_code": "z", "bogus": True}],
        },
        False,
    ),
    GatewayCase(
        "missing required field survives through refs",
        {"user": {"name": "a", "address": {"city": "c"}}, "previous_addresses": []},
        False,
    ),
    GatewayCase(
        "wrong scalar type at nested path",
        {"user": {"name": 123, "address": {"city": "c", "zip_code": "z"}}, "previous_addresses": []},
        False,
    ),
]


@pytest.mark.parametrize("case", ENVELOPE_CASES, ids=lambda case: case.label)
def test_extra_fields_forbidden_through_nested_refs(case: GatewayCase) -> None:
    _assert_gateway_matches_model(Envelope, case)


# ---------------------------------------------------------------------------
# 2. Nested / recursive refs
# ---------------------------------------------------------------------------

NODE_CASES = [
    GatewayCase(
        "valid recursive tree",
        {
            "label": "root",
            "children": [
                {"label": "child", "children": [{"label": "grandchild"}]},
            ],
        },
        True,
    ),
    GatewayCase("extra field at recursive root", {"label": "root", "bogus": 1}, False),
    GatewayCase(
        "extra field at deepest recursive level",
        {
            "label": "root",
            "children": [
                {
                    "label": "child",
                    "children": [
                        {"label": "grandchild", "bogus": 1},
                    ],
                }
            ],
        },
        False,
    ),
    GatewayCase("missing required label at nested level", {"label": "root", "children": [{"children": []}]}, False),
    GatewayCase("wrong type at nested level", {"label": "root", "children": [{"label": 7}]}, False),
]


@pytest.mark.parametrize("case", NODE_CASES, ids=lambda case: case.label)
def test_recursive_ref_keeps_constraints_at_every_depth(case: GatewayCase) -> None:
    _assert_gateway_matches_model(Node, case)


# ---------------------------------------------------------------------------
# 3. Literal discriminated unions
# ---------------------------------------------------------------------------

PET_OWNER_CASES = [
    GatewayCase("cat branch", {"name": "a", "pet": {"pet_type": "cat", "meow_volume": 3}}, True),
    GatewayCase("dog branch", {"name": "a", "pet": {"pet_type": "dog", "bark_volume": 4}}, True),
    # earliest relaxation: an unknown literal must match neither branch
    GatewayCase(
        "unknown discriminator literal",
        {"name": "a", "pet": {"pet_type": "fish", "meow_volume": 3}},
        False,
    ),
    GatewayCase(
        "missing discriminator property",
        {"name": "a", "pet": {"meow_volume": 3}},
        False,
    ),
    # branch-specific shape: cat payload with dog-only field
    GatewayCase(
        "foreign field for selected branch",
        {"name": "a", "pet": {"pet_type": "cat", "bark_volume": 4}},
        False,
    ),
    GatewayCase(
        "wrong type inside selected branch",
        {"name": "a", "pet": {"pet_type": "cat", "meow_volume": "loud"}},
        False,
    ),
    GatewayCase(
        "extra field on union owner",
        {"name": "a", "pet": {"pet_type": "cat", "meow_volume": 3}, "x": 1},
        False,
    ),
]


@pytest.mark.parametrize("case", PET_OWNER_CASES, ids=lambda case: case.label)
def test_literal_discriminated_union_gate(case: GatewayCase) -> None:
    _assert_gateway_matches_model(PetOwner, case)


# ---------------------------------------------------------------------------
# 4. Required and nullable
# ---------------------------------------------------------------------------

PROFILE_CASES = [
    GatewayCase("explicit nulls", {"nickname": None, "age": None}, True),
    GatewayCase("real values", {"nickname": "n", "age": 5}, True),
    # optional field with default may be omitted; the required nullable one may not
    GatewayCase("optional field omitted", {"nickname": "n"}, True),
    GatewayCase("required nullable field omitted", {"age": 5}, False),
    GatewayCase("null is not a string for the wrong JSON type", {"nickname": 7}, False),
    GatewayCase("null accepted but extra field rejected", {"nickname": None, "ghost": 1}, False),
]


@pytest.mark.parametrize("case", PROFILE_CASES, ids=lambda case: case.label)
def test_required_and_nullable_gate(case: GatewayCase) -> None:
    _assert_gateway_matches_model(Profile, case)


# ---------------------------------------------------------------------------
# 5. Input aliases
# ---------------------------------------------------------------------------

ALIAS_CASES = [
    GatewayCase("canonical aliased input", {"userName": "a", "emailAddress": "e@x.io"}, True),
    GatewayCase("extra field next to aliases", {"userName": "a", "emailAddress": "e@x.io", "nope": 1}, False),
    GatewayCase("missing aliased required field", {"userName": "a"}, False),
    GatewayCase("wrong JSON type under alias", {"userName": 1, "emailAddress": "e@x.io"}, False),
]


@pytest.mark.parametrize("case", ALIAS_CASES, ids=lambda case: case.label)
def test_input_aliases_gate(case: GatewayCase) -> None:
    _assert_gateway_matches_model(Aliased, case)


# ---------------------------------------------------------------------------
# Evidence scope: divergences that must NOT be misreported as gateway holes
# ---------------------------------------------------------------------------


class PopulateByNameModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    user_name: str = Field(alias="userName")


def test_evidence_scope_populate_by_name_gateway_is_stricter_not_looser() -> None:
    """Field-name input is a pydantic-only convenience; JSON Schema cannot express
    "accept either name". The gateway rejecting it is a stricter (safe) direction,
    not a dropped restriction, so it is recorded as an evidence boundary."""
    schema = PopulateByNameModel.model_json_schema()
    field_name_payload = {"user_name": "a"}
    alias_payload = {"userName": "a"}

    assert _model_accepts(PopulateByNameModel, field_name_payload) is True
    assert _schema_accepts(schema, field_name_payload) is False
    assert _model_accepts(PopulateByNameModel, alias_payload) is True
    assert _schema_accepts(schema, alias_payload) is True


class CoercedModel(BaseModel):
    count: int


def test_evidence_scope_lax_python_coercion_is_out_of_json_range() -> None:
    """In lax mode pydantic coerces the JSON string "3" into int 3. A Draft 2020-12
    "type": "integer" keyword cannot express coercion, so within the agreed strict
    JSON input domain both reject it; the lax acceptance is documented, not treated
    as a schema defect."""
    schema = CoercedModel.model_json_schema()
    payload = {"count": "3"}

    assert _model_accepts(CoercedModel, payload) is False
    assert _schema_accepts(schema, payload) is False

    assert CoercedModel.model_validate(payload).count == 3  # lax Python coercion exists
    assert schema["properties"]["count"] == {"title": "Count", "type": "integer"}


class CustomRuleModel(BaseModel):
    tag: str

    @field_validator("tag")
    @classmethod
    def _no_spaces(cls, value: str) -> str:
        if " " in value:
            raise ValueError("spaces not allowed")
        return value


def test_evidence_scope_custom_validator_constraint_is_not_in_schema() -> None:
    """A custom validator rule ("no spaces") has no JSON Schema representation.
    It is recorded as an expressibility limit: equivalent admission on this rule is
    not claimed, and application-side validation remains the enforcement point."""
    schema = CustomRuleModel.model_json_schema()
    payload = {"tag": "has space"}

    assert schema == {
        "properties": {"tag": {"title": "Tag", "type": "string"}},
        "required": ["tag"],
        "title": "CustomRuleModel",
        "type": "object",
    }
    assert _schema_accepts(schema, payload) is True
    assert _model_accepts(CustomRuleModel, payload) is False


def test_serialization_mode_schema_is_out_of_comparison_range() -> None:
    """Serialization-mode schemas describe model output, not gateway admission;
    pin that the comparison only ever uses the validation-mode schema."""
    validation_schema = Aliased.model_json_schema(mode="validation")
    serialization_schema = Aliased.model_json_schema(mode="serialization")

    validation_props = validation_schema["properties"]
    serialization_props = serialization_schema["properties"]

    # validation mode only ever lists the accepted input names
    assert set(validation_props) == {"userName", "emailAddress"}
    # plain alias= is bidirectional, so it survives in serialization mode;
    # validation_alias= only governs input and output reverts to the field name
    assert set(serialization_props) == {"userName", "email_address"}
    assert "emailAddress" not in serialization_props
    assert "email_address" in serialization_props


# ---------------------------------------------------------------------------
# Mutation detection: prove the positive tests would catch a dropped restriction.
# Only deep copies of generated schemas are mutated; models/schemas stay intact.
# ---------------------------------------------------------------------------


def test_mutated_schema_dropping_additional_properties_is_detected() -> None:
    original = Envelope.model_json_schema()
    mutated = copy.deepcopy(original)
    del mutated["$defs"]["Address"]["additionalProperties"]

    payload = {
        "user": {"name": "a", "address": {"city": "c", "zip_code": "z", "bogus": 1}},
        "previous_addresses": [],
    }

    assert _schema_accepts(original, payload) is False
    assert _schema_accepts(mutated, payload) is True  # the mutation opens the hole
    assert _model_accepts(Envelope, payload) is False  # model still enforces it
    assert original == Envelope.model_json_schema()  # source schema untouched


def test_mutated_schema_dropping_literal_constraint_is_detected() -> None:
    original = PetOwner.model_json_schema()
    mutated = copy.deepcopy(original)
    cat_branch = mutated["$defs"]["Cat"]["properties"]["pet_type"]
    cat_branch.pop("const")
    cat_branch["type"] = "string"

    payload = {"name": "a", "pet": {"pet_type": "fish", "meow_volume": 3}}

    assert _schema_accepts(original, payload) is False
    assert _schema_accepts(mutated, payload) is True  # without const the branch matches
    assert _model_accepts(PetOwner, payload) is False
    assert original == PetOwner.model_json_schema()


# ---------------------------------------------------------------------------
# Non-mutation guarantee: exercising the entry points changes neither artifact.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [Envelope, Node, PetOwner, Profile, Aliased],
    ids=lambda model: model.__name__,
)
def test_comparison_never_mutates_model_or_schema(model: type[BaseModel]) -> None:
    schema_before = copy.deepcopy(model.model_json_schema())
    field_names_before = tuple(model.model_fields)
    field_infos_before = tuple(model.model_fields.values())

    list(Draft202012Validator(schema_before).iter_errors({}))
    try:
        model.model_validate({}, strict=True)
    except Exception:
        pass

    assert model.model_json_schema() == schema_before
    assert tuple(model.model_fields) == field_names_before
    assert tuple(model.model_fields.values()) == field_infos_before
