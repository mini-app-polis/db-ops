"""db-poller: what it reads, what it publishes, and what a fault does.

Postgres and CloudWatch are replaced at their seams (the connection and the
CloudWatch client), so these run with no database, no AWS and no secrets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from db_ops import poller
from db_ops.poller import QUERIES, PollerError, Settings

NOW = datetime(2026, 10, 15, 19, 30, tzinfo=UTC)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "db_poller_database_url_kaianolevine": "postgresql://metrics@a/railway",
        "db_poller_database_url_deejaytools": "postgresql://metrics@b/railway",
        "db_poller_aws_access_key_id": "AKIATEST",
        "db_poller_aws_secret_access_key": "secret",
        "db_poller_run_timeout_seconds": 60.0,
    }
    values.update(overrides)
    return Settings(**values)


class FakeResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class FakeConn:
    """Answers each known query with a value; ``fail`` names queries that raise."""

    def __init__(
        self, values: dict[str, Any] | None = None, fail: set[str] | None = None
    ) -> None:
        self.values = values or {}
        self.fail = fail or set()

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str) -> FakeResult:
        name = next(n for n, _, q in QUERIES if q == query)
        if name in self.fail:
            raise psycopg.errors.UndefinedTable(f"{name} unavailable")
        return FakeResult((self.values.get(name, 1),))


class FakeReport:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str | None]] = []
        self.issues: list[tuple[str, str | None]] = []
        self.processed = 0
        self.counters: dict[str, Any] = {}
        self.sent: list[bool] = []

    def note(self, reason: str, item: str | None = None) -> None:
        self.notes.append((reason, item))

    def issue(
        self, reason: str, item: str | None = None, *, detail: str | None = None
    ) -> None:
        self.issues.append((reason, item))

    def ok(self, n: int = 1) -> None:
        self.processed += n

    def count(self, key: str, value: Any) -> None:
        self.counters[key] = value

    def send(self, *, notable: bool = False) -> None:
        self.sent.append(notable)


class FakeCloudWatch:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def put_metric_data(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.calls.append(kwargs)


@pytest.fixture
def cloudwatch(monkeypatch: pytest.MonkeyPatch) -> FakeCloudWatch:
    client = FakeCloudWatch()
    monkeypatch.setattr(poller, "_cloudwatch_client", lambda settings: client)
    return client


# ── Reading ──────────────────────────────────────────────────────────────


def test_every_metric_is_read_with_its_unit_and_dimension() -> None:
    data = poller.collect(FakeConn(), "deejaytools", NOW, FakeReport())  # type: ignore[arg-type]

    assert [d["MetricName"] for d in data] == [name for name, _, _ in QUERIES]
    assert all(
        d["Dimensions"] == [{"Name": "Database", "Value": "deejaytools"}] for d in data
    )
    units = {d["MetricName"]: d["Unit"] for d in data}
    assert units["DatabaseSize"] == "Bytes"
    assert units["ConnectionUtilization"] == "Percent"
    assert units["StatementExecTime"] == "Milliseconds"


def test_a_null_value_is_left_out_not_published_as_zero() -> None:
    conn = FakeConn({"CacheHitRatio": None, "ArchiveAge": None})
    data = poller.collect(conn, "kaianolevine", NOW, FakeReport())  # type: ignore[arg-type]

    names = {d["MetricName"] for d in data}
    assert "CacheHitRatio" not in names
    assert "ArchiveAge" not in names
    assert "DatabaseSize" in names


def test_one_unreadable_metric_does_not_cost_the_rest() -> None:
    report = FakeReport()
    conn = FakeConn(fail={"StatementCalls", "StatementExecTime"})

    data = poller.collect(conn, "kaianolevine", NOW, report)  # type: ignore[arg-type]

    assert len(data) == len(QUERIES) - 2
    assert ("metric_unreadable", "kaianolevine/StatementCalls") in report.notes


# ── Publishing ───────────────────────────────────────────────────────────


def test_each_database_is_one_put_under_the_namespace(
    cloudwatch: FakeCloudWatch,
) -> None:
    report = FakeReport()
    published = poller.poll(
        _settings(),
        report,  # type: ignore[arg-type]
        NOW,
        connect=lambda url, settings: FakeConn(),
    )

    assert published == {"kaianolevine": len(QUERIES), "deejaytools": len(QUERIES)}
    assert [c["Namespace"] for c in cloudwatch.calls] == [poller.NAMESPACE] * 2
    assert report.processed == 2 and report.issues == []


def test_an_unreachable_database_publishes_nothing_and_does_not_fail_the_run(
    cloudwatch: FakeCloudWatch,
) -> None:
    def connect(url: str, settings: Settings) -> FakeConn:
        if url.endswith("@a/railway"):
            raise psycopg.OperationalError("connection refused")
        return FakeConn()

    report = FakeReport()
    published = poller.poll(_settings(), report, NOW, connect=connect)  # type: ignore[arg-type]

    assert published == {"deejaytools": len(QUERIES)}
    assert report.notes == [("unreachable", "kaianolevine")]
    assert report.issues == []


def test_a_failed_publish_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        poller, "_cloudwatch_client", lambda settings: FakeCloudWatch(fail=True)
    )
    report = FakeReport()

    with pytest.raises(PollerError, match="kaianolevine, deejaytools"):
        poller.poll(
            _settings(),
            report,  # type: ignore[arg-type]
            NOW,
            connect=lambda url, settings: FakeConn(),
        )
    assert {reason for reason, _ in report.issues} == {"publish_failed"}


def test_an_exhausted_budget_stops_the_run(cloudwatch: FakeCloudWatch) -> None:
    report = FakeReport()
    with pytest.raises(PollerError):
        poller.poll(
            _settings(db_poller_run_timeout_seconds=0),
            report,  # type: ignore[arg-type]
            NOW,
            connect=lambda url, settings: FakeConn(),
        )
    assert cloudwatch.calls == []


def test_connections_carry_their_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_connect(url: str, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(poller.psycopg, "connect", fake_connect)
    poller._connect("postgresql://x", _settings(db_poller_db_statement_timeout_ms=1234))

    assert seen["connect_timeout"] == 10
    assert seen["options"] == "-c statement_timeout=1234"
    assert seen["autocommit"] is True


# ── Gating and configuration ─────────────────────────────────────────────


def test_outside_production_nothing_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("CLOUDWATCH_METRICS_ENABLED", raising=False)
    assert poller.work("run-1") == {"service": "db-poller", "skipped": True}


def test_missing_configuration_names_the_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    with pytest.raises(PollerError) as caught:
        Settings.from_env()
    assert "DB_POLLER_DATABASE_URL_KAIANOLEVINE" in str(caught.value)
    assert "DB_POLLER_AWS_ACCESS_KEY_ID" in str(caught.value)


def test_a_failed_poll_sends_its_report_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poller, "effect_enabled", lambda effect: True)
    monkeypatch.setattr(
        poller.Settings, "from_env", classmethod(lambda cls: _settings())
    )
    reports: list[FakeReport] = []

    def _report(**kwargs: Any) -> FakeReport:
        reports.append(FakeReport())
        return reports[-1]

    def _boom(*args: Any, **kwargs: Any) -> dict[str, int]:
        raise PollerError("metrics not published for: kaianolevine")

    monkeypatch.setattr(poller, "RunReport", _report)
    monkeypatch.setattr(poller, "poll", _boom)

    with pytest.raises(PollerError):
        poller.work("run-1")
    assert reports[0].sent == [True]


def test_a_clean_poll_sends_an_unremarkable_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poller, "effect_enabled", lambda effect: True)
    monkeypatch.setattr(
        poller.Settings, "from_env", classmethod(lambda cls: _settings())
    )
    reports: list[FakeReport] = []

    def _report(**kwargs: Any) -> FakeReport:
        reports.append(FakeReport())
        return reports[-1]

    monkeypatch.setattr(poller, "RunReport", _report)
    monkeypatch.setattr(poller, "poll", lambda *a, **k: {"kaianolevine": 10})

    result = poller.work("run-1")
    assert result["published"] == {"kaianolevine": 10}
    assert reports[0].sent == [False]
