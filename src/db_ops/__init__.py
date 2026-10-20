"""db-ops — Postgres operations for the ecosystem's production databases.

One package, three entry points, each its own Railway cron service with its
own start command, so a failure in one can never hide in another:

    python -m db_ops.poller   db-poller  — metrics to CloudWatch
    python -m db_ops.backup   db-backup  — nightly pg_dump to S3
    python -m db_ops.drill    db-drill   — automated restore drills

Install name:  db-ops
Import name:   db_ops
"""

from ._version import __version__ as __version__
