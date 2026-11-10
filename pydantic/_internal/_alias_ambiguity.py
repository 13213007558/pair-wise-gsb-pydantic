"""Opt-in detection of ambiguous inputs that populate one field through several valid entries.

A field can be populated from more than one input entry when it declares a field name,
an alias, `AliasChoices`, or an `AliasPath`, possibly produced by an alias generator. By
default Pydantic silently picks one of them. When `validate_alias_ambiguity` is enabled,
the presence of several *readable* entries for the same field is reported as a conflict,
even when their values are identical.

The check only considers entries that the current configuration allows reading: the
field name is a candidate only when `validate_by_name` is enabled, while aliases are
candidates only when `validate_by_alias` is enabled.
"""

from __future__ import annotations as _annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic_core import (
    ArgsKwargs,
    InitErrorDetails,
    PydanticCustomError,
    PydanticUndefined,
    ValidationError,
    core_schema,
)

from ..aliases import AliasChoices, AliasPath

__all__ = ('build_alias_ambiguity_check',)

_MISSING = PydanticUndefined


def _normalize_alias_paths(
    validation_alias: str | AliasPath | AliasChoices | None,
) -> list[tuple[str | int, ...]]:
    """Return the list of alias paths (each a tuple of keys) declared by a field."""
    if validation_alias is None:
        return []
    if isinstance(validation_alias, str):
        return [(validation_alias,)]
    if isinstance(validation_alias, AliasPath):
        return [tuple(validation_alias.convert_to_aliases())]
    if isinstance(validation_alias, AliasChoices):
        return [tuple(path) for path in validation_alias.convert_to_aliases()]
    return []


def _read_path(data: Any, path: tuple[str | int, ...], *, via_attributes: bool = False) -> Any:
    """Read `path` from `data`.

    Returns `PydanticUndefined` when any component of the path does not exist, so that a
    missing path can be distinguished from a path that exists and holds `None`. When
    `via_attributes` is set, non-mapping objects are traversed through attributes instead
    of subscripts (mirroring `from_attributes` validation); mappings always use keys.
    """
    current = data
    for key in path:
        if isinstance(current, str):
            return _MISSING
        if via_attributes and not isinstance(current, Mapping):
            if not isinstance(key, str):
                return _MISSING
            try:
                current = getattr(current, key)
            except AttributeError:
                return _MISSING
        else:
            try:
                current = current[key]
            except (KeyError, IndexError, TypeError):
                return _MISSING
    return current


def _read_attribute(data: Any, name: str) -> Any:
    """Read attribute `name` from an object, returning `PydanticUndefined` when absent."""
    try:
        return getattr(data, name)
    except AttributeError:
        return _MISSING


def _collect_present_entries(
    data: Any,
    *,
    name_entries: Sequence[tuple[str | int, ...]],
    alias_entries: Sequence[tuple[str | int, ...]],
    from_attributes: bool,
) -> list[tuple[str | int, ...]]:
    """Collect the distinct, currently readable input entries that target one field."""
    via_attributes = from_attributes and not isinstance(data, Mapping)
    present: list[tuple[str | int, ...]] = []
    for path in (*name_entries, *alias_entries):
        if path in present:
            continue
        if via_attributes and len(path) == 1 and isinstance(path[0], str):
            value = _read_attribute(data, path[0])
        else:
            value = _read_path(data, path, via_attributes=via_attributes)
        if value is not _MISSING:
            present.append(path)
    return present


def build_alias_ambiguity_check(
    fields: dict[str, Any],
    *,
    model_name: str,
    validate_by_alias: bool,
    validate_by_name: bool,
    from_attributes: bool,
) -> Any | None:
    """Build a function that wraps a schema with an alias-ambiguity before-validator.

    Returns `None` when no field can be reached through more than one readable entry, in
    which case ambiguity is impossible and no validator needs to run.

    Args:
        fields: A mapping of field names to `FieldInfo` instances.
        model_name: The name of the validated model, used to title the error.
        validate_by_alias: Whether alias entries (including `AliasChoices`/`AliasPath`)
            are readable under the current configuration.
        validate_by_name: Whether the plain field name is readable.
        from_attributes: Whether non-mapping objects are read through attributes.
    """
    field_entries: dict[str, tuple[tuple[str | int, ...], tuple[str | int, ...]]] = {}
    for field_name, field_info in fields.items():
        name_entries: tuple[tuple[str | int, ...], ...] = ()
        if validate_by_name:
            name_entries = ((field_name,),)

        alias_entries: list[tuple[str | int, ...]] = []
        if validate_by_alias:
            for path in _normalize_alias_paths(field_info.validation_alias):
                if path not in alias_entries and path not in name_entries:
                    alias_entries.append(path)

        if len(name_entries) + len(alias_entries) < 2:
            continue

        field_entries[field_name] = (tuple(name_entries), tuple(alias_entries))

    if not field_entries:
        return None

    def check_alias_ambiguity(data: Any) -> Any:
        if data is None:
            return data
        # Dataclasses receive constructor arguments wrapped in `ArgsKwargs`. Positional
        # arguments cannot be inspected by alias name, so only keyword arguments are checked.
        if isinstance(data, ArgsKwargs):
            payload = data.kwargs
            if not isinstance(payload, Mapping):
                return data
        else:
            payload = data

        is_mapping = isinstance(payload, Mapping)
        object_like = from_attributes and not is_mapping
        if not is_mapping and not object_like:
            return data

        conflicts: list[InitErrorDetails] = []
        for field_name, (name_entries, alias_entries) in field_entries.items():
            present = _collect_present_entries(
                payload,
                name_entries=name_entries,
                alias_entries=alias_entries,
                from_attributes=from_attributes,
            )
            if len(present) > 1:
                rendered = ' and '.join(_render_path(path) for path in present)
                custom_error = PydanticCustomError(
                    'alias_ambiguity',
                    'Field {field} is provided through multiple input entries: {paths}',
                    {'field': field_name, 'paths': rendered},
                )
                # Build each error explicitly with a redacted input so that account
                # credentials present in the payload are never echoed back.
                conflicts.append(InitErrorDetails(type=custom_error, input=None))

        if conflicts:
            raise ValidationError.from_exception_data(model_name, conflicts)
        return data

    def wrap(schema: core_schema.CoreSchema) -> core_schema.CoreSchema:
        return core_schema.no_info_before_validator_function(check_alias_ambiguity, schema)

    return wrap


def _render_path(path: tuple[str | int, ...]) -> str:
    """Render an input entry path without echoing the value stored at it."""
    if len(path) == 1:
        return repr(path[0])
    return ''.join(f'[{key!r}]' if isinstance(key, str) else f'[{key}]' for key in path)
