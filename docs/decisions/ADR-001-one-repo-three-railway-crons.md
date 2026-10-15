# ADR-001: One Repository, Three Railway Cron Services

**Date:** 2026-10-15
**Status:** Accepted
**Repo:** db-ops
**Tasks:** MINI-58 through MINI-66 (Asana, MiniAppPolis board)

---

## Context

The two production Postgres databases on Railway had no backups, no restore
drill, and no metrics outside Railway's own dashboard. Three pieces of work
close that:

- **db-poller** — Postgres metrics (connections, size, cache hit ratio,
  oldest transaction, pg_stat_statements totals, WAL archive health) to
  CloudWatch every 5 minutes.
- **db-backup** — a nightly `pg_dump` of each database to S3: the one copy
  that does not live on Railway.
- **db-drill** — restores from both backup layers on a schedule and checks
  the result, so a recovery point and recovery time are measured rather
  than assumed.

The plan doc placed the poller on Lambda. Lambda would reach the databases
only through their public TCP proxies, from outside Railway's private
network.

## Decision

**Railway cron services, not Lambda.** All three run in the `mini-app-polis
ecosystem` project's production environment and reach both databases over
Railway private networking (`*.railway.internal`), so no database
connection crosses the public internet. Decided 2026-10-14; overrides the
plan doc's Phase 2.

**One repository, three services.** The three share a domain (both
databases), dependencies (a Postgres driver, AWS credentials, the shared
logger) and a release cadence, so they share a repository. They do not
share a process: each is its own Railway service with its own start command
(`python -m db_ops.poller` / `.backup` / `.drill`), so a failing backup can
never be hidden by a healthy poller, and each is scheduled, alarmed and
restarted on its own.

**A cron run is the retry unit.** `restartPolicyType: NEVER`: a failed run
exits non-zero, reports, and the next scheduled run is the retry.

**Production only.** Development databases carry no backups and need no
metrics; each service gates its work on the environment.

## Consequences

- Registered as `pipeline-cog` rather than a new repo type. That type now
  means a queue-driven Lambda (ecosystem-standards ADR-009), so the rules
  that describe a queue, a dead-letter target or a Lambda function do not
  fit. They are recorded as deferrals in `evaluator.yaml` — one repository
  is not enough to justify a new type (ecosystem-standards ADR-002). Once
  wiki-curator-cog moves to Lambda, db-ops is the only Railway-hosted cog,
  and if a second repository of this shape appears, the type is what
  should change.
- Each service's source, build, start command, schedule, restart policy,
  region and variable references are declared in `.railway/railway.ts`
  (Railway Infrastructure as Code), planned on the pull request into
  `main` and applied by CI when it merges — never from a workstation.
  The original plan was one config file per service under `railway/`, but
  Railway refuses a per-service config path for new services
  (config-as-code is deprecated, and stops being read on 2026-12-01;
  amended 2026-10-15, MINI-63). The file is a named partial, `db-ops`,
  because the ecosystem project holds services from other repositories:
  it owns these three and cannot touch the rest (amended 2026-10-15,
  MINI-67).
- Secrets use per-service names (`SENTRY_DSN_DB_OPS`, and the database and
  AWS names each task adds) because Doppler syncs one shared config to every
  Railway service.
