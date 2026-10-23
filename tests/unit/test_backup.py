"""db-backup: what it writes where, and what a failure does.

pg_dump and S3 are replaced at their seams (``dump`` and the S3 client), so
these run with no database, no AWS and no secrets.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from db_ops import backup
from db_ops.backup import BackupError, Settings, object_keys

URLS = {
    "kaianolevine": "postgresql://backup@postgres.railway.internal/railway",
    "deejaytools": "postgresql://backup@postgres-twzl.railway.internal/railway",
}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "db_backup_bucket": "mini-app-polis-db-backups-test",
        "db_backup_aws_access_key_id": "AKIATEST",
        "db_backup_aws_secret_access_key": "secret",
        "aws_region": "us-east-1",
        "db_backup_database_url_kaianolevine": URLS["kaianolevine"],
        "db_backup_database_url_deejaytools": URLS["deejaytools"],
        "healthchecks_url_db_backup": "https://hc-ping.com/test-check",
        "db_backup_run_timeout_seconds": 60.0,
        "db_backup_db_connect_timeout_seconds": 5,
    }
    values.update(overrides)
    return Settings(**values)


class FakeS3:
    def __init__(self, fail_on: str | None = None) -> None:
        self.uploads: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_on = fail_on

    def upload_file(
        self, source: str, bucket: str, key: str, ExtraArgs: dict[str, Any]
    ) -> None:  # noqa: N803 — boto3's spelling
        if self.fail_on and self.fail_on in key:
            raise RuntimeError("AccessDenied")
        assert Path(source).exists()
        self.uploads.append((bucket, key, ExtraArgs))


class FakeReport:
    def __init__(self) -> None:
        self.created_items: list[str] = []
        self.issues: list[tuple[str, str | None]] = []
        self.sent: list[bool] = []
        self.processed = 0
        self.counters: dict[str, Any] = {}

    def created(self, kind: str, item: str | None = None) -> None:
        self.created_items.append(f"{kind}:{item}")

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


@pytest.fixture
def fake_dump(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace pg_dump with a file write; record which URLs were dumped."""
    dumped: list[str] = []

    def _dump(url: str, target: Path, **kwargs: Any) -> None:
        if "fail" in url:
            raise BackupError("pg_dump exited 1: connection refused")
        target.write_bytes(b"PGDMP fake")
        dumped.append(url)

    monkeypatch.setattr(backup, "dump", _dump)
    return dumped


# ── Object keys: the contract with the bucket's lifecycle ────────────────


def test_an_ordinary_day_writes_only_a_daily_key() -> None:
    now = datetime(2026, 10, 15, 9, 17, 3, tzinfo=UTC)
    assert object_keys("deejaytools", now) == [
        "daily/deejaytools/2026-10-15T091703Z.dump"
    ]


def test_the_first_of_the_month_also_writes_a_monthly_key() -> None:
    now = datetime(2026, 11, 1, 9, 17, 3, tzinfo=UTC)
    assert object_keys("kaianolevine", now) == [
        "daily/kaianolevine/2026-11-01T091703Z.dump",
        "monthly/kaianolevine/2026-11-01T091703Z.dump",
    ]


# ── A run ─────────────────────────────────────────────────────────────────


def test_both_databases_are_uploaded_with_a_checksum(
    monkeypatch: pytest.MonkeyPatch, fake_dump: list[str]
) -> None:
    s3 = FakeS3()
    monkeypatch.setattr(backup, "_s3_client", lambda settings: s3)
    report = FakeReport()

    written = backup.back_up(
        _settings(),
        report,
        datetime(2026, 10, 15, tzinfo=UTC),  # type: ignore[arg-type]
    )

    assert sorted(fake_dump) == sorted(URLS.values())
    assert written == [
        "daily/kaianolevine/2026-10-15T000000Z.dump",
        "daily/deejaytools/2026-10-15T000000Z.dump",
    ]
    assert {u[0] for u in s3.uploads} == {"mini-app-polis-db-backups-test"}
    assert all(u[2] == {"ChecksumAlgorithm": "SHA256"} for u in s3.uploads)
    assert report.processed == 2 and report.issues == []


