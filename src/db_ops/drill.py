"""db-drill — prove point-in-time recovery works, monthly, on both databases.

A backup nobody has restored is a hope. Once a month, for each database in
:attr:`Settings.databases`, one at a time (MINI-64):

1. **Mark a moment.** As the ``drill`` role, write a *canary* row to
   ``ops.drill_canary``, read the database's clock, wait a couple of
   seconds, and write a *fence* row. The restore target is one second after
   the canary, so before the fence.
2. **Wait for the archive.** Poll ``pg_stat_archiver`` until the WAL segment
   holding the fence has been archived. Canary commit to archived is the
   **measured RPO**: how much a restore at that moment would have lost.
3. **Restore.** Ask Railway for a point-in-time restore to the target, into
   a new service ``drill-<database>-<UTC minute>``. The source is untouched.
4. **Wait for it.** Poll until the fork has its private host and accepts a
   connection as a promoted primary. Restore request to promoted is the
   **measured RTO**.
5. **Verify** over private networking: the canary is there and the fence is
   not (the restore stopped at its target, no earlier and no later);
   ``schema_migrations`` matches the source; every table the source has
   exists in the fork, and none that holds real data on the source is empty.
6. **Tear down**, in a ``finally``: delete the fork's volume, then the fork.
   This is not housekeeping. The fork carries the source's PITR archive
   credential, which can write to production's archive, so it must not
   outlive the drill: the run fails if the deletion cannot be confirmed,
   and every run flags any ``drill-*`` service older than 30 minutes and
   any production volume mounted on nothing. Nothing in a process can
   survive the process being stopped, so a deploy during a run is the one
   way to leave a fork behind (README).

The figures go to the runs channel every time. A failure is a WARN, so it
also reaches the errors channel, and Sentry, and the run exits 1.

Production only. The forks are created in the environment the service runs
in, and that must be production. Anywhere else the run logs that it would
have drilled and exits 0, unless ``DB_DRILL_ENABLED=true`` asks for a
deliberate test — which still refuses a non-production Railway environment.

Then the second layer, in :mod:`db_ops.dump_drill` (MINI-65): the latest
nightly S3 dump of each database is restored into a throwaway Postgres
inside this container and checked the same way, minus the canary.

    python -m db_ops.drill
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from mini_app_polis import logger as log
from mini_app_polis.environment import is_production
from mini_app_polis.pipeline_status import RunReport
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from db_ops._entry import run
from db_ops.railway_api import RailwayApi, RailwayApiError, TokenKind

SERVICE = "db-drill"
REPO = "db-ops"
FORK_PREFIX = "drill-"

LOG = log.get_logger()

Sleep = Callable[[float], None]
Connect = Callable[[str, "Settings"], Any]

CANARY_TABLE = """
CREATE TABLE IF NOT EXISTS ops.drill_canary (
    run_id     text        NOT NULL,
    kind       text        NOT NULL CHECK (kind IN ('canary', 'fence')),
    written_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, kind)
)
"""


class DrillError(RuntimeError):
    """A drill that failed. The message is safe to log."""


class Settings(BaseSettings):
    """Everything a run reads from the environment, read once.

    Field names are the variable names, lowercased. No ``.env`` file is read.
    The ``RAILWAY_*`` values are set by Railway on every service.
    """

    model_config = SettingsConfigDict(extra="ignore")

    #: As the ``drill`` role: pg_read_all_data, and owner of the ``ops``
    #: schema the canaries go in. The fork has the same role and password,
    #: so the same URL with the fork's host reaches it.
    db_drill_database_url_kaianolevine: SecretStr
    db_drill_database_url_deejaytools: SecretStr
    #: Which Railway service holds each database, by name.
    db_drill_source_service_kaianolevine: str = "Postgres"
    db_drill_source_service_deejaytools: str = "Postgres-deejaytools"

    db_drill_railway_token: SecretStr
    db_drill_railway_token_kind: TokenKind = "project"
    railway_project_id: str
    railway_environment_id: str
    railway_environment_name: str
    #: Read by mini_app_polis's KaianoApiClient when the run report is sent.
    db_ops_api_key: SecretStr | None = None

    #: The whole run's budget: two restores, one after the other.
    run_timeout_seconds: float = 5400
    #: How long the fence's WAL segment may take to reach the archive. The
    #: databases switch segments at least every 60 s (archive_timeout).
    archive_wait_seconds: float = 300
    #: How long one fork may take to appear, restore and promote.
    restore_timeout_seconds: float = 1800
    #: How long to wait for Railway to create, or confirm deleting, a fork.
    teardown_wait_seconds: float = 300
    poll_interval_seconds: float = 15
    #: Between the canary and the fence; the restore target falls between.
    fence_gap_seconds: float = 2
    db_connect_timeout_seconds: int = 10
    db_statement_timeout_ms: int = 60_000
    #: A source table with at least this many rows must not be empty in the
    #: fork. A smaller one may have been empty at the target.
    key_table_min_rows: int = 100
    #: A drill-* service older than this was not torn down. A whole run
    #: takes about fifteen minutes.
    stale_fork_seconds: float = 1800

    # -- the S3 dump drill (MINI-65) --
    #: The off-platform backup bucket, and the read-only key for it
    #: (mini-app-polis/infra database_backups.tf, db-backup-reader).
    db_backup_bucket: str
    db_drill_aws_access_key_id: SecretStr
    db_drill_aws_secret_access_key: SecretStr
    aws_region: str = "us-east-1"
    #: The PostgreSQL 18 server binaries the image installs.
    pg_bin_dir: str = "/usr/lib/postgresql/18/bin"
    #: One dump's download and pg_restore.
    dump_restore_timeout_seconds: float = 1200
    #: The newest dump older than this means db-backup has stopped.
    dump_max_age_hours: float = 36

    @property
    def databases(self) -> dict[str, tuple[str, str]]:
        """Database name -> (its Railway service name, its connection string)."""
        return {
            "kaianolevine": (
                self.db_drill_source_service_kaianolevine,
                self.db_drill_database_url_kaianolevine.get_secret_value(),
            ),
            "deejaytools": (
                self.db_drill_source_service_deejaytools,
                self.db_drill_database_url_deejaytools.get_secret_value(),
            ),
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
            raise DrillError(
                f"invalid or missing configuration: {', '.join(names)}"
            ) from None


@dataclass
class Mark:
    """The moment a drill restores to, and how it was pinned."""

    #: The database's clock just after the canary committed.
    canary_at: datetime
    #: The WAL segment that holds the fence.
    fence_wal: str

    @property
    def target(self) -> datetime:
        """One second after the canary, and before the fence."""
        return self.canary_at + timedelta(seconds=1)


@dataclass
class Verification:
    """What the fork held, against the source."""

    tables: int = 0
    rows_source: int = 0
    rows_fork: int = 0
    problems: list[str] = field(default_factory=list)


# -- the databases -----------------------------------------------------------


def connect(url: str, settings: Settings) -> psycopg.Connection[Any]:
    """An autocommit connection with the drill's timeouts."""
    return psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=settings.db_connect_timeout_seconds,
        application_name=SERVICE,
        options=f"-c statement_timeout={settings.db_statement_timeout_ms}",
    )


