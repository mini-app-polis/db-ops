"""db-backup — a nightly pg_dump of both production databases to S3.

The copy that does not live on Railway. Point-in-time recovery covers a bad
migration or a dropped table, but its archive is a Railway bucket; this is
the backup for when Railway itself is the problem.

One run, for each database in :attr:`Settings.database_urls`:

1. ``pg_dump --format=custom`` over Railway private networking, as the
   dedicated ``backup`` role (``pg_read_all_data`` and nothing else).
2. Upload to the backup bucket with the write-only key, under
   ``daily/<database>/<UTC timestamp>.dump`` — and, on the first run of a
   month, under ``monthly/`` as well. The bucket's lifecycle keys off those
   prefixes, so the layout is a contract with mini-app-polis/infra.

A failure on one database does not stop the other. The run fails — exit 1,
Sentry, a Healthchecks.io ``/fail`` and a WARN run report — if either did.

Production only. Anywhere else the run logs that it would have backed up
and exits 0, unless ``DB_BACKUP_ENABLED=true`` asks for a deliberate test.

    python -m db_ops.backup
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import httpx
import psycopg
from mini_app_polis import logger as log
from mini_app_polis.environment import (
    Effect,
    effect_enabled,
    is_production,
    log_settings,
)
from mini_app_polis.pipeline_status import RunReport
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from db_ops._entry import run

SERVICE = "db-backup"
REPO = "db-ops"

LOG = log.get_logger()


class BackupError(RuntimeError):
    """A backup that did not complete. The message is safe to log."""


class Settings(BaseSettings):
    """Everything a run reads from the environment, read once.

    Field names are the variable names, lowercased. Nothing reads a ``.env``
    file: values come from the process environment (Doppler ``prd`` through
    Railway's shared variables). Secrets are ``SecretStr`` so a repr or a
    validation error can never print one.
    """

    model_config = SettingsConfigDict(extra="ignore")

    db_backup_database_url_kaianolevine: SecretStr
    db_backup_database_url_deejaytools: SecretStr
    db_backup_bucket: str
    db_backup_aws_access_key_id: SecretStr
    db_backup_aws_secret_access_key: SecretStr
    aws_region: str = "us-east-1"
    healthchecks_url_db_backup: SecretStr | None = None
    #: Read by mini_app_polis's KaianoApiClient when the run report is sent,
    #: not by this module. Declared here so the run's whole configuration is
    #: one contract; a missing key costs the run report, not the backup.
    db_ops_api_key: SecretStr | None = None

    #: The whole run's budget. Railway skips a scheduled start while the
    #: previous run is still going, so a hung dump would take every later
    #: night with it; the run stops itself instead.
    db_backup_run_timeout_seconds: float = 1800
    db_backup_db_connect_timeout_seconds: int = 15
    #: How long pg_dump waits for a table lock before giving up, rather than
    #: queueing behind a long migration.
    db_lock_wait_timeout_seconds: int = 60
    healthchecks_timeout_seconds: float = 5

    @property
    def database_urls(self) -> dict[str, str]:
        """Database name in the object key -> its connection string."""
        return {
            "kaianolevine": self.db_backup_database_url_kaianolevine.get_secret_value(),
            "deejaytools": self.db_backup_database_url_deejaytools.get_secret_value(),
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
            raise BackupError(
                f"invalid or missing configuration: {', '.join(names)}"
            ) from None


def object_keys(database: str, now: datetime) -> list[str]:
    """Where one dump goes: always ``daily/``, and ``monthly/`` on the 1st."""
    stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
    keys = [f"daily/{database}/{stamp}.dump"]
    if now.day == 1:
        keys.append(f"monthly/{database}/{stamp}.dump")
    return keys


def _enabled() -> bool:
    """Production, or a deliberate test asked for with DB_BACKUP_ENABLED."""
    raw = os.getenv("DB_BACKUP_ENABLED", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return is_production()


def _ping(settings: Settings, suffix: str = "") -> None:
    """Tell Healthchecks.io a run started, succeeded or failed. Never raises."""
    if not settings.healthchecks_url_db_backup or not effect_enabled(
        Effect.HEALTHCHECKS
    ):
        return
    url = settings.healthchecks_url_db_backup.get_secret_value().rstrip("/")
    try:
        # no-retry: the ping is not the job, and a missed one costs a late
        # check, not a backup. PIPE-007.
        response = httpx.get(
            url + suffix, timeout=settings.healthchecks_timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 — the ping is not the job
        LOG.warning("%s.ping_failed suffix=%s err=%s", SERVICE, suffix or "/", exc)
        return
    # Healthchecks answers 4xx for a malformed or unknown check URL, and an
    # unheard ping is the failure it exists to catch — say so loudly.
    if response.is_error:
        LOG.warning(
            "%s.ping_rejected suffix=%s status=%d",
            SERVICE,
            suffix or "/",
            response.status_code,
        )


def dump(
    url: str,
    target: Path,
    *,
    timeout: float,
    connect_timeout: int,
    lock_wait_timeout: int,
) -> None:
    """Run pg_dump into ``target``. Raises BackupError without echoing the URL.

    The password goes to pg_dump in PGPASSWORD, never on its command line,
    where any process on the host could read it.
    """
    dbname, password = _split_password(url)
    env = {
        **os.environ,
        "PGCONNECT_TIMEOUT": str(connect_timeout),
        "PGAPPNAME": SERVICE,
    }
    if password is not None:
        env["PGPASSWORD"] = password
    try:
        result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                f"--lock-wait-timeout={lock_wait_timeout}s",
                f"--file={target}",
                f"--dbname={dbname}",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"pg_dump did not finish within {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise BackupError("pg_dump is not installed in this image") from exc
    if result.returncode != 0:
        # pg_dump's stderr names the host and role, never the password.
        raise BackupError(
            f"pg_dump exited {result.returncode}: {result.stderr.strip()[-500:]}"
        )


def _split_password(url: str) -> tuple[str, str | None]:
    """A connection string without its password, and the password."""
    try:
        params = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        # The message would quote the connection string.
        raise BackupError("the database URL is not a valid connection string") from None
    password = params.pop("password", None)
    return make_conninfo("", **params), None if password is None else str(password)


def upload(client: Any, settings: Settings, source: Path, key: str) -> None:
    """Upload one dump. The bucket has Object Lock, so a checksum is required."""
    # no-retry here: boto3's own retry policy covers the transfer (PIPE-007),
    # and the next night is the retry for anything worse.
    client.upload_file(
        str(source),
        settings.db_backup_bucket,
        key,
        ExtraArgs={"ChecksumAlgorithm": "SHA256"},
    )


def _s3_client(settings: Settings) -> Any:
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.db_backup_aws_access_key_id.get_secret_value(),
        aws_secret_access_key=(
            settings.db_backup_aws_secret_access_key.get_secret_value()
        ),
    )


def back_up(settings: Settings, report: RunReport, now: datetime) -> list[str]:
    """Dump and upload every database. Returns the keys written.

    Each database is attempted whatever happened to the one before it;
    failures are recorded on the report and raised together at the end.
    """
    deadline = time.monotonic() + settings.db_backup_run_timeout_seconds
    client = _s3_client(settings)
    written: list[str] = []
    failed: list[str] = []

    with tempfile.TemporaryDirectory(prefix="db-backup-") as tmp:
        for database, url in settings.database_urls.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report.issue("out_of_time", database)
                failed.append(database)
                continue
            target = Path(tmp) / f"{database}.dump"
            try:
                dump(
                    url,
                    target,
                    timeout=remaining,
                    connect_timeout=settings.db_backup_db_connect_timeout_seconds,
                    lock_wait_timeout=settings.db_lock_wait_timeout_seconds,
                )
                size = target.stat().st_size
                for key in object_keys(database, now):
                    upload(client, settings, target, key)
                    written.append(key)
                    report.created("dump", key)
                report.ok()
                report.count(f"{database}_bytes", size)
                LOG.info("%s.dumped database=%s bytes=%d", SERVICE, database, size)
            except Exception as exc:  # noqa: BLE001 — recorded, raised below
                report.issue("backup_failed", database, detail=str(exc)[:300])
                LOG.error(
                    "%s.database_failed database=%s err=%s", SERVICE, database, exc
                )
                failed.append(database)
            finally:
                target.unlink(missing_ok=True)

    if failed:
        raise BackupError(f"backup failed for: {', '.join(failed)}")
    return written


def work(run_id: str) -> dict[str, Any]:
    """Back up both databases once and return what was written."""
    if not _enabled():
        LOG.info(
            "%s.skipped reason=not_production hint=set DB_BACKUP_ENABLED=true "
            "for a deliberate test",
            SERVICE,
        )
        return {"service": SERVICE, "skipped": True}

    settings = Settings.from_env()
    # One line per setting; values only for names in SETTINGS_ALLOWLIST.
    log_settings(settings)
    _ping(settings, "/start")
    report = RunReport(flow_name=SERVICE, repo=REPO, run_id=run_id)
    try:
        written = back_up(settings, report, datetime.now(UTC))
    except BaseException:
        report.send(notable=True)
        _ping(settings, "/fail")
        raise
    report.send()
    _ping(settings)
    return {"service": SERVICE, "written": written}


def main() -> int:
    """Run the service once and return the process exit code."""
    return run(SERVICE, work)


if __name__ == "__main__":
    sys.exit(main())
