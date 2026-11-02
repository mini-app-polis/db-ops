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

**Monthly, at 09:43 UTC on the 3rd** (`43 9 3 * *`; moved from the 2nd on 2026-11-02). Slow drift is caught
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

## What a passing drill does not prove (2026-11-02)

A PITR restore replays WAL forward from the source's last pgBackRest base
backup (Railway takes a weekly full and a daily differential, at a time of
day set per database), and Railway's image sets `archive_timeout=60`, so
each database archives a 16 MB segment a minute however little it writes.

On 2026-11-02 a hand-run drill restored both databases to about 19:25 UTC.
kaianolevine replayed about ten hours of WAL (some 9 GB) from its 09:36
base backup and passed. deejaytools, twelve hours past its 07:41 base
backup, filled its restored service's 5 GB volume after replaying about
4.5 GB (`No space left on device`, then `recovery ended before configured
recovery target was reached`), and every restart replayed from the base
backup and failed again. The image logs `sized wal-drop= MiB` with no value
on the restored service, so the replay sizing it attempts did not take
effect there. Why one replay kept its WAL and the other did not is not
known; it is with Railway.

So:

- A restore's WAL replay grows with the time since the source's last base
  backup, and on a 5 GB volume a replay of a few GB has been seen to fail.
  The scheduled run, a couple of hours after each database's base backup,
  replays the least. A pass does not show that a restore to a later point
  that day would succeed.
- Until that is explained, treat a PITR restore to many hours after the last
  base backup as uncertain, with the S3 dump as the recovery path for
  those points. db-backup was moved from nightly to every four hours the
  same day, so that path loses at most four hours of writes.
- A failed hand-run drill should be read with its distance from each base
  backup in mind before it is treated as a broken restore path.
