# db-ops

[![CI](https://github.com/mini-app-polis/db-ops/actions/workflows/ci.yml/badge.svg)](https://github.com/mini-app-polis/db-ops/actions/workflows/ci.yml)
[![Version](https://img.shields.io/github/v/tag/mini-app-polis/db-ops?label=version)](https://github.com/mini-app-polis/db-ops/releases)

Postgres operations for the ecosystem's two production databases on Railway —
`Postgres` (api-kaianolevine-com and the cogs) and `Postgres-deejaytools`
(api-deejaytools). One repository, three Railway cron services, each with its
own start command so a failure in one can never hide in another:

| Service | Start command | Job |
|---|---|---|
| `db-poller` | `python -m db_ops.poller` | Publishes both databases' metrics to CloudWatch, `MiniAppPolis/Postgres` |
| `db-backup` | `python -m db_ops.backup` | Nightly `pg_dump -Fc` of both databases to S3 — the copy that is not on Railway |
| `db-drill` | `python -m db_ops.drill` | Monthly: restores a point-in-time fork of each database and the latest S3 dump of each, verifies them, deletes them, and reports the measured RPO and RTO |

All three are live.

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

The bucket (mini-app-polis/infra `database_backups.tf`) is versioned, and
every object version sits under a 7-day governance-mode Object Lock, so not
even the writer's key can delete or overwrite a fresh dump. db-backup's key
(`db-backup-writer`) can only `PutObject` under `daily/` and `monthly/`: a
leaked key can add dumps but cannot read, list or delete one. db-drill reads
with a separate key (`db-backup-reader`: `GetObject` and `ListBucket` only).
Governance rather than compliance mode, so that a deejaytools user's erasure
request can still reach the backups.

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

A database it cannot reach publishes nothing for itself, which is what its
`unreachable` alarm watches for; a failed publish fails the run. Production
only, through `Effect.CLOUDWATCH_METRICS`.

The alarms (mini-app-polis/infra `database_metrics.tf`), one set per
database, email through the `mini-app-polis-data-alerts` topic on alarm and
on recovery. The `mini-app-polis-data` dashboard shows the same metrics.

| Alarm | Fires when |
|---|---|
| `postgres-<db>-unreachable` | No `Connections` sample for 3 periods in a row (15 min). Missing data counts as breaching, so this is the alarm for a database, or the poller, that has gone quiet |
| `postgres-<db>-connections-high` | `ConnectionUtilization` above 80% for 2 periods |
| `postgres-<db>-archive-failing` | `ArchiveFailedCount` rises in 2 periods in a row: WAL is not reaching the PITR archive |
| `postgres-<db>-wal-dir-filling` | `WalDirSize` above 1.5 GiB for 3 periods: the archive is falling behind, short of pgBackRest's ~2.2 GiB cap, past which it drops WAL and PITR has a hole |

Only `unreachable` treats missing data as breaching; the rest leave a
silent database to it.

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

Then the **S3 dump layer**, for each database: the newest
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

It needs a Railway token that may restore, read and delete services:
`DB_DRILL_RAILWAY_TOKEN`. In production it is a **workspace** token
(`DB_DRILL_RAILWAY_TOKEN_KIND=workspace`, set in `.railway/railway.ts`),
because Railway refuses `volumeInstancePITRRestore` with a project token
(found building the drill). That is a broad credential: it has its own Doppler
entry and only db-drill reads it. The code's default is still `project`, for
the day Railway allows it.

The cadence, and what would change it, is
[ADR-003](docs/decisions/ADR-003-monthly-restore-drill.md).

### Running a drill by hand

Trigger an off-schedule run of the production service; it runs exactly what
the schedule runs, both layers for both databases, and reports the same way:

```bash
railway cron run  --service db-drill --environment production
railway cron list --service db-drill --environment production   # status, then a ready-made logs command
```

The Railway dashboard's Cron Runs tab on db-drill does the same. A run took
7–10 minutes on 2026-10-15. Running it from a workstation does not drill:
outside production it logs that it would have and exits, and even with
`DB_DRILL_ENABLED=true` it refuses any Railway environment but production.

While a hand-run drill is going, **nothing protects it from a deploy**. The
CI apply guard covers only the scheduled window (the 2nd, 09:00–10:59 UTC),
and `main` deploys db-drill on push, so do not merge into `main` (a
commons-update bump included) until the run has finished. If one is cut
short anyway, clean up as above: each leftover fork's volume first, then
the service.

## Recovery record

What each production database can be recovered to, from what, and how fast
— measured by `db-drill`, not estimated. There are two layers. The
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
  channel.
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

## Recovering a database

**Never rehearsed end to end.** The drill proves each layer restores and
measures how fast; it stops before cutover. These steps are the starting
procedure, built from the drill and Railway's documentation. Correct them
after the first rehearsal or real use.

### Which layer

- **Something went wrong inside a database** (a bad migration, deleted rows,
  a runaway script) and Railway is fine: point-in-time recovery.
- **Only some rows are needed back:** restore a PITR fork, copy the rows out,
  delete the fork. Production stays where it is.
- **Railway has lost the database, its PITR bucket or the project:** the
  nightly dump. Anything written since the newest dump (up to a day) is gone.

### From point-in-time recovery

1. If the damage is still happening, stop the API service that writes to the
   database first.
2. Restore to a moment just before the damage, into a new service. Either
   the database's Backups tab → **Restore to this moment**, or:
   ```bash
   railway postgres pitr restore --service Postgres-deejaytools \
     --at 2026-10-16T14:05:00Z --new-service-name deejaytools-restored --yes
   ```
   The source keeps running untouched. Expect about 2–3 minutes (the drill's
   RTO).
3. Check the fork before trusting it: `schema_migrations` matches, every table
   is there, and the rows you lost are back.
4. Cut over by pointing every consumer at the fork. A physical restore brings
   the roles and their passwords with it, so only the host changes:

   | Database | Consumer | Setting |
   |---|---|---|
   | `kaianolevine` | api-kaianolevine-com | `DATABASE_URL` (a reference to the `Postgres` service, held on the service, not in Doppler), and `DATABASE_URL_MIGRATIONS` where set |
   | `kaianolevine` | db-ops | `DB_POLLER_` / `DB_BACKUP_` / `DB_DRILL_DATABASE_URL_KAIANOLEVINE` (Doppler `prd`), and `DB_DRILL_SOURCE_SERVICE_KAIANOLEVINE` if the service name changed |
   | `deejaytools` | api-deejaytools | `DEEJAYTOOLS_DATABASE_URL` (a reference to `Postgres-deejaytools`), and `DEEJAYTOOLS_DATABASE_URL_MIGRATIONS` where set |
   | `deejaytools` | db-ops | the same three `…_DEEJAYTOOLS` URLs, and `DB_DRILL_SOURCE_SERVICE_DEEJAYTOOLS` |

5. **The fork is not archiving.** Enable PITR on it (Backups tab; it gets its
   own bucket), or the database has no point-in-time recovery from here on.
6. The fork's recovery variables (`WAL_RECOVER_FROM_*`) still hold the old
   source's archive credential, which can write to the old archive. Keep the
   old service and its bucket until the fork is verified and archiving, then
   decide whether to remove those variables. That has not been tested.

### From the nightly dump

1. Find and download the dump, with an AWS profile that can read the bucket
   (the admin profile, or the `db-backup-reader` key):
   ```bash
   aws s3 ls "s3://$DB_BACKUP_BUCKET/daily/deejaytools/"
   aws s3 cp "s3://$DB_BACKUP_BUCKET/daily/deejaytools/<stamp>.dump" .
   ```
   Use `monthly/` for anything older than 30 days.
2. Create a new PostgreSQL 18 database. `kaianolevine` needs `pgvector`
   available on the server, because its schema creates the `vector` extension.
3. Restore: `pg_restore --no-owner --no-acl --exit-on-error -d "<new url>"
   <file>.dump`.
4. **Recreate the roles and grants.** The restore above carries no roles and
   no privileges: the runtime role (without DDL, per OPS-002), the migrations
   role, and `metrics`, `backup` and `drill` for db-ops. This step has never
   been written down; it is the gap a real dump recovery would hit first.
5. Cut over as in step 4 above, then turn on PITR for the new database.

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

`.railway/` is applied by CI and never from a workstation, as
mini-app-polis/infra applies Terraform. A pull request into `main` that
changes it runs `railway-plan.yml`, which writes the plan to the job
summary; merging the pull request runs `railway-apply.yml`, which plans
again and applies. Removing a service or a variable is destructive and is
refused unless the pull request is labelled `allow-destroy`. Actions →
Railway apply → Run workflow re-applies `main` by hand. Railway itself does
not read `.railway/` when it deploys. Both workflows call the fleet's shared
ones in `mini-app-polis/.github`, which pin the CLI version.

Read the plan before merging: a service missing from the file is
deleted, if the pull request is labelled `allow-destroy`. The apply refuses to run on the 2nd between 09:00 and 10:59 UTC,
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
