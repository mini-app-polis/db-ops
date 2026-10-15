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
| `db-drill` | `python -m db_ops.drill` | Restores the latest PITR point and S3 dump into scratch databases and verifies them | MINI-64, MINI-65 |

All three are **stubs** today: each boots, logs that it has nothing to do, and
exits 0. Each service's own task fills it in and creates its Railway service.

Each run is one wake: Railway starts the process on the service's cron
schedule, it does its work, and it exits. Railway reports the run by exit
code and skips a scheduled start while the previous one is still running, so
a run that raises exits 1 and no run may hang. See
[ADR-001](docs/decisions/ADR-001-one-repo-three-railway-crons.md) for why this
is Railway rather than Lambda and one repository rather than three.

---

## Recovery layers this repo serves

| Layer | Where | Recovery point | Owned by |
|---|---|---|---|
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
databases. Each service points at its own config file under `railway/`
(Settings → Config-as-code → `railway/db-poller.json`, and so on), which
holds its start command and `restartPolicyType: NEVER`: a cron run that fails
is retried by the next scheduled run, not by a restart. The cron schedule is
added to that file by the service's own task.

`main` deploys to production on push; `dev` is where work lands first.
Versioning is semantic-release from conventional commits.

## Configuration

See [`.env.example`](.env.example) for every variable the code reads.
