"""Fixtures for running the entry points the way Railway does.

Railway starts each service with ``python -m db_ops.<service>`` in a fresh
process. These fixtures do the same, with an environment stripped of
anything that would reach a real service.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

Runner = Callable[[str], subprocess.CompletedProcess[str]]


@pytest.fixture
def run_module() -> Runner:
    """Run ``python -m <module>`` in a child process and return the result."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SENTRY_DSN", "DB_", "AWS_"))
    }
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["ENVIRONMENT"] = "development"

    def _run(module: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", module],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

    return _run
