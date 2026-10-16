"""db-drill, second layer — prove the nightly S3 dumps restore (MINI-65).

Point-in-time recovery lives on Railway; the nightly ``pg_dump`` in S3 is the
copy for when Railway itself is the problem (MINI-63). This proves that copy
is usable, in the same monthly run as the PITR drill, for each database:

1. Find the newest ``daily/<database>/`` dump in the backup bucket, with the
   read-only key. Its age is this layer's **effective RPO**: what a restore
   from S3 right now would lose.
2. Download it, and ``pg_restore`` it into a throwaway PostgreSQL 18 started
   inside this container — on a Unix socket only, never a Railway database
   and never dev. ``--no-owner --no-acl``: the production roles do not
   exist here, and the data is what is being proved.
3. Check the restored copy against the live source, as the PITR drill does
   minus the canary: every migration in the dump is one the source has,
   every source table exists, and none with real data on the source is
   empty in the copy.

The scratch server and every file are removed when the run ends, pass or
fail. No Railway API is involved.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import boto3
from botocore.config import Config
from mini_app_polis import logger as log
from mini_app_polis.pipeline_status import RunReport

from db_ops.drill import SERVICE, Connect, DrillError, Settings, compare, connect

LOG = log.get_logger()

#: The scratch server's only superuser. Local socket, trust auth: nothing
#: outside this container can reach it.
SCRATCH_USER = "drill"


class ScratchPostgres:
    """A throwaway PostgreSQL server in a temporary directory.

    Listens on a Unix socket only. The directory is created on entering the
    ``with`` and, with the server, removed on leaving it — whatever happened
    inside. Constructing one acquires nothing, so an instance that is never
    entered leaves nothing behind.
    """

    def __init__(self, bin_dir: str, *, timeout: float = 120) -> None:
        self.bin = Path(bin_dir)
        self.timeout = timeout
        self._root: Path | None = None
        self._started = False

    @property
    def root(self) -> Path:
        """The temporary directory holding the cluster, its socket and dumps."""
        if self._root is None:
            raise RuntimeError("ScratchPostgres is used inside a with block")
        return self._root

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def socket_dir(self) -> Path:
        return self.root / "sock"

    def conninfo(self, dbname: str) -> str:
        """How to reach one database on this server."""
        return f"host={self.socket_dir} dbname={dbname} user={SCRATCH_USER}"

    def _run(self, *args: str, timeout: float | None = None) -> None:
        result = subprocess.run(
            [str(self.bin / args[0]), *args[1:]],
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
            check=False,
        )
        if result.returncode != 0:
            raise DrillError(
                f"{args[0]} exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[-300:]}"
            )

    def __enter__(self) -> ScratchPostgres:
        try:
            self._root = Path(tempfile.mkdtemp(prefix="db-drill-"))
            self.socket_dir.mkdir()
            self._run(
                "initdb",
                f"--pgdata={self.data}",
                f"--username={SCRATCH_USER}",
                "--auth=trust",
                "--encoding=UTF8",
                "--no-sync",
            )
            # Durability is pointless for a server that is about to be
            # deleted, and turning it off halves the restore time.
            options = (
                f"-c listen_addresses='' -k {self.socket_dir} "
                "-c fsync=off -c synchronous_commit=off -c full_page_writes=off"
            )
            self._run(
                "pg_ctl",
                "start",
                f"--pgdata={self.data}",
                "--wait",
                f"--timeout={int(self.timeout)}",
                f"--log={self.root / 'server.log'}",
                f"--options={options}",
            )
            self._started = True
        finally:
            # Anything short of a running server is undone before raising.
            if not self._started:
                self._remove()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._remove()

    def _remove(self) -> None:
        if self._started:
            try:
                self._run("pg_ctl", "stop", f"--pgdata={self.data}", "--mode=immediate")
            except (DrillError, subprocess.TimeoutExpired) as exc:
                LOG.warning("%s.scratch_stop_failed err=%s", SERVICE, exc)
            self._started = False
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)

    def restore(self, dbname: str, dump: Path, timeout: float) -> None:
        """Create ``dbname`` and restore ``dump`` into it."""
        self._run(
            "createdb",
            f"--host={self.socket_dir}",
            f"--username={SCRATCH_USER}",
            dbname,
        )
        self._run(
            "pg_restore",
            f"--host={self.socket_dir}",
            f"--username={SCRATCH_USER}",
            f"--dbname={dbname}",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            str(dump),
            timeout=timeout,
        )


def s3_client(settings: Settings) -> Any:
    """The read-only key's S3 client."""
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.db_drill_aws_access_key_id.get_secret_value(),
        aws_secret_access_key=(
            settings.db_drill_aws_secret_access_key.get_secret_value()
        ),
        # Retried by botocore's standard mode (PIPE-007).
        config=Config(retries={"mode": "standard", "max_attempts": 3}),
    )


