"""Experimental module exposing a function to atomically rebuild a group of models."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic._internal import _mock_val_ser

from ..errors import PydanticUserError
from ..main import BaseModel

_SNAPSHOT_ATTRS = (
    '__pydantic_core_schema__',
    '__pydantic_validator__',
    '__pydantic_serializer__',
    '__pydantic_fields__',
    '__pydantic_computed_fields__',
    '__pydantic_complete__',
    '__signature__',
)


def rebuild_models(
    models: Iterable[type[BaseModel]],
    *,
    types_namespace: Mapping[str, Any] | None = None,
    raise_errors: bool = True,
) -> bool | None:
    """Atomically rebuild a group of models against a shared types namespace.

    This is useful when the forward references of several models can only be
    resolved once all of them are defined (e.g. when models are registered by
    plugins), and rebuilding them one by one with `model_rebuild()` could
    leave some models in a broken state if another one fails.

    The rebuild is all-or-nothing: the new core schemas, validators and serializers are
    only published if *every* model in the group can be rebuilt. If any model
    fails to rebuild, all the models in the group keep the definitions they had
    before the call. Models outside the group are never modified, and existing
    `TypeAdapter` instances keep using the definitions they were built with.

    Args:
        models: The models to rebuild. References between members of the group
            (including through generic parametrizations) are resolved against the shared
            namespace.
        types_namespace: An explicit types namespace, used for every member of
            the group. The group members are also added to this namespace under
            their class name, so that they can reference each other by name.
        raise_errors: Whether to raise an error if the group cannot be rebuilt.
            If `False`, `False` is returned instead.

    Returns:
        `None` if the group is empty. `True` if every model was
        successfully rebuilt, `False` otherwise (only when
        `raise_errors=False`).

    Raises:
        TypeError: If a member of `models` is not a `BaseModel` subclass.
        PydanticUndefinedAnnotation: If a type annotation of one of the models
            cannot be resolved, even with the shared namespace.
        PydanticUserError: If the core schema of one of the models is invalid.
    """
    group = list(dict.fromkeys(models))
    for model in group:
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError(f'{model!r} is not a `BaseModel` subclass.')
    if not group:
        return None

    ns: dict[str, Any] = {model.__name__: model for model in group}
    if types_namespace is not None:
        ns.update(types_namespace)

    snapshots = {model: _snapshot_model(model) for model in group}

    # Force-reset every member *before* rebuilding any of them, so that
    # references between group members are re-resolved against the shared namespace
    # instead of reusing the cached core schemas:
    for model in group:
        model.__pydantic_complete__ = False
        for attr in ('__pydantic_core_schema__', '__pydantic_validator__', '__pydantic_serializer__'):
            if attr in model.__dict__ and not isinstance(getattr(model, attr), _mock_val_ser.MockValSer):
                delattr(model, attr)

    try:
        _rebuild_group(group, ns)
    except Exception:
        for model, snapshot in snapshots.items():
            _restore_model(model, snapshot)
        if raise_errors:
            raise
        return False
    return True


def _rebuild_group(group: list[type[BaseModel]], ns: dict[str, Any]) -> None:
    """Rebuild every model of the group, resolving intra-group references iteratively.

    Models whose dependencies (e.g. other group members) are not resolvable yet
    fail with `raise_errors=False` and are retried on the next pass. If a
    full pass completes without any model being rebuilt, the remaining models
    cannot be resolved with the shared namespace and the underlying error is raised.
    """
    pending = list(group)
    while pending:
        progressed = False
        for model in list(pending):
            if model.model_rebuild(force=True, raise_errors=False, _types_namespace=ns):
                pending.remove(model)
                progressed = True
        if not progressed:
            # Surface the underlying resolution error (e.g.
            # `PydanticUndefinedAnnotation`) by rebuilding one of the
            # remaining models with errors raised:
            pending[0].model_rebuild(force=True, raise_errors=True, _types_namespace=ns)
            # If no error was raised but the rebuild still reported a failure (e.g. an
            # invalid core schema), raise a generic error:
            raise PydanticUserError(f'Failed to rebuild models: {[model.__name__ for model in pending]}')


def _snapshot_model(model: type[BaseModel]) -> dict[str, Any]:
    return {attr: model.__dict__[attr] for attr in _SNAPSHOT_ATTRS if attr in model.__dict__}


def _restore_model(model: type[BaseModel], snapshot: dict[str, Any]) -> None:
    for attr in _SNAPSHOT_ATTRS:
        if attr in snapshot:
            setattr(model, attr, snapshot[attr])
        elif attr in model.__dict__:
            delattr(model, attr)
