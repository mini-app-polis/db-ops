"""The shell every db-ops entry point runs inside.

Each service — db-poller, db-backup, db-drill — is a Railway cron: Railway
starts the process on a schedule, the process does one run and exits. This
module is everything around that run that the three have in common: the run
id, Sentry, the boot line, and turning the outcome into an exit code.

**The exit code is load-bearing.** Railway reports a cron run by the
process's exit status, and it skips a scheduled start while the previous
one is still running. A run that raises must therefore end non-zero, and a
run must never hang — each service's own work sets its deadlines.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

import sentry_sdk
from mini_app_polis import logger as log
from mini_app_polis.environment import current_environment

from db_ops._version import __version__

LOG = log.get_logger()

# The shared cogs Sentry project, under the same name every cog reads.
# Events are told apart by the `service` tag _init_sentry sets.
SENTRY_DSN_VAR = "SENTRY_DSN_COGS"

Work = Callable[[str], dict[str, Any]]


def _init_sentry(service: str) -> None:
    dsn = os.getenv(SENTRY_DSN_VAR, "").strip()
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=0.0,
        environment=current_environment().value,
        release=os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown"),
    )
    sentry_sdk.get_global_scope().set_tag("service", service)


def run(service: str, work: Work) -> int:
    """Run ``work`` once for ``service`` and return the process exit code.

    ``work`` receives the run id and returns a summary that is logged on
    success. Any exception it raises is captured to Sentry, logged, and
    turned into exit code 1.
    """
    # Minted before anything else, so the id is in the first log line —
    # the one a run that dies during setup still writes.
    run_id = str(uuid.uuid4())
    _init_sentry(service)
    LOG.info(
        "%s.boot env=%s version=%s run_id=%s",
        service,
        current_environment().value,
        __version__,
        run_id,
    )
    try:
        summary = work(run_id)
    except Exception as exc:  # noqa: BLE001 — top-level guard
        sentry_sdk.capture_exception(exc)
        LOG.error("%s.failed run_id=%s err=%s", service, run_id, exc)
        return 1
    LOG.info("%s.complete run_id=%s summary=%s", service, run_id, summary)
    return 0
