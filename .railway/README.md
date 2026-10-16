# .railway

`railway.ts` declares db-ops' three Railway services — db-poller, db-backup
and db-drill — as Railway Infrastructure as Code. It is a named partial
(`db-ops`): it manages those three and nothing else in the
`mini-app-polis ecosystem` project, and only in production.

It is applied by CI, never from a workstation, as mini-app-polis/infra
applies Terraform: a pull request into `main` that changes `.railway/` is
planned, with the plan in the job summary (`.github/workflows/railway-plan.yml`),
and merging it plans again and applies (`railway-apply.yml`). Removing a
service or a variable is destructive and applies only if the pull request is
labelled **`allow-destroy`**. Actions → Railway apply → Run workflow
re-applies `main` by hand. Railway itself does not read this directory at
deploy time.

See the main README's Deployment section, and ADR-001.
