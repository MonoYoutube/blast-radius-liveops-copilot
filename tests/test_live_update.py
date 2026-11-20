"""The live update: the edit itself, and new endpoint -> synapse build --discover -> usable tool.

Everything here runs on an isolated copy of the systems (eval/instance.py). The committed backend is never edited.
"""
import asyncio
import os
import subprocess
import sys
import time

import pytest
import requests

from agent.servers import ROOT, load_servers
from eval import instance, scripted_check
from scripts import build_mcp, demo, live_update
from tests.conftest import synapse_available

NEW_ENDPOINT = "/incidents/INC-2026-11-18-EU2/compensations"
COMMITTED = ROOT / "systems" / "game_backend" / "incidents.py"


@pytest.fixture()
def backend(tmp_path, monkeypatch):
    """incidents.py of a private copy; live_update works on it instead of the committed file."""
    folders = instance.create(tmp_path, "original")
    for key, folder in folders.items():
        monkeypatch.setenv(f"{key.upper()}_DIR", str(folder))
    path = live_update.app_file()
    assert path == folders["game_backend"] / "incidents.py" and path != COMMITTED
    return path


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "live_update.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def build(*flags: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "build_mcp.py"), "--system", "game_backend", *flags],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def wait_status(url: str, wanted: int, seconds: int = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=3).status_code == wanted:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def test_apply_and_revert_are_exact_inverses(backend):
    before = backend.read_bytes()
    assert cli("status").stdout.strip() == "not applied"
    assert live_update.apply() and not live_update.apply()                 # the second apply changes nothing
    assert live_update.block_state(backend.read_text(encoding="utf-8")) == "applied"
    assert cli("status").stdout.strip() == "applied"
    assert b"\r\n" not in backend.read_bytes()
    assert live_update.revert() and not live_update.revert()
    assert backend.read_bytes() == before
    assert COMMITTED.read_bytes() == before                                # the committed file was only ever read


def test_revert_keeps_everything_outside_the_markers(backend):
    original = backend.read_bytes()
    live_update.apply()
    top, below = b"# a note somebody added at the top\n", b"\n\n# and code somebody added below\nEXTRA = 1\n"
    backend.write_bytes(top + backend.read_bytes() + below)
    assert live_update.revert()
    after = backend.read_bytes()
    assert after == top + original + below                                 # their bytes, untouched; only the block is gone
    compile(after, "incidents.py", "exec")                                 # still valid Python


CANONICAL = COMMITTED.read_bytes().replace(b"\r\n", b"\n")
PREFIXES = {
    "canonical": CANONICAL,
    "crlf": CANONICAL.replace(b"\n", b"\r\n"),
    "spare-blank-lines": CANONICAL + b"\n\n",
    "no-final-line-break": CANONICAL.rstrip(b"\n"),
    "crlf-no-final-line-break": CANONICAL.replace(b"\n", b"\r\n").rstrip(b"\r\n"),
    "bom": b"\xef\xbb\xbf" + CANONICAL,
    "note-at-the-end": CANONICAL + b"\n# an uncommitted developer note\n",
}
SUFFIXES = {
    "nothing": b"",
    "note-on-new-lines": b"\n# saved while the demo was paused\n",
    "note-right-after": b"# saved while the demo was paused",
    "crlf-note": b"\r\n# saved while the demo was paused\r\n",
}


@pytest.mark.parametrize("prefix", PREFIXES.values(), ids=list(PREFIXES))
@pytest.mark.parametrize("suffix", SUFFIXES.values(), ids=list(SUFFIXES))
def test_only_the_block_is_added_and_removed_byte_for_byte(backend, prefix, suffix):
    """Whatever the file looks like before, and whatever is appended after the block: revert leaves prefix + suffix."""
    backend.write_bytes(prefix)
    assert live_update.apply()
    applied = backend.read_bytes()
    assert applied.startswith(prefix)                                      # existing content is extended, never rewritten
    compile(applied, "incidents.py", "exec")
    backend.write_bytes(applied + suffix)
    assert live_update.block_state(backend.read_bytes()) == "applied"
    assert live_update.revert()
    assert backend.read_bytes() == prefix + suffix


