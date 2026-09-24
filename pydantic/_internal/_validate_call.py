from __future__ import annotations as _annotations

import collections.abc
import functools
import inspect
from collections.abc import Awaitable
from functools import partial
from typing import Any, Callable, get_args, get_origin

import pydantic_core

from ..config import ConfigDict
from ..plugin._schema_validator import create_schema_validator
from . import _typing_extra
from ._config import ConfigWrapper
from ._generate_schema import GenerateSchema, ValidateCallSupportedTypes
from ._namespace_utils import MappingNamespace, NsResolver, ns_for_function


def extract_function_name(func: ValidateCallSupportedTypes) -> str:
    """Extract the name of a `ValidateCallSupportedTypes` object."""
    return f'partial({func.func.__name__})' if isinstance(func, functools.partial) else func.__name__


def extract_function_qualname(func: ValidateCallSupportedTypes) -> str:
    """Extract the qualname of a `ValidateCallSupportedTypes` object."""
    return f'partial({func.func.__qualname__})' if isinstance(func, functools.partial) else func.__qualname__


def update_wrapper_attributes(wrapped: ValidateCallSupportedTypes, wrapper: Callable[..., Any]):
    """Update the `wrapper` function with the attributes of the `wrapped` function. Return the updated function."""
    if inspect.iscoroutinefunction(wrapped):

        @functools.wraps(wrapped)
        async def wrapper_function(*args, **kwargs):  # type: ignore
            return await wrapper(*args, **kwargs)
    else:

        @functools.wraps(wrapped)
        def wrapper_function(*args, **kwargs):
            return wrapper(*args, **kwargs)

    # We need to manually update this because `partial` object has no `__name__` and `__qualname__`.
    wrapper_function.__name__ = extract_function_name(wrapped)
    wrapper_function.__qualname__ = extract_function_qualname(wrapped)
    wrapper_function.raw_function = wrapped  # type: ignore

    return wrapper_function


def _extract_async_gen_types(return_type: Any, ns_resolver: NsResolver) -> tuple[Any, Any] | None:
    """Extract the `(yield_type, send_type)` pair from an async generator return annotation.

    Returns `None` if `return_type` is not an `AsyncGenerator` (or `AsyncIterator`/`AsyncIterable`)
    annotation, in which case the return value should be validated as a whole.
    """
    if isinstance(return_type, str):
        return_type, _ = _typing_extra.try_eval_type(return_type, *ns_resolver.types_namespace)
    origin = get_origin(return_type)
    if origin is None:
        # e.g. a bare `AsyncGenerator` annotation:
        origin = return_type
    if origin is collections.abc.AsyncGenerator:
        args = get_args(return_type)
        yield_type = args[0] if len(args) >= 1 else Any
        send_type = args[1] if len(args) >= 2 else Any
        return yield_type, send_type
    if origin in (collections.abc.AsyncIterator, collections.abc.AsyncIterable):
        args = get_args(return_type)
        return (args[0] if args else Any), None
    return None


def _async_gen_validation_error(
    exc: pydantic_core.ValidationError, kind: str, index: int
) -> pydantic_core.ValidationError:
    """Rebuild a validation error so that the location indicates which yield/send failed."""
    line_errors: list[pydantic_core.InitErrorDetails] = []
    for error in exc.errors(include_url=False):
        line_error: pydantic_core.InitErrorDetails = {
            'type': error['type'],
            'loc': (kind, index, *error['loc']),
            'input': error['input'],
        }
        if 'ctx' in error:
            line_error['ctx'] = error['ctx']
        line_errors.append(line_error)
    try:
        return pydantic_core.ValidationError.from_exception_data(exc.title, line_errors)
    except ValueError:
        # Custom error types can't be rebuilt from their string representation,
        # fall back to the original error with a note indicating the location:
        exc.add_note(f'Validation failed at {kind} #{index}')
        return exc


