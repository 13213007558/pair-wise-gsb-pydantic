from __future__ import annotations as _annotations

import functools
import inspect
import typing
from collections.abc import AsyncGenerator, Awaitable
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


def _extract_async_gen_types(return_type: Any) -> tuple[Any, Any] | None:
    """Extract the yield and send types from an AsyncGenerator annotation.

    Return None if return_type is not an AsyncGenerator annotation.
    """
    if (
        get_origin(return_type) is AsyncGenerator
        or return_type is AsyncGenerator
        or return_type is typing.AsyncGenerator
    ):
        args = get_args(return_type)
        yield_type = args[0] if args else Any
        send_type = args[1] if len(args) > 1 else Any
        return yield_type, send_type
    return None


def _add_loc_to_validation_error(
    exc: pydantic_core.ValidationError, loc_prefix: tuple[Any, ...]
) -> pydantic_core.ValidationError:
    """Rebuild a ValidationError with each error location prefixed, e.g. with the yield/send index."""
    line_errors = []
    for error in exc.errors(include_url=False):
        details = {'type': error['type'], 'loc': loc_prefix + error['loc'], 'input': error['input']}
        if 'ctx' in error:
            details['ctx'] = error['ctx']
        line_errors.append(details)
    return pydantic_core.ValidationError.from_exception_data(exc.title, line_errors)


class _ValidatedAsyncGenerator:
    """An async generator wrapper that validates yielded and sent values on behalf of validate_call.

    The wrapped generator is only consumed lazily: values are validated as they are yielded or sent,
    and the generator is never advanced eagerly nor collected.
    """

    __slots__ = (
        '_gen',
        '_validate_yield',
        '_validate_send',
        '_started',
        '_yield_index',
        '_send_index',
        '_closed',
    )

    def __init__(
        self,
        gen: AsyncGenerator[Any, Any],
        validate_yield: Callable[[Any], Any],
        validate_send: Callable[[Any], Any],
    ) -> None:
        self._gen = gen
        self._validate_yield = validate_yield
        self._validate_send = validate_send
        self._started = False
        self._yield_index = 0
        self._send_index = 0
        self._closed = False

    def __aiter__(self) -> _ValidatedAsyncGenerator:
        return self

    async def __anext__(self) -> Any:
        self._started = True
        return await self._process_yielded(await self._gen.__anext__())

    async def asend(self, value: Any) -> Any:
        if self._started:
            value = await self._process_sent(value)
        else:
            # The priming send is not validated, it can only be None anyway.
            self._started = True
        return await self._process_yielded(await self._gen.asend(value))

    async def athrow(self, *args: Any) -> Any:
        # Delegate directly so that the thrown exception (and any exception raised in response,
        # e.g. by cancellation) keeps its original propagation semantics.
        return await self._process_yielded(await self._gen.athrow(*args))

    async def aclose(self) -> None:
        await self._close()

    async def _process_yielded(self, value: Any) -> Any:
        index = self._yield_index
        self._yield_index += 1
        try:
            return self._validate_yield(value)
        except pydantic_core.ValidationError as exc:
            await self._close()
            raise _add_loc_to_validation_error(exc, ('yield', index))

    async def _process_sent(self, value: Any) -> Any:
        index = self._send_index
        self._send_index += 1
        try:
            return self._validate_send(value)
        except pydantic_core.ValidationError as exc:
            await self._close()
            raise _add_loc_to_validation_error(exc, ('send', index))

    async def _close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._gen.aclose()


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
            async_gen_types = None
            if inspect.isasyncgenfunction(self.function):
                resolved_return_type = return_type
                if isinstance(resolved_return_type, str):
                    # e.g. when the function was defined with future annotations:
                    try:
                        resolved_return_type = _typing_extra.eval_type_backport(
                            _typing_extra._make_forward_ref(resolved_return_type),
                            *self.ns_resolver.types_namespace,
                        )
                    except (NameError, TypeError):
                        # Let the schema generation below raise the appropriate error.
                        resolved_return_type = return_type
                async_gen_types = _extract_async_gen_types(resolved_return_type)
            if async_gen_types is not None:
                yield_type, send_type = async_gen_types
                yield_validator = self._create_return_validator(yield_type, core_config)
                send_validator = self._create_return_validator(send_type, core_config)

                def return_val_wrapper(gen: AsyncGenerator[Any, Any]) -> Any:
                    return _ValidatedAsyncGenerator(
                        gen, yield_validator.validate_python, send_validator.validate_python
                    )

                self.__return_pydantic_validator__ = return_val_wrapper
            elif inspect.iscoroutinefunction(self.function):
                validator = self._create_return_validator(return_type, core_config)

                async def return_val_wrapper(aw: Awaitable[Any]) -> None:
                    return validator.validate_python(await aw)

                self.__return_pydantic_validator__ = return_val_wrapper
            else:
                validator = self._create_return_validator(return_type, core_config)
                self.__return_pydantic_validator__ = validator.validate_python
        else:
            self.__return_pydantic_validator__ = None

        self.__pydantic_complete__ = True

    def _create_return_validator(self, return_type: Any, core_config: Any) -> Any:
        gen_schema = GenerateSchema(self.config_wrapper, self.ns_resolver)
        schema = gen_schema.clean_schema(gen_schema.generate_schema(return_type))
        return create_schema_validator(
            schema,
            self.schema_type,
            self.module,
            self.qualname,
            'validate_call',
            core_config,
            self.config_wrapper.plugin_settings,
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.__pydantic_complete__:
            self._create_validators()

        res = self.__pydantic_validator__.validate_python(pydantic_core.ArgsKwargs(args, kwargs))
        if self.__return_pydantic_validator__:
            return self.__return_pydantic_validator__(res)
        else:
            return res