def test_the_block_uses_the_line_endings_of_the_file(backend):
    crlf = CANONICAL.replace(b"\n", b"\r\n")
    backend.write_bytes(crlf)
    live_update.apply()
    applied = backend.read_bytes()
    assert applied.count(b"\n") == applied.count(b"\r\n") > crlf.count(b"\r\n")      # not one bare LF was introduced
    assert cli("status").stdout.strip() == "applied"
    assert cli("revert").returncode == 0 and backend.read_bytes() == crlf

    backend.write_bytes(CANONICAL)
    live_update.apply()
    assert b"\r" not in backend.read_bytes()


def test_a_block_written_by_the_released_version_is_still_recognised(backend):
    """Release 29f8435 wrote rstrip + three line breaks + snippet. For the committed file that is the same bytes as now."""
    snippet = live_update.SNIPPET.read_text(encoding="utf-8")
    released = (CANONICAL.decode("utf-8").rstrip("\n") + "\n\n\n" + snippet).encode("utf-8")
    backend.write_bytes(CANONICAL)
    live_update.apply()
    assert backend.read_bytes() == released
    assert live_update.revert() and backend.read_bytes() == CANONICAL

    pasted = CANONICAL + snippet.encode("utf-8")                           # pasted by hand, without the two line breaks
    backend.write_bytes(pasted)
    assert live_update.block_state(pasted) == "applied"
    assert live_update.revert() and backend.read_bytes() == CANONICAL


def test_an_edited_block_is_left_alone_unless_forced(backend):
    live_update.apply()
    edited = backend.read_text(encoding="utf-8").replace('"""List compensations', '"""(edited) List compensations')
    backend.write_text(edited, encoding="utf-8", newline="\n")
    assert live_update.block_state(edited) == "modified"
    with pytest.raises(live_update.BlockModified):
        live_update.revert()
    refused = cli("revert")
    assert refused.returncode != 0 and "--force" in refused.stderr
    assert cli("status").stdout.startswith("modified")
    assert cli("apply").returncode != 0                                    # nor is a second copy appended
    assert backend.read_text(encoding="utf-8") == edited                   # still exactly what the user left
    forced = cli("revert", "--force")
    assert forced.returncode == 0 and "endpoint removed" in forced.stdout
    assert live_update.block_state(backend.read_text(encoding="utf-8")) == "absent"


def test_undo_takes_back_only_its_own_apply(backend):
    before = backend.read_bytes()

    live_update.apply()                                                    # 1. nobody touched the file: exact bytes come back
    restored, note = live_update.undo(before, backend.read_bytes())
    assert restored and "byte for byte" in note and backend.read_bytes() == before

    live_update.apply()                                                    # 2. somebody edited elsewhere: their edit stays
    after = backend.read_bytes()
    backend.write_bytes(b"# their edit\n" + after)
    restored, note = live_update.undo(before, after)
    assert restored and "were kept" in note
    assert backend.read_bytes().startswith(b"# their edit\n") and live_update.START.encode() not in backend.read_bytes()

    backend.write_bytes(before)
    live_update.apply()                                                    # 3. somebody edited the block itself: hands off
    after = backend.read_bytes()
    theirs = after.replace(b"List compensations", b"List ALL compensations")
    backend.write_bytes(theirs)
    restored, note = live_update.undo(before, after)
    assert not restored and "left in place" in note and backend.read_bytes() == theirs

    backend.write_bytes(before)                                            # 4. already gone: nothing to do, nothing rewritten
    restored, note = live_update.undo(before, after)
    assert restored and "already gone" in note and backend.read_bytes() == before


def test_demo_reports_a_restore_it_could_not_complete(backend, monkeypatch, capsys):
    change = demo.RunState()
    change.source_before = backend.read_bytes()
    live_update.apply()
    change.source_after = backend.read_bytes()
    monkeypatch.setattr(demo, "api_status", lambda path: 0)                # no API is running in this test

    def locked(before, after):
        raise PermissionError("the file is locked by another program")

    monkeypatch.setattr(live_update, "undo", locked)
    problems = demo.take_back(change)
    output = capsys.readouterr().out
    assert len(problems) == 1 and "PermissionError" in problems[0] and "could not be restored" in problems[0]
    assert "RESTORE INCOMPLETE" in output and "python scripts/live_update.py revert" in output
    assert "Initial state restored" not in output
    assert backend.read_bytes() == change.source_after                      # nothing was half done


