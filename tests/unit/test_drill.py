"""db-drill: the canary, the restore, the verification, and above all the teardown.

Postgres and Railway are replaced at their seams — the connection factory
and the API object — so these run with no database, no token and no fork.
The canary/fence logic itself was proven once against a real local PITR
(archive, base backup, recovery_target_time): recovery stopped before the
fence's commit and kept the canary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from mini_app_polis.pipeline_status import RunReport
from psycopg import sql

from db_ops import drill
from db_ops.drill import DrillError, Settings
from db_ops.railway_api import RailwayApiError, Service, ServiceInstance, VolumeInstance

CANARY_AT = datetime(2026, 11, 2, 9, 43, 0, tzinfo=UTC)
FENCE_WAL = "000000010000000000000012"
SOURCE_URL = "postgresql://drill:pw@postgres.railway.internal:5432/railway"
FORK_HOST = "drill-fork.railway.internal"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "db_drill_database_url_kaianolevine": SOURCE_URL,
        "db_drill_database_url_deejaytools": SOURCE_URL.replace(
            "postgres.", "postgres-twzl."
        ),
        "db_drill_railway_token": "tok",
        "railway_project_id": "proj",
        "railway_environment_id": "env",
        "railway_environment_name": "production",
        "teardown_wait_seconds": 0,
        "archive_wait_seconds": 0,
        "restore_timeout_seconds": 60,
        "db_backup_bucket": "backups",
        "db_drill_aws_access_key_id": "AKIATEST",
        "db_drill_aws_secret_access_key": "secret",
    }
    values.update(overrides)
    return Settings(**values)


def _no_sleep(seconds: float) -> None:
    return None


# ── Fakes ────────────────────────────────────────────────────────────────


class Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class Conn:
    """A database that answers the drill's queries from a few fields."""

    def __init__(
        self,
        *,
        kinds: set[str] | None = None,
        migrations: list[str] | None = None,
        tables: dict[str, int] | None = None,
        archived: list[str | None] | None = None,
        in_recovery: bool = False,
    ) -> None:
        self.kinds = kinds if kinds is not None else {"canary"}
        self.migrations = migrations if migrations is not None else ["001.sql"]
        self.tables = tables if tables is not None else {"sets": 500, "plays": 3}
        self.archived = archived if archived is not None else [FENCE_WAL]
        self.in_recovery = in_recovery
        self.inserted: list[str] = []
        self.closed = False

    def __enter__(self) -> Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True

    def execute(self, query: Any, params: tuple[Any, ...] | None = None) -> Result:
        if isinstance(query, sql.Composable):
            table = query.as_string(None).split(".")[-1].strip('"')
            if table not in self.tables:
                raise psycopg.errors.UndefinedTable(table)
            return Result([(self.tables[table],)])
        if "CREATE TABLE" in query:
            return Result([])
        if "INSERT INTO ops.drill_canary" in query:
            self.inserted.append("fence" if "'fence'" in query else "canary")
            return Result([])
        if "clock_timestamp()" in query:
            return Result([(CANARY_AT,)])
        if "pg_walfile_name" in query:
            return Result([(FENCE_WAL,)])
        if "pg_stat_archiver" in query:
            wal = self.archived.pop(0) if len(self.archived) > 1 else self.archived[0]
            return Result([(wal, CANARY_AT + timedelta(seconds=42) if wal else None)])
        if "pg_is_in_recovery" in query:
            return Result([(self.in_recovery,)])
        if "FROM ops.drill_canary" in query:
            return Result([(k,) for k in sorted(self.kinds)])
        if "schema_migrations" in query:
            if self.migrations == ["<missing>"]:
                raise psycopg.errors.UndefinedTable("schema_migrations")
            return Result([(m,) for m in self.migrations])
        if "pg_stat_user_tables" in query:
            return Result([("public", t) for t in sorted(self.tables)])
        raise AssertionError(f"unexpected query: {query}")