def test_one_failure_does_not_stop_the_other_database(
    monkeypatch: pytest.MonkeyPatch, fake_dump: list[str]
) -> None:
    s3 = FakeS3()
    monkeypatch.setattr(backup, "_s3_client", lambda settings: s3)
    report = FakeReport()
    with pytest.raises(BackupError, match="kaianolevine"):
        backup.back_up(
            _settings(db_backup_database_url_kaianolevine="postgresql://fail"),
            report,  # type: ignore[arg-type]
            datetime(2026, 10, 15, tzinfo=UTC),
        )

    assert [u[1] for u in s3.uploads] == ["daily/deejaytools/2026-10-15T000000Z.dump"]
    assert report.issues == [("backup_failed", "kaianolevine")]


def test_a_failed_upload_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, fake_dump: list[str]
) -> None:
    monkeypatch.setattr(backup, "_s3_client", lambda settings: FakeS3("deejaytools"))
    report = FakeReport()

    with pytest.raises(BackupError, match="deejaytools"):
        backup.back_up(
            _settings(),
            report,
            datetime(2026, 10, 15, tzinfo=UTC),  # type: ignore[arg-type]
        )
    assert ("backup_failed", "deejaytools") in report.issues


def test_an_exhausted_budget_skips_the_rest(
    monkeypatch: pytest.MonkeyPatch, fake_dump: list[str]
) -> None:
    monkeypatch.setattr(backup, "_s3_client", lambda settings: FakeS3())
    report = FakeReport()

    with pytest.raises(BackupError):
        backup.back_up(
            _settings(db_backup_run_timeout_seconds=0),
            report,  # type: ignore[arg-type]
            datetime(2026, 10, 15, tzinfo=UTC),
        )
    assert fake_dump == []
    assert {reason for reason, _ in report.issues} == {"out_of_time"}


# ── Configuration and gating ─────────────────────────────────────────────


def test_missing_configuration_names_every_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("DB_BACKUP_BUCKET", "bucket")
    monkeypatch.setenv("DB_BACKUP_DATABASE_URL_DEEJAYTOOLS", "postgresql://x:pw@h/db")

    with pytest.raises(BackupError) as caught:
        Settings.from_env()
    message = str(caught.value)
    assert "DB_BACKUP_BUCKET" not in message
    assert "DB_BACKUP_AWS_ACCESS_KEY_ID" in message
    assert "DB_BACKUP_DATABASE_URL_KAIANOLEVINE" in message
    assert "pw" not in message


def test_secrets_never_appear_in_a_repr() -> None:
    text = repr(_settings())
    assert "AKIATEST" not in text
    assert "railway.internal" not in text
    assert "hc-ping.com" not in text


def test_outside_production_the_run_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("DB_BACKUP_ENABLED", raising=False)
    assert backup.work("run-1") == {"service": "db-backup", "skipped": True}


def test_dump_raises_without_echoing_the_connection_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_url = "postgresql://backup:s3cret@postgres.railway.internal/railway"
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 1, "", 'pg_dump: error: connection to server failed: role "backup"'
        )

    monkeypatch.setattr(backup.subprocess, "run", _run)
    with pytest.raises(BackupError) as caught:
        backup.dump(
            secret_url,
            tmp_path / "x.dump",
            timeout=10,
            connect_timeout=1,
            lock_wait_timeout=1,
        )
    assert "s3cret" not in str(caught.value)
    assert "--format=custom" in calls[0]


def test_the_password_goes_in_the_environment_not_the_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_url = "postgresql://backup:s3%2Fcret@postgres.railway.internal:5432/railway"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs["env"]))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backup.subprocess, "run", _run)
    backup.dump(
        secret_url,
        tmp_path / "x.dump",
        timeout=10,
        connect_timeout=1,
        lock_wait_timeout=1,
    )

    [(argv, env)] = calls
    assert not any("s3" in arg and "cret" in arg for arg in argv)
    assert env["PGPASSWORD"] == "s3/cret"
    [dbname] = [arg for arg in argv if arg.startswith("--dbname=")]
    assert "postgres.railway.internal" in dbname and "backup" in dbname


def test_a_malformed_url_is_refused_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(BackupError) as caught:
        backup.dump(
            "postgresql://backup:s3cret@host/db?bogus=1",
            tmp_path / "x.dump",
            timeout=10,
            connect_timeout=1,
            lock_wait_timeout=1,
        )
    assert "s3cret" not in str(caught.value)


