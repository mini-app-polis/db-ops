# One image for all three db-ops services; each Railway service picks its
# own start command (railway/<service>.json). See ADR-002.
#
# Why an image rather than Railpack: db-backup and db-drill need pg_dump and
# pg_restore at the servers' major version, 18, and pg_dump refuses to dump
# a newer server. Debian's own repositories stop short of 18, so the client
# comes from the PostgreSQL project's apt repository (PGDG), which Railpack
# has no way to add.
#
# db-drill also restores the nightly dumps into a throwaway PostgreSQL 18
# inside its own container (MINI-65), so the server is here too, with
# pgvector — api-kaianolevine-com's schema uses it — and without the
# cluster Debian would otherwise create at install.
#
# Base images are pinned by digest (CD-022). To move to a newer patch, look
# up the tag's current digest and replace it here, in a commit of its own.

FROM python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

# Keep the client's major in step with the servers' (postgres-ssl:18).
ARG PG_MAJOR=18

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    install -d /etc/postgresql-common; \
    echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf; \
    apt-get install -y --no-install-recommends \
        "postgresql-client-${PG_MAJOR}" \
        "postgresql-${PG_MAJOR}" \
        "postgresql-${PG_MAJOR}-pgvector"; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    pg_dump --version | grep -q " ${PG_MAJOR}\."; \
    "/usr/lib/postgresql/${PG_MAJOR}/bin/initdb" --version | grep -q " ${PG_MAJOR}\."

COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml uv.lock README.md ./
COPY src/db_ops/_version.py src/db_ops/_version.py
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

# CD-023: the final stage runs unprivileged. Dumps are written under /tmp,
# and so is db-drill's scratch server — initdb refuses to run as root anyway.
RUN useradd --system --uid 10001 --no-create-home --home-dir /app dbops
USER dbops
