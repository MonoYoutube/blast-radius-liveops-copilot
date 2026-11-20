"""What the guided demo leaves behind when it does not reach its regular clean-up.

Each test runs scripts/demo.py against an isolated, running copy of the systems (eval/instance.py):
a failing step, Ctrl+C, edits made by somebody else meanwhile, and an endpoint that was already there.
The demo may take back only what it changed itself.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest
import requests

from agent.mcp_hub import McpHub
from agent.servers import ROOT, load_servers
from eval import instance, scripted_check
from scripts import build_mcp, demo, live_update
from tests.conftest import synapse_available

pytestmark = [pytest.mark.synapse, pytest.mark.skipif(not synapse_available(), reason="Synapse CLI not installed")]

# Stands in for the Synapse CLI: every call goes to the real CLI, except the rebuild that would
# discover the live endpoint. That one fails (WRAPPER_MODE=fail) or never returns (WRAPPER_MODE=hang).
WRAPPER = r"""
const { spawnSync } = require("child_process");
const fs = require("fs");
const args = process.argv.slice(2);
const real = JSON.parse(process.env.REAL_SYNAPSE);
const liveDiscover = args.includes("--discover") && args.some((a) => a.includes("/compensations"));
if (liveDiscover && process.env.WRAPPER_MODE === "fail") {
  console.error("simulated failure of synapse build --discover");
  process.exit(37);
} else if (liveDiscover && process.env.WRAPPER_MODE === "hang") {
  fs.writeFileSync(process.env.WRAPPER_MARKER, "reached");
  setInterval(() => {}, 1000);
} else {
  const viaNode = real[0] === "node";
  const command = viaNode ? process.execPath : real[0];
  const result = spawnSync(command, [...real.slice(1), ...args], { stdio: "inherit", shell: !viaNode && process.platform === "win32" });
  process.exit(result.status === null ? 1 : result.status);
}
"""


class Copy:
    """A running isolated copy, with the handful of observations the tests compare."""

    def __init__(self, inner):
        self.inner = inner
        self.game = inner.folders["game_backend"]
        self.desk = inner.folders["support_desk"]
        self.source = self.game / "incidents.py"
        self.server = self.game / "synapse_auto_server.py"
        self.cache = self.game / ".synapse" / "surface_cache.json"
        self.url = f"http://127.0.0.1:{inner.ports['game_backend']}"

    def files(self) -> dict:
        """Everything the demo may touch: the source, both generated servers, both scan caches (None = absent)."""
        paths = {"source": self.source, "server": self.server, "cache": self.cache,
                 "desk server": self.desk / "synapse_auto_server.py", "desk cache": self.desk / ".synapse" / "surface_cache.json"}
        return {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}

    def endpoint_status(self) -> int:
        deadline = time.time() + 30                          # a reloading API refuses connections for a moment
        while True:
            try:
                return requests.get(self.url + demo.NEW_ENDPOINT, timeout=5).status_code
            except requests.RequestException:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)

    def settle(self) -> None:
        """After the test itself edited the source: give the API reloader time to notice and come back."""
        time.sleep(1.5)
        demo.wait_for("/incidents", 200)

    def tools(self) -> set:
        async def names():
            async with McpHub(load_servers("local")) as hub:
                return set(hub.names())
        return asyncio.run(names())

    def assert_as_found(self, before: dict) -> None:
        after = self.files()
        assert [name for name in before if after[name] != before[name]] == [], "files differ from the state the demo found"
        assert live_update.block_state(self.source.read_text(encoding="utf-8")) == "absent"
        assert not list(self.game.glob("*.prev")) and not list(self.game.glob("synapse_auto_server_[0-9]*.py"))
        assert self.endpoint_status() == 404
        assert self.tools() == scripted_check.BASE_TOOLS


@pytest.fixture()
def copy():
    handlers = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGTERM", "SIGHUP", "SIGBREAK") if hasattr(signal, name)}
    with instance.running("original", reload=True) as inner:
        yield Copy(inner)
    for name, handler in handlers.items():                 # demo.main() installs its own; give pytest its handlers back
        signal.signal(getattr(signal, name), handler)


def run_in_process(monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["demo.py", "--auto", "--no-llm"])
    try:
        demo.main()
    except SystemExit as stop:
        return stop.code
    return 0


def run_as_command(tmp_path, mode: str = "", **popen) -> subprocess.Popen:
    env = dict(os.environ)
    if mode:
        wrapper = tmp_path / "synapse_double.js"
        wrapper.write_text(WRAPPER, encoding="utf-8")
        env.update(REAL_SYNAPSE=json.dumps(build_mcp.synapse_command()), SYNAPSE_BIN=str(wrapper),
                   WRAPPER_MODE=mode, WRAPPER_MARKER=str(tmp_path / "reached"))
    return subprocess.Popen([sys.executable, str(ROOT / "scripts" / "demo.py"), "--auto", "--no-llm"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **popen)


def test_failed_discover_build_is_rolled_back_and_the_demo_runs_again(copy, tmp_path):
    built = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_mcp.py"), "--fresh"], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr          # the copy now has scan caches, like a used working tree
    before = copy.files()
    assert before["cache"] and before["desk cache"]

    first = run_as_command(tmp_path, "fail")
    output = first.communicate(timeout=300)[0]
    assert first.returncode == demo.EXIT_STEP_FAILED, output
    assert "simulated failure of synapse build --discover" in output and "Step failed (exit 1)" in output
    assert "Putting everything back as it was found" in output and "restored byte for byte" in output
    assert "Initial state restored" in output and "RESTORE INCOMPLETE" not in output
    assert f"GET {demo.NEW_ENDPOINT} -> 404" in output
    copy.assert_as_found(before)
    assert all(passed for _, passed, *_ in asyncio.run(scripted_check.run("local", "baseline")))

    second = run_as_command(tmp_path)                       # the real CLI this time: a normal run from the restored state
    output = second.communicate(timeout=300)[0]
    assert second.returncode == 0, output
    assert "total: 11 tools" in output and "Done. The repository is back in its initial state." in output
    after = copy.files()
    assert after["source"] == before["source"] and after["server"] == before["server"] and after["desk server"] == before["desk server"]
    assert copy.endpoint_status() == 404 and copy.tools() == scripted_check.BASE_TOOLS
    assert copy.server.read_bytes().replace(b"\r\n", b"\n") == \
        (ROOT / "systems" / "game_backend" / "synapse_auto_server.py").read_bytes().replace(b"\r\n", b"\n")


def test_ctrl_c_after_the_discover_build_restores_source_servers_and_scan_caches(copy, monkeypatch, capfd):
    before = copy.files()
    assert before["cache"] is None                           # a fresh copy has no scan cache yet
    seen = {}
    ask = demo.ask

    def interrupt_at_step_8(question, no_llm, stage="baseline"):
        if stage == "live":                                  # by now the server has 7 tools and the cache knows the endpoint
            seen["tools"] = copy.tools()
            seen["cache"] = copy.cache.read_bytes()
            raise KeyboardInterrupt
        ask(question, no_llm, stage)

    monkeypatch.setattr(demo, "ask", interrupt_at_step_8)
    code = run_in_process(monkeypatch)
    output = capfd.readouterr().out

    assert code == demo.EXIT_INTERRUPTED, output
    assert seen["tools"] == scripted_check.LIVE_TOOLS and b"compensations" in seen["cache"]
    assert "Interrupted." in output and "Initial state restored" in output
    assert "the MCP server the demo found is back (6 tools)" in output
    assert "removed again (it was not there when the demo started)" in output      # the scan caches of this run
    copy.assert_as_found(before)


def test_a_step_failing_before_the_edit_leaves_the_tree_as_found(copy, monkeypatch, capfd):
    before = copy.files()

    def fail_at_step_2(question, no_llm, stage="baseline"):
        raise SystemExit("\nStep failed (exit 3).")

    monkeypatch.setattr(demo, "ask", fail_at_step_2)
    code = run_in_process(monkeypatch)
    output = capfd.readouterr().out

    assert code == demo.EXIT_STEP_FAILED, output
    assert "stopped before it edited the game backend" in output and "Initial state restored" in output
    assert "restored byte for byte" not in output            # the source was never edited, so it is not rewritten either
    copy.assert_as_found(before)


def fail_step_6_after(action):
    def step():
        action()
        raise SystemExit("\nStep failed (exit 1).")
    return step


def test_a_block_somebody_edited_meanwhile_is_left_alone_and_reported(copy, monkeypatch, capfd):
    before = copy.files()

    def edit_the_block():
        text = copy.source.read_text(encoding="utf-8").replace('"""List compensations', '"""(their wording) List compensations')
        copy.source.write_text(text, encoding="utf-8", newline="\n")

    monkeypatch.setattr(demo, "expect_stale_build_failure", fail_step_6_after(edit_the_block))
    code = run_in_process(monkeypatch)
    output = capfd.readouterr().out

    assert code == demo.EXIT_NOT_RESTORED, output
    assert "RESTORE INCOMPLETE" in output and "left in place" in output and "live_update.py revert" in output
    assert "Initial state restored" not in output
    assert "(their wording) List compensations" in copy.source.read_text(encoding="utf-8")     # their work is still there
    assert copy.files()["server"] == before["server"]                                            # what the builds rewrote went back


def test_notes_somebody_keeps_in_the_same_file_survive_the_rollback(copy, monkeypatch, capfd):
    """An uncommitted note that was already in the file, and another one saved while the demo was running."""
    earlier, during = b"# a developer note that was already here\n", b"# a developer note saved while the demo was running\n"
    copy.source.write_bytes(copy.source.read_bytes() + b"\n" + earlier)
    copy.settle()
    before = copy.files()

    def save_a_note_after_the_block():
        assert live_update.block_state(copy.source.read_text(encoding="utf-8")) == "applied"
        with copy.source.open("ab") as handle:
            handle.write(b"\n" + during)

    monkeypatch.setattr(demo, "expect_stale_build_failure", fail_step_6_after(save_a_note_after_the_block))
    code = run_in_process(monkeypatch)
    output = capfd.readouterr().out

    assert code == demo.EXIT_STEP_FAILED, output
    assert "were kept" in output and "Initial state restored" in output
    source = copy.source.read_bytes()
    assert source == before["source"] + b"\n" + during      # both notes, byte for byte; only the demo's block is gone
    assert earlier in source and b"list_incident_compensations" not in source
    compile(source, "incidents.py", "exec")
    assert copy.files()["server"] == before["server"] and copy.endpoint_status() == 404


def test_an_endpoint_that_was_already_there_is_not_removed(copy, monkeypatch, capfd):
    assert live_update.apply()                              # somebody applied it before the demo started
    demo.wait_for(demo.NEW_ENDPOINT, 200)                   # wait for the API reloader to serve that state
    theirs = copy.files()
    code = run_in_process(monkeypatch)
    output = capfd.readouterr().out

    assert code == demo.EXIT_STEP_FAILED, output
    assert "already in" in output and "will not remove it" in output and "live_update.py revert" in output
    assert "This run changed nothing." in output and "Putting" not in output
    assert copy.files() == theirs
    assert live_update.block_state(copy.source.read_text(encoding="utf-8")) == "applied"


@pytest.mark.skipif(sys.platform == "win32", reason="sends SIGINT to a process group; on Windows press Ctrl+C by hand")
def test_a_real_ctrl_c_during_the_discover_build(copy, tmp_path):
    """SIGINT to the whole foreground group, as a terminal does: the demo, build_mcp.py and the CLI all receive it."""
    before = copy.files()
    proc = run_as_command(tmp_path, "hang", start_new_session=True)
    deadline = time.time() + 180
    while not (tmp_path / "reached").exists():              # the hanging `synapse build --discover` has started
        assert proc.poll() is None and time.time() < deadline, "the demo did not reach the discover build"
        time.sleep(0.2)
    os.killpg(proc.pid, signal.SIGINT)
    output = proc.communicate(timeout=120)[0]

    assert proc.returncode == demo.EXIT_INTERRUPTED, output
    assert "Interrupted." in output and "Initial state restored" in output and "Traceback" not in output
    copy.assert_as_found(before)