def test_each_service_reads_only_its_own_timeout_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db_ops import drill, poller

    # The old shared names are not read by anyone.
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "7")
    monkeypatch.setenv("DB_BACKUP_RUN_TIMEOUT_SECONDS", "99")
    monkeypatch.setenv("DB_BACKUP_DB_CONNECT_TIMEOUT_SECONDS", "9")

    own = ("db_backup_run_timeout_seconds", "db_backup_db_connect_timeout_seconds")
    backup_settings = Settings(
        **{k: v for k, v in _settings().model_dump().items() if k not in own}
    )
    assert backup_settings.db_backup_run_timeout_seconds == 99
    assert backup_settings.db_backup_db_connect_timeout_seconds == 9

    poller_settings = poller.Settings(
        db_poller_database_url_kaianolevine="postgresql://a/x",
        db_poller_database_url_deejaytools="postgresql://b/x",
        db_poller_aws_access_key_id="AKIATEST",
        db_poller_aws_secret_access_key="secret",
    )
    assert poller_settings.db_poller_run_timeout_seconds == 120
    assert poller_settings.db_poller_db_connect_timeout_seconds == 10
    assert poller_settings.db_poller_db_statement_timeout_ms == 5000

    drill_settings = drill.Settings(
        db_drill_database_url_kaianolevine="postgresql://a/x",
        db_drill_database_url_deejaytools="postgresql://b/x",
        db_drill_railway_token="tok",
        railway_project_id="proj",
        railway_environment_id="env",
        railway_environment_name="production",
        db_backup_bucket="backups",
        db_drill_aws_access_key_id="AKIATEST",
        db_drill_aws_secret_access_key="secret",
    )
    assert drill_settings.db_drill_run_timeout_seconds == 5400
    assert drill_settings.db_drill_db_connect_timeout_seconds == 10
    assert drill_settings.db_drill_db_statement_timeout_ms == 60_000


def test_a_missing_pg_dump_is_a_backup_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _run(args: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError("pg_dump")

    monkeypatch.setattr(backup.subprocess, "run", _run)
    with pytest.raises(BackupError, match="not installed"):
        backup.dump(
            "postgresql://x",
            tmp_path / "x.dump",
            timeout=10,
            connect_timeout=1,
            lock_wait_timeout=1,
        )


def test_a_hung_dump_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _run(args: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(backup.subprocess, "run", _run)
    with pytest.raises(BackupError, match="did not finish"):
        backup.dump(
            "postgresql://x",
            tmp_path / "x.dump",
            timeout=10,
            connect_timeout=1,
            lock_wait_timeout=1,
        )


@respx.mock
def test_pings_are_sent_only_when_the_effect_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.get(url__startswith="https://hc-ping.com/test-check").mock(
        return_value=httpx.Response(200)
    )
    monkeypatch.setenv("HEALTHCHECKS_ENABLED", "false")
    backup._ping(_settings(), "/start")
    assert route.call_count == 0

    monkeypatch.setenv("HEALTHCHECKS_ENABLED", "true")
    backup._ping(_settings(), "/fail")
    assert route.call_count == 1
    assert str(route.calls[0].request.url).endswith("/test-check/fail")


def test_a_failed_run_reports_and_pings_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup, "_enabled", lambda: True)
    monkeypatch.setattr(
        backup.Settings, "from_env", classmethod(lambda cls: _settings())
    )
    pings: list[str] = []
    monkeypatch.setattr(
        backup, "_ping", lambda settings, suffix="": pings.append(suffix)
    )
    reports: list[FakeReport] = []

    def _report(**kwargs: Any) -> FakeReport:
        reports.append(FakeReport())
        return reports[-1]

    monkeypatch.setattr(backup, "RunReport", _report)

    def _boom(settings: Settings, report: Any, now: datetime) -> list[str]:
        raise BackupError("backup failed for: deejaytools")

    monkeypatch.setattr(backup, "back_up", _boom)

    with pytest.raises(BackupError):
        backup.work("run-1")
    assert pings == ["/start", "/fail"]
    assert reports[0].sent == [True]


@respx.mock
def test_a_rejected_ping_is_logged_as_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(url__startswith="https://hc-ping.com/test-check").mock(
        return_value=httpx.Response(400)
    )
    monkeypatch.setenv("HEALTHCHECKS_ENABLED", "true")

    with caplog.at_level("WARNING"):
        backup._ping(_settings(), "/start")

    assert "ping_rejected" in caplog.text
    assert "status=400" in caplog.text
