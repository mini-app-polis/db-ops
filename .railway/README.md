# .railway

`railway.ts` declares db-ops' three Railway services — db-poller, db-backup
and db-drill — as Railway Infrastructure as Code. It is a named partial
(`db-ops`): it manages those three and nothing else in the
`mini-app-polis ecosystem` project, and only in production.

It is applied by CI, never from a workstation: a pull request into `main`
that changes `.railway/` gets the plan as a comment
(`.github/workflows/railway-plan.yml`), and merging it applies exactly that
plan (`railway-apply.yml`). Railway itself does not read this directory at
deploy time.

See the main README's Deployment section, and ADR-001.
