"""Shared fixtures: both source systems running on their usual ports, and a guard for the committed files."""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.servers import load_servers  # noqa: E402
from scripts import run_apis  # noqa: E402

# Files a run may create next to the committed ones: caches, the Synapse scan cache and account binding, parked builds.
LOCAL_ONLY = ("__pycache__", ".synapse", ".pytest_cache")


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def synapse_available() -> bool:
    return bool(os.environ.get("SYNAPSE_BIN") or shutil.which("synapse"))


def start_api(spec) -> subprocess.Popen:
    """Start one API without auto-reload, through the same launcher the demo uses (it knows the Windows specifics)."""
    return run_apis._spawn(spec, reload=False, quiet=True)


def committed_files() -> dict[str, str]:
    """SHA-256 of every file under systems/, demo/ and mcp/exposure.json that belongs to the repository."""
    digests = {}
    for folder in ("systems", "demo"):
        for path in sorted((ROOT / folder).rglob("*")):
            relative = path.relative_to(ROOT)
            if not path.is_file() or any(part in LOCAL_ONLY for part in relative.parts) or path.suffix == ".prev":
                continue
            digests[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    digests["mcp/exposure.json"] = hashlib.sha256((ROOT / "mcp" / "exposure.json").read_bytes()).hexdigest()
    return digests


@pytest.fixture(scope="session", autouse=True)
def committed_tree_untouched():
    """The suite works on isolated copies. No test may change a source system, its data or its generated server."""
    before = committed_files()
    yield
    after = committed_files()
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    assert not changed, f"the test run changed committed files: {changed}"


@pytest.fixture(scope="session")
def apis():
    """Start whichever of the two APIs is not already running (e.g. via scripts/run_apis.py)."""
    started = [start_api(spec) for spec in load_servers("local") if not port_open(spec.port)]
    try:
        run_apis.wait_ready()
        yield {spec.key: spec.base_url for spec in load_servers("local")}
    finally:
        run_apis.stop(started)
