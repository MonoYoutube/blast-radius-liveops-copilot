"""Guided walkthrough of the whole demo, one step at a time.

    python scripts/demo.py             # pauses before every step (use this while recording)
    python scripts/demo.py --auto      # no pauses
    python scripts/demo.py --no-llm    # no model needed: uses the scripted check instead of the agent

Both APIs must be running in another terminal:  python scripts/run_apis.py
Every step prints the command it runs, so each one can also be typed by hand.

The demo rebuilds both MCP servers and, in its live part, edits the game backend.
If a step fails or you press Ctrl+C before the regular clean-up, the demo first
puts every file it touched back exactly as it found it: the backend source, both
generated servers and Synapse's scan caches. It undoes only what this run did.
An endpoint that was already there when the demo started, or a block somebody
edited in the meantime, is left alone and reported.

Exit codes: 0 done, 1 a step failed, 130 interrupted (in both cases everything is
as the demo found it), 3 something could not be put back (the output says what
is left and how to finish by hand).
"""
from __future__ import annotations

import argparse
import hashlib
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.servers import GENERATED_NAME, ROOT, load_servers, shown, utf8_stdio  # noqa: E402
from scripts import live_update  # noqa: E402

INCIDENT = "INC-2026-11-18-EU2"
Q1 = f"Which players were affected by incident {INCIDENT}, and which of them have written to support since it started? Who is still waiting for a first reply?"
Q2 = "Ticket T-1042 says a Battle Pass was paid for and never arrived. Is that true, and what should we do?"
Q2_FALSE = "Ticket T-1047 says the Battle Pass was charged twice. Is that true?"
Q3 = f"For incident {INCIDENT}: which affected players have already been compensated, and who is still waiting for a reply without any compensation?"
NEW_ENDPOINT = f"/incidents/{INCIDENT}/compensations"
LIVE_TOOL = "list_incident_compensations"
EXIT_STEP_FAILED, EXIT_NOT_RESTORED, EXIT_INTERRUPTED = 1, 3, 130
MANUAL_RESTORE = (
    "    python scripts/live_update.py status\n"
    "    python scripts/live_update.py revert\n"
    "    python scripts/build_mcp.py --system game_backend --fresh"
)

PY = sys.executable
AUTO = False


def banner(title: str, note: str = "") -> None:
    print("\n" + "=" * 78)
    print(title)
    if note:
        print(note)
    print("=" * 78)
    if not AUTO:
        input("[Enter] ")


def run(args: list[str]) -> None:
    shown_args = " ".join(f'"{a}"' if " " in a else a for a in args)
    print(f"\n$ python {shown_args}\n", flush=True)
    code = subprocess.run([PY, *args], cwd=ROOT).returncode
    if code != 0:
        raise SystemExit(f"\nStep failed (exit {code}).")


def game_backend():
    return next(spec for spec in load_servers("local") if spec.key == "game_backend")


def stale_build_verdict(returncode: int, output: str, server_kept: bool) -> str:
    """Explain the outcome of the plain rebuild in step 6. An empty string means "failed for the expected reason".

    Expected: Synapse reuses its last scan, so it cannot find the endpoint that was just added, the build
    fails and the previous server file stays in place. Anything else is a real problem and stops the demo.
    """
    if returncode == 0:
        return "the plain rebuild succeeded, so the scan cache was not reused. Run scripts/build_mcp.py once and start the demo again."
    if "Using the endpoints discovered" not in output:
        return "the build failed before Synapse reused its last scan; this is not the stale-cache case."
    if "Endpoints Not Found" not in output:
        return "the build failed for a reason other than the missing endpoint."
    if not server_kept:
        return "the previous synapse_auto_server.py was not kept after the failed build."
    return ""


def expect_stale_build_failure() -> None:
    game = game_backend()
    digest = lambda: hashlib.sha256(game.generated_file.read_bytes()).hexdigest() if game.generated_file.exists() else ""
    before = digest()
    args = ["scripts/build_mcp.py", "--system", "game_backend", "--include-live"]
    print(f"\n$ python {' '.join(args)}\n")
    proc = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = (proc.stdout or "") + (proc.stderr or "")
    print(output)
    problem = stale_build_verdict(proc.returncode, output, bool(before) and digest() == before)
    if problem:
        raise SystemExit(f"\nStep 6 did not behave as expected: {problem}")
    print("As expected: the cached scan does not contain the new endpoint, and the previous server file is untouched.")


def ask(question: str, no_llm: bool, stage: str = "baseline") -> None:
    if no_llm:
        run(["-m", "eval.scripted_check", "--stage", stage])
    else:
        run(["-m", "agent.copilot", question])


