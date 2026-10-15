"""Each service's start command, run as Railway runs it.

The unit tests call ``main()`` in-process. This runs the module in its own
interpreter, so a broken ``__main__`` guard, an import that only fails at
startup, or a start command naming the wrong module fails here rather than
on the first scheduled run.

The start commands are set on each Railway service, not read from this
repository (see the README's Deployment section); this list is the record of
what they are, and a service whose command differs from it is misconfigured.
"""

from __future__ import annotations

import pytest

from .conftest import Runner

START_COMMANDS = {
    "db-poller": "python -m db_ops.poller",
    "db-backup": "python -m db_ops.backup",
    "db-drill": "python -m db_ops.drill",
}


@pytest.mark.parametrize(("service", "command"), sorted(START_COMMANDS.items()))
def test_start_command_runs_and_exits_zero(
    service: str, command: str, run_module: Runner
) -> None:
    module = command.removeprefix("python -m ")

    result = run_module(module)

    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert f"{service}.boot" in output
    assert f"{service}.complete" in output