def latest_dump(s3: Any, bucket: str, database: str) -> tuple[str, datetime]:
    """The newest nightly dump of one database: its key and when it was written."""
    newest: tuple[str, datetime] | None = None
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"daily/{database}/"
    )
    for page in pages:
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".dump"):
                continue
            if newest is None or obj["LastModified"] > newest[1]:
                newest = (obj["Key"], obj["LastModified"])
    if newest is None:
        raise DrillError(f"no dump of {database} in s3://{bucket}/daily/{database}/")
    return newest


def drill(
    s3: Any,
    scratch: ScratchPostgres,
    database: str,
    source_url: str,
    settings: Settings,
    report: RunReport,
    now: datetime,
    *,
    connect_to: Connect = connect,
) -> dict[str, float]:
    """Restore one database's newest dump and check it. Returns its figures."""
    key, written_at = latest_dump(s3, settings.db_backup_bucket, database)
    age_hours = round((now - written_at).total_seconds() / 3600, 1)
    LOG.info(
        "%s.dump database=%s key=%s age_hours=%s", SERVICE, database, key, age_hours
    )

    target = scratch.root / f"{database}.dump"
    started = time.monotonic()
    try:
        s3.download_file(settings.db_backup_bucket, key, str(target))
        size = target.stat().st_size
        scratch.restore(database, target, settings.dump_restore_timeout_seconds)
    finally:
        target.unlink(missing_ok=True)
    restore_seconds = round(time.monotonic() - started)

    with (
        connect_to(scratch.conninfo(database), settings) as restored,
        connect_to(source_url, settings) as source,
    ):
        checked = compare(
            source, restored, settings, label="dump", copy_may_be_older=True
        )

    report.count(f"{database}_dump_age_hours", age_hours)
    report.count(f"{database}_dump_restore_seconds", restore_seconds)
    report.count(f"{database}_dump_tables", checked.tables)
    report.count(f"{database}_dump_rows", checked.rows_fork)
    problems = list(checked.problems)
    if age_hours > settings.dump_max_age_hours:
        problems.append(f"the newest dump is {age_hours} h old: is db-backup running?")
    if problems:
        raise DrillError("; ".join(problems))
    return {
        "age_hours": age_hours,
        "bytes": size,
        "restore_seconds": restore_seconds,
        "tables": checked.tables,
        "rows": checked.rows_fork,
    }


def drill_all(
    settings: Settings,
    report: RunReport,
    *,
    s3: Any = None,
    scratch_factory: Callable[[str], ScratchPostgres] = ScratchPostgres,
    connect_to: Connect = connect,
    now: datetime | None = None,
) -> dict[str, dict[str, float]]:
    """Drill each database's newest dump. Raises if any failed."""
    s3 = s3 or s3_client(settings)
    now = now or datetime.now(UTC)
    figures: dict[str, dict[str, float]] = {}
    failed: list[str] = []

    with ExitStack() as stack:
        try:
            scratch = stack.enter_context(scratch_factory(settings.pg_bin_dir))
        except (DrillError, OSError, subprocess.TimeoutExpired) as exc:
            report.issue("dump_drill_failed", "scratch server", detail=str(exc)[:300])
            raise DrillError(f"the scratch Postgres did not start: {exc}") from None
        for database, (_, url) in settings.databases.items():
            try:
                figures[database] = drill(
                    s3,
                    scratch,
                    database,
                    url,
                    settings,
                    report,
                    now,
                    connect_to=connect_to,
                )
                report.ok()
            except Exception as exc:  # noqa: BLE001 — recorded, raised below
                report.issue("dump_drill_failed", database, detail=str(exc)[:300])
                LOG.error(
                    "%s.dump_drill_failed database=%s err=%s", SERVICE, database, exc
                )
                failed.append(database)

    if failed:
        raise DrillError(f"dump drill failed for: {', '.join(failed)}")
    return figures
