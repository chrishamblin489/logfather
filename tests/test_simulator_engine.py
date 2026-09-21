"""Runs the PikPak simulator's JavaScript tests (simulator/tests/*.test.js) under node."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_simulator_js():
    tests = sorted(str(p) for p in (REPO / "simulator" / "tests").glob("*.test.js"))
    assert tests
    result = subprocess.run(
        ["node", "--test", *tests],
        capture_output=True, text=True, cwd=REPO,
    )
    assert result.returncode == 0, result.stdout + result.stderr
