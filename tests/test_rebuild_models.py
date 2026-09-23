"""Tests for ${bt}pydantic.rebuild_models${bt}: atomic group rebuilding of models."""

from typing import Generic, TypeVar

import pytest

from pydantic import (
    BaseModel,
    PydanticUndefinedAnnotation,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
    rebuild_models,
)

T = TypeVar('T')


def test_rebuild_models_circular_references_atomic_failure_then_success() -> None:
    """A and B reference each other; a deliberately missing name makes the first
    rebuild attempt fail; providing the name and retrying publishes the group."""

    class A(BaseModel):
        b: 'B | None' = None

    class B(BaseModel):
        a: 'A | None' = None
        dep: 'Dep'  # noqa: F821

    assert not A.__pydantic_complete__
    assert not B.__pydantic_complete__

    # Validation is not possible before the group is rebuilt:
    with pytest.raises(PydanticUserError, match='is not fully defined'):
        A.model_validate({'b': None})

    # Snapshot the pre-call definitions of every member (taken after the
    # validation attempt above, as mocks may regenerate themselves on access):
    a_schema_before = A.__pydantic_core_schema__
    a_validator_before = A.__pydantic_validator__
    a_serializer_before = A.__pydantic_serializer__
    a_fields_before = A.__pydantic_fields__
    b_schema_before = B.__pydantic_core_schema__
    b_validator_before = B.__pydantic_validator__
    b_serializer_before = B.__pydantic_serializer__
    b_fields_before = B.__pydantic_fields__

    # 'Dep' is deliberately missing: the whole group fails to rebuild...
    with pytest.raises(PydanticUndefinedAnnotation, match="name 'Dep' is not defined"):
        rebuild_models([A, B])

    # ...and every member keeps the exact definitions it had before the call:
    assert not A.__pydantic_complete__
    assert not B.__pydantic_complete__
    assert A.__pydantic_core_schema__ is a_schema_before
    assert A.__pydantic_validator__ is a_validator_before
    assert A.__pydantic_serializer__ is a_serializer_before
    assert A.__pydantic_fields__ is a_fields_before
    assert B.__pydantic_core_schema__ is b_schema_before
    assert B.__pydantic_validator__ is b_validator_before
    assert B.__pydantic_serializer__ is b_serializer_before
    assert B.__pydantic_fields__ is b_fields_before

    with pytest.raises(PydanticUserError, match='is not fully defined'):
        A.model_validate({'b': None})
    with pytest.raises(PydanticUserError, match='is not fully defined'):
        B.model_validate({'a': None, 'dep': {'x': 1}})

    class Dep(BaseModel):
        x: int

    # Retry with the missing name provided:
    assert rebuild_models([A, B], types_namespace={'Dep': Dep}) is True

    assert A.__pydantic_complete__
    assert B.__pydantic_complete__

    # Validation now works, including through the circular A <-> B references:
    a = A.model_validate({'b': {'a': {'b': None}, 'dep': {'x': 1}}})
    assert isinstance(a.b, B)
    assert isinstance(a.b.a, A)
    assert a.b.a.b is None
    assert a.b.dep.x == 1

    # The validation rules of 'Dep' are enforced through the group:
    with pytest.raises(ValidationError):
        A.model_validate({'b': {'a': None, 'dep': {'x': 'not-an-int'}}})

    # Exported results reflect the published definitions:
    assert a.model_dump() == {'b': {'a': {'b': None}, 'dep': {'x': 1}}}
    assert a.model_dump_json() == '{"b":{"a":{"b":null},"dep":{"x":1}}}'
    assert sorted(A.model_json_schema()['$defs']) == ['A', 'B', 'Dep']


def test_rebuild_models_raise_errors_false() -> None:
    class A(BaseModel):
        dep: 'Dep'

    validator_before = A.__pydantic_validator__

    assert rebuild_models([A], raise_errors=False) is False
    assert not A.__pydantic_complete__
    assert A.__pydantic_validator__ is validator_before

    class Dep(BaseModel):
        x: int

    assert rebuild_models([A], types_namespace={'Dep': Dep}) is True
    assert A.model_validate({'dep': {'x': 1}}).dep.x == 1


def test_rebuild_models_rolls_back_every_member_on_failure() -> None:
    """If a later member of the group fails to rebuild, members that were
    already successfully rebuilt are restored to their pre-call definitions."""

    class A(BaseModel):
        x: int

    class B(BaseModel):
        dep: 'Dep'  # noqa: F821

    assert A.__pydantic_complete__
    a_schema_before = A.__pydantic_core_schema__
    a_validator_before = A.__pydantic_validator__
    a_serializer_before = A.__pydantic_serializer__
    a_fields_before = A.__pydantic_fields__

    # A rebuilds fine, but B fails: A must be restored as well.
    with pytest.raises(PydanticUndefinedAnnotation, match="name 'Dep' is not defined"):
        rebuild_models([A, B])

    assert A.__pydantic_complete__
    assert A.__pydantic_core_schema__ is a_schema_before
    assert A.__pydantic_validator__ is a_validator_before
    assert A.__pydantic_serializer__ is a_serializer_before
    assert A.__pydantic_fields__ is a_fields_before
    assert A.model_validate({'x': 1}).x == 1
    assert not B.__pydantic_complete__


