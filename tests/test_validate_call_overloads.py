import asyncio
from functools import partial
from typing import Annotated, Any, overload

import pytest

from pydantic import (
    AfterValidator,
    AmbiguousOverloadError,
    NoMatchingOverloadError,
    PydanticUserError,
    ValidationError,
    validate_call,
)


class Counter:
    """Helper to count executions of validators and function bodies."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, value: Any) -> Any:
        self.count += 1
        return value


def test_dispatch_selects_matching_branch() -> None:
    body_calls = Counter()

    @overload
    def rpc(a: int) -> int: ...

    @overload
    def rpc(a: str, *, tag: str = 'x') -> str: ...

    def rpc(a, tag='x'):
        body_calls(a)
        return a if isinstance(a, int) else f'{a}:{tag}'

    f = validate_call(validate_overloads=True)(rpc)

    assert f(5) == 5
    assert f('v', tag='y') == 'v:y'
    assert body_calls.count == 2


def test_arguments_are_converted_with_chosen_branch() -> None:
    received = []

    @overload
    def rpc(a: int) -> int: ...

    @overload
    def rpc(a: list) -> list: ...

    def rpc(a):
        received.append(a)
        return a

    f = validate_call(validate_overloads=True)(rpc)
    assert f('5') == 5  # coerced using the int overload, then passed to the implementation
    assert received == [5]


def test_binding_filter_runs_before_type_validation() -> None:
    """Overloads that cannot bind the arguments must not run any parameter validator."""
    int_validator = Counter()
    kw_validator = Counter()
    body_calls = Counter()

    @overload
    def rpc(a: Annotated[int, AfterValidator(int_validator)]) -> int: ...

    @overload
    def rpc(a: str, *, extra: Annotated[str, AfterValidator(kw_validator)]) -> str: ...

    def rpc(a, extra=''):
        body_calls(a)
        return a

    f = validate_call(validate_overloads=True)(rpc)

    # Only binds (and validates) the second overload:
    assert f('v', extra='e') == 'v'
    assert int_validator.count == 0
    assert kw_validator.count == 1
    assert body_calls.count == 1

    # Only binds (and validates) the first overload:
    assert f(1) == 1
    assert int_validator.count == 1
    assert kw_validator.count == 1
    assert body_calls.count == 2


def test_ambiguous_match_raises_without_priority() -> None:
    body_calls = Counter()

    @overload
    def rpc(a: bool) -> bool: ...

    @overload
    def rpc(a: int) -> int: ...

    def rpc(a):
        body_calls(a)
        return a

    f = validate_call(validate_overloads=True)(rpc)

    with pytest.raises(AmbiguousOverloadError, match=r'matched multiple overloads: 0 .* 1 '):
        f(True)  # True is valid both as bool and as int
    assert body_calls.count == 0

    # an int that is not a bool only matches the second overload:
    assert f(5) == 5
    assert body_calls.count == 1


@pytest.mark.parametrize('priority,expected', [([0, 1], 'bool'), ([1, 0], 'int')])
def test_overload_priority_resolves_ambiguity(priority, expected) -> None:
    body_calls = Counter()

    @overload
    def rpc(a: bool) -> str: ...

    @overload
    def rpc(a: int) -> str: ...

    def rpc(a):
        body_calls(a)
        return 'bool' if isinstance(a, bool) else 'int'

    f = validate_call(validate_overloads=True, overload_priority=priority)(rpc)
    assert f(True) == expected
    assert body_calls.count == 1


def test_priority_not_matching_any_candidate_still_raises() -> None:
    @overload
    def rpc(a: bool) -> bool: ...

    @overload
    def rpc(a: int) -> int: ...

    @overload
    def rpc(a: bytes) -> bytes: ...

    def rpc(a):
        return a

    f = validate_call(validate_overloads=True, overload_priority=[2])(rpc)
    with pytest.raises(AmbiguousOverloadError, match='None of the matched overloads is listed'):
        f(True)


def test_validators_and_body_run_exactly_once() -> None:
    """Probing candidates and calling must not re-run the chosen branch's conversion."""
    bool_validator = Counter()
    int_validator = Counter()
    body_calls = Counter()

    @overload
    def rpc(a: Annotated[bool, AfterValidator(bool_validator)]) -> bool: ...

    @overload
    def rpc(a: Annotated[int, AfterValidator(int_validator)]) -> int: ...

    def rpc(a):
        body_calls(a)
        return a

    f = validate_call(validate_overloads=True, overload_priority=[0, 1])(rpc)
    assert f(True) is True

    # each candidate was probed once; the chosen branch was not converted again for the call
    assert bool_validator.count == 1
    assert int_validator.count == 1
    assert body_calls.count == 1


def test_failed_candidate_validator_not_rerun() -> None:
    int_validator = Counter()
    str_validator = Counter()
    body_calls = Counter()

    @overload
    def rpc(a: Annotated[int, AfterValidator(int_validator)]) -> int: ...

    @overload
    def rpc(a: Annotated[str, AfterValidator(str_validator)]) -> str: ...

    def rpc(a):
        body_calls(a)
        return a

    f = validate_call(validate_overloads=True)(rpc)

    assert f('abc') == 'abc'
    # 'abc' cannot be parsed as int, so the int AfterValidator never ran:
    assert int_validator.count == 0
    assert str_validator.count == 1
    assert body_calls.count == 1

    assert f(5) == 5
    assert int_validator.count == 1
    assert str_validator.count == 1
    assert body_calls.count == 2