def mark(conn: Any, run_id: str, settings: Settings, *, sleep: Sleep) -> Mark:
    """Write the canary, then the fence, on an autocommit connection."""
    conn.execute(CANARY_TABLE)
    conn.execute(
        "INSERT INTO ops.drill_canary (run_id, kind) VALUES (%s, 'canary') "
        "ON CONFLICT (run_id, kind) DO NOTHING",
        (run_id,),
    )
    # Read after the canary has committed, so the canary is before this.
    canary_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    sleep(settings.fence_gap_seconds)
    conn.execute(
        "INSERT INTO ops.drill_canary (run_id, kind) VALUES (%s, 'fence') "
        "ON CONFLICT (run_id, kind) DO NOTHING",
        (run_id,),
    )
    # The segment holding the fence's commit: the last byte written so far.
    fence_wal = conn.execute(
        "SELECT pg_walfile_name(pg_current_wal_lsn() - 1)"
    ).fetchone()[0]
    return Mark(canary_at=canary_at.astimezone(UTC), fence_wal=fence_wal)


def wait_archived(conn: Any, wal: str, settings: Settings, *, sleep: Sleep) -> datetime:
    """Wait until ``wal`` has been archived, and return when it was."""
    deadline = time.monotonic() + settings.archive_wait_seconds
    while True:
        last_wal, last_at = conn.execute(
            "SELECT last_archived_wal, last_archived_time FROM pg_stat_archiver"
        ).fetchone()
        # Segment names sort in WAL order, timeline first. A .history or
        # .partial name is not a segment and is skipped.
        if last_wal and len(last_wal) == 24 and last_wal >= wal and last_at:
            return last_at.astimezone(UTC)
        if time.monotonic() >= deadline:
            raise DrillError(
                f"WAL segment {wal} was not archived within "
                f"{settings.archive_wait_seconds:.0f}s (last archived: {last_wal})"
            )
        sleep(5)


