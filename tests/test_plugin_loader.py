import importlib.metadata as importlib_metadata
import threading
from unittest.mock import patch

import pytest

from pydantic import BaseModel
import pydantic.plugin._loader as loader


class EntryPoint:
    def __init__(self, name, value, group, load_impl=None):
        self.name = name
        self.value = value
        self.group = group
        # `load_impl` lets a test drive dynamic behaviour (raising, counting, barriers);
        # when omitted the entry point resolves to the static `value`, matching the old helper.
        self._load_impl = load_impl
        self.load_count = 0

    def load(self):
        self.load_count += 1
        if self._load_impl is not None:
            return self._load_impl()
        return self.value


class Dist:
    entry_points = []

    def __init__(self, entry_points):
        self.entry_points = entry_points


@pytest.fixture
def reset_plugins():
    global loader
    initial_plugins = loader._plugins
    initial_loading = loader._loading_in_progress
    loader._plugins = None
    loader._loading_in_progress = False
    # the thread-local guard must not leak a loading state into or out of a test
    loader._loading_plugins.value = False
    yield
    # teardown
    loader._plugins = initial_plugins
    loader._loading_in_progress = initial_loading
    loader._loading_plugins.value = False


@pytest.fixture(autouse=True)
def mock():
    mock_entry_1 = EntryPoint(name='test_plugin1', value='test_plugin:plugin1', group='pydantic')
    mock_entry_2 = EntryPoint(name='test_plugin2', value='test_plugin:plugin2', group='pydantic')
    mock_entry_3 = EntryPoint(name='test_plugin3', value='test_plugin:plugin3', group='pydantic')
    mock_dist = Dist([mock_entry_1, mock_entry_2, mock_entry_3])

    with patch.object(importlib_metadata, 'distributions', return_value=[mock_dist]):
        yield


def distributions_with(entry_points):
    return patch.object(importlib_metadata, 'distributions', return_value=[Dist(entry_points)])


def test_loader(reset_plugins):
    res = loader.get_plugins()
    assert list(res) == ['test_plugin:plugin1', 'test_plugin:plugin2', 'test_plugin:plugin3']


def test_disable_all(reset_plugins, monkeypatch):
    monkeypatch.setenv('PYDANTIC_DISABLE_PLUGINS', '__all__')
    res = loader.get_plugins()
    assert res == ()


def test_disable_all_1(reset_plugins, monkeypatch):
    monkeypatch.setenv('PYDANTIC_DISABLE_PLUGINS', '1')
    res = loader.get_plugins()
    assert res == ()


def test_disable_true(reset_plugins, monkeypatch):
    monkeypatch.setenv('PYDANTIC_DISABLE_PLUGINS', 'true')
    res = loader.get_plugins()
    assert res == ()


def test_disable_one(reset_plugins, monkeypatch):
    monkeypatch.setenv('PYDANTIC_DISABLE_PLUGINS', 'test_plugin1')
    res = loader.get_plugins()
    assert len(list(res)) == 2
    assert 'test_plugin:plugin1' not in list(res)


def test_disable_multiple(reset_plugins, monkeypatch):
    monkeypatch.setenv('PYDANTIC_DISABLE_PLUGINS', 'test_plugin1,test_plugin2')
    res = loader.get_plugins()
    assert len(list(res)) == 1
    assert 'test_plugin:plugin1' not in list(res)
    assert 'test_plugin:plugin2' not in list(res)


class RecordingHandler:
    def __init__(self, log, label):
        self._log = log
        self._label = label

    def on_enter(self, *args, **kwargs):
        self._log.append(f'{self._label}:enter')

    def on_success(self, result):
        self._log.append(f'{self._label}:success')


class RecordingPlugin:
    def __init__(self, log, label):
        self._log = log
        self._label = label

    def new_schema_validator(self, schema, schema_type, schema_type_path, schema_kind, config, plugin_settings):
        return RecordingHandler(self._log, self._label), None, None


def _entry_points_with_failure(plugin_log):
    """Two well-behaved plugins around one plugin whose import raises RuntimeError."""
    plugin1 = RecordingPlugin(plugin_log, 'plugin1')
    plugin3 = RecordingPlugin(plugin_log, 'plugin3')

    def boom():
        raise RuntimeError('second plugin exploded during import')

    return [
        EntryPoint('test_plugin1', 'test_plugin:plugin1', 'pydantic', load_impl=lambda: plugin1),
        EntryPoint('test_plugin2', 'test_plugin:plugin2', 'pydantic', load_impl=boom),
        EntryPoint('test_plugin3', 'test_plugin:plugin3', 'pydantic', load_impl=lambda: plugin3),
    ]


