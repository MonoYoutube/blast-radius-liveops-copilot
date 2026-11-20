"""Shared fixtures: both source systems running on their usual ports."""
from __future__ import annotations

import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.servers import load_servers  # noqa: E402
from scripts import run_apis  # noqa: E402


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_api(spec, port: int | None = None) -> subprocess.Popen:
    """Start one API without auto-reload, through the same launcher the demo uses (it knows the Windows specifics)."""
    target = replace(spec, port=port) if port else spec
    return run_apis._spawn(target, reload=False, quiet=True)


@pytest.fixture(scope="session")
def apis():
    """Start whichever of the two APIs is not already running (e.g. via scripts/run_apis.py)."""
    started = [start_api(spec) for spec in load_servers("local") if not port_open(spec.port)]
    try:
        run_apis.wait_ready()
        yield {spec.key: spec.base_url for spec in load_servers("local")}
    finally:
        run_apis.stop(started)