def verify(source: Any, fork: Any, run_id: str, settings: Settings) -> Verification:
    """Check a PITR fork against the source: its canary, then :func:`compare`."""
    result = compare(source, fork, settings)
    kinds = {
        row[0]
        for row in fork.execute(
            "SELECT kind FROM ops.drill_canary WHERE run_id = %s", (run_id,)
        ).fetchall()
    }
    if "canary" not in kinds:
        result.problems.append("canary missing: the restore stopped short")
    if "fence" in kinds:
        result.problems.append("fence present: the restore went past its target")
    return result


def compare(
    source: Any,
    copy: Any,
    settings: Settings,
    *,
    label: str = "fork",
    copy_may_be_older: bool = False,
) -> Verification:
    """Check a restored copy against the live source. Problems are collected.

    ``schema_migrations`` must match — or, for a copy taken earlier than now
    (``copy_may_be_older``), hold nothing the source does not. Every source
    table must exist in the copy, and none with real data on the source may
    be empty in it.
    """
    result = Verification()

    migrations = "SELECT filename FROM public.schema_migrations ORDER BY filename"
    try:
        source_migrations = [r[0] for r in source.execute(migrations).fetchall()]
        copy_migrations = [r[0] for r in copy.execute(migrations).fetchall()]
    except psycopg.Error as exc:
        result.problems.append(f"schema_migrations unreadable: {_short(exc)}")
    else:
        matches = (
            bool(copy_migrations) and set(copy_migrations) <= set(source_migrations)
            if copy_may_be_older
            else source_migrations == copy_migrations
        )
        if not matches:
            result.problems.append(
                f"schema_migrations differ: source {len(source_migrations)}, "
                f"{label} {len(copy_migrations)}"
            )

    tables = source.execute(
        "SELECT schemaname, relname FROM pg_stat_user_tables "
        "WHERE schemaname <> 'ops' ORDER BY 1, 2"
    ).fetchall()
    if not tables:
        result.problems.append("the source has no tables to compare")
    for schema, table in tables:
        count = sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        source_rows = source.execute(count).fetchone()[0]
        try:
            copy_rows = copy.execute(count).fetchone()[0]
        except psycopg.Error:
            result.problems.append(f"{schema}.{table} missing from the {label}")
            continue
        result.tables += 1
        result.rows_source += source_rows
        result.rows_fork += copy_rows
        if source_rows >= settings.key_table_min_rows and copy_rows == 0:
            result.problems.append(
                f"{schema}.{table} empty in the {label} ({source_rows} rows in source)"
            )
    return result


# -- Railway -----------------------------------------------------------------


