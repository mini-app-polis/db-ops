"""db-drill's S3 dump layer: which dump, where it is restored, what is checked.

S3, the scratch server and Postgres are replaced at their seams. The real
path — initdb, pg_ctl, pg_restore of a custom-format dump with pgvector and
pgcrypto, then the checks — was run once against local PostgreSQL binaries
while building it; ``ScratchPostgres`` is exercised here against stand-in
binaries that only record how they were called.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mini_app_polis.pipeline_status import RunReport

from db_ops import dump_drill
from db_ops.drill import DrillError, Settings
from db_ops.dump_drill import ScratchPostgres

from .test_drill import Conn, _settings

NOW = datetime(2026, 11, 2, 9, 45, tzinfo=UTC)


class S3:
    """A bucket of dump keys, each with the time it was written."""

    def __init__(self, keys: dict[str, datetime]) -> None:
        self.keys = keys
        self.downloaded: list[str] = []

    def get_paginator(self, name: str) -> Any:
        assert name == "list_objects_v2"
        keys = self.keys

        class Pages:
            def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
                found = [
                    {"Key": k, "LastModified": t}
                    for k, t in keys.items()
                    if k.startswith(Prefix)
                ]
                # Two pages, so pagination is followed.
                return [{"Contents": found[:1]}, {"Contents": found[1:]}, {}]

        return Pages()

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.downloaded.append(Key)
        return {"Body": Body([b"PGDMP"])}


class Body:
    """A streaming body that hands out its chunks, then nothing."""

    def __init__(self, chunks: list[bytes], on_read: Any = None) -> None:
        self.chunks = list(chunks)
        self.on_read = on_read
        self.closed = False

    def read(self, size: int) -> bytes:
        if self.on_read:
            self.on_read()
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        self.closed = True


def _bucket(**ages: float) -> S3:
    keys: dict[str, datetime] = {}
    for db in ("kaianolevine", "deejaytools"):
        keys[f"daily/{db}/2026-11-01T091700Z.dump"] = NOW - timedelta(hours=24.5)
        keys[f"daily/{db}/2026-11-02T091700Z.dump"] = NOW - timedelta(
            hours=ages.get(db, 0.5)
        )
        keys[f"daily/{db}/notes.txt"] = NOW
    return S3(keys)


class Scratch:
    """A scratch server that records what it was asked to restore."""

    def __init__(self, root: Path, fail_restore: set[str] | None = None) -> None:
        self.root = root
        self.fail_restore = fail_restore or set()
        self.restored: list[tuple[str, str]] = []
        self.entered = self.exited = False

    def __enter__(self) -> Scratch:
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited = True

    def conninfo(self, dbname: str) -> str:
        return f"host=/scratch dbname={dbname}"

    def restore(self, dbname: str, dump: Path, timeout: float) -> None:
        assert dump.exists()
        if dbname in self.fail_restore:
            raise DrillError('pg_restore exited 1: extension "vector" is not available')
        self.restored.append((dbname, dump.name))


def _run(
    tmp_path: Path,
    s3: S3,
    *,
    copy: Conn | None = None,
    source: Conn | None = None,
    scratch: Scratch | None = None,
    deadline: float | None = None,
    **settings: Any,
) -> tuple[RunReport, Any, Scratch]:
    scratch = scratch or Scratch(tmp_path)
    source = source or Conn()
    copy = copy or Conn()
    report = RunReport(flow_name="db-drill", repo="db-ops", run_id="run-1")

    def connect_to(url: str, s: Settings) -> Conn:
        return copy if url.startswith("host=/scratch") else source

    try:
        result: Any = dump_drill.drill_all(
            _settings(**settings),
            report,
            deadline=deadline,
            s3=s3,
            scratch_factory=lambda bin_dir: scratch,  # type: ignore[arg-type,return-value]
            connect_to=connect_to,
            now=NOW,
        )
    except DrillError as exc:
        result = exc
    return report, result, scratch


def test_the_newest_dump_of_each_database_is_restored_and_checked(
    tmp_path: Path,
) -> None:
    s3 = _bucket(kaianolevine=0.5, deejaytools=0.6)

    report, figures, scratch = _run(tmp_path, s3)

    assert s3.downloaded == [
        "daily/kaianolevine/2026-11-02T091700Z.dump",
        "daily/deejaytools/2026-11-02T091700Z.dump",
    ]
    assert scratch.restored == [
        ("kaianolevine", "kaianolevine.dump"),
        ("deejaytools", "deejaytools.dump"),
    ]
    assert figures["kaianolevine"]["age_hours"] == 0.5
    assert figures["deejaytools"]["tables"] == 2
    assert report.counters["deejaytools_dump_age_hours"] == 0.6
    assert report.processed == 2 and not report.issues
    # The scratch server is stopped, and no dump is left on disk.
    assert scratch.exited
    assert list(tmp_path.glob("*.dump")) == []


def test_a_stale_newest_dump_fails_the_drill(tmp_path: Path) -> None:
    s3 = _bucket(deejaytools=40)
    del s3.keys["daily/deejaytools/2026-11-01T091700Z.dump"]

    report, exc, _ = _run(tmp_path, s3)

    assert isinstance(exc, DrillError)
    assert "40.0 h old" in " ".join(report.examples["dump_drill_failed"])
    assert report.processed == 1


def test_no_dump_at_all_fails_that_database(tmp_path: Path) -> None:
    s3 = _bucket()
    s3.keys = {k: t for k, t in s3.keys.items() if "deejaytools" not in k}

    report, exc, _ = _run(tmp_path, s3)

    assert "no dump of deejaytools" in " ".join(report.examples["dump_drill_failed"])


def test_a_failed_restore_fails_that_database_and_cleans_up(tmp_path: Path) -> None:
    scratch = Scratch(tmp_path, fail_restore={"kaianolevine"})

    report, exc, _ = _run(tmp_path, _bucket(), scratch=scratch)

    assert "vector" in " ".join(report.examples["dump_drill_failed"])
    assert scratch.restored == [("deejaytools", "deejaytools.dump")]
    assert list(tmp_path.glob("*.dump")) == [] and scratch.exited


@pytest.mark.parametrize(
    ("copy", "ok"),
    [
        # Taken last night; a migration applied since is fine.
        (Conn(migrations=["001.sql"]), True),
        (Conn(migrations=["001.sql", "999.sql"]), False),
        (Conn(migrations=[]), False),
        (Conn(tables={"sets": 0, "plays": 3}), False),
        (Conn(tables={"plays": 3}), False),
    ],
)
def test_the_restored_copy_is_checked_against_the_source(
    tmp_path: Path, copy: Conn, ok: bool
) -> None:
    source = Conn(migrations=["001.sql", "002.sql"])

    report, result, _ = _run(tmp_path, _bucket(), copy=copy, source=source)

    assert (not report.issues) is ok


def test_a_slow_download_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(dump_drill.time, "monotonic", lambda: clock[0])

    def _tick() -> None:
        clock[0] += 400  # each chunk takes 400 s

    class SlowS3(S3):
        def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            self.downloaded.append(Key)
            self.body = Body([b"x"] * 10, on_read=_tick)
            return {"Body": self.body}

    s3 = SlowS3(_bucket().keys)
    scratch = Scratch(tmp_path)

    report, result, _ = _run(
        tmp_path, s3, scratch=scratch, dump_restore_timeout_seconds=1000
    )

    assert isinstance(result, DrillError)
    assert report.issues.get("dump_drill_failed") == 2
    assert scratch.restored == []
    assert s3.body.closed
    assert not list(tmp_path.glob("*.dump"))


def test_the_restore_gets_only_what_the_run_has_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dump_drill.time, "monotonic", lambda: 1000.0)
    timeouts: list[float] = []

    class TimedScratch(Scratch):
        def restore(self, dbname: str, dump: Path, timeout: float) -> None:
            timeouts.append(timeout)
            super().restore(dbname, dump, timeout)

    _run(
        tmp_path,
        _bucket(),
        scratch=TimedScratch(tmp_path),
        deadline=1300.0,
        dump_restore_timeout_seconds=1200,
    )

    assert timeouts == [300.0, 300.0]


def test_a_database_not_started_before_the_deadline_is_out_of_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dump_drill.time, "monotonic", lambda: 2000.0)
    s3 = _bucket()

    report, result, scratch = _run(tmp_path, s3, deadline=1999.0)

    assert isinstance(result, DrillError)
    assert report.issues.get("out_of_time") == 2
    assert s3.downloaded == []


def test_a_scratch_server_that_will_not_start_fails_the_layer(tmp_path: Path) -> None:
    class Broken(Scratch):
        def __enter__(self) -> Scratch:
            raise DrillError("initdb exited 1: could not create directory")

    report, exc, _ = _run(tmp_path, _bucket(), scratch=Broken(tmp_path))

    assert isinstance(exc, DrillError) and "did not start" in str(exc)
    assert report.issues["dump_drill_failed"] == 1


# ── ScratchPostgres, against stand-in binaries ───────────────────────────


def _bin_dir(tmp_path: Path, failing: str | None = None) -> Path:
    """Executables that log their arguments, one of them failing if asked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("initdb", "pg_ctl", "createdb", "pg_restore"):
        exit_code = 1 if name == failing else 0
        script = bin_dir / name
        script.write_text(
            f'#!/bin/sh\necho "{name} $*" >> {log}\n'
            f'[ {exit_code} = 0 ] || echo "{name} failed" >&2\nexit {exit_code}\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def test_scratch_postgres_starts_restores_and_removes_itself(tmp_path: Path) -> None:
    bin_dir = _bin_dir(tmp_path)

    with ScratchPostgres(str(bin_dir)) as scratch:
        root = scratch.root
        assert scratch.conninfo("kaianolevine").startswith(f"host={root / 'sock'}")
        scratch.restore("kaianolevine", root / "x.dump", timeout=5)

    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert [c.split()[0] for c in calls] == [
        "initdb",
        "pg_ctl",
        "createdb",
        "pg_restore",
        "pg_ctl",
    ]
    assert "listen_addresses=''" in calls[1]
    assert "--no-owner" in calls[3] and "--exit-on-error" in calls[3]
    assert "stop" in calls[4]
    assert not root.exists()


def test_a_scratch_postgres_that_fails_to_start_leaves_nothing(tmp_path: Path) -> None:
    bin_dir = _bin_dir(tmp_path, failing="initdb")
    scratch = ScratchPostgres(str(bin_dir))

    with pytest.raises(DrillError, match="initdb exited 1: initdb failed"):
        scratch.__enter__()
    assert not scratch.root.exists()


def test_a_failed_stop_still_removes_the_directory(tmp_path: Path) -> None:
    bin_dir = _bin_dir(tmp_path)
    with ScratchPostgres(str(bin_dir)) as scratch:
        (bin_dir / "pg_ctl").write_text("#!/bin/sh\nexit 1\n")
    assert not scratch.root.exists()


@pytest.mark.skipif(
    not Path("/usr/lib/postgresql/18/bin/initdb").exists()
    or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs PostgreSQL 18 server binaries and a non-root user",
)
def test_a_real_scratch_server_accepts_a_connection() -> None:  # pragma: no cover
    import psycopg

    with ScratchPostgres("/usr/lib/postgresql/18/bin") as scratch:
        with psycopg.connect(scratch.conninfo("postgres")) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