def test_runtime_error_is_not_cached_and_retries(reset_plugins):
    plugin_log: list[str] = []
    entry_points = _entry_points_with_failure(plugin_log)

    with distributions_with(entry_points):
        # The first modelling attempt must surface the failure rather than silently dropping the
        # broken audit plugin and continuing with a half-populated registry.
        with pytest.raises(RuntimeError, match='second plugin exploded'):
            loader.get_plugins()
        assert loader._plugins is None, 'a failed discovery must not publish a partial registry'

        # The next call retries discovery from scratch instead of reusing partial state.
        with pytest.raises(RuntimeError, match='second plugin exploded'):
            loader.get_plugins()
        assert loader._plugins is None

    # The first two entry points are retried from the beginning; the third is never reached because
    # the second one aborts discovery. This proves nothing half-built was cached and reused.
    assert [ep.load_count for ep in entry_points] == [2, 2, 0]
    assert plugin_log == []


def test_discovery_retries_until_success_then_publishes_full_registry(reset_plugins):
    plugin_log: list[str] = []
    plugin1 = RecordingPlugin(plugin_log, 'plugin1')
    plugin2 = RecordingPlugin(plugin_log, 'plugin2')

    state = {'failures_remaining': 1}

    def flaky_load():
        if state['failures_remaining'] > 0:
            state['failures_remaining'] -= 1
            raise RuntimeError('not yet')
        return plugin2

    flaky_entry = EntryPoint('test_plugin2', 'test_plugin:plugin2', 'pydantic', load_impl=flaky_load)
    entry_points = [
        EntryPoint('test_plugin1', 'test_plugin:plugin1', 'pydantic', load_impl=lambda: plugin1),
        flaky_entry,
    ]

    with distributions_with(entry_points):
        with pytest.raises(RuntimeError, match='not yet'):
            loader.get_plugins()
        assert loader._plugins is None

        # Once the environment recovers, the retry completes and publishes both plugins.
        plugins = list(loader.get_plugins())
        assert plugins == [plugin1, plugin2]
        assert loader._plugins == {'test_plugin:plugin1': plugin1, 'test_plugin:plugin2': plugin2}


def test_import_and_attribute_errors_are_skipped_but_other_errors_propagate(reset_plugins):
    plugin_log: list[str] = []
    good_plugin = RecordingPlugin(plugin_log, 'good')

    def import_error():
        raise ImportError('missing dependency')

    def attribute_error():
        raise AttributeError('plugin object missing')

    entry_points = [
        EntryPoint('good', 'test_plugin:good', 'pydantic', load_impl=lambda: good_plugin),
        EntryPoint('bad_import', 'test_plugin:bad_import', 'pydantic', load_impl=import_error),
        EntryPoint('bad_attr', 'test_plugin:bad_attr', 'pydantic', load_impl=attribute_error),
    ]

    with distributions_with(entry_points):
        with pytest.warns(UserWarning) as warnings_record:
            plugins = list(loader.get_plugins())

    # The two historically tolerated failure modes are skipped, the healthy plugin is kept.
    assert plugins == [good_plugin]
    assert loader._plugins == {'test_plugin:good': good_plugin}
    messages = [str(w.message) for w in warnings_record]
    assert any('ImportError' in m and 'bad_import' in m for m in messages)
    assert any('AttributeError' in m and 'bad_attr' in m for m in messages)

    # A fresh, different failure (ValueError, not ImportError/AttributeError) must propagate.
    def value_error():
        raise ValueError('not tolerated')

    with distributions_with(
        [EntryPoint('good', 'test_plugin:good', 'pydantic', load_impl=lambda: good_plugin),
         EntryPoint('bad_value', 'test_plugin:bad_value', 'pydantic', load_impl=value_error)]
    ):
        loader._plugins = None
        with pytest.raises(ValueError, match='not tolerated'):
            loader.get_plugins()
        assert loader._plugins is None


