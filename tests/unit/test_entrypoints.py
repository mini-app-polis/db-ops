"""The three entry points, and the shell they share.

What matters at this stage is the contract with Railway: each service has
its own module, a run that finishes exits 0, and a run that raises exits 1
and reaches Sentry instead of disappearing.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from db_ops import _entry

SERVICES = [
    ("db_ops.poller", "db-poller"),
    ("db_ops.backup", "db-backup"),
    ("db_ops.drill", "db-drill"),
]


@pytest.fixture(autouse=True)
def _no_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_entry.SENTRY_DSN_VAR, raising=False)


@pytest.mark.parametrize(("module", "service"), SERVICES)
def test_each_stub_names_its_own_service_and_exits_zero(
    module: str, service: str
) -> None:
    mod = importlib.import_module(module)
    assert mod.SERVICE == service
    assert mod.main() == 0


STUBS = [s for s in SERVICES if s[0] != "db_ops.backup"]


@pytest.mark.parametrize(("module", "service"), STUBS)
def test_stub_work_reports_itself_as_a_stub(module: str, service: str) -> None:
    mod = importlib.import_module(module)
    assert mod.work("run-1") == {"service": service, "stub": True}


def test_a_run_that_raises_exits_one_and_is_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[BaseException] = []
    monkeypatch.setattr(_entry.sentry_sdk, "capture_exception", captured.append)

    def boom(run_id: str) -> dict[str, Any]:
        raise RuntimeError("database unreachable")

    assert _entry.run("db-test", boom) == 1
    assert len(captured) == 1
    assert str(captured[0]) == "database unreachable"


def test_the_run_id_reaches_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def record(run_id: str) -> dict[str, Any]:
        seen.append(run_id)
        return {}

    assert _entry.run("db-test", record) == 0
    assert len(seen) == 1 and len(seen[0]) == 36


def test_sentry_initialises_only_when_its_own_dsn_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    tags: list[tuple[str, str]] = []

    class _Scope:
        def set_tag(self, key: str, value: str) -> None:
            tags.append((key, value))

    monkeypatch.setattr(_entry.sentry_sdk, "init", lambda **kw: calls.append(kw))
    monkeypatch.setattr(_entry.sentry_sdk, "get_global_scope", lambda: _Scope())

    _entry._init_sentry("db-test")
    assert calls == []

    monkeypatch.setenv(_entry.SENTRY_DSN_VAR, "https://key@sentry.example/1")
    _entry._init_sentry("db-test")
    assert calls[0]["dsn"] == "https://key@sentry.example/1"
    assert tags == [("service", "db-test")]