def test_rebuild_models_existing_type_adapter_keeps_old_definitions() -> None:
    class A(BaseModel):
        b: 'B | None' = None

    class B(BaseModel):
        a: 'A | None' = None

    assert rebuild_models([A, B]) is True

    # TypeAdapter reuses the model's own published definitions:
    old_adapter = TypeAdapter(A)
    old_core_schema = A.__pydantic_core_schema__
    old_validator = A.__pydantic_validator__
    old_serializer = A.__pydantic_serializer__
    assert old_adapter.core_schema is old_core_schema
    assert old_adapter.validator is old_validator
    assert old_adapter.serializer is old_serializer

    data = {'b': {'a': {'b': None}}}
    assert old_adapter.dump_python(old_adapter.validate_python(data)) == data

    # A forced group rebuild publishes fresh definitions on the classes:
    assert rebuild_models([A, B]) is True
    assert A.__pydantic_core_schema__ is not old_core_schema
    assert A.__pydantic_validator__ is not old_validator
    assert A.__pydantic_serializer__ is not old_serializer

    # The old adapter still uses the pre-rebuild definitions it was built from:
    assert old_adapter.core_schema is old_core_schema
    assert old_adapter.validator is old_validator
    assert old_adapter.serializer is old_serializer
    assert old_adapter.dump_python(old_adapter.validate_python(data)) == data

    # New adapters use the definitions published by the group rebuild:
    new_adapter = TypeAdapter(A)
    assert new_adapter.core_schema is A.__pydantic_core_schema__
    assert new_adapter.validator is A.__pydantic_validator__
    assert new_adapter.serializer is A.__pydantic_serializer__
    assert new_adapter.dump_python(new_adapter.validate_python(data)) == data


def test_rebuild_models_failure_keeps_existing_mock_type_adapter_stable() -> None:
    class A(BaseModel):
        dep: 'Dep'  # noqa: F821

    adapter = TypeAdapter(A)
    assert not adapter.pydantic_complete
    with pytest.raises(PydanticUserError, match='is not fully defined'):
        adapter.validate_python({'dep': {}})

    with pytest.raises(PydanticUndefinedAnnotation, match="name 'Dep' is not defined"):
        rebuild_models([A])

    # The failed rebuild did not change anything for the existing adapter:
    assert not adapter.pydantic_complete
    with pytest.raises(PydanticUserError, match='is not fully defined'):
        adapter.validate_python({'dep': {}})


def test_rebuild_models_recursive_generic_parameters() -> None:
    """Forward references nested inside generic parameters are resolved
    recursively, using the shared types namespace."""

    class Box(BaseModel, Generic[T]):
        item: T

    class A(BaseModel):
        box: 'Box[Payload]'
        boxes: 'list[Box[Payload]]'

    class Payload(BaseModel):
        x: int

    assert not A.__pydantic_complete__
    assert rebuild_models([A], types_namespace={'Payload': Payload}) is True

    a = A.model_validate({'box': {'item': {'x': 1}}, 'boxes': [{'item': {'x': 2}}]})
    assert a.box.item.x == 1
    assert a.boxes[0].item.x == 2
    with pytest.raises(ValidationError):
        A.model_validate({'box': {'item': {'x': 'not-an-int'}}, 'boxes': []})


def test_rebuild_models_generic_group_members_referencing_each_other() -> None:
    class Pair(BaseModel, Generic[T]):
        first: T
        second: T

    class A(BaseModel):
        pair: 'Pair[B]'

    class B(BaseModel):
        a: 'A | None' = None

    assert rebuild_models([A, B]) is True

    a = A.model_validate(
        {'pair': {'first': {'a': {'pair': {'first': {'a': None}, 'second': {'a': None}}}}, 'second': {'a': None}}}
    )
    assert isinstance(a.pair.first, B)
    assert isinstance(a.pair.first.a, A)
    assert isinstance(a.pair.first.a.pair.first, B)
    assert a.pair.second.a is None
    with pytest.raises(ValidationError):
        A.model_validate({'pair': {'first': {'a': 1}, 'second': {'a': None}}})


def test_rebuild_models_shared_namespace_takes_precedence() -> None:
    """Names from the shared namespace win over the models' own parent namespaces."""

    class B(BaseModel):
        x: int

    class A(BaseModel):
        # 'Dep' being undefined, the whole annotation is reevaluated during the rebuild:
        b: 'tuple[B, Dep]'

    class BNew(BaseModel):
        y: str

    class Dep(BaseModel):
        z: int

    assert rebuild_models([A], types_namespace={'B': BNew, 'Dep': Dep}) is True
    assert A.model_fields['b'].annotation == tuple[BNew, Dep]
    b, dep = A.model_validate({'b': [{'y': 'a'}, {'z': 1}]}).b
    assert isinstance(b, BNew)
    assert isinstance(dep, Dep)


def test_rebuild_models_leaves_models_outside_the_group_untouched() -> None:
    class A(BaseModel):
        b: 'B'

    class B(BaseModel):
        x: int

    class Outside(BaseModel):
        b: B

    outside_schema = Outside.__pydantic_core_schema__
    outside_validator = Outside.__pydantic_validator__
    outside_serializer = Outside.__pydantic_serializer__

    assert rebuild_models([A, B]) is True

    # Models outside of the group keep their identity and definitions:
    assert Outside.__pydantic_core_schema__ is outside_schema
    assert Outside.__pydantic_validator__ is outside_validator
    assert Outside.__pydantic_serializer__ is outside_serializer
    assert Outside.model_validate({'b': {'x': 1}}).b.x == 1


def test_rebuild_models_invalid_input() -> None:
    with pytest.raises(TypeError, match='Expected a Pydantic model'):
        rebuild_models([int])

    assert rebuild_models([]) is True