def fork_name(database: str, now: datetime) -> str:
    """The fork's service name. Teardown deletes nothing without the prefix."""
    return f"{FORK_PREFIX}{database}-{now:%Y%m%d-%H%M}"


def _raise_if_failed(api: RailwayApi, workflow_id: str | None) -> None:
    if not workflow_id:
        return
    status, error = api.workflow_status(workflow_id)
    if status == "Error":
        raise DrillError(f"Railway's restore failed: {(error or 'no detail')[:200]}")


def wait_for_fork(
    api: RailwayApi,
    name: str,
    workflow_id: str | None,
    deadline: float,
    settings: Settings,
    *,
    sleep: Sleep,
) -> str:
    """Wait until Railway has created the fork and given it a private host."""
    while True:
        _raise_if_failed(api, workflow_id)
        found = api.find_service(name)
        if found:
            domain = api.private_domain(found.service_id)
            if domain:
                return domain
        if time.monotonic() >= deadline:
            raise DrillError(f"{name} did not appear within the restore budget")
        sleep(settings.poll_interval_seconds)


def wait_promoted(
    api: RailwayApi,
    url: str,
    workflow_id: str | None,
    deadline: float,
    settings: Settings,
    *,
    sleep: Sleep,
    connect_to: Connect,
) -> Any:
    """Connect to the fork once it has finished recovery; return the connection."""
    last_error = "never reached"
    while True:
        _raise_if_failed(api, workflow_id)
        try:
            conn = connect_to(url, settings)
        except (psycopg.Error, OSError) as exc:
            # Expected for a while: no DNS yet, then a server still restoring.
            last_error = _short(exc)
        else:
            try:
                in_recovery = conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]
            except psycopg.Error as exc:
                last_error = _short(exc)
                in_recovery = True
            if not in_recovery:
                return conn
            last_error = "still in recovery"
            conn.close()
        if time.monotonic() >= deadline:
            raise DrillError(f"the fork never finished recovery: {last_error}")
        sleep(settings.poll_interval_seconds)


def tear_down(api: RailwayApi, name: str, settings: Settings, *, sleep: Sleep) -> bool:
    """Delete the fork's volumes, then the fork; return whether there was one.

    A requested restore produces a fork even if the drill gave up before it
    appeared, so a missing fork is waited for. A delete call that errors is
    not the verdict — Railway can take longer to answer than the request
    waits, and delete anyway — so the outcome is read back: the fork and
    its volumes must be gone from the environment. Raises
    :class:`DrillError` if that cannot be confirmed.
    """
    if not name.startswith(FORK_PREFIX):
        raise DrillError(f"refusing to delete {name!r}: not a drill fork")

    deadline = time.monotonic() + settings.teardown_wait_seconds
    found = api.find_service(name)
    while found is None and time.monotonic() < deadline:
        sleep(settings.poll_interval_seconds)
        found = api.find_service(name)
    if found is None:
        return False

    # Volumes first: once the service is gone, so is the record of which
    # volumes were its. The Railway CLI deletes in the same order.
    volume_ids = {v.volume_id for v in api.volumes_of(found.service_id)}
    errors: list[str] = []
    for volume_id in sorted(volume_ids):
        _attempt(api.delete_volume, volume_id, errors)
    _attempt(api.delete_service, found.service_id, errors)

    while True:
        services, volumes = api.environment_instances()
        left = [s.name for s in services if s.name == name] + [
            f"volume {v.volume_id}" for v in volumes if v.volume_id in volume_ids
        ]
        if not left:
            return True
        if time.monotonic() >= deadline:
            detail = f" (last error: {errors[-1]})" if errors else ""
            raise DrillError(f"still there after deletion: {', '.join(left)}{detail}")
        sleep(settings.poll_interval_seconds)


def _attempt(delete: Callable[[str], None], target: str, errors: list[str]) -> None:
    try:
        delete(target)
    except RailwayApiError as exc:
        LOG.warning("%s.delete_unconfirmed target=%s err=%s", SERVICE, target, exc)
        errors.append(str(exc)[:200])