class Api:
    """Railway, with a fork that appears once a restore is asked for."""

    def __init__(
        self,
        *,
        workflow: tuple[str, str | None] = ("Running", None),
        fork_appears: bool = True,
        delete_sticks: bool = True,
        stale: list[Service] | None = None,
    ) -> None:
        self.services: dict[str, str] = {
            "Postgres": "s-pg",
            "Postgres-deejaytools": "s-dj",
        }
        self.volumes: dict[str, list[str]] = {"s-pg": ["v-pg"], "s-dj": ["v-dj"]}
        self.workflow = workflow
        self.fork_appears = fork_appears
        self.delete_sticks = delete_sticks
        self.stale = stale or []
        self.calls: list[tuple[str, Any]] = []

    def environment_instances(
        self,
    ) -> tuple[list[ServiceInstance], list[VolumeInstance]]:
        services = [ServiceInstance(i, n) for n, i in self.services.items()]
        volumes = [
            VolumeInstance(f"vi-{v}", v, sid)
            for sid, vs in self.volumes.items()
            for v in vs
        ]
        return services, volumes

    def find_service(self, name: str) -> ServiceInstance | None:
        sid = self.services.get(name)
        return ServiceInstance(sid, name) if sid else None

    def volumes_of(self, service_id: str) -> list[VolumeInstance]:
        return [
            VolumeInstance(f"vi-{v}", v, service_id)
            for v in self.volumes.get(service_id, [])
        ]

    def project_services(self) -> list[Service]:
        return self.stale

    def workflow_status(self, workflow_id: str) -> tuple[str, str | None]:
        return self.workflow

    def private_domain(self, service_id: str) -> str | None:
        return FORK_HOST

    def pitr_restore(self, volume_instance_id: str, target: datetime, name: str) -> str:
        self.calls.append(("restore", (volume_instance_id, target, name)))
        if self.fork_appears:
            self.services[name] = f"s-{name}"
            self.volumes[f"s-{name}"] = [f"v-{name}"]
        return "wf-1"

    def delete_volume(self, volume_id: str) -> None:
        self.calls.append(("delete_volume", volume_id))
        if self.delete_sticks:
            self.volumes = {
                sid: [v for v in vs if v != volume_id]
                for sid, vs in self.volumes.items()
            }

    def delete_service(self, service_id: str) -> None:
        self.calls.append(("delete_service", service_id))
        if self.delete_sticks:
            self.services = {n: i for n, i in self.services.items() if i != service_id}


def _connector(source: Conn, fork: Conn) -> Any:
    def connect_to(url: str, settings: Settings) -> Conn:
        return fork if FORK_HOST in url else source

    return connect_to


def _report() -> RunReport:
    return RunReport(flow_name="db-drill", repo="db-ops", run_id="run-1")


def _drill_all(api: Api, source: Conn, fork: Conn, **settings: Any) -> Any:
    report = _report()
    try:
        result = drill.drill_all(
            api,  # type: ignore[arg-type]
            _settings(**settings),
            report,
            "run-1",
            sleep=_no_sleep,
            connect_to=_connector(source, fork),
        )
    except DrillError as exc:
        return report, exc
    return report, result


# ── The happy path ───────────────────────────────────────────────────────


def test_a_clean_drill_restores_verifies_and_tears_down_both_forks() -> None:
    api, source, fork = Api(), Conn(), Conn()

    report, figures = _drill_all(api, source, fork)

    assert set(figures) == {"kaianolevine", "deejaytools"}
    assert figures["kaianolevine"]["rpo_seconds"] == 42
    assert figures["kaianolevine"]["tables"] == 2
    assert report.processed == 2 and not report.issues
    assert report.counters["deejaytools_rpo_seconds"] == 42
    assert source.inserted == ["canary", "fence"] * 2

    restores = [args for op, args in api.calls if op == "restore"]
    assert [r[0] for r in restores] == ["vi-v-pg", "vi-v-dj"]
    # The target is one second after the canary — before the fence.
    assert restores[0][1] == CANARY_AT + timedelta(seconds=1)
    assert restores[0][2].startswith("drill-kaianolevine-")

    # Each fork: its volume, then the fork itself, and nothing else.
    deletes = [c for c in api.calls if c[0].startswith("delete")]
    assert [op for op, _ in deletes] == ["delete_volume", "delete_service"] * 2
    assert all(str(arg).startswith(("v-drill-", "s-drill-")) for _, arg in deletes)
    assert not any(n.startswith("drill-") for n in api.services)
    assert fork.closed


def test_the_fork_is_only_used_once_it_has_left_recovery() -> None:
    api, source = Api(), Conn()
    states = iter([True, True, False])
    connected: list[Conn] = []

    def connect_to(url: str, settings: Settings) -> Conn:
        if FORK_HOST not in url:
            return source
        conn = Conn(in_recovery=next(states))
        connected.append(conn)
        return conn

    report = _report()
    drill.drill(
        api,  # type: ignore[arg-type]
        "kaianolevine",
        "Postgres",
        SOURCE_URL,
        "run-1",
        _settings(),
        report,
        deadline=float("inf"),
        sleep=_no_sleep,
        connect_to=connect_to,
    )

    assert [c.in_recovery for c in connected] == [True, True, False]
    assert connected[0].closed and connected[1].closed


def test_the_archive_is_waited_for() -> None:
    source = Conn(archived=[None, "000000010000000000000011", FENCE_WAL])
    report, figures = _drill_all(Api(), source, Conn(), archive_wait_seconds=60)

    assert not report.issues and figures["kaianolevine"]["rpo_seconds"] == 42


