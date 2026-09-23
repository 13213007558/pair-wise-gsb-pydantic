import pytest

from pydantic import BaseModel, TypeAdapter
from pydantic.experimental.rebuild_models import rebuild_models


def test_rebuild_models_group_missing_then_fixed() -> None:
    class A(BaseModel):
        b: 'B | None' = None
        extra: 'Missing'  # noqa: F821

    class B(BaseModel):
        a: A

    assert not A.__pydantic_complete__
    assert not B.__pydantic_complete__

    # First attempt fails: the 'Missing' type name is deliberately not defined yet.
    assert rebuild_models([A, B], raise_errors=False) is False

    # All-or-nothing: every member keeps its pre-call (incomplete) definition.
    assert not A.__pydantic_complete__
    assert not B.__pydantic_complete__

    # Retry, now providing the missing type name:
    assert rebuild_models([A, B], types_namespace={'Missing': int}) is True
    assert A.__pydantic_complete__
    assert B.__pydantic_complete__

    a = A(b={'a': {'b': None, 'extra': '1'}, 'extra': '2'}, extra='3')
    assert a.extra == 3
    assert a.b.a.b is None

    # The exported JSON schema reflects the published definitions:
    assert A.model_json_schema()['$defs']['A']['properties']['extra'] == {'title': 'Extra', 'type': 'integer'}


def test_rebuild_models_group_atomic_rollback() -> None:
    class Good(BaseModel):
        x: int

    class Bad(BaseModel):
        y: 'Missing'  # noqa: F821

    assert Good.__pydantic_complete__
    old_validator = Good.__pydantic_validator__
    old_schema = Good.__pydantic_core_schema__

    assert rebuild_models([Good, Bad], raise_errors=False) is False

    # The failure of 'Bad' leaves 'Good' with its pre-call definitions:
    assert Good.__pydantic_complete__
    assert Good.__pydantic_validator__ is old_validator
    assert Good.__pydantic_core_schema__ is old_schema
    assert not Bad.__pydantic_complete__


def test_rebuild_models_existing_type_adapter_stays_stable() -> None:
    class M(BaseModel):
        x: 'Missing'  # noqa: F821

    class N(BaseModel):
        m: M

    # The models are incomplete, so a `TypeAdapter` built now holds mocks:
    old_adapter = TypeAdapter(M)
    old_validator = old_adapter.validator

    assert rebuild_models([M, N], types_namespace={'Missing': int}) is True

    # The existing adapter is left untouched, while new adapters use the published definitions:
    assert old_adapter.validator is old_validator
    assert M(x='1').x == 1
    assert TypeAdapter(M).validate_python({'x': '1'}).x == 1


def test_rebuild_models_generic_members() -> None:
    from typing import Generic, TypeVar

    T = TypeVar('T')

    class Box(BaseModel, Generic[T]):
        content: T
        other: 'Box2 | None' = None

    class Box2(BaseModel):
        box: 'Box[int]'

    assert rebuild_models([Box, Box2]) is True

    box2 = Box2(box={'content': '1'})
    assert box2.box.content == 1
    assert box2.box.other is None


def test_rebuild_models_out_of_group_types_untouched() -> None:
    class Outside(BaseModel):
        x: int

    class Inside(BaseModel):
        outside: Outside
        extra: 'Missing'  # noqa: F821

    old_validator = Outside.__pydantic_validator__
    assert rebuild_models([Inside], raise_errors=False) is False
    assert Outside.__pydantic_validator__ is old_validator


def test_rebuild_models_invalid_member_type() -> None:
    with pytest.raises(TypeError):
        rebuild_models([int])  # type: ignore[arg-type]


def test_rebuild_models_empty_group() -> None:
    assert rebuild_models([]) is None