def test_reentrant_model_creation_while_importing_plugin(reset_plugins):
    """A plugin that builds a Pydantic model during its own import must not deadlock."""
    nested_log: list[str] = []
    reentrant_calls = []
    import_events = []

    class ReentrantPlugin:
        def new_schema_validator(self, schema, schema_type, schema_type_path, schema_kind, config, plugin_settings):
            return RecordingHandler(nested_log, 'reentrant'), None, None

    def load_reentrant_plugin():
        import_events.append('load:start')
        # Same-thread recursion: discovery is in progress on this thread. The guard must return no
        # plugins for this nested model instead of re-entering discovery (which would deadlock).
        nested = loader.get_plugins()
        reentrant_calls.append(list(nested))

        class ModelBuiltDuringImport(BaseModel):
            value: int

        ModelBuiltDuringImport(value=1)
        import_events.append('load:end')
        return ReentrantPlugin()

    outer_log: list[str] = []
    outer_plugin = RecordingPlugin(outer_log, 'outer')
    entry_points = [
        EntryPoint('reentrant', 'test_plugin:reentrant', 'pydantic', load_impl=load_reentrant_plugin),
        EntryPoint('outer', 'test_plugin:outer', 'pydantic', load_impl=lambda: outer_plugin),
    ]

    with distributions_with(entry_points):
        plugins = list(loader.get_plugins())

    assert reentrant_calls == [[]], 'the same-thread nested call gets an empty plugin set'
    assert import_events == ['load:start', 'load:end']
    assert len(plugins) == 2

    # After discovery completes, a brand new model actually runs the imported plugin callbacks.
    class AuditedModel(BaseModel):
        value: int

    AuditedModel(value=5)
    assert nested_log == ['reentrant:enter', 'reentrant:success']
    assert outer_log == ['outer:enter', 'outer:success']


def test_concurrent_model_creation_waits_and_uses_full_registry(reset_plugins):
    """A second thread modelling while discovery runs must wait, never get an empty plugin set."""
    plugin_log: list[str] = []
    plugin1 = RecordingPlugin(plugin_log, 'plugin1')
    plugin2 = RecordingPlugin(plugin_log, 'plugin2')

    # Both worker threads start together. The one that wins discovery parks inside the gated
    # import; the other must block on the condition variable (verified via `waiter_parked`) rather
    # than returning an empty plugin set because of a global loading flag.
    start_barrier = threading.Barrier(2)
    import_entered = threading.Event()
    waiter_parked = threading.Event()
    release_import = threading.Event()
    original_wait = loader._plugins_lock.wait

    def tracked_wait(*args, **kwargs):
        waiter_parked.set()
        return original_wait(*args, **kwargs)

    def slow_load_plugin1():
        import_entered.set()
        assert release_import.wait(timeout=5), 'timed out waiting to release the plugin import'
        return plugin1

    entry_points = [
        EntryPoint('test_plugin1', 'test_plugin:plugin1', 'pydantic', load_impl=slow_load_plugin1),
        EntryPoint('test_plugin2', 'test_plugin:plugin2', 'pydantic', load_impl=lambda: plugin2),
    ]

    outcomes: list[object] = []

    def worker():
        start_barrier.wait(timeout=5)
        with patch.object(loader._plugins_lock, 'wait', side_effect=tracked_wait):
            outcomes.append(list(loader.get_plugins()))

    with distributions_with(entry_points):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        # Discovery has begun and the second thread is parked on the condition variable.
        assert import_entered.wait(timeout=5)
        assert waiter_parked.wait(timeout=5)
        release_import.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive(), 'a waiting thread must be woken up, not left blocked'

    assert outcomes == [[plugin1, plugin2], [plugin1, plugin2]]
    assert entry_points[0].load_count == 1, 'discovery runs once despite concurrent callers'

    # Verify through real model validation that both plugins' callbacks actually fire, rather than
    # merely checking the length of the published registry. All handlers' `on_enter` run before
    # validation and all `on_success` after it, so compare as an order-insensitive multiset.
    class ConcurrentModel(BaseModel):
        value: int

    ConcurrentModel(value=1)
    assert sorted(plugin_log) == sorted(
        [
            'plugin1:enter',
            'plugin1:success',
            'plugin2:enter',
            'plugin2:success',
        ]
    )
    assert set(plugin_log) == {
        'plugin1:enter',
        'plugin1:success',
        'plugin2:enter',
        'plugin2:success',
    }


