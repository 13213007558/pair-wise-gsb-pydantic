from __future__ import annotations as _annotations

import functools
import inspect
from collections.abc import Awaitable, Sequence
from functools import partial
from typing import Any, Callable

import pydantic_core
from typing_extensions import get_overloads

from ..config import ConfigDict
from ..errors import PydanticUserError
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


class _OverloadBranch:
    """A single `@overload` signature of a function decorated with `validate_call(validate_overloads=True)`.

    Holds everything needed to bind, validate and dispatch a call to this branch exactly once.
    """

    __slots__ = ('signature', 'args_validator', 'return_validator')

    def __init__(
        self,
        signature: inspect.Signature,
        args_validator: Callable[[pydantic_core.ArgsKwargs], tuple[tuple[Any, ...], dict[str, Any]]],
        return_validator: Callable[[Any], Any],
    ) -> None:
        self.signature = signature
        self.args_validator = args_validator
        self.return_validator = return_validator


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

        self.ns_resolver = NsResolver(
            namespaces_tuple=ns_for_function(self.schema_type, parent_namespace=parent_namespace)
        )
        self.config_wrapper = ConfigWrapper(config)
        if not self.config_wrapper.defer_build:
            self._create_validators()
        else:
            self.__pydantic_complete__ = False

    def _create_validators(self) -> None:
        if self.validate_overloads:
            self._create_overload_validators()
            return

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

    def _create_overload_validators(self) -> None:
        """Build one `_OverloadBranch` per `@overload` signature of the wrapped function.

        Each branch gets an arguments-only validator (so that probing a candidate does not
        execute the function body) and a return validator built from the branch's own
        return annotation.
        """
        function = self.function
        underlying = function.func if isinstance(function, partial) else function
        overloads = get_overloads(underlying)
        if not overloads:
            raise PydanticUserError(
                f'`validate_overloads` is enabled for `{self.qualname}`, '
                'but no `@overload` signatures were found for it',
                code='validate-call-no-overloads',
            )
        if self.overload_priority is not None:
            invalid = [i for i in self.overload_priority if not isinstance(i, int) or not 0 <= i < len(overloads)]
            if invalid:
                raise PydanticUserError(
                    f'`overload_priority` contains invalid overload indices {invalid} for `{self.qualname}`, '
                    f'which has {len(overloads)} `@overload` signature(s)',
                    code='validate-call-invalid-overload-priority',
                )

        core_config = self.config_wrapper.core_config(title=self.qualname)
        branches: list[_OverloadBranch] = []
        for overload_func in overloads:
            if isinstance(function, partial):
                # Re-apply the partial to each overload signature so that binding and
                # schema generation see the effective (partially applied) signature:
                effective: ValidateCallSupportedTypes = partial(overload_func, *function.args, **function.keywords)
            else:
                effective = overload_func

            signature = inspect.signature(effective)

            gen_schema = GenerateSchema(self.config_wrapper, self.ns_resolver)
            args_schema = gen_schema.clean_schema(gen_schema._arguments_schema(effective))
            args_validator = create_schema_validator(
                args_schema,
                self.schema_type,
                self.module,
                self.qualname,
                'validate_call',
                core_config,
                self.config_wrapper.plugin_settings,
            )

            if signature.return_annotation is not signature.empty:
                globalns, localns = gen_schema._types_namespace
                return_type = _typing_extra.get_function_type_hints(
                    effective, globalns=globalns, localns=localns, include_keys={'return'}
                )['return']
            else:
                return_type = Any
            return_gen_schema = GenerateSchema(self.config_wrapper, self.ns_resolver)
            return_schema = return_gen_schema.clean_schema(return_gen_schema.generate_schema(return_type))
            return_validator = create_schema_validator(
                return_schema,
                self.schema_type,
                self.module,
                self.qualname,
                'validate_call',
                core_config,
                self.config_wrapper.plugin_settings,
            )

            branches.append(
                _OverloadBranch(
                    signature,
                    args_validator.validate_python,
                    return_validator.validate_python,
                )
            )

        self._overload_branches = branches
        self.__pydantic_validator__ = None
        self.__return_pydantic_validator__ = None
        self.__pydantic_complete__ = True

    def _call_with_overloads(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Dispatch a call to the matching `@overload` branch.

        Candidates are first filtered using Python's argument binding rules, then by
        actually validating the arguments against each remaining branch. The converted
        arguments of the selected branch are reused for the final call, so validators
        run at most once per branch and the function body runs at most once.
        """
        matched: list[tuple[int, tuple[Any, ...], dict[str, Any]]] = []
        line_errors: list[pydantic_core.InitErrorDetails] = []
        for index, branch in enumerate(self._overload_branches):
            try:
                branch.signature.bind(*args, **kwargs)
            except TypeError as exc:
                line_errors.append(
                    {
                        'type': pydantic_core.PydanticCustomError(
                            'overload_binding',
                            'Overload {index}: arguments cannot be bound to the signature: {error}',
                            {'index': index, 'error': str(exc)},
                        ),
                        'loc': (index,),
                        'input': pydantic_core.ArgsKwargs(args, kwargs),
                    }
                )
                continue
            try:
                converted_args, converted_kwargs = branch.args_validator(pydantic_core.ArgsKwargs(args, kwargs))
            except pydantic_core.ValidationError as exc:
                line_errors.append(
                    {
                        'type': pydantic_core.PydanticCustomError(
                            'overload_type',
                            'Overload {index}: arguments failed validation: {error}',
                            {'index': index, 'error': str(exc)},
                        ),
                        'loc': (index,),
                        'input': pydantic_core.ArgsKwargs(args, kwargs),
                    }
                )
                continue
            matched.append((index, converted_args, converted_kwargs))

        if not matched:
            raise pydantic_core.ValidationError.from_exception_data(self.qualname, line_errors)

        if len(matched) == 1:
            index, converted_args, converted_kwargs = matched[0]
        else:
            index = self._select_overload([m[0] for m in matched])
            converted_args, converted_kwargs = next((a, k) for i, a, k in matched if i == index)

        branch = self._overload_branches[index]
        if inspect.iscoroutinefunction(self.function):

            async def call_and_validate() -> Any:
                return branch.return_validator(await self.function(*converted_args, **converted_kwargs))

            return call_and_validate()

        result = self.function(*converted_args, **converted_kwargs)
        return branch.return_validator(result)

    def _select_overload(self, matched_indices: list[int]) -> int:
        """Pick a branch among several matching ones using `overload_priority`.

        Declaration order is never used to break ties silently: without an explicit
        priority (or with an ambiguous one) a `PydanticUserError` is raised.
        """
        priority = self.overload_priority
        if priority is not None:

            def rank(index: int) -> int:
                try:
                    return priority.index(index)
                except ValueError:
                    return len(priority)

            best_rank = min(rank(i) for i in matched_indices)
            best = [i for i in matched_indices if rank(i) == best_rank]
            if len(best) == 1:
                return best[0]

        raise PydanticUserError(
            f'Call to `{self.qualname}` is ambiguous: overloads {matched_indices} all accept the provided '
            'arguments. Pass `overload_priority` to `validate_call` to define an explicit priority.',
            code='validate-call-overload-ambiguity',
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.__pydantic_complete__:
            self._create_validators()

        if self.validate_overloads:
            return self._call_with_overloads(args, kwargs)

        res = self.__pydantic_validator__.validate_python(pydantic_core.ArgsKwargs(args, kwargs))
        if self.__return_pydantic_validator__:
            return self.__return_pydantic_validator__(res)
        else:
            return res
