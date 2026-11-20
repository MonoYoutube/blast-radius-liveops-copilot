"""The live part of the demo, end to end: new endpoint -> synapse build --discover -> usable tool."""
import asyncio
import os
import shutil
import subprocess
import sys

import pytest
import requests

from agent.servers import ROOT, load_servers
from eval import scripted_check
from scripts import live_update, run_apis
from tests.conftest import start_api

PRIVATE_PORT = 8111
pytestmark = pytest.mark.synapse


def synapse_available() -> bool:
    return bool(os.environ.get("SYNAPSE_BIN") or shutil.which("synapse"))


def build(*flags: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "build_mcp.py"), "--system", "game_backend", *flags],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.mark.skipif(not synapse_available(), reason="Synapse CLI not installed")
def test_new_endpoint_becomes_a_tool_after_discover(apis):
    if live_update.is_applied(live_update.APP.read_text(encoding="utf-8")):
        pytest.skip("live endpoint is already applied in the working tree")
    game = next(spec for spec in load_servers("local") if spec.key == "game_backend")
    private = None
    try:
        assert build().returncode == 0                                   # make sure a scan cache exists
        assert live_update.apply()
        private = start_api(game, PRIVATE_PORT)                          # fresh process that has the new route
        os.environ["GAME_BACKEND_URL"] = f"http://127.0.0.1:{PRIVATE_PORT}"
        run_apis.wait_ready()
        assert requests.get(f"http://127.0.0.1:{PRIVATE_PORT}/incidents/INC-2026-11-18-EU2/compensations", timeout=10).status_code == 200

        stale = build("--include-live")                                  # cached scan does not know the endpoint
        assert stale.returncode != 0 and "Endpoints Not Found" in (stale.stdout + stale.stderr)
        assert game.generated_file.exists(), "a failed build must keep the previous server"

        fresh = build("--discover", "--include-live")
        assert fresh.returncode == 0, fresh.stdout + fresh.stderr
        assert "list_incident_compensations" in game.generated_file.read_text(encoding="utf-8")

        results = asyncio.run(scripted_check.run("local", "live"))       # a new MCP session sees the tool at once
        labels = [label for label, *_ in results]
        assert any(label.startswith("Q3") for label in labels)
        assert all(passed for _, passed, *_ in results), results
    finally:
        os.environ.pop("GAME_BACKEND_URL", None)
        if private:
            run_apis.stop([private])
        live_update.revert()
        assert build("--fresh").returncode == 0
    assert "list_incident_compensations" not in game.generated_file.read_text(encoding="utf-8")


@pytest.mark.skipif(not synapse_available(), reason="Synapse CLI not installed")
def test_committed_servers_are_exactly_what_synapse_generates(apis):
    """The generated files are never edited by hand: a fresh build must reproduce them byte for byte."""
    if live_update.is_applied(live_update.APP.read_text(encoding="utf-8")):
        pytest.skip("live endpoint is applied in the working tree")

    def snapshot():
        return {s.key: s.generated_file.read_text(encoding="utf-8").replace("\r\n", "\n") for s in load_servers("local")}

    before = snapshot()
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_mcp.py"), "--fresh"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot() == before