async def _validated_async_gen(
    gen: collections.abc.AsyncGenerator[Any, Any],
    yield_validator: pydantic_core.SchemaValidator,
    send_validator: pydantic_core.SchemaValidator,
) -> collections.abc.AsyncGenerator[Any, Any]:
    """Lazily wrap an async generator, validating and coercing yielded and sent values.

    Values produced by the wrapped generator are validated against the yield type, and values
    sent via `asend()` (after the initial one) are validated against the send type before being
    forwarded. If validation fails, the wrapped generator is closed (so that its `finally`
    blocks run) and the validation error is raised with a location indicating which yield or
    send failed. Exceptions thrown in via `athrow()` (including cancellation) are forwarded
    to the wrapped generator, preserving the original propagation semantics.
    """
    yield_count = 0
    send_count = 0
    sent: Any = None
    started = False

    async def validated_yield(raw: Any) -> Any:
        nonlocal yield_count
        yield_count += 1
        try:
            return yield_validator.validate_python(raw)
        except pydantic_core.ValidationError as exc:
            await gen.aclose()
            raise _async_gen_validation_error(exc, 'yield', yield_count) from exc

    try:
        while True:
            try:
                raw = await (gen.asend(sent) if started else gen.__anext__())
                started = True
            except StopAsyncIteration:
                return
            converted = await validated_yield(raw)
            while True:
                try:
                    sent = yield converted
                    break
                except GeneratorExit:
                    await gen.aclose()
                    raise
                except BaseException as thrown:
                    # The caller used `athrow()` on the wrapper (or the surrounding task was
                    # cancelled): forward the exception to the wrapped generator, so that it
                    # propagates (or is handled) exactly as it would be without validation.
                    try:
                        raw = await gen.athrow(thrown)
                    except StopAsyncIteration:
                        return
                    converted = await validated_yield(raw)
            send_count += 1
            try:
                sent = send_validator.validate_python(sent)
            except pydantic_core.ValidationError as exc:
                await gen.aclose()
                raise _async_gen_validation_error(exc, 'send', send_count) from exc
    finally:
        await gen.aclose()


class ValidateCallWrapper:
    """This is a wrapper around a function that validates the arguments passed to it, and optionally the return value."""

    __slots__ = (
        'function',
        'validate_return',
        'schema_type',
        'module',
        'qualname',
        'ns_resolver',
        'config_wrapper',
        '__pydantic_complete__',
        '__pydantic_validator__',
        '__return_pydantic_validator__',
    )

    def __init__(
        self,
        function: ValidateCallSupportedTypes,
        config: ConfigDict | None,
        validate_return: bool,
        parent_namespace: MappingNamespace | None,
    ) -> None:
        self.function = function
        self.validate_return = validate_return
        if isinstance(function, partial):
            self.schema_type = function.func
            self.module = function.func.__module__
        else:
            self.schema_type = function
            self.module = function.__module__
        self.qualname = extract_function_qualname(function)

        self.ns_resolver = NsResolver(
            namespaces_tuple=ns_for_function(self.schema_type, parent_namespace=parent_namespace)
        )
        self.config_wrapper = ConfigWrapper(config)
        if not self.config_wrapper.defer_build:
            self._create_validators()
        else:
            self.__pydantic_complete__ = False

    def _create_validators(self) -> None:
        gen_schema = GenerateSchema(self.config_wrapper, self.ns_resolver)
        schema = gen_schema.clean_schema(gen_schema.generate_schema(self.function))
        core_config = self.config_wrapper.core_config(title=self.qualname)

        self.__pydantic_validator__ = create_schema_validator(
            schema,
            self.schema_type,
            self.module,
            self.qualname,
            'validate_call',
            core_config,
            self.config_wrapper.plugin_settings,
        )
        if self.validate_return:
            signature = inspect.signature(self.function)
            return_type = signature.return_annotation if signature.return_annotation is not signature.empty else Any

            def build_return_validator(tp: Any) -> pydantic_core.SchemaValidator:
                gen_schema = GenerateSchema(self.config_wrapper, self.ns_resolver)
                schema = gen_schema.clean_schema(gen_schema.generate_schema(tp))
                return create_schema_validator(
                    schema,
                    self.schema_type,
                    self.module,
                    self.qualname,
                    'validate_call',
                    core_config,
                    self.config_wrapper.plugin_settings,
                )

            async_gen_types = (
                _extract_async_gen_types(return_type, self.ns_resolver)
                if inspect.isasyncgenfunction(self.function)
                else None
            )
            if async_gen_types is not None:
                # For async generator functions, validate yielded and sent values separately
                # and lazily, without consuming the returned async generator:
                yield_type, send_type = async_gen_types
                yield_validator = build_return_validator(yield_type)
                send_validator = build_return_validator(send_type)

                def return_val_wrapper(
                    gen: collections.abc.AsyncGenerator[Any, Any],
                ) -> collections.abc.AsyncGenerator[Any, Any]:
                    return _validated_async_gen(gen, yield_validator, send_validator)

                self.__return_pydantic_validator__ = return_val_wrapper
            elif inspect.iscoroutinefunction(self.function):
                validator = build_return_validator(return_type)

                async def return_val_wrapper(aw: Awaitable[Any]) -> None:
                    return validator.validate_python(await aw)

                self.__return_pydantic_validator__ = return_val_wrapper
            else:
                self.__return_pydantic_validator__ = build_return_validator(return_type).validate_python
        else:
            self.__return_pydantic_validator__ = None

        self.__pydantic_complete__ = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.__pydantic_complete__:
            self._create_validators()

        res = self.__pydantic_validator__.validate_python(pydantic_core.ArgsKwargs(args, kwargs))
        if self.__return_pydantic_validator__:
            return self.__return_pydantic_validator__(res)
        else:
            return res
