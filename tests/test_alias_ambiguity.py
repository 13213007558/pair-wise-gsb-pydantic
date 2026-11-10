"""Tests for the opt-in `validate_alias_ambiguity` configuration."""

from typing import Annotated, Any
from typing_extensions import TypedDict

import pytest

import pydantic.dataclasses as pdc
from pydantic import (
    AliasChoices,
    AliasGenerator,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.config import with_config

pytestmark = pytest.mark.thread_unsafe


def _conflict(exc: ValidationError) -> dict:
    errors = exc.errors()
    assert len(errors) == 1
    error = errors[0]
    assert error['type'] == 'alias_ambiguity'
    return error


def test_default_is_lenient() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        account_id: int = Field(validation_alias='accountId')

    assert Model(account_id=1, accountId=2).account_id == 2
    assert Model.model_validate({'account_id': 1, 'accountId': 2}).account_id == 2


def test_conflict_even_when_values_are_equal() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias='accountId')

    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate({'account_id': 7, 'accountId': 7})
    error = _conflict(exc_info.value)
    assert error['loc'] == ()
    assert error['input'] is None
    assert 'account_id' in error['msg']
    assert 'accountId' in error['msg']


def test_single_entry_still_accepted() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias='accountId')

    assert Model(account_id=1).account_id == 1
    assert Model(accountId=2).account_id == 2


def test_input_is_not_mutated() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias='accountId')

    data = {'account_id': 1, 'accountId': 2}
    with pytest.raises(ValidationError):
        Model.model_validate(data)
    assert data == {'account_id': 1, 'accountId': 2}


def test_error_does_not_echo_credentials() -> None:
    class AuthRequest(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias='accountId')
        password: str = 'unused'

    payload = {'account_id': 1, 'accountId': 2, 'password': 'super-secret'}
    with pytest.raises(ValidationError) as exc_info:
        AuthRequest.model_validate(payload)
    rendered = str(exc_info.value)
    assert 'super-secret' not in rendered
    assert exc_info.value.errors()[0]['input'] is None


def test_alias_choices_conflict() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(validate_alias_ambiguity=True)
        x: int = Field(validation_alias=AliasChoices('a', 'b'))

    assert Model.model_validate({'a': 1}).x == 1
    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate({'a': 1, 'b': 2})
    error = _conflict(exc_info.value)
    assert error['ctx'] == {'field': 'x', 'paths': "'a' and 'b'"}


def test_alias_path_missing_is_not_a_candidate() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        x: int | None = Field(validation_alias=AliasChoices('top', AliasPath('nested', 'id')))

    # The nested path does not exist, so only `top` is present: no conflict.
    assert Model.model_validate({'top': 1, 'nested': {}}).x == 1
    assert Model.model_validate({'top': 1}).x == 1


def test_alias_path_present_but_null_conflicts() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        x: int | None = Field(validation_alias=AliasChoices('top', AliasPath('nested', 'id')))

    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate({'top': 1, 'nested': {'id': None}})
    error = _conflict(exc_info.value)
    assert error['ctx']['paths'] == "'top' and ['nested']['id']"
    # The None value itself is not rendered.
    assert 'None' not in error['msg']


def test_alias_path_through_list() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(validate_alias_ambiguity=True)
        first_id: int = Field(validation_alias=AliasChoices('first', AliasPath('items', 0, 'id')))

    assert Model.model_validate({'items': [{'id': 9}]}).first_id == 9
    # A missing list index means the path entry is absent.
    assert Model.model_validate({'first': 1, 'items': []}).first_id == 1
    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate({'first': 1, 'items': [{'id': 9}]})
    error = _conflict(exc_info.value)
    assert error['ctx']['paths'] == "'first' and ['items'][0]['id']"


def test_closed_field_name_is_not_a_candidate() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(validate_alias_ambiguity=True)
        account_id: int = Field(alias='accountId')

    # validate_by_name is off, so `account_id` is an ignored extra, never a candidate.
    assert Model.model_validate({'account_id': 1, 'accountId': 2}).account_id == 2


def test_closed_alias_is_not_a_candidate() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(
            validate_by_alias=False,
            validate_by_name=True,
            validate_alias_ambiguity=True,
        )
        account_id: int = Field(alias='accountId')

    # Aliases are disabled, so `accountId` is not a candidate.
    assert Model.model_validate({'account_id': 1, 'accountId': 2}).account_id == 1


def test_alias_generator_conflict() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(
            populate_by_name=True,
            validate_alias_ambiguity=True,
            alias_generator=AliasGenerator(
                validation_alias=lambda name: AliasChoices(name, name.upper())
            ),
        )
        account_id: int

    assert Model.model_validate({'account_id': 1}).account_id == 1
    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate({'account_id': 1, 'ACCOUNT_ID': 2})
    _conflict(exc_info.value)


def test_runs_before_business_validators() -> None:
    calls: list[tuple[str, Any]] = []

    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias='accountId')

        @field_validator('account_id')
        @classmethod
        def field_check(cls, value: int) -> int:
            calls.append(('field', value))
            return value

        @model_validator(mode='before')
        @classmethod
        def model_before(cls, data: Any) -> Any:
            calls.append(('model_before', data))
            return data

    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate({'account_id': 'not-an-int', 'accountId': 2})
    _conflict(exc_info.value)
    assert calls == []

    # Validators still run for an unambiguous input.
    Model.model_validate({'accountId': 2})
    assert calls == [('model_before', {'accountId': 2}), ('field', 2)]


