"""Runs the PikPak simulator's JavaScript engine tests (simulator/tests) under node."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_simulator_engine_js():
    result = subprocess.run(
        ["node", "--test", str(REPO / "simulator" / "tests" / "engine.test.js")],
        capture_output=True, text=True, cwd=REPO,
    )
    assert result.returncode == 0, result.stdout + result.stderr
