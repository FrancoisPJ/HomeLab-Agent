"""bench.py must run against fake-llm (its numbers are meaningless there)."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from homelab_agent.fakes.llm import create_app

from .conftest import ROOT


def test_bench_runs_against_fake_llm(run_server: Any, tmp_path: Any) -> None:
    srv = run_server(create_app())
    out = tmp_path / "bench.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench.py"), "--url", f"{srv.url}/v1",
         "--model", "fast", "--runs", "1", "--json", str(out)],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert "tool-calling" in proc.stdout
    assert out.exists()