def test_an_interrupted_build_puts_the_previous_server_back(backend, monkeypatch, capsys):
    game = next(spec for spec in load_servers("local") if spec.key == "game_backend")
    assert game.path == backend.parent
    before = game.generated_file.read_bytes()
    monkeypatch.setattr(build_mcp, "synapse_command", lambda: ["synapse"])

    def interrupted(*args, **kwargs):                                       # Ctrl+C while the CLI is running
        assert not game.generated_file.exists(), "the previous server is parked during the build"
        raise KeyboardInterrupt

    monkeypatch.setattr(build_mcp.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        build_mcp.build_one(game, "local", discover=True, include_live=True, smart_names=False)
    assert game.generated_file.read_bytes() == before
    assert not game.generated_file.with_name(game.generated_file.name + ".prev").exists()
    assert "build interrupted: kept the previous" in capsys.readouterr().out


@pytest.mark.synapse
@pytest.mark.skipif(not synapse_available(), reason="Synapse CLI not installed")
def test_new_endpoint_becomes_a_tool_after_discover():
    committed_server = (ROOT / "systems" / "game_backend" / "synapse_auto_server.py").read_bytes().replace(b"\r\n", b"\n")
    with instance.running("original", reload=True) as copy:
        game = next(spec for spec in load_servers("local") if spec.key == "game_backend")
        assert game.path == copy.folders["game_backend"]
        assert build().returncode == 0                                   # make sure a scan cache exists
        assert live_update.apply()
        assert wait_status(game.base_url + NEW_ENDPOINT, 200), "the API did not reload with the new route"

        stale = build("--include-live")                                  # cached scan does not know the endpoint
        assert stale.returncode != 0 and "Endpoints Not Found" in (stale.stdout + stale.stderr)
        assert game.generated_file.exists(), "a failed build must keep the previous server"
        assert not game.generated_file.with_name(game.generated_file.name + ".prev").exists()

        fresh = build("--discover", "--include-live")
        assert fresh.returncode == 0, fresh.stdout + fresh.stderr
        assert "list_incident_compensations" in game.generated_file.read_text(encoding="utf-8")

        results = asyncio.run(scripted_check.run("local", "live"))       # a new MCP session sees the tool at once
        assert any(label.startswith("Q3") for label, *_ in results) and len(results) == 13
        assert all(passed for _, passed, *_ in results), results

        assert live_update.revert()                                      # and back: canonical bytes again
        assert wait_status(game.base_url + NEW_ENDPOINT, 404)
        assert build("--fresh").returncode == 0
        assert game.generated_file.read_bytes().replace(b"\r\n", b"\n") == committed_server
        assert not os.path.exists(str(ROOT / "mcp" / "claude_desktop_config.json")) or \
            str(copy.root) not in (ROOT / "mcp" / "claude_desktop_config.json").read_text(encoding="utf-8")


@pytest.mark.synapse
@pytest.mark.skipif(not synapse_available(), reason="Synapse CLI not installed")
def test_committed_servers_are_exactly_what_synapse_generates(tmp_path, monkeypatch):
    """The generated files are never edited by hand: a fresh build of the same code must reproduce them byte for byte."""
    if live_update.is_applied(COMMITTED.read_text(encoding="utf-8")):
        pytest.skip("live endpoint is applied in the working tree")
    folders = instance.create(tmp_path, "original")
    for key, folder in folders.items():
        monkeypatch.setenv(f"{key.upper()}_DIR", str(folder))
        (folder / "synapse_auto_server.py").unlink()                     # whatever is there afterwards, Synapse wrote it
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_mcp.py"), "--fresh"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    for key, folder in folders.items():
        built = (folder / "synapse_auto_server.py").read_bytes().replace(b"\r\n", b"\n")
        assert built == (ROOT / "systems" / key / "synapse_auto_server.py").read_bytes().replace(b"\r\n", b"\n"), key