def unattached_volumes(api: RailwayApi) -> list[str]:
    """Volumes in this environment mounted on nothing.

    Deleting a fork's service in the dashboard leaves its volume behind —
    a full copy of production data, billed, and attached to nothing. Every
    volume in production belongs to a database, so any unattached one is
    worth a look.
    """
    _, volumes = api.environment_instances()
    return sorted(v.name or v.volume_id for v in volumes if not v.service_id)


def stale_forks(api: RailwayApi, now: datetime, settings: Settings) -> list[str]:
    """``drill-*`` services in this environment older than the stale threshold."""
    services, _ = api.environment_instances()
    here = {s.service_id for s in services if s.name.startswith(FORK_PREFIX)}
    if not here:
        return []
    cutoff = now - timedelta(seconds=settings.stale_fork_seconds)
    return sorted(
        s.name
        for s in api.project_services()
        if s.service_id in here and s.created_at < cutoff
    )


# -- the run -----------------------------------------------------------------


def drill(
    api: RailwayApi,
    database: str,
    source_service: str,
    url: str,
    run_id: str,
    settings: Settings,
    report: RunReport,
    deadline: float,
    *,
    sleep: Sleep = time.sleep,
    connect_to: Connect = connect,
) -> dict[str, int]:
    """Restore, verify and tear down one database's fork. Returns its figures."""
    source = api.find_service(source_service)
    if source is None:
        raise DrillError(f"no service named {source_service!r}")
    volumes = api.volumes_of(source.service_id)
    if len(volumes) != 1:
        raise DrillError(f"{source_service} has {len(volumes)} volumes, expected 1")

    with connect_to(url, settings) as conn:
        moment = mark(conn, run_id, settings, sleep=sleep)
        archived_at = wait_archived(conn, moment.fence_wal, settings, sleep=sleep)
    rpo = round((archived_at - moment.canary_at).total_seconds())
    LOG.info(
        "%s.marked database=%s target=%s rpo_seconds=%d",
        SERVICE,
        database,
        moment.target.isoformat(),
        rpo,
    )

    name = fork_name(database, datetime.now(UTC))
    requested = False
    started = time.monotonic()
    restore_deadline = min(deadline, started + settings.restore_timeout_seconds)
    try:
        workflow_id = api.pitr_restore(volumes[0].instance_id, moment.target, name)
        requested = True
        report.created("drill fork", name)
        host = wait_for_fork(
            api, name, workflow_id, restore_deadline, settings, sleep=sleep
        )
        fork_conn = wait_promoted(
            api,
            make_conninfo(url, host=host),
            workflow_id,
            restore_deadline,
            settings,
            sleep=sleep,
            connect_to=connect_to,
        )
        rto = round(time.monotonic() - started)
        LOG.info("%s.promoted database=%s rto_seconds=%d", SERVICE, database, rto)
        with fork_conn, connect_to(url, settings) as source_conn:
            checked = verify(source_conn, fork_conn, run_id, settings)
    finally:
        if requested:
            _tear_down_or_flag(api, name, settings, report, sleep=sleep)

    report.count(f"{database}_rpo_seconds", rpo)
    report.count(f"{database}_rto_seconds", rto)
    report.count(f"{database}_tables", checked.tables)
    report.count(f"{database}_rows", checked.rows_fork)
    if checked.problems:
        raise DrillError("; ".join(checked.problems))
    return {
        "rpo_seconds": rpo,
        "rto_seconds": rto,
        "tables": checked.tables,
        "rows_fork": checked.rows_fork,
        "rows_source": checked.rows_source,
    }


def _tear_down_or_flag(
    api: RailwayApi, name: str, settings: Settings, report: RunReport, *, sleep: Sleep
) -> None:
    try:
        if tear_down(api, name, settings, sleep=sleep):
            report.removed("drill fork", name)
            LOG.info("%s.torn_down fork=%s", SERVICE, name)
        else:
            report.issue("fork_never_appeared", name)
    except (DrillError, RailwayApiError) as exc:
        # Loud: this fork can write to production's WAL archive.
        report.issue("teardown_failed", name, detail=str(exc)[:200])
        LOG.error("%s.teardown_failed fork=%s err=%s", SERVICE, name, exc)


