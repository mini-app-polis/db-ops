"""db-ops — Postgres operations for the ecosystem's production databases.

One package, three entry points, each its own Railway cron service with its
own start command, so a failure in one can never hide in another:

    python -m db_ops.poller   db-poller  — metrics to CloudWatch (MINI-58)
    python -m db_ops.backup   db-backup  — nightly pg_dump to S3 (MINI-63)
    python -m db_ops.drill    db-drill   — automated restore drills (MINI-64/65)

Install name:  db-ops
Import name:   db_ops
"""

from ._version import __version__ as __version__