# ── Verification ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fork", "expected"),
    [
        (Conn(kinds=set()), "canary missing"),
        (Conn(kinds={"canary", "fence"}), "fence present"),
        (Conn(migrations=["001.sql", "002.sql"]), "schema_migrations differ"),
        (Conn(migrations=["<missing>"]), "schema_migrations unreadable"),
        (Conn(tables={"sets": 0, "plays": 3}), "public.sets empty in the fork"),
        (Conn(tables={"sets": 500}), "public.plays missing from the fork"),
    ],
)
def test_a_fork_that_does_not_match_fails_the_drill_and_is_still_torn_down(
    fork: Conn, expected: str
) -> None:
    api = Api()

    report, exc = _drill_all(api, Conn(), fork)

    assert isinstance(exc, DrillError)
    assert expected in " ".join(report.examples["drill_failed"])
    assert not any(n.startswith("drill-") for n in api.services)


def test_a_small_table_may_be_empty_at_the_target() -> None:
    report, figures = _drill_all(Api(), Conn(), Conn(tables={"sets": 500, "plays": 0}))

    assert not report.issues


def test_a_source_with_no_tables_is_not_a_pass() -> None:
    report, exc = _drill_all(Api(), Conn(tables={}), Conn(tables={}))

    assert "no tables" in " ".join(report.examples["drill_failed"])


# ── Failure, and teardown whatever happens ──────────────────────────────


def test_an_unarchived_fence_fails_before_anything_is_restored() -> None:
    api = Api()

    report, exc = _drill_all(api, Conn(archived=[None]), Conn())

    assert isinstance(exc, DrillError)
    assert "was not archived" in " ".join(report.examples["drill_failed"])
    assert not [c for c in api.calls if c[0] == "restore"]


def test_a_failed_restore_workflow_fails_fast_and_cleans_up() -> None:
    api = Api(workflow=("Error", "pgbackrest: no backup set"))

    report, exc = _drill_all(api, Conn(), Conn())

    assert "no backup set" in " ".join(report.examples["drill_failed"])
    assert ("delete_service", "s-" + api.calls[0][1][2]) in api.calls


def test_a_fork_that_never_promotes_times_out_and_is_torn_down() -> None:
    api = Api()

    report, exc = _drill_all(
        api, Conn(), Conn(in_recovery=True), restore_timeout_seconds=0
    )

    assert "never finished recovery" in " ".join(report.examples["drill_failed"])
    assert not any(n.startswith("drill-") for n in api.services)


def test_a_fork_that_never_appears_is_flagged() -> None:
    report, exc = _drill_all(
        Api(fork_appears=False), Conn(), Conn(), restore_timeout_seconds=0
    )

    assert report.issues["fork_never_appeared"] == 2
    assert "did not appear" in " ".join(report.examples["drill_failed"])


def test_a_fork_that_survives_deletion_fails_the_run_loudly() -> None:
    api = Api(delete_sticks=False)

    report, exc = _drill_all(api, Conn(), Conn())

    assert isinstance(exc, DrillError)
    assert report.issues["teardown_failed"] == 2
    assert "teardown failures: 2" in str(exc)


def test_a_teardown_api_failure_is_recorded_not_swallowed() -> None:
    api = Api()

    def refuse(volume_id: str) -> None:
        raise RailwayApiError("Railway API error: Not Authorized")

    api.delete_volume = refuse  # type: ignore[method-assign]

    report, exc = _drill_all(api, Conn(), Conn())

    assert report.issues["teardown_failed"] == 2
    assert "Not Authorized" in " ".join(report.examples["teardown_failed"])


def test_a_delete_that_times_out_but_happens_is_not_a_failure() -> None:
    # Seen on the first live drill: Railway took longer to answer the
    # deletes than the request waited, and deleted the fork anyway.
    api = Api()
    real_delete_service = api.delete_service

    def slow(service_id: str) -> None:
        real_delete_service(service_id)
        raise RailwayApiError("Railway API unreachable: ReadTimeout")

    api.delete_service = slow  # type: ignore[method-assign]

    report, figures = _drill_all(api, Conn(), Conn())

    assert not report.issues
    assert report.processed == 2
    assert not any(n.startswith("drill-") for n in api.services)


def test_a_volume_left_behind_fails_the_teardown() -> None:
    api = Api()

    def keep(volume_id: str) -> None:
        api.calls.append(("delete_volume", volume_id))

    api.delete_volume = keep  # type: ignore[method-assign]

    report, exc = _drill_all(api, Conn(), Conn())

    assert report.issues["teardown_failed"] == 2
    assert "volume v-drill-" in " ".join(report.examples["teardown_failed"])


