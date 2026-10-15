"""db-poller — both production databases' Postgres metrics to CloudWatch.

Filled in by MINI-58.

A stub until then: it boots, logs that it has nothing to do, and
exits 0, so the Railway service and its start command can be wired up and
proven before any work is in it.

    python -m db_ops.poller
"""

from __future__ import annotations

import sys
from typing import Any

from mini_app_polis import logger as log

from db_ops._entry import run

SERVICE = "db-poller"

LOG = log.get_logger()


def work(run_id: str) -> dict[str, Any]:
    """Do one run of this service's work and return its summary."""
    LOG.info("%s.stub run_id=%s nothing to do yet", SERVICE, run_id)
    return {"service": SERVICE, "stub": True}


def main() -> int:
    """Run the service once and return the process exit code."""
    return run(SERVICE, work)


if __name__ == "__main__":
    sys.exit(main())
