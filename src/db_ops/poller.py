"""db-poller — both production databases' Postgres metrics to CloudWatch.

Every five minutes (Railway's shortest cron interval), for each database in
:attr:`Settings.database_urls`, over Railway private networking as the
``metrics`` role (``pg_monitor`` and nothing else):

* ``Connections`` and ``ConnectionUtilization`` (against ``max_connections``)
* ``DatabaseSize``
* ``CacheHitRatio``
* ``OldestTransactionAge``
* ``StatementCalls`` and ``StatementExecTime`` — cumulative totals from
  ``pg_stat_statements``. A cron run keeps no state, so the mean query time is
  derived in CloudWatch with metric math, ``DIFF(time) / DIFF(calls)`` (MINI-61).
* ``ArchiveFailedCount`` (cumulative), ``ArchiveAge`` and ``WalDirSize`` — the
  health of point-in-time recovery's WAL archive (MINI-59). A stalled archive
  spools WAL on the database's own volume; without these, PITR coverage is
  lost silently and found out at restore time.

Published under ``MiniAppPolis/Postgres`` with a ``Database`` dimension, one
``PutMetricData`` per database. A database that cannot be reached publishes
nothing for itself, which is what the ``postgres-<db>-unreachable`` alarm
watches for; the other still reports. One metric that cannot be read is
skipped and the rest are published.

Every connection carries a connect timeout and a statement timeout, and the
run has an overall budget: Railway skips a scheduled start while the previous
run is still going, so one hung query would stop all polling.

Production only, through ``Effect.CLOUDWATCH_METRICS``.

    python -m db_ops.poller
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import boto3
import psycopg
from botocore.config import Config
from mini_app_polis import logger as log
from mini_app_polis.environment import Effect, effect_enabled
from mini_app_polis.pipeline_status import RunReport
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from db_ops._entry import run

SERVICE = "db-poller"
REPO = "db-ops"
NAMESPACE = "MiniAppPolis/Postgres"

LOG = log.get_logger()


class PollerError(RuntimeError):
    """A poll that could not deliver its metrics. Safe to log."""


class Settings(BaseSettings):
    """Everything a run reads from the environment, read once.

    Field names are the variable names, lowercased. No ``.env`` file is read.
    """

    model_config = SettingsConfigDict(extra="ignore")

    db_poller_database_url_kaianolevine: SecretStr
    db_poller_database_url_deejaytools: SecretStr
    db_poller_aws_access_key_id: SecretStr
    db_poller_aws_secret_access_key: SecretStr
    aws_region: str = "us-east-1"
    #: Read by mini_app_polis's KaianoApiClient when a run report is sent.
    db_ops_api_key: SecretStr | None = None

    #: The whole run's budget, well inside the five-minute schedule.
    run_timeout_seconds: float = 120
    db_connect_timeout_seconds: int = 10
    db_statement_timeout_ms: int = 5000
    cloudwatch_timeout_seconds: int = 10

    @property
    def database_urls(self) -> dict[str, str]:
        """Database name in the Database dimension -> its connection string."""
        return {
            "kaianolevine": self.db_poller_database_url_kaianolevine.get_secret_value(),
            "deejaytools": self.db_poller_database_url_deejaytools.get_secret_value(),
        }

    @classmethod
    def from_env(cls) -> Settings:
        """Read and validate the settings, naming every missing variable."""
        try:
            # pydantic-settings fills the required fields from the
            # environment, which mypy cannot see.
            return cls()  # type: ignore[call-arg]
        except ValidationError as exc:
            names = sorted(
                str(err["loc"][0]).upper() for err in exc.errors() if err["loc"]
            )
            raise PollerError(
                f"invalid or missing configuration: {', '.join(names)}"
            ) from None


class Connection(Protocol):
    """The slice of a psycopg connection this module uses."""

    def execute(self, query: Any) -> Any:
        """Run one query and return its cursor, as psycopg does."""
        ...


#: (metric name, unit, query returning one numeric value or NULL).
#: NULL means "nothing to report this time" — no traffic yet, no archive —
#: and the metric is left out rather than published as zero.
QUERIES: list[tuple[str, str, str]] = [
    (
        "Connections",
        "Count",
        "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'",
    ),
    (
        "ConnectionUtilization",
        "Percent",
        "SELECT 100.0 * count(*) / current_setting('max_connections')::int "
        "FROM pg_stat_activity WHERE backend_type = 'client backend'",
    ),
    ("DatabaseSize", "Bytes", "SELECT pg_database_size(current_database())"),
    (
        "CacheHitRatio",
        "Percent",
        "SELECT 100.0 * sum(blks_hit) / nullif(sum(blks_hit) + sum(blks_read), 0) "
        "FROM pg_stat_database WHERE datname = current_database()",
    ),
    (
        "OldestTransactionAge",
        "Seconds",
        "SELECT coalesce(max(extract(epoch FROM now() - xact_start)), 0) "
        "FROM pg_stat_activity WHERE xact_start IS NOT NULL "
        "AND pid <> pg_backend_pid() AND backend_type = 'client backend'",
    ),
    (
        "StatementCalls",
        "Count",
        "SELECT coalesce(sum(calls), 0) FROM pg_stat_statements",
    ),
    (
        "StatementExecTime",
        "Milliseconds",
        "SELECT coalesce(sum(total_exec_time), 0) FROM pg_stat_statements",
    ),
    ("ArchiveFailedCount", "Count", "SELECT failed_count FROM pg_stat_archiver"),
    (
        "ArchiveAge",
        "Seconds",
        "SELECT extract(epoch FROM now() - last_archived_time) FROM pg_stat_archiver",
    ),
    ("WalDirSize", "Bytes", "SELECT coalesce(sum(size), 0) FROM pg_ls_waldir()"),
]


def collect(
    conn: Connection, database: str, now: datetime, report: RunReport
) -> list[dict[str, Any]]:
    """Read every metric for one database. A metric that fails is skipped."""
    data: list[dict[str, Any]] = []
    for name, unit, query in QUERIES:
        try:
            row = conn.execute(query).fetchone()
        except psycopg.Error as exc:
            # The connection is in autocommit, so one failed statement does
            # not poison the next. Counted, not flagged: a missing extension
            # is a setup question, and the gap shows on the dashboard.
            LOG.warning(
                "%s.metric_failed database=%s metric=%s err=%s",
                SERVICE,
                database,
                name,
                str(exc).strip()[:200],
            )
            report.note("metric_unreadable", f"{database}/{name}")
            continue
        value = row[0] if row else None
        if value is None:
            continue
        data.append(
            {
                "MetricName": name,
                "Dimensions": [{"Name": "Database", "Value": database}],
                "Timestamp": now,
                "Value": float(value),
                "Unit": unit,
            }
        )
    return data


def _connect(url: str, settings: Settings) -> psycopg.Connection[Any]:
    return psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=settings.db_connect_timeout_seconds,
        application_name=SERVICE,
        options=f"-c statement_timeout={settings.db_statement_timeout_ms}",
    )


def _cloudwatch_client(settings: Settings) -> Any:
    return boto3.client(
        "cloudwatch",
        region_name=settings.aws_region,
        aws_access_key_id=settings.db_poller_aws_access_key_id.get_secret_value(),
        aws_secret_access_key=(
            settings.db_poller_aws_secret_access_key.get_secret_value()
        ),
        # Retried by botocore's standard mode (PIPE-007); bounded so a slow
        # CloudWatch cannot outlast the run's budget.
        config=Config(
            connect_timeout=settings.cloudwatch_timeout_seconds,
            read_timeout=settings.cloudwatch_timeout_seconds,
            retries={"mode": "standard", "max_attempts": 3},
        ),
    )


def poll(
    settings: Settings,
    report: RunReport,
    now: datetime,
    *,
    connect: Callable[[str, Settings], Any] = _connect,
) -> dict[str, int]:
    """Collect and publish each database's metrics. Returns metrics per database.

    Raises :class:`PollerError` if a database's metrics were read but could
    not be published: that is this service broken, not a database down.
    """
    deadline = time.monotonic() + settings.run_timeout_seconds
    client = _cloudwatch_client(settings)
    published: dict[str, int] = {}
    failed: list[str] = []

    for database, url in settings.database_urls.items():
        if time.monotonic() >= deadline:
            report.issue("out_of_time", database)
            failed.append(database)
            continue
        try:
            with connect(url, settings) as conn:
                data = collect(conn, database, now, report)
        except (psycopg.Error, OSError) as exc:
            # Publish nothing for it: the absence is what the unreachable
            # alarm fires on. A note rather than an issue, so a database that is
            # down does not also send a message every five minutes.
            LOG.warning(
                "%s.unreachable database=%s err=%s",
                SERVICE,
                database,
                str(exc).strip()[:200],
            )
            report.note("unreachable", database)
            continue
        if not data:
            continue
        try:
            client.put_metric_data(Namespace=NAMESPACE, MetricData=data)
        except Exception as exc:  # noqa: BLE001 — recorded, raised below
            report.issue("publish_failed", database, detail=str(exc)[:300])
            LOG.error("%s.publish_failed database=%s err=%s", SERVICE, database, exc)
            failed.append(database)
            continue
        published[database] = len(data)
        report.ok()
        report.count(f"{database}_metrics", len(data))

    if failed:
        raise PollerError(f"metrics not published for: {', '.join(failed)}")
    return published


def work(run_id: str) -> dict[str, Any]:
    """Poll both databases once and return how many metrics each published."""
    if not effect_enabled(Effect.CLOUDWATCH_METRICS):
        LOG.info(
            "%s.skipped reason=not_production hint=set "
            "CLOUDWATCH_METRICS_ENABLED=true for a deliberate test",
            SERVICE,
        )
        return {"service": SERVICE, "skipped": True}

    settings = Settings.from_env()
    report = RunReport(flow_name=SERVICE, repo=REPO, run_id=run_id)
    try:
        published = poll(settings, report, datetime.now(UTC))
    except BaseException:
        report.send(notable=True)
        raise
    # A clean poll has no outcome to report, so this is logged and not sent:
    # a message every five minutes would bury the runs channel.
    report.send()
    return {"service": SERVICE, "published": published}


def main() -> int:
    """Run the service once and return the process exit code."""
    return run(SERVICE, work)


if __name__ == "__main__":
    sys.exit(main())
