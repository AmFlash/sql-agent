"""Keeps the golden reference SQL honest: every reference answer must pass the strict validator and run."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_eval_harness_mock_mode_passes():
    r = subprocess.run([sys.executable, "eval/run_eval.py", "--mock", "--min", "1.0"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OVERALL" in r.stdout
