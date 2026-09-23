from __future__ import annotations as _annotations

import functools
import inspect
import typing
from collections.abc import Awaitable, Sequence
from functools import partial
from typing import Any, Callable, NamedTuple

import pydantic_core

from ..config import ConfigDict
from ..errors import AmbiguousOverloadError, NoMatchingOverloadError, PydanticUserError
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


class _OverloadBranch(NamedTuple):
    """Precomputed data for a single `@overload` branch of a validated function."""

    signature: inspect.Signature
    arguments_validator: Any  # SchemaValidator | PluggableSchemaValidator
    return_validator: Any | None  # SchemaValidator | PluggableSchemaValidator | None


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


class ValidateCallWrapper:
    """This is a wrapper around a function that validates the arguments passed to it, and optionally the return value."""

    __slots__ = (
        'function',
        'validate_return',
        'validate_overloads',
        'overload_priority',
        'schema_type',
        'module',
        'qualname',
        'ns_resolver',
        'config_wrapper',
        '_overloads',
        '_overload_branches',
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
        validate_overloads: bool = False,
        overload_priority: Sequence[int] | None = None,
    ) -> None:
        self.function = function
        self.validate_return = validate_return
        self.validate_overloads = validate_overloads
        self.overload_priority = tuple(overload_priority) if overload_priority is not None else None
        if isinstance(function, partial):
            self.schema_type = function.func
            self.module = function.func.__module__
        else:
            self.schema_type = function
            self.module = function.__module__
        self.qualname = extract_function_qualname(function)

        self._overload_branches: list[_OverloadBranch] | None = None
        self._overloads: list[Callable[..., Any]] | None = None
        if validate_overloads:
            overloads = list(typing.get_overloads(self.schema_type))
            if not overloads:
                raise PydanticUserError(
                    f'`validate_overloads=True` requires `{self.qualname}` to have at least one `@overload` definition',
                    code='validate-call-overloads',
                )
            if self.overload_priority is not None and any(
                not isinstance(i, int) or isinstance(i, bool) or i < 0 or i >= len(overloads)
                for i in self.overload_priority
            ):
                raise PydanticUserError(
                    f'`overload_priority` must contain valid overload indices (0 to {len(overloads) - 1}) '
                    f'for `{self.qualname}`, got {list(self.overload_priority)}',
                    code='validate-call-overloads',
                )
            self._overloads = overloads

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
        if self._overloads is not None:
            self._create_overload_validators(gen_schema)
            self.__pydantic_validator__ = None
            self.__return_pydantic_validator__ = None
            self.__pydantic_complete__ = True
            return

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
            gen_schema = GenerateSchema(self.config_wrapper, self.ns_resolver)
            schema = gen_schema.clean_schema(gen_schema.generate_schema(return_type))
            validator = create_schema_validator(
                schema,
                self.schema_type,
                self.module,
                self.qualname,
                'validate_call',
                core_config,
                self.config_wrapper.plugin_settings,
            )
            if inspect.iscoroutinefunction(self.function):

                async def return_val_wrapper(aw: Awaitable[Any]) -> None:
                    return validator.validate_python(await aw)

                self.__return_pydantic_validator__ = return_val_wrapper
            else:
                self.__return_pydantic_validator__ = validator.validate_python
        else:
            self.__return_pydantic_validator__ = None

        self.__pydantic_complete__ = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.__pydantic_complete__:
            self._create_validators()

        if self._overload_branches is not None:
            return self._call_overloaded(args, kwargs)

        res = self.__pydantic_validator__.validate_python(pydantic_core.ArgsKwargs(args, kwargs))
        if self.__return_pydantic_validator__:
            return self.__return_pydantic_validator__(res)
        else:
            return res

    def _schema_function_for_overload(self, overload_func: Callable[..., Any]) -> ValidateCallSupportedTypes:
        """Apply the bound arguments of the wrapped function (if it is a `functools.partial`) to an overload."""
        if isinstance(self.function, partial):
            return partial(overload_func, *self.function.args, **(self.function.keywords or {}))
        return overload_func

    def _create_overload_validators(self, gen_schema: GenerateSchema) -> None:
        """Build an arguments (and optionally return) validator for every `@overload` branch."""
        assert self._overloads is not None
        core_config = self.config_wrapper.core_config(title=self.qualname)
        globalns, localns = gen_schema._types_namespace

        branches: list[_OverloadBranch] = []
        for overload_func in self._overloads:
            schema_func = self._schema_function_for_overload(overload_func)
            sig = inspect.signature(schema_func)
            arguments_schema = gen_schema.clean_schema(gen_schema._arguments_schema(schema_func))
            arguments_validator = create_schema_validator(
                arguments_schema,
                self.schema_type,
                self.module,
                self.qualname,
                'validate_call',
                core_config,
                self.config_wrapper.plugin_settings,
            )
            return_validator = None
            if self.validate_return:
                type_hints = _typing_extra.get_function_type_hints(
                    schema_func, globalns=globalns, localns=localns, include_keys={'return'}
                )
                return_schema_gen = GenerateSchema(self.config_wrapper, self.ns_resolver)
                return_schema = return_schema_gen.clean_schema(
                    return_schema_gen.generate_schema(type_hints.get('return', Any))
                )
                return_validator = create_schema_validator(
                    return_schema,
                    self.schema_type,
                    self.module,
                    self.qualname,
                    'validate_call',
                    core_config,
                    self.config_wrapper.plugin_settings,
                )
            branches.append(_OverloadBranch(sig, arguments_validator, return_validator))
        self._overload_branches = branches

    def _call_overloaded(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Dispatch a call to the matching `@overload` branch and invoke the function at most once."""
        branches = self._overload_branches
        assert branches is not None

        # Phase 1: filter the overloads using Python's argument binding rules only, so that no
        # parameter validator runs for a branch that cannot even be bound to the arguments.
        binding_failures: dict[int, TypeError] = {}
        candidates: list[int] = []
        for index, branch in enumerate(branches):
            try:
                branch.signature.bind(*args, **kwargs)
            except TypeError as exc:  # noqa: PERF203
                binding_failures[index] = exc
            else:
                candidates.append(index)

        # Phase 2: validate the argument types of the remaining candidates. The converted
        # arguments are kept so that the chosen branch is never converted a second time.
        type_failures: dict[int, pydantic_core.ValidationError] = {}
        matched: dict[int, Any] = {}
        for index in candidates:
            try:
                converted = branches[index].arguments_validator.validate_python(pydantic_core.ArgsKwargs(args, kwargs))
            except pydantic_core.ValidationError as exc:  # noqa: PERF203
                type_failures[index] = exc
            else:
                matched[index] = converted

        if not matched:
            raise NoMatchingOverloadError(self._no_matching_overload_message(binding_failures, type_failures))
        if len(matched) == 1:
            chosen = next(iter(matched))
        else:
            chosen = self._resolve_ambiguity(list(matched))

        branch = branches[chosen]
        converted_args, converted_kwargs = matched[chosen]
        result = self.function(*converted_args, **converted_kwargs)
        if branch.return_validator is not None:
            if inspect.iscoroutinefunction(self.function):

                async def validated_result() -> Any:
                    return branch.return_validator.validate_python(await result)

                return validated_result()
            return branch.return_validator.validate_python(result)
        return result

    def _resolve_ambiguity(self, matched_indices: list[int]) -> int:
        """Choose among multiple matching overloads using the explicit `overload_priority`, if any."""
        if self.overload_priority is not None:
            for index in self.overload_priority:
                if index in matched_indices:
                    return index
        raise AmbiguousOverloadError(self._ambiguous_overload_message(matched_indices))

    def _no_matching_overload_message(
        self,
        binding_failures: dict[int, TypeError],
        type_failures: dict[int, pydantic_core.ValidationError],
    ) -> str:
        assert self._overload_branches is not None
        lines = [f'No overload of `{self.qualname}` matched the provided arguments:']
        for index, branch in enumerate(self._overload_branches):
            if index in binding_failures:
                reason = f'arguments could not be bound: {binding_failures[index]}'
            else:
                validation_error = str(type_failures[index]).replace('\n', '\n  ')
                reason = f'validation failed:\n  {validation_error}'
            lines.append(f'Overload {index} `{branch.signature}`: {reason}')
        return '\n'.join(lines)

    def _ambiguous_overload_message(self, matched_indices: list[int]) -> str:
        assert self._overload_branches is not None
        matched = ', '.join(f'{index} `{self._overload_branches[index].signature}`' for index in matched_indices)
        if self.overload_priority is None:
            hint = 'Pass `overload_priority` to `validate_call` to define an explicit priority order.'
        else:
            hint = 'None of the matched overloads is listed in `overload_priority`.'
        return f'Call to `{self.qualname}` matched multiple overloads: {matched}. {hint}'
