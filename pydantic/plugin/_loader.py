from __future__ import annotations

import importlib.metadata as importlib_metadata
import os
import threading
import warnings
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from . import PydanticPluginProtocol


PYDANTIC_ENTRY_POINT_GROUP: Final[str] = 'pydantic'

# Published registry of plugins. It is only ever assigned a *complete* result of discovery:
# either the full plugin mapping, or an empty mapping when plugins are disabled / none are
# installed. It stays `None` while discovery has not run or the last discovery raised, so a
# half-finished result is never cached and the next call retries discovery from scratch.
_plugins: dict[str, PydanticPluginProtocol] | None = None
# Cross-thread guard: `True` while some thread is inside discovery. Only ever read or written
# while holding `_plugins_lock`, so a second thread waits on the condition variable instead of
# starting a concurrent (duplicate) discovery or observing an empty plugin set.
_loading_in_progress: bool = False
# Condition variable serialising discovery and parking concurrent callers until discovery
# finishes, fails, or the waiting thread is interrupted.
_plugins_lock: threading.Condition = threading.Condition()
# Per-thread recursion guard. A plugin may itself create Pydantic models while it is being
# imported; that re-entrant call on the *same* thread gets no plugins (the registry cannot be
# complete yet), while other threads block on `_plugins_lock` rather than seeing this flag.
_loading_plugins: threading.local = threading.local()


def _is_reentrant_call() -> bool:
    return getattr(_loading_plugins, 'value', False)


def get_plugins() -> Iterable[PydanticPluginProtocol]:
    """Load plugins for Pydantic.

    Inspired by: https://github.com/pytest-dev/pluggy/blob/1.3.0/src/pluggy/_manager.py#L376-L402
    """
    global _plugins, _loading_in_progress
    disabled_plugins = os.getenv('PYDANTIC_DISABLE_PLUGINS')
    if _is_reentrant_call():
        # Same-thread re-entry while importing a plugin (the plugin itself builds a Pydantic
        # model): the registry cannot be complete yet, so run without plugins. Checked before
        # touching the lock so the re-entrant thread never blocks on a lock it already owns.
        return ()
    elif disabled_plugins in ('__all__', '1', 'true'):
        return ()

    with _plugins_lock:
        while _plugins is None:
            if _loading_in_progress:
                # Another thread owns discovery: wait for it to publish a complete result or to
                # fail. A failure leaves `_plugins` as `None`, so the loop retries discovery.
                _plugins_lock.wait()
                continue

            # This thread owns discovery. Publish the cross-thread in-progress state, then release
            # the lock while importing plugins so waiters block here instead of seeing an empty set.
            _loading_in_progress = True
            _loading_plugins.value = True
            discovered_plugins: dict[str, PydanticPluginProtocol] | None = None
            try:
                _plugins_lock.release()
                discovered_plugins = _discover_plugins(disabled_plugins)
            finally:
                _plugins_lock.acquire()
                _loading_plugins.value = False
                _loading_in_progress = False
                if discovered_plugins is not None:
                    # Discovery completed without an unhandled error: publish the complete registry.
                    _plugins = discovered_plugins
                # On an unhandled exception `_plugins` stays `None`, so the next iteration retries
                # discovery from scratch rather than caching a half-populated registry.
                # Wake every waiter on success, failure, or interruption so none stays blocked.
                _plugins_lock.notify_all()

        return tuple(_plugins.values())


def _discover_plugins(disabled_plugins: str | None) -> dict[str, PydanticPluginProtocol]:
    # Build into a local mapping so an exception never leaves partial state behind; nothing is
    # published to the module-level `_plugins` until discovery fully completes.
    discovered_plugins: dict[str, PydanticPluginProtocol] = {}
    for dist in importlib_metadata.distributions():
        for entry_point in dist.entry_points:
            if entry_point.group != PYDANTIC_ENTRY_POINT_GROUP:
                continue
            if entry_point.value in discovered_plugins:
                continue
            if disabled_plugins is not None and entry_point.name in disabled_plugins.split(','):
                continue
            try:
                discovered_plugins[entry_point.value] = entry_point.load()
            except (ImportError, AttributeError) as e:
                # Only these two historically tolerated failures are skipped with a warning; any
                # other exception propagates and aborts the whole discovery so `_plugins` stays
                # unset and the next call retries instead of using a half-populated registry.
                warnings.warn(
                    f'{e.__class__.__name__} while loading the `{entry_point.name}` Pydantic plugin, '
                    f'this plugin will not be installed.\n\n{e!r}',
                    stacklevel=2,
                )
    return discovered_plugins