def test_no_matching_overload_lists_binding_and_type_errors() -> None:
    body_calls = Counter()

    @overload
    def rpc(a: int, b: int) -> int: ...

    @overload
    def rpc(a: str) -> str: ...

    def rpc(a, b=0):
        body_calls(a)
        return a

    f = validate_call(validate_overloads=True)(rpc)

    with pytest.raises(NoMatchingOverloadError) as exc_info:
        f(1.5)

    message = str(exc_info.value)
    assert 'No overload of' in message
    assert 'Overload 0' in message and '(a: int, b: int) -> int' in message
    assert 'Overload 1' in message and '(a: str) -> str' in message
    assert 'validation failed' in message
    assert body_calls.count == 0

    # wrong arity: both overloads report binding errors
    with pytest.raises(NoMatchingOverloadError) as exc_info:
        f(1, 2, 3)
    assert exc_info.value.args[0].count('arguments could not be bound') == 2
    assert body_calls.count == 0


def test_return_validated_with_chosen_branch() -> None:
    body_calls = Counter()

    @overload
    def rpc(a: int) -> int: ...

    @overload
    def rpc(a: str) -> str: ...

    def rpc(a):
        body_calls(a)
        return a

    f = validate_call(validate_overloads=True, validate_return=True)(rpc)
    assert f(5) == 5
    assert f('x') == 'x'
    assert body_calls.count == 2

    @overload
    def bad(a: int) -> int: ...

    @overload
    def bad(a: str) -> str: ...

    def bad(a):
        return None  # never matches the chosen branch's return annotation

    g = validate_call(validate_overloads=True, validate_return=True)(bad)
    with pytest.raises(ValidationError):
        g(5)
    with pytest.raises(ValidationError):
        g('x')


def test_async_return_validated_after_await() -> None:
    body_calls = Counter()
    results = []

    @overload
    def rpc(a: int) -> int: ...

    @overload
    def rpc(a: str) -> str: ...

    async def rpc(a):
        body_calls(a)
        return a

    f = validate_call(validate_overloads=True, validate_return=True)(rpc)
    results.append(asyncio.run(f(5)))
    results.append(asyncio.run(f('x')))
    assert results == [5, 'x']
    assert body_calls.count == 2

    @overload
    def bad(a: int) -> int: ...

    async def bad(a):
        return 'not an int'

    g = validate_call(validate_overloads=True, validate_return=True)(bad)
    with pytest.raises(ValidationError):
        asyncio.run(g(5))


def test_async_dispatch_runs_body_once() -> None:
    body_calls = Counter()

    @overload
    def rpc(a: int) -> int: ...

    @overload
    def rpc(a: str, *, tag: str = 't') -> str: ...

    async def rpc(a, tag='t'):
        body_calls(a)
        return a

    f = validate_call(validate_overloads=True)(rpc)
    assert asyncio.run(f('v', tag='z')) == 'v'
    assert asyncio.run(f(3)) == 3
    assert body_calls.count == 2


def test_positional_only_keyword_only_and_defaults() -> None:
    body_calls = Counter()

    @overload
    def rpc(a: int, /, b: int = 2) -> int: ...

    @overload
    def rpc(a: str, /, *, b: str = 'd') -> str: ...

    def rpc(a, b='d'):
        body_calls(a)
        return f'{a}|{b}'

    f = validate_call(validate_overloads=True)(rpc)

    assert f(1) == '1|2'  # default from the chosen overload is applied
    assert f(1, 3) == '1|3'
    assert f('v') == 'v|d'
    assert f('v', b='z') == 'v|z'
    assert body_calls.count == 4

    # 'b' is keyword-only in the second overload, so this only binds the first one
    # (and fails its type validation):
    with pytest.raises(NoMatchingOverloadError):
        f('v', 'z')
    assert body_calls.count == 4


def test_partial_of_overloaded_function() -> None:
    body_calls = Counter()

    @overload
    def rpc(tag: str, a: int) -> str: ...

    @overload
    def rpc(tag: str, a: str) -> str: ...

    def rpc(tag, a):
        body_calls(a)
        return f'{tag}:{a}'

    f = validate_call(validate_overloads=True)(partial(rpc, 'T'))

    assert f(1) == 'T:1'
    assert f('v') == 'T:v'
    assert body_calls.count == 2

    with pytest.raises(NoMatchingOverloadError):
        f('T', 1)  # 'tag' is already bound by the partial
    assert body_calls.count == 2


def test_partial_with_keyword_of_overloaded_function() -> None:
    @overload
    def rpc(a: int, *, b: int = 1) -> int: ...

    @overload
    def rpc(a: str, *, b: str = 'x') -> str: ...

    def rpc(a, b=None):
        return (a, b)

    f = validate_call(validate_overloads=True)(partial(rpc, b=5))
    # b=5 is only valid for the first overload:
    assert f(3) == (3, 5)


def test_validate_overloads_without_overloads_errors() -> None:
    def rpc(a: int) -> int:
        return a

    with pytest.raises(PydanticUserError, match='validate_overloads'):
        validate_call(validate_overloads=True)(rpc)


def test_invalid_overload_priority_errors() -> None:
    @overload
    def rpc(a: int) -> int: ...

    @overload
    def rpc(a: str) -> str: ...

    def rpc(a):
        return a

    with pytest.raises(PydanticUserError, match='overload_priority'):
        validate_call(validate_overloads=True, overload_priority=[2])(rpc)

    with pytest.raises(PydanticUserError, match='overload_priority'):
        validate_call(validate_overloads=True, overload_priority=[-1])(rpc)


def test_default_mode_still_uses_implementation_signature() -> None:
    @overload
    def rpc(a: int) -> int: ...

    @overload
    def rpc(a: str) -> str: ...

    def rpc(a):
        return a

    f = validate_call(rpc)
    # the implementation signature accepts anything:
    assert f(1.5) == 1.5
    assert f([1, 2]) == [1, 2]
