"""Guided walkthrough of the whole demo, one step at a time.

    python scripts/demo.py             # pauses before every step (use this while recording)
    python scripts/demo.py --auto      # no pauses
    python scripts/demo.py --no-llm    # no model needed: uses the scripted check instead of the agent

Both APIs must be running in another terminal:  python scripts/run_apis.py
Every step prints the command it runs, so each one can also be typed by hand.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.servers import ROOT, load_servers, utf8_stdio  # noqa: E402
from scripts import live_update  # noqa: E402

INCIDENT = "INC-2026-11-18-EU2"
Q1 = f"Which players were affected by incident {INCIDENT}, and which of them have written to support since it started? Who is still waiting for a first reply?"
Q2 = "Ticket T-1042 says a Battle Pass was paid for and never arrived. Is that true, and what should we do?"
Q2_FALSE = "Ticket T-1047 says the Battle Pass was charged twice. Is that true?"
Q3 = f"For incident {INCIDENT}: which affected players have already been compensated, and who is still waiting for a reply without any compensation?"
NEW_ENDPOINT = f"/incidents/{INCIDENT}/compensations"

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
    shown = " ".join(f'"{a}"' if " " in a else a for a in args)
    print(f"\n$ python {shown}\n", flush=True)
    code = subprocess.run([PY, *args], cwd=ROOT).returncode
    if code != 0:
        raise SystemExit(f"\nStep failed (exit {code}). Fix it and start the demo again.")


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
    game = next(spec for spec in load_servers("local") if spec.key == "game_backend")
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
    game = next(spec for spec in load_servers("local") if spec.key == "game_backend")
    try:
        return urllib.request.urlopen(game.base_url + path, timeout=3).status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def wait_for(path: str, wanted: int, seconds: int = 40) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if api_status(path) == wanted:
            return
        time.sleep(1)
    raise SystemExit(
        f"\nGET {path} did not return {wanted}. Is scripts/run_apis.py running with auto-reload? "
        "Restart it and start the demo again."
    )


def main() -> None:
    global AUTO
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="do not pause between steps")
    parser.add_argument("--no-llm", action="store_true", help="replace agent questions with the scripted check")
    args = parser.parse_args()
    utf8_stdio()
    AUTO = args.auto

    if api_status("/incidents") != 200:
        raise SystemExit("The APIs are not running. Start them first:  python scripts/run_apis.py")
    if live_update.is_applied(live_update.APP.read_text(encoding="utf-8")):
        print("The live endpoint is already applied. Restoring the initial state first.")
        live_update.revert()
        wait_for(NEW_ENDPOINT, 404)
        run(["scripts/build_mcp.py", "--system", "game_backend", "--fresh"])

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
    run(["scripts/live_update.py", "apply"])
    wait_for(NEW_ENDPOINT, 200)
    print(f"GET {NEW_ENDPOINT} -> 200: the API serves the new endpoint. The agent cannot see it yet.")

    banner("6/8  A plain rebuild does not pick it up",
           "Synapse reuses its last scan. This step is expected to fail with 'Endpoints Not Found'.")
    expect_stale_build_failure()

    banner("7/8  synapse build --discover", "Rescan the code and rebuild the MCP server.")
    run(["scripts/build_mcp.py", "--system", "game_backend", "--discover", "--include-live"])
    run(["-m", "agent.copilot", "--tools"])

    banner("8/8  The agent uses the new tool on the very next question", "No restart: each question opens a fresh MCP session.")
    ask(Q3, args.no_llm, stage="live")
    if not args.no_llm:
        ask(Q2, False)

    banner("Clean up", "Remove the endpoint again and rebuild, so the demo can be repeated.")
    run(["scripts/live_update.py", "revert"])
    wait_for(NEW_ENDPOINT, 404)
    run(["scripts/build_mcp.py", "--system", "game_backend", "--fresh"])
    print("\nDone. The repository is back in its initial state.")


if __name__ == "__main__":
    main()