def drill_all(
    api: RailwayApi,
    settings: Settings,
    report: RunReport,
    run_id: str,
    *,
    sleep: Sleep = time.sleep,
    connect_to: Connect = connect,
) -> dict[str, dict[str, int]]:
    """Drill each database in turn. Raises if any drill or teardown failed."""
    if settings.railway_environment_name != "production":
        raise DrillError(
            f"refusing to drill from the {settings.railway_environment_name!r} "
            "Railway environment: forks hold production data"
        )
    deadline = time.monotonic() + settings.run_timeout_seconds
    figures: dict[str, dict[str, int]] = {}
    failed: list[str] = []

    for database, (source_service, url) in settings.databases.items():
        if time.monotonic() >= deadline:
            report.issue("out_of_time", database)
            failed.append(database)
            continue
        try:
            figures[database] = drill(
                api,
                database,
                source_service,
                url,
                run_id,
                settings,
                report,
                deadline,
                sleep=sleep,
                connect_to=connect_to,
            )
            report.ok()
        except Exception as exc:  # noqa: BLE001 — recorded, raised below
            report.issue("drill_failed", database, detail=str(exc)[:300])
            LOG.error("%s.drill_failed database=%s err=%s", SERVICE, database, exc)
            failed.append(database)

    try:
        for name in stale_forks(api, datetime.now(UTC), settings):
            report.issue("stale_drill_service", name)
        for name in unattached_volumes(api):
            report.issue("unattached_volume", name)
    except RailwayApiError as exc:
        report.issue("stale_check_failed", detail=str(exc)[:200])

    teardowns = report.issues.get("teardown_failed", 0)
    if failed or teardowns:
        raise DrillError(
            f"drill failed for: {', '.join(failed) or 'none'}; "
            f"teardown failures: {teardowns}"
        )
    return figures


def _short(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:200] if text else type(exc).__name__


def _enabled() -> bool:
    """Production, or a deliberate test asked for with DB_DRILL_ENABLED."""
    raw = os.getenv("DB_DRILL_ENABLED", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return is_production()


def work(run_id: str) -> dict[str, Any]:
    """Drill both recovery layers for both databases; return the figures."""
    from db_ops import dump_drill  # here, not at the top: it imports this module

    if not _enabled():
        LOG.info(
            "%s.skipped reason=not_production hint=set DB_DRILL_ENABLED=true "
            "for a deliberate test",
            SERVICE,
        )
        return {"service": SERVICE, "skipped": True}

    settings = Settings.from_env()
    api = RailwayApi(
        settings.db_drill_railway_token.get_secret_value(),
        settings.db_drill_railway_token_kind,
        settings.railway_project_id,
        settings.railway_environment_id,
    )
    report = RunReport(flow_name=SERVICE, repo=REPO, run_id=run_id)
    failures: list[str] = []
    figures: dict[str, Any] = {}
    try:
        # Each layer is drilled whatever happened to the other: a broken
        # PITR drill must not hide whether the dumps restore, or the reverse.
        try:
            figures["pitr"] = drill_all(api, settings, report, run_id)
        except DrillError as exc:
            failures.append(f"pitr: {exc}")
        try:
            figures["dump"] = dump_drill.drill_all(settings, report)
        except DrillError as exc:
            failures.append(f"dump: {exc}")
    finally:
        # Monthly and measured: every run is worth a message, pass or fail.
        report.send(notable=True)
        api.close()
    if failures:
        raise DrillError("; ".join(failures))
    return {"service": SERVICE, "drilled": figures}


def main() -> int:
    """Run the service once and return the process exit code."""
    return run(SERVICE, work)


if __name__ == "__main__":
    sys.exit(main())
