// The three db-ops Railway services, declared here and applied by CI: a pull
// request into main that changes .railway/ is planned
// (.github/workflows/railway-plan.yml), and merging it plans again and
// applies (railway-apply.yml), refusing to remove anything unless the pull
// request is labelled allow-destroy. Nothing is applied from a workstation.
// Railway itself does not read this file when it deploys.
//
// A named partial: the ecosystem project holds services from several
// repositories, so this file manages only what it declares — db-poller,
// db-backup and db-drill — and leaves every other service alone. Omitting
// one of these three from the file would delete it on the next apply.
//
// Production only. Planned against any other environment it declares
// nothing, and a partial that owns nothing there changes nothing.
//
// Variables are references to the environment's shared variables, which
// Doppler prd keeps current (`ctx.shared.NAME` is `${{shared.NAME}}`);
// no value lives in this file.

import { defineRailway, github, project, service } from "railway/iac";

export const partial = "db-ops";

const REGION = "us-east4-eqdc4a";

export default defineRailway((ctx) => {
  if (ctx.environment !== "production") {
    return project(ctx.projectName ?? "mini-app-polis ecosystem", {
      resources: [],
    });
  }

  const shared = (...names: string[]) =>
    Object.fromEntries(names.map((name) => [name, ctx.shared[name]]));

  // Every service: one image, built from the root Dockerfile on main
  // (ADR-0002), one replica, run on its cron, never restarted — a failed
  // run is retried by the next scheduled one (ADR-0001).
  const cron = (module: string, cronSchedule: string) => ({
    source: github("mini-app-polis/db-ops", { branch: "main" }),
    build: { builder: "DOCKERFILE" as const, dockerfilePath: "Dockerfile" },
    replicas: { [REGION]: 1 },
    deploy: {
      startCommand: `python -m db_ops.${module}`,
      cronSchedule,
      restartPolicyType: "NEVER" as const,
    },
  });

  // No budget, timeout or _ENABLED switch is passed: production runs on
  // the code defaults (.env.example lists them). A reference to a shared
  // variable Doppler does not hold would arrive empty and fail validation.
  const common = [
    "AWS_REGION",
    "DB_OPS_API_KEY",
    "ENVIRONMENT",
    "KAIANO_API_BASE_URL",
    "LOGGING_LEVEL",
    "SENTRY_DSN_COGS",
  ];

  // Every 5 minutes.
  const poller = service("db-poller", {
    ...cron("poller", "*/5 * * * *"),
    env: {
      ...shared(
        ...common,
        "DB_POLLER_AWS_ACCESS_KEY_ID",
        "DB_POLLER_AWS_SECRET_ACCESS_KEY",
        "DB_POLLER_DATABASE_URL_DEEJAYTOOLS",
        "DB_POLLER_DATABASE_URL_KAIANOLEVINE",
      ),
    },
  });

  // Nightly at 09:17 UTC.
  const backup = service("db-backup", {
    ...cron("backup", "17 9 * * *"),
    env: {
      ...shared(
        ...common,
        "DB_BACKUP_AWS_ACCESS_KEY_ID",
        "DB_BACKUP_AWS_SECRET_ACCESS_KEY",
        "DB_BACKUP_BUCKET",
        "DB_BACKUP_DATABASE_URL_DEEJAYTOOLS",
        "DB_BACKUP_DATABASE_URL_KAIANOLEVINE",
        "HEALTHCHECKS_URL_DB_BACKUP",
      ),
    },
  });

  // Monthly, 09:43 UTC on the 2nd. Never apply this
  // file while a drill is running: a deploy stops the run before it can
  // tear its forks down (README).
  const drill = service("db-drill", {
    ...cron("drill", "43 9 2 * *"),
    env: {
      ...shared(
        ...common,
        "DB_BACKUP_BUCKET",
        "DB_DRILL_AWS_ACCESS_KEY_ID",
        "DB_DRILL_AWS_SECRET_ACCESS_KEY",
        "DB_DRILL_DATABASE_URL_DEEJAYTOOLS",
        "DB_DRILL_DATABASE_URL_KAIANOLEVINE",
        "DB_DRILL_RAILWAY_TOKEN",
      ),
      // A project token cannot start a PITR restore.
      DB_DRILL_RAILWAY_TOKEN_KIND: "workspace",
    },
  });

  return project(ctx.projectName ?? "mini-app-polis ecosystem", {
    resources: [poller, backup, drill],
  });
});
