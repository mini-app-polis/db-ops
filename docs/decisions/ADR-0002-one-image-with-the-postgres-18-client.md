# ADR-0002: One Image, With the PostgreSQL 18 Client

**Date:** 2026-10-15
**Status:** Accepted
**Repo:** db-ops

---

## Context

db-backup runs `pg_dump` against both production databases, and db-drill
will run `pg_restore`. Both servers are `postgres-ssl:18`. `pg_dump` refuses
to dump a server newer than itself, so the client must be version 18 or
later.

Railpack, which built the repository until now, installs apt packages from
Debian's own repositories only. Those stop short of PostgreSQL 18, and
Railpack has no way to add the PostgreSQL project's repository (PGDG).

## Decision

**A root `Dockerfile`, used by all three services.** `python:3.11-slim-bookworm`
plus `postgresql-client-18` from PGDG, uv for the locked install, and an
unprivileged final user. Each Railway service builds this Dockerfile and runs
its own start command, so the three still share nothing at runtime but the
image.

The build fails if the installed `pg_dump` is not the pinned major, so a
repository change cannot silently hand the services an older client.

## Consequences

- db-ops is the first repository in the fleet with an image definition.
  The container rules that were waiting for one now have a subject:
  CD-022 (base images pinned by digest) and CD-023 (non-root final stage)
  apply and are met, and CD-021 — `status: gap` until "the first service
  adds a Dockerfile" — can be made checkable in ecosystem-standards.
- Base images move only by a digest change in a commit. A security fix in
  the base image is therefore a deliberate bump, not something that arrives
  on the next build.
- When the databases move to a new major, `PG_MAJOR` moves with them, in
  the same change.

## Amendment — 2026-10-15: the server too

db-drill restores the nightly S3 dumps into a throwaway PostgreSQL inside
its own container, never into a Railway database. So the image also carries
`postgresql-18` (the server) and `postgresql-18-pgvector`, because
api-kaianolevine-com's schema creates the `vector` extension and
`pg_restore --exit-on-error` fails without it. Debian's automatic `main`
cluster is switched off before install; the drill runs `initdb` itself,
under `/tmp`, as the unprivileged user. The build now also fails if the
server's `initdb` is not the pinned major.

The image is larger by the server's size; db-poller and db-backup carry
binaries they never run. Splitting images per service is the alternative,
and is not worth a second build for one package set.
