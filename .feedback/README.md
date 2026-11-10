# Feedback task starting environment

Upstream: https://github.com/pydantic/pydantic
Upstream commit: `1a8850d101e67d2744ba8c6286e1172d7cd89d0b`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= -o 'markers=thread_unsafe: single-thread test' tests/test_serialize_as_any.py tests/test_plugins.py tests/test_aliases.py tests/test_type_adapter.py
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-10): `353 passed, 32 skipped, 1 xfailed in 1.04s`
Whole-command wall time: 1.948 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
