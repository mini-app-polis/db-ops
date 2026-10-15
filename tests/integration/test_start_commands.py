"""Each Railway start command, run as Railway runs it.

The unit tests call ``main()`` in-process. This runs the module in its own
interpreter, so a broken ``__main__`` guard, an import that only fails at
startup, or a start command naming the wrong module fails here rather than
on the first scheduled run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import Runner

RAILWAY_DIR = Path(__file__).resolve().parents[2] / "railway"

SERVICES = ["db-poller", "db-backup", "db-drill"]


def _start_command(service: str) -> str:
    config = json.loads((RAILWAY_DIR / f"{service}.json").read_text())
    return str(config["deploy"]["startCommand"])


@pytest.mark.parametrize("service", SERVICES)
def test_start_command_runs_and_exits_zero(service: str, run_module: Runner) -> None:
    command = _start_command(service)
    assert command.startswith("python -m db_ops.")
    module = command.removeprefix("python -m ")

    result = run_module(module)

    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert f"{service}.boot" in output
    assert f"{service}.complete" in output


@pytest.mark.parametrize("service", SERVICES)
def test_cron_runs_are_not_restarted(service: str) -> None:
    config = json.loads((RAILWAY_DIR / f"{service}.json").read_text())
    assert config["deploy"]["restartPolicyType"] == "NEVER"
