"""The hub that connects the copilot to MCP servers: isolation and clean failure."""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from agent.mcp_hub import McpHub
from agent.servers import ServerSpec

PROBE = str(Path(__file__).parent / "helpers" / "probe_server.py")


def probe_spec(namespace: str, env: dict) -> ServerSpec:
    return ServerSpec(key=namespace, namespace=namespace, server_name=namespace, path=Path(PROBE).parent, port=0,
                      command=sys.executable, args=[PROBE], env=env)


def test_child_servers_do_not_inherit_model_or_account_keys(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-must-not-leak-0000")
    monkeypatch.setenv("SYNAPSE_API_KEY", "syn-must-not-leak-0000")

    async def read():
        async with McpHub([probe_spec("probe", {"ENDPOINT_BASE_URL": "http://127.0.0.1:1"})]) as hub:
            out = {}
            for name in ("LLM_API_KEY", "SYNAPSE_API_KEY", "ENDPOINT_BASE_URL", "PATH"):
                out[name] = json.loads((await hub.call("probe__get_env", {"name": name}))[0])["value"]
            return out

    seen = asyncio.run(read())
    assert seen["LLM_API_KEY"] is None and seen["SYNAPSE_API_KEY"] is None
    assert seen["ENDPOINT_BASE_URL"] == "http://127.0.0.1:1"      # what the spec declares is passed
    assert seen["PATH"]                                           # the SDK's minimal system environment


def test_first_server_is_stopped_when_a_later_one_fails(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    good = probe_spec("good", {"PROBE_HEARTBEAT_FILE": str(heartbeat)})
    bad = ServerSpec(key="bad", namespace="bad", server_name="bad", path=tmp_path, port=0,
                     command=sys.executable, args=["-c", "import sys; sys.exit(3)"], env={})

    async def start():
        async with McpHub([good, bad]):
            pass

    with pytest.raises(BaseException):
        asyncio.run(start())
    assert heartbeat.exists(), "the first server should have started"
    time.sleep(1.0)
    last = float(heartbeat.read_text(encoding="utf-8"))
    time.sleep(1.0)
    assert float(heartbeat.read_text(encoding="utf-8")) == last, "the first server kept running after the hub failed"


def test_missing_generated_server_is_reported_clearly(tmp_path):
    missing = ServerSpec(key="x", namespace="x", server_name="x", path=tmp_path, port=0,
                         command=sys.executable, args=[str(tmp_path / "synapse_auto_server.py")], env={})

    async def start():
        async with McpHub([missing]):
            pass

    with pytest.raises(SystemExit) as err:
        asyncio.run(start())
    assert "build_mcp.py" in str(err.value)


def test_server_logs_stay_out_of_the_console_but_explain_a_failed_start(tmp_path, capfd):
    noisy = ServerSpec(key="noisy", namespace="noisy", server_name="noisy", path=tmp_path, port=0, command=sys.executable,
                       args=["-c", "import sys; sys.stderr.write('boom: cannot bind sk-leaky-key-0000\\n'); sys.exit(3)"], env={})

    async def start():
        async with McpHub([noisy]):
            pass

    import os
    os.environ["SOME_API_KEY"] = "sk-leaky-key-0000"
    try:
        with pytest.raises(Exception) as err:
            asyncio.run(start())
    finally:
        del os.environ["SOME_API_KEY"]
    notes = "\n".join(getattr(err.value, "__notes__", []))
    assert "boom: cannot bind" in notes and "sk-leaky-key-0000" not in notes      # diagnosable, and masked
    assert "boom" not in capfd.readouterr().err                                   # nothing was dumped on the console


def test_working_servers_print_nothing_to_the_console(capfd):
    async def read():
        async with McpHub([probe_spec("probe", {})]) as hub:
            return await hub.call("probe__get_env", {"name": "PATH"})

    _, is_error = asyncio.run(read())
    captured = capfd.readouterr()
    assert not is_error and "Processing request" not in captured.err + captured.out
