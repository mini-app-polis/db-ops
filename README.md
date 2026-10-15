# db-ops

[![CI](https://github.com/mini-app-polis/db-ops/actions/workflows/ci.yml/badge.svg)](https://github.com/mini-app-polis/db-ops/actions/workflows/ci.yml)
[![Version](https://img.shields.io/github/v/tag/mini-app-polis/db-ops?label=version)](https://github.com/mini-app-polis/db-ops/releases)

Postgres operations for the ecosystem's two production databases on Railway —
`Postgres` (api-kaianolevine-com and the cogs) and `Postgres-deejaytools`
(api-deejaytools). One repository, three Railway cron services, each with its
own start command so a failure in one can never hide in another:

| Service | Start command | Job | Task |
|---|---|---|---|
| `db-poller` | `python -m db_ops.poller` | Publishes both databases' metrics to CloudWatch, `MiniAppPolis/Postgres` | MINI-58 |
| `db-backup` | `python -m db_ops.backup` | Nightly `pg_dump -Fc` of both databases to S3 — the copy that is not on Railway | MINI-63 |
| `db-drill` | `python -m db_ops.drill` | Monthly: restores a point-in-time fork of each database and the latest S3 dump of each, verifies them, deletes them, and reports the measured RPO and RTO | MINI-64, MINI-65 |

All three are live: `db-poller` (MINI-58), `db-backup` (MINI-63) and
`db-drill` (MINI-64, MINI-65).

All three run from one image, built from the root `Dockerfile`, which brings
the PostgreSQL 18 client tools (`pg_dump`, `pg_restore`) from the PostgreSQL
apt repository — see [ADR-002](docs/decisions/ADR-002-one-image-with-the-postgres-18-client.md).

## db-backup

Nightly at 09:17 UTC. For each database it runs `pg_dump --format=custom` as
the `backup` role (`pg_read_all_data`, nothing else) over private networking
and uploads the dump with the write-only key:

```
daily/<database>/<UTC timestamp>.dump      every run    (kept 30 days)
monthly/<database>/<UTC timestamp>.dump    on the 1st   (kept 90 days)
```

Both databases are attempted whatever happens to the other. Any failure
ends the run with exit 1, a Sentry event, a Healthchecks.io `/fail` ping and
a WARN run report; a clean run sends a run report naming what it wrote.
Outside production it logs that it would have run and exits 0.

Each run is one wake: Railway starts the process on the service's cron
schedule, it does its work, and it exits. Railway reports the run by exit
code and skips a scheduled start while the previous one is still running, so
a run that raises exits 1 and no run may hang. See
[ADR-001](docs/decisions/ADR-001-one-repo-three-railway-crons.md) for why this
is Railway rather than Lambda and one repository rather than three.

---

## db-poller

Every 5 minutes. For each database, as the `metrics` role (`pg_monitor`,
nothing else) over private networking, it reads ten values and publishes
them in one `PutMetricData` under `MiniAppPolis/Postgres`, dimension
`Database`:

| Metric | Unit | Source |
|---|---|---|
| `Connections`, `ConnectionUtilization` | Count, Percent | `pg_stat_activity` against `max_connections` |
| `DatabaseSize` | Bytes | `pg_database_size` |
| `CacheHitRatio` | Percent | `pg_stat_database` |
| `OldestTransactionAge` | Seconds | `pg_stat_activity` |
| `StatementCalls`, `StatementExecTime` | Count, Milliseconds | `pg_stat_statements` totals; mean = `DIFF(time)/DIFF(calls)` in CloudWatch |
| `ArchiveFailedCount`, `ArchiveAge` | Count, Seconds | `pg_stat_archiver` (PITR's WAL archive) |
| `WalDirSize` | Bytes | `pg_ls_waldir()` |

A database it cannot reach publishes nothing for itself, which is what the
`DatabaseReachable` alarm (MINI-61) watches for; a failed publish fails the
run. Production only, through `Effect.CLOUDWATCH_METRICS`.

## db-drill

Monthly, 09:43 UTC on the 2nd. For each database in turn, as the `drill`
role (`pg_read_all_data`, and owner of the `ops` schema):

1. Writes a **canary** row to `ops.drill_canary`, reads the database clock,
   waits two seconds and writes a **fence** row. The restore target is one
   second after the canary.
2. Waits for `pg_stat_archiver` to show the fence's WAL segment archived.
   Canary to archived is the **measured RPO**.
3. Asks Railway for a point-in-time restore to the target, into a new
   service `drill-<database>-<UTC minute>` (Railway's public API,
   `volumeInstancePITRRestore`). The source keeps running untouched.
4. Polls until the fork has a private host and answers as a promoted
   primary. Request to promoted is the **measured RTO**.
5. Verifies over private networking: the canary is in the fork and the
   fence is not; `schema_migrations` matches the source; every source table
   exists in the fork, and none with 100 or more rows on the source is
   empty in it.
6. Deletes the fork's volume, then the fork, in a `finally`, and waits for
   Railway to confirm it is gone.

**The teardown is the point of the design.** A PITR fork carries the
source's archive-bucket credential, which can write to production's WAL
archive (Railway has no read-only variant). A fork whose deletion cannot be
confirmed fails the run, and every run flags any `drill-*` service older
than 30 minutes and any volume in production that is mounted on nothing.
Railway deletes volumes asynchronously; one it has accepted for deletion
counts as gone.

**Never deploy db-drill, and never touch a `drill-*` service, while a run
is going.** A deploy stops the running container before its `finally` can
tear the forks down, and deleting a fork's service by hand leaves its
volume — a full copy of production — behind. If a run is cut short,
delete each leftover fork's volume first, then the service.

Then the **S3 dump layer** (MINI-65), for each database: the newest
`daily/<database>/` dump is downloaded with the read-only key (its age is
this layer's effective RPO), restored with `pg_restore --no-owner --no-acl
--exit-on-error` into a throwaway PostgreSQL 18 that the drill starts inside
its own container on a Unix socket, and checked against the source as
above minus the canary — the dump's migrations must all be ones the source
has, since the source may have moved on since last night. A newest dump
older than 36 hours fails the drill: db-backup has stopped. The scratch
server and every file are removed at the end. Each layer is drilled
whatever happened to the other.

Every run sends a run report with each database's `rpo_seconds`,
`rto_seconds`, tables and rows, and `dump_age_hours` and
`dump_restore_seconds`; a failure is a WARN (so it reaches the
errors channel), goes to Sentry and exits 1. It refuses to run from any
Railway environment but production.

It needs a Railway token that may create and delete services:
`DB_DRILL_RAILWAY_TOKEN`, a project token for the production environment
by default (`DB_DRILL_RAILWAY_TOKEN_KIND=project`), or a workspace token
(`workspace`) if a project token is refused.

## Recovery record

What each production database can be recovered to, from what, and how fast
— measured by `db-drill`, not estimated (MINI-66). There are two layers. The
Railway workspace is on the Hobby plan, so there are no volume backups and
no third layer.

| Database | Layer | RPO | RTO | Last successful drill |
|---|---|---|---|---|
| `kaianolevine` (`Postgres`) | Point-in-time recovery — Railway, pgBackRest → Railway bucket | **≤ 1 min** — measured 4–51 s | **≤ 3 min** — measured 136–160 s | 2026-10-15 21:32 UTC |
| `kaianolevine` (`Postgres`) | Nightly `pg_dump` — S3, off Railway | **≤ 24 h** — the dump drilled was 2.3 h old | **seconds** to restore — measured 1 s for 9.9 MB | 2026-10-15 21:41 UTC |
| `deejaytools` (`Postgres-deejaytools`) | Point-in-time recovery | **≤ 1 min** — measured 4–49 s | **≤ 3 min** — measured 119–128 s | 2026-10-15 21:37 UTC |
| `deejaytools` (`Postgres-deejaytools`) | Nightly `pg_dump` | **≤ 24 h** — the dump drilled was 2.3 h old | **seconds** to restore — measured < 1 s for 217 KB | 2026-10-15 21:41 UTC |

How to read it:

- **Point-in-time recovery** is the first choice for anything that goes
  wrong inside a database — a bad migration, a dropped table, a runaway
  script. RPO is how far the WAL archive trails the database: the drill
  writes a canary, measures until its WAL segment is archived, restores to
  one second after it, and checks the canary is there and the next row is
  not. `archive_timeout=60` bounds it at about a minute. RTO is from asking
  Railway for the restore to the fork answering as a promoted primary; it
  does **not** include cutting the application over to the fork (swapping
  `DATABASE_URL`), which is manual and undrilled. Restore window: about four
  weeks (Railway keeps the last four weekly full backups).
- **The nightly dump** is for when Railway itself is the problem. Its RPO is
  the age of the newest dump, at most a day plus the run (09:17 UTC daily); a
  newest dump older than 36 hours fails the drill. Its RTO above is the
  `pg_restore` into a scratch PostgreSQL 18 only — standing up a new
  database somewhere to restore into is not part of the drill and would
  dominate a real recovery.
- Every figure comes from these runs, all 2026-10-15 (UTC), all verified
  against the live source (migrations, every table, no emptied tables):

  | Run | Version | PITR `kaianolevine` RPO / RTO | PITR `deejaytools` RPO / RTO | Dump layer |
  |---|---|---|---|---|
  | 20:43 | 1.3.0 | 13 s / 160 s | 14 s / 128 s | — (not built yet) |
  | 21:01 | 1.3.2 | 51 s / 136 s | 4 s / 119 s | — |
  | 21:20 | 1.4.0 | 4 s / 154 s | cut short by a deploy | — |
  | 21:32 | 1.4.1 | 24 s / 151 s | 49 s / 119 s | both pass, dumps 2.3 h old |

  Run `e58b74bc` (21:32) is the first clean run of both layers, with
  teardown confirmed. The evidence is the run reports in the Discord runs
  channel and the comments on MINI-64 and MINI-65.
- The drill repeats monthly (09:43 UTC on the 2nd). When a run's figures
  move outside the ranges above, update this table.

### Erasure and backups (deejaytools)

deejaytools holds real users' data. A row deleted from the live database is
**not** gone from its backups until they age out:

- point-in-time recovery: up to about **4–5 weeks** (the oldest retained
  weekly full backup and the WAL after it);
- nightly dumps in S3: `daily/` up to **38 days** (30-day expiry, then
  8 days as a noncurrent version), `monthly/` up to **98 days** (90 + 8).

The data-erasure path deejaytools does not have yet must say so: erasure
reaches the live database at once and the backups when they expire, and
nothing restored from a backup older than an erasure may be put back into
service without re-applying it.

---|---|---|---|
| Point-in-time recovery | Railway (pgBackRest → Railway bucket) | ~1 min, ~4-week window | Railway; drilled by `db-drill` |
| Nightly logical dump | S3, off Railway | 24 h | `db-backup`; drilled by `db-drill` |

The Railway workspace is on the Hobby plan, so there are no volume backups.

---

## Running locally

Secrets come from Doppler's shared `dev` config through the
[Doppler CLI](https://docs.doppler.com/docs/install-cli) — nothing reads a
`.env` file, and local runs never use `prd`.

```bash
brew install gnupg dopplerhq/cli/doppler   # once per machine
doppler login                              # once per machine

git clone git@github.com:mini-app-polis/db-ops.git
cd db-ops
doppler setup                              # reads doppler.yaml: mini-app-polis-ecosystem / dev
uv sync --all-extras
uv run pre-commit install
uv run pre-commit run --all-files
```

Run one service:

```bash
doppler run -- uv run python -m db_ops.poller
```

### Tests

```bash
uv run pytest      # needs no Doppler and no secrets
uv run mypy src/
```

---

## Deployment

Railway, `mini-app-polis ecosystem` project, **production environment only**
— there is nothing for these services to do against the development
databases. Each service builds the root `Dockerfile` from `main`, and all
three are declared in [`.railway/railway.ts`](.railway/railway.ts) — Railway
Infrastructure as Code, a named partial (`db-ops`) that manages these three
services and nothing else in the project:

| Service | Start command | Cron (UTC) | Restart policy |
|---|---|---|---|
| `db-backup` | `python -m db_ops.backup` | `17 9 * * *` | Never |
| `db-poller` | `python -m db_ops.poller` | `*/5 * * * *` | Never |
| `db-drill` | `python -m db_ops.drill` | `43 9 2 * *` | Never |

A cron run that fails is retried by the next scheduled run, not by a
restart. `tests/integration/test_start_commands.py` proves each start
command runs.

`.railway/` is applied by CI and never from a workstation. A pull request
into `main` that changes it runs `railway-plan.yml`, which posts the plan
as a comment and pins it; merging the pull request runs
`railway-apply.yml`, which applies exactly that plan. If the environment
changed after the plan (a dashboard edit) or `.railway/` changed before the
merge, the apply fails and the pull request needs a fresh plan — what was
reviewed is what lands. Railway itself does not read `.railway/` when it
deploys.

Read the plan comment before merging: a service missing from the file is
deleted. The apply refuses to run on the 2nd between 09:00 and 10:59 UTC,
because changing db-drill redeploys it, and a redeploy stops a running
drill before its forks are torn down; re-run the job afterwards.

The workflows authenticate with the `RAILWAY_TOKEN` repository secret: a
Railway project token for the ecosystem project's production environment,
which can plan and apply there and nowhere else.

Each service's variables are references to the environment's shared
variables (`${{shared.NAME}}`), which Doppler `prd` keeps current; the file
declares the names, never a value.

`main` deploys to production on push; `dev` is where work lands first.
Versioning is semantic-release from conventional commits.

## Configuration

See [`.env.example`](.env.example) for every variable the code reads.
