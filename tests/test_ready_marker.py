"""The browser readiness marker has to disappear when setup is interrupted."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from resolveai.settings import repo_root
from resolveai.testing import READY_PATH


def test_sigterm_removes_the_ready_marker() -> None:
    root = repo_root()
    ready = root / READY_PATH
    ready.unlink(missing_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-m", "resolveai.testing", "wait-ready"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    deadline = time.time() + 5
    while time.time() < deadline and not ready.is_file():
        if process.poll() is not None:
            raise AssertionError("ready waiter exited before writing the marker")
        time.sleep(0.05)
    assert ready.is_file()
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=5)
    assert process.returncode != 0
    assert not ready.exists()