def test_a_missing_source_service_or_volume_fails_that_database_only() -> None:
    api = Api()
    del api.services["Postgres"]

    report, exc = _drill_all(api, Conn(), Conn())

    assert report.processed == 1
    assert "no service named 'Postgres'" in " ".join(report.examples["drill_failed"])

    api = Api()
    api.volumes["s-dj"] = []
    report, exc = _drill_all(api, Conn(), Conn())
    assert "0 volumes" in " ".join(report.examples["drill_failed"])


def test_out_of_time_skips_what_is_left() -> None:
    report, exc = _drill_all(Api(), Conn(), Conn(), run_timeout_seconds=0)

    assert report.issues["out_of_time"] == 2 and isinstance(exc, DrillError)


def test_old_drill_services_are_flagged_and_new_ones_are_not() -> None:
    now = datetime.now(UTC)
    api = Api(
        stale=[
            Service(
                "s-old", "drill-kaianolevine-20261001-0943", now - timedelta(days=1)
            ),
            Service("s-new", "drill-deejaytools-20261102-0943", now),
        ]
    )
    api.services["drill-kaianolevine-20261001-0943"] = "s-old"
    api.services["drill-deejaytools-20261102-0943"] = "s-new"

    report, _ = _drill_all(api, Conn(), Conn())

    assert report.examples["stale_drill_service"] == [
        "drill-kaianolevine-20261001-0943"
    ]


def test_a_failed_stale_check_is_flagged() -> None:
    api = Api()

    def refuse() -> list[Service]:
        raise RailwayApiError("Railway API answered 503")

    api.project_services = refuse  # type: ignore[method-assign]
    api.services["drill-x"] = "s-x"

    report, _ = _drill_all(api, Conn(), Conn())

    assert report.issues["stale_check_failed"] == 1


def test_teardown_never_touches_a_service_without_the_prefix() -> None:
    with pytest.raises(DrillError, match="not a drill fork"):
        drill.tear_down(Api(), "Postgres", _settings(), sleep=_no_sleep)  # type: ignore[arg-type]


def test_a_non_production_railway_environment_is_refused() -> None:
    api = Api()

    with pytest.raises(DrillError, match="forks hold production data"):
        drill.drill_all(
            api,  # type: ignore[arg-type]
            _settings(railway_environment_name="development"),
            _report(),
            "run-1",
        )
    assert api.calls == []


# ── The run ──────────────────────────────────────────────────────────────


def test_outside_production_the_run_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_DRILL_ENABLED", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")

    assert drill.work("run-1") == {"service": "db-drill", "skipped": True}


def test_missing_configuration_names_every_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("DB_DRILL_DATABASE_URL_KAIANOLEVINE", "DB_DRILL_RAILWAY_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(DrillError, match="DB_DRILL_RAILWAY_TOKEN"):
        Settings.from_env()


class FakeReport:
    def __init__(self, **kwargs: Any) -> None:
        self.sent: list[bool] = []

    def send(self, *, notable: bool = False) -> None:
        self.sent.append(notable)


@pytest.mark.parametrize(
    ("pitr_fails", "dump_fails"), [(False, False), (True, False), (False, True)]
)
def test_both_layers_are_drilled_and_every_run_reports(
    monkeypatch: pytest.MonkeyPatch, pitr_fails: bool, dump_fails: bool
) -> None:
    monkeypatch.setenv("DB_DRILL_ENABLED", "true")
    for key, value in _settings().model_dump().items():
        raw = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        if raw is not None:
            monkeypatch.setenv(key.upper(), str(raw))
    reports: list[FakeReport] = []

    def _new_report(**kwargs: Any) -> FakeReport:
        reports.append(FakeReport())
        return reports[-1]

    dump_calls: list[Settings] = []

    def _drill_all(api: Any, settings: Settings, report: Any, run_id: str) -> Any:
        assert api._headers["Project-Access-Token"] == "tok"
        if pitr_fails:
            raise DrillError("restore failed")
        return {"kaianolevine": {"rto_seconds": 120}}

    def _dump_drill_all(settings: Settings, report: Any) -> Any:
        dump_calls.append(settings)
        if dump_fails:
            raise DrillError("pg_restore exited 1")
        return {"kaianolevine": {"age_hours": 0.5}}

    from db_ops import dump_drill

    monkeypatch.setattr(drill, "RunReport", _new_report)
    monkeypatch.setattr(drill, "drill_all", _drill_all)
    monkeypatch.setattr(dump_drill, "drill_all", _dump_drill_all)

    if pitr_fails or dump_fails:
        with pytest.raises(DrillError, match="pitr" if pitr_fails else "dump"):
            drill.work("run-1")
    else:
        assert drill.work("run-1")["drilled"] == {
            "pitr": {"kaianolevine": {"rto_seconds": 120}},
            "dump": {"kaianolevine": {"age_hours": 0.5}},
        }
    # A failed PITR drill does not stop the dump drill, and the report goes
    # out either way.
    assert len(dump_calls) == 1
    assert reports[0].sent == [True]