def test_waiting_thread_retries_after_failed_discovery(reset_plugins):
    """When discovery fails, a woken waiter retries instead of reusing the absent partial cache."""
    state = {'failures_remaining': 1}
    start_barrier = threading.Barrier(2)
    import_entered = threading.Event()
    waiter_parked = threading.Event()
    release_import = threading.Event()
    original_wait = loader._plugins_lock.wait

    def tracked_wait(*args, **kwargs):
        waiter_parked.set()
        return original_wait(*args, **kwargs)

    def flaky_load():
        if state['failures_remaining'] > 0:
            state['failures_remaining'] -= 1
            raise RuntimeError('transient discovery failure')
        return RecordingPlugin([], 'recovered')

    def gated_load():
        import_entered.set()
        assert release_import.wait(timeout=5), 'timed out waiting to release the plugin import'
        return flaky_load()

    entry_points = [EntryPoint('flaky', 'test_plugin:flaky', 'pydantic', load_impl=gated_load)]
    outcomes: list[dict[str, object]] = []

    def worker():
        start_barrier.wait(timeout=5)
        outcome: dict[str, object] = {}
        try:
            with patch.object(loader._plugins_lock, 'wait', side_effect=tracked_wait):
                outcome['plugins'] = list(loader.get_plugins())
        except BaseException as exc:  # noqa: BLE001 - assert the propagated failure below
            outcome['error'] = exc
        outcomes.append(outcome)

    with distributions_with(entry_points):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        # One thread discovers (and will fail once released), the other is parked on the condition.
        assert import_entered.wait(timeout=5)
        assert waiter_parked.wait(timeout=5)
        release_import.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive(), 'a waiting thread must be woken up, not left blocked'

    # One thread observed the failure; the woken waiter retried (the second attempt succeeds) and
    # published a complete registry, so the failure was never cached as a half-populated result.
    errors = [o['error'] for o in outcomes if 'error' in o]
    successes = [o['plugins'] for o in outcomes if 'plugins' in o]
    assert [str(e) for e in errors] == ['transient discovery failure']
    assert len(successes) == 1
    assert [type(p).__name__ for p in successes[0]] == ['RecordingPlugin']
    assert loader._plugins is not None
    assert list(loader._plugins) == ['test_plugin:flaky']


def test_interrupted_waiter_recovers_and_gets_full_registry(reset_plugins):
    """A waiter woken by an exception must terminate cleanly without leaking discovery state."""
    plugin_log: list[str] = []
    plugin = RecordingPlugin(plugin_log, 'plugin')

    import_entered = threading.Event()
    release_import = threading.Event()
    start_discoverer = threading.Event()
    interrupted_waiter_parked = threading.Event()
    original_wait = loader._plugins_lock.wait

    def interrupted_wait(*args, **kwargs):
        # Only the explicitly interrupted waiter uses this wrapper: its first wait raises, then it
        # re-enters the condition-variable loop and waits normally for discovery to complete.
        if not getattr(interrupted_wait, 'fired', False):
            interrupted_wait.fired = True  # type: ignore[attr-defined]
            interrupted_waiter_parked.set()
            raise KeyboardInterrupt()
        return original_wait(*args, **kwargs)

    def gated_load():
        import_entered.set()
        assert release_import.wait(timeout=5), 'timed out waiting to release the plugin import'
        return plugin

    entry_points = [EntryPoint('test_plugin', 'test_plugin:plugin', 'pydantic', load_impl=gated_load)]
    discoverer_outcome: dict[str, object] = {}
    interrupted_outcome: dict[str, object] = {}
    plain_outcome: dict[str, object] = {}

    def discoverer():
        start_discoverer.wait(timeout=5)
        discoverer_outcome['plugins'] = list(loader.get_plugins())

    def interrupted_waiter():
        # Block until the discoverer is inside the gated import, so this thread becomes a waiter.
        assert import_entered.wait(timeout=5)
        try:
            with patch.object(loader._plugins_lock, 'wait', side_effect=interrupted_wait):
                interrupted_outcome['plugins'] = list(loader.get_plugins())
        except BaseException as exc:  # noqa: BLE001 - the interruption itself is part of the test
            interrupted_outcome['interrupted_with'] = exc.__class__.__name__
            # Re-enter after the interruption: discovery must still complete and be reusable.
            interrupted_outcome['plugins_after_retry'] = list(loader.get_plugins())

    def plain_waiter():
        assert import_entered.wait(timeout=5)
        plain_outcome['plugins'] = list(loader.get_plugins())

    with distributions_with(entry_points):
        discoverer_thread = threading.Thread(target=discoverer)
        interrupted_thread = threading.Thread(target=interrupted_waiter)
        plain_thread = threading.Thread(target=plain_waiter)
        discoverer_thread.start()
        interrupted_thread.start()
        plain_thread.start()
        start_discoverer.set()
        # The discoverer parks in the gated import; the interrupted waiter reaches its first wait.
        assert interrupted_waiter_parked.wait(timeout=5)
        release_import.set()
        for thread in (discoverer_thread, interrupted_thread, plain_thread):
            thread.join(timeout=5)
            assert not thread.is_alive(), 'an interrupted waiter must still terminate'

    # No discovery state leaks and the complete registry is eventually published and reusable.
    assert loader._loading_in_progress is False
    assert loader._plugins is not None
    assert list(loader._plugins) == ['test_plugin:plugin']
    # The interrupted waiter really saw the interruption and recovered a full registry on re-entry.
    assert interrupted_outcome['interrupted_with'] == 'KeyboardInterrupt'
    assert interrupted_outcome['plugins_after_retry'] == [plugin]
    # The uninterrupted waiter and the discoverer both received the same complete registry.
    assert discoverer_outcome['plugins'] == [plugin]
    assert plain_outcome['plugins'] == [plugin]