def test_nested_models_accumulate_locations() -> None:
    class Inner(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias='accountId')

    class Outer(BaseModel):
        inner: Inner
        items: list[Inner]

    with pytest.raises(ValidationError) as exc_info:
        Outer.model_validate(
            {
                'inner': {'account_id': 1, 'accountId': 2},
                'items': [{'accountId': 3}, {'account_id': 4, 'accountId': 5}],
            }
        )
    locations = {tuple(error['loc']): error for error in exc_info.value.errors()}
    assert set(locations) == {('inner',), ('items', 1)}
    assert all(error['input'] is None for error in locations.values())


def test_type_adapter_matches_model_entry_points() -> None:
    class Inner(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias='accountId')

    adapter = TypeAdapter(Inner)
    assert adapter.validate_python({'accountId': 2}).account_id == 2
    with pytest.raises(ValidationError) as exc_info:
        adapter.validate_python({'account_id': 1, 'accountId': 2})
    error = _conflict(exc_info.value)
    assert error['loc'] == ()
    assert adapter.validate_json('{"accountId": 2}').account_id == 2
    with pytest.raises(ValidationError):
        adapter.validate_json('{"account_id": 1, "accountId": 2}')


def test_inherited_field_is_checked() -> None:
    class Base(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        account_id: int = Field(validation_alias='accountId')

    class Child(Base):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        other: str = 'default'

    assert Child.model_validate({'accountId': 2}).account_id == 2
    with pytest.raises(ValidationError) as exc_info:
        Child.model_validate({'account_id': 1, 'accountId': 2})
    error = _conflict(exc_info.value)
    assert error['ctx']['field'] == 'account_id'


def test_two_unambiguous_fields_pass() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias='accountId')
        other_id: int = Field(validation_alias='otherId')

    result = Model.model_validate({'accountId': 1, 'otherId': 2})
    assert result.account_id == 1
    assert result.other_id == 2
    result = Model.model_validate({'account_id': 1, 'otherId': 2})
    assert result.account_id == 1


def test_multiple_conflicting_fields_are_all_reported() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        a: int = Field(validation_alias='aAlias')
        b: int = Field(validation_alias='bAlias')

    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate({'a': 1, 'aAlias': 2, 'b': 3, 'bAlias': 4})
    errors = exc_info.value.errors()
    assert [error['ctx']['field'] for error in errors] == ['a', 'b']
    assert all(error['type'] == 'alias_ambiguity' for error in errors)
    assert all(error['input'] is None for error in errors)


def test_from_attributes_object_conflict() -> None:
    class Holder:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    class Model(BaseModel):
        model_config = ConfigDict(
            populate_by_name=True,
            from_attributes=True,
            validate_alias_ambiguity=True,
        )
        account_id: int = Field(validation_alias='accountId')

    assert Model.model_validate(Holder(accountId=2)).account_id == 2
    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate(Holder(account_id=1, accountId=2))
    _conflict(exc_info.value)


def test_from_attributes_nested_alias_path() -> None:
    class Holder:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    class Model(BaseModel):
        model_config = ConfigDict(
            populate_by_name=True,
            from_attributes=True,
            validate_alias_ambiguity=True,
        )
        x: int | None = Field(validation_alias=AliasChoices('top', AliasPath('nested', 'id')))

    # A present nested attribute conflicts with the top-level entry.
    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate(Holder(top=1, nested=Holder(id=None)))
    _conflict(exc_info.value)
    # A missing nested attribute leaves only one readable entry.
    assert Model.model_validate(Holder(top=1)).x == 1


def test_root_model_supports_check() -> None:
    class Root(RootModel[dict[str, Any]]):
        model_config = ConfigDict(validate_alias_ambiguity=True)

    assert Root.model_validate({'a': 1}).root == {'a': 1}


def test_dataclass_conflict() -> None:
    @pdc.dataclass(config=ConfigDict(populate_by_name=True, validate_alias_ambiguity=True))
    class DataClass:
        account_id: int = Field(validation_alias='accountId')

    assert DataClass(accountId=2).account_id == 2
    with pytest.raises(ValidationError) as exc_info:
        DataClass(account_id=1, accountId=2)
    _conflict(exc_info.value)

    adapter = TypeAdapter(DataClass)
    with pytest.raises(ValidationError):
        adapter.validate_python({'account_id': 1, 'accountId': 2})
    # Positional construction is not ambiguous.
    assert DataClass(1).account_id == 1


def test_typed_dict_conflict() -> None:
    @with_config(ConfigDict(populate_by_name=True, validate_alias_ambiguity=True))
    class TD(TypedDict):
        account_id: Annotated[int, Field(validation_alias='accountId')]

    adapter = TypeAdapter(TD)
    assert adapter.validate_python({'accountId': 2}) == {'account_id': 2}
    with pytest.raises(ValidationError) as exc_info:
        adapter.validate_python({'account_id': 1, 'accountId': 2})
    _conflict(exc_info.value)


def test_duplicate_alias_path_is_counted_once() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(validate_alias_ambiguity=True)
        x: int = Field(validation_alias=AliasChoices('a', 'a'))

    # The same entry declared twice still resolves to a single input key.
    assert Model.model_validate({'a': 1}).x == 1


def test_field_name_inside_alias_choices_is_deduplicated() -> None:
    class Model(BaseModel):
        model_config = ConfigDict(populate_by_name=True, validate_alias_ambiguity=True)
        account_id: int = Field(validation_alias=AliasChoices('accountId', 'account_id'))

    assert Model.model_validate({'account_id': 1}).account_id == 1
    with pytest.raises(ValidationError) as exc_info:
        Model.model_validate({'account_id': 1, 'accountId': 2})
    _conflict(exc_info.value)
