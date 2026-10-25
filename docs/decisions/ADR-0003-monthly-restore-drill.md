# ADR-0003: The Restore Drill Runs Monthly

**Date:** 2026-10-16
**Status:** Accepted
**Repo:** db-ops

---

## Context

db-drill restores both recovery layers of both production databases and
measures the result (README, db-drill). It was first set to run monthly
without recording why, and the planning that preceded it had proposed a
quarterly drill by hand. Once the drill was automated, the cadence became a
choice about what it should catch and what each run costs.

What each run costs:

- **Money: almost nothing.** Two PITR forks live 2–3 minutes each, and
  Railway does not charge for restore traffic out of the archive bucket. The
  S3 dumps are megabytes. The standing costs, the PITR archive and the
  nightly dumps, are paid whether or not anything is drilled.
- **Exposure.** Each fork carries the source's archive credential, which can
  write to production's WAL archive, for as long as it exists. A deploy
  during a run can leave forks behind, so the CI apply refuses to run during
  the scheduled window.

What the drill catches that nothing else does: slow drift. Railway changing
its restore API or token permissions, a token or key expiring, the schema or
migration runner moving away from what the checks expect. Fast failures
already have faster signals: a failed nightly dump alerts that night, and a
broken WAL archive trips `archive-failing` or `wal-dir-filling` within
minutes (README, db-poller).

## Decision

**Monthly, at 09:43 UTC on the 2nd** (`43 9 2 * *`). Slow drift is caught
within a month, the recovery record's figures are never more than a month
old, and the exposure and the apply blackout happen twelve times a year
rather than fifty-two.

## Consequences

- A problem in the restore path can go unnoticed for up to a month. That is
  accepted because nothing between drills would use the restore path either.
- Run it by hand (README, "Running a drill by hand") after anything that
  could break recovery: a Railway change to PITR, a token or key rotation, a
  Postgres major upgrade, a change to db-drill itself.
- Revisit the cadence, more often for a while, if a scheduled drill fails or
  its figures move outside the recovery record's ranges.