def api_status(path: str) -> int:
    try:
        return urllib.request.urlopen(game_backend().base_url + path, timeout=3).status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def reaches(path: str, wanted: int, seconds: int = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if api_status(path) == wanted:
            return True
        time.sleep(1)
    return False


def endpoint_gone(seconds: int = 30, down_after: int = 8) -> str:
    """After the source was restored: "gone" (the API answers 404), "down" (no API answers), "served" (still 200).

    A reloading API refuses connections for a moment, so "down" needs several seconds without any answer.
    """
    deadline, silent_since = time.time() + seconds, None
    while time.time() < deadline:
        status = api_status(NEW_ENDPOINT)
        if status == 404:
            return "gone"
        if status == 0:
            silent_since = silent_since or time.time()
            if time.time() - silent_since >= down_after:
                return "down"
        else:
            silent_since = None
        time.sleep(0.5)
    return "served"


def wait_for(path: str, wanted: int, seconds: int = 40) -> None:
    if not reaches(path, wanted, seconds):
        raise SystemExit(
            f"\nGET {path} did not return {wanted}. Is scripts/run_apis.py running with auto-reload? "
            "Restart it and start the demo again."
        )


class RunState:
    """What this run found and what it changed, so that an unfinished run can leave everything as it was.

    Step 1 rebuilds both MCP servers; step 5 edits the backend source; step 7 rebuilds the game server
    with the new tool. The generated servers and Synapse's scan caches are remembered as the demo found
    them, the source right before and right after the edit. restore() puts all of it back. It needs
    neither Synapse nor a model, so it also works when a broken build is the reason for stopping.
    """

    def __init__(self) -> None:
        self.entry: dict[Path, bytes | None] = {}        # generated servers and scan caches at the start; None = absent
        self.source_before: bytes | None = None          # set as soon as this run starts editing the source
        self.source_after: bytes | None = None
        self.finished = False                            # the regular clean-up at the end went through

    def remember_entry(self) -> None:
        for spec in load_servers("local"):
            for path in (spec.generated_file, spec.path / ".synapse" / "surface_cache.json"):
                self.entry[path] = path.read_bytes() if path.exists() else None

    def add_endpoint(self) -> None:
        self.source_before = live_update.app_file().read_bytes()
        run(["scripts/live_update.py", "apply"])
        self.source_after = live_update.app_file().read_bytes()

    @property
    def edited_source(self) -> bool:
        return self.source_before is not None

    @property
    def pending(self) -> bool:
        return bool(self.entry) and not self.finished

    def _restore_artifacts(self, report: bool) -> list[str]:
        problems = []
        for path, before in self.entry.items():
            try:
                if path.name == GENERATED_NAME:
                    path.with_name(GENERATED_NAME + ".prev").unlink(missing_ok=True)    # parked by an interrupted build
                if (path.read_bytes() if path.exists() else None) == before:
                    continue
                if before is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(before)
                if not report:
                    continue
                if before is None:
                    print(f"  {shown(path)}: removed again (it was not there when the demo started)")
                elif path.name == GENERATED_NAME:
                    print(f"  {shown(path)}: the MCP server the demo found is back ({before.count(b'@mcp.tool(')} tools)")
                else:
                    print(f"  {shown(path)}: the scan cache the demo found is back")
            except Exception as exc:
                problems.append(f"{shown(path)} could not be restored ({type(exc).__name__}: {exc})")
                print("  " + problems[-1])
        return problems

    def restore(self) -> list[str]:
        """Put back what this run changed. Returns what could not be restored (empty list: all of it is back)."""
        problems: list[str] = []
        restored = True
        if self.edited_source:
            try:
                restored, note = live_update.undo(self.source_before, self.source_after or b"")
            except Exception as exc:
                restored, note = False, f"{shown(live_update.app_file())} could not be restored ({type(exc).__name__}: {exc})"
            print("  " + note)
            if not restored:
                problems.append(note)

        problems += self._restore_artifacts(report=True)

        if self.edited_source and restored:
            outcome = endpoint_gone()
            if outcome == "gone":
                print(f"  API: GET {NEW_ENDPOINT} -> 404, the endpoint is no longer served")
            elif outcome == "down":
                print("  API: not running, nothing to wait for")
            else:
                problems.append("the running API still serves the endpoint; restart scripts/run_apis.py")
                print("  " + problems[-1])
        # A build that was still writing when the demo stopped may have finished meanwhile: check once more.
        problems += self._restore_artifacts(report=False)
        return list(dict.fromkeys(problems))


def walk(args, state: RunState) -> None:
    if not reaches("/incidents", 200, 10):               # a few seconds of patience: the API may be starting or reloading
        raise SystemExit("The APIs are not running. Start them first:  python scripts/run_apis.py")
    block = live_update.block_state(live_update.app_file().read_text(encoding="utf-8"))
    if block != "absent":
        raise SystemExit(
            f"The live endpoint is already in {shown(live_update.app_file())}"
            + (" (and the block was edited)" if block == "modified" else "")
            + ". This run did not put it there, so the demo will not remove it.\n"
            "The demo starts from the initial state. To get there yourself:\n" + MANUAL_RESTORE
        )
    state.remember_entry()

    banner("1/8  Two codebases, two MCP servers generated by Synapse",
           "The agent's only access to the game backend and the support desk are these tools.")
    run(["scripts/build_mcp.py", "--discover"])
    run(["-m", "agent.copilot", "--tools"])

    banner("2/8  Question that needs both systems: incident -> tickets")
    ask(Q1, args.no_llm)

    if not args.no_llm:
        banner("3/8  Question that needs both systems: ticket -> purchase log")
        ask(Q2, False)
        ask(Q2_FALSE, False)

        banner("4/8  A question the tools cannot answer yet",
               "No tool exposes compensations, so the agent has to say what it could not verify.")
        ask(Q3, False)

    banner("5/8  Live: a developer adds a new endpoint to the game backend",
           "demo/new_endpoint_snippet.py is appended to systems/game_backend/incidents.py")
    print((ROOT / "demo" / "new_endpoint_snippet.py").read_text(encoding="utf-8"))
    state.add_endpoint()
    wait_for(NEW_ENDPOINT, 200)
    print(f"GET {NEW_ENDPOINT} -> 200: the API serves the new endpoint. The agent cannot see it yet.")

    banner("6/8  A plain rebuild does not pick it up",
           "Synapse reuses its last scan. This step is expected to fail with 'Endpoints Not Found'.")
    expect_stale_build_failure()

    banner("7/8  synapse build --discover", "Rescan the code and rebuild the MCP server.")
    run(["scripts/build_mcp.py", "--system", "game_backend", "--discover", "--include-live"])
    run(["-m", "agent.copilot", "--tools"])

    banner("8/8  The agent uses the new tool on the very next question",
           "No manual client restart: each question opens a fresh MCP session.")
    ask(Q3, args.no_llm, stage="live")
    if not args.no_llm:
        ask(Q2, False)

    banner("Clean up", "Remove the endpoint again and rebuild, so the demo can be repeated.")
    run(["scripts/live_update.py", "revert"])
    wait_for(NEW_ENDPOINT, 404)
    run(["scripts/build_mcp.py", "--system", "game_backend", "--fresh"])
    state.finished = True
    print("\nDone. The repository is back in its initial state.")


def take_back(state: RunState) -> list[str]:
    """Run the restore with Ctrl+C switched off, so an impatient second press cannot leave it half done."""
    print("\n" + "-" * 78)
    if state.edited_source:
        print("The demo stopped after it had edited the game backend. Putting everything back as it was found:")
    else:
        print("The demo stopped before it edited the game backend. Putting back what its builds rewrote:")
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:                                   # not the main thread (when driven from a test)
        previous = None
    try:
        problems = state.restore()
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    if problems:
        print("\nRESTORE INCOMPLETE. Still to do:")
        for problem in problems:
            print(f"  - {problem}")
        print("Check and finish by hand:\n" + MANUAL_RESTORE)
    else:
        print("Initial state restored: source, generated servers and scan caches are as the demo found them. It can be started again.")
    return problems


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def main() -> None:
    global AUTO
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="do not pause between steps")
    parser.add_argument("--no-llm", action="store_true", help="replace agent questions with the scripted check")
    args = parser.parse_args()
    utf8_stdio()
    AUTO = args.auto
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):       # a closed terminal or a kill is handled like Ctrl+C
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _interrupt)
            except (ValueError, OSError):
                pass

    state = RunState()
    try:
        walk(args, state)
        return
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        code = EXIT_INTERRUPTED
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        print(exc.code if isinstance(exc.code, str) else f"\nStopped (exit {exc.code}).")
        code = EXIT_STEP_FAILED
    except Exception:
        traceback.print_exc()
        code = EXIT_STEP_FAILED
    if not state.pending:
        print("This run changed nothing.")
    elif take_back(state):
        code = EXIT_NOT_RESTORED
    sys.exit(code)


if __name__ == "__main__":
    main()
