"""Isolated copies of the two systems, for checks that must not touch the demo data.

    python -m eval.instance                                   # list the scenarios
    python -m eval.scripted_check --scenario partial_grant --stage live
    python -m eval.agent_eval --scenario unrelated_ticket --only q1_topics

A scenario copies systems/game_backend and systems/support_desk into a temporary
folder, changes the data of the copy, starts both APIs from the copy on free
ports and points every script at it through <KEY>_DIR / <KEY>_PORT environment
variables (read by agent/servers.py, eval/ground_truth.py and scripts/live_update.py).

The committed systems, their data files and their generated MCP servers are only
read. The copy uses the generated servers exactly as they are committed; for the
live stage the endpoint is added to the copy and `synapse build --discover` runs there.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent.servers import ROOT, load_exposure, scrub
from scripts import live_update, run_apis

INCIDENT = "INC-2026-11-18-EU2"
# Left behind on purpose: caches, the Synapse account binding and scan cache, files parked by a build.
NOT_COPIED = shutil.ignore_patterns("__pycache__", ".synapse", "*.prev", "synapse_auto_server_[0-9]*.py")


def _partial_grant(game: dict, desk: dict) -> None:
    game["compensations"].append({
        "compensation_id": "C-9005", "player_id": "P-1004", "incident_id": INCIDENT,
        "grant": "300 Gems", "reason": "match crash", "related_purchase_id": None,
        "granted_at": "2026-11-19T09:00:00Z", "granted_by": "auto-rule:crash-compensation",
    })


def _unrelated_ticket(game: dict, desk: dict) -> None:
    desk["tickets"].append({
        "ticket_id": "T-1052", "requester_email": "oleg.v@example.com", "requester_name": "Oleg V.",
        "subject": "Suggestion: colour-blind mode for the minimap", "category": "feedback", "status": "new",
        "priority": "low", "created_at": "2026-11-18T19:10:00Z", "tags": ["feature-request"],
        "messages": [{"author": "customer", "at": "2026-11-18T19:10:00Z",
                      "text": "Team colours on the minimap are hard to tell apart. Could you add a colour-blind mode?"}],
    })


SCENARIOS = {
    "original": ("The demo data, unchanged.", None),
    "partial_grant": (
        "IronTulip (P-1004) also received the 300 Gems crash grant (C-9005). "
        "The 500 Gems purchase PU-5002 is still charged and not delivered, and ticket T-1043 is still unanswered.",
        _partial_grant,
    ),
    "unrelated_ticket": (
        "Pixelknight (P-1006), who lost a match to the crash, sent a feature request (T-1052) while the incident was running. "
        "It is a ticket from an affected player since the start, and it is not about the incident.",
        _unrelated_ticket,
    ),
}


@dataclass
class Instance:
    root: Path
    folders: dict[str, Path]
    ports: dict[str, int]
    scenario: str
    live: bool

    @property
    def env(self) -> dict[str, str]:
        """The variables that point a script at this copy."""
        out = {}
        for key, folder in self.folders.items():
            out[f"{key.upper()}_DIR"] = str(folder)
            out[f"{key.upper()}_PORT"] = str(self.ports[key])
        return out


def check_name(scenario: str) -> None:
    if scenario not in SCENARIOS:
        raise SystemExit(f"unknown scenario {scenario!r}. Known: {', '.join(SCENARIOS)}")


def create(target: Path, scenario: str = "original") -> dict[str, Path]:
    """Copy both systems under `target` and apply the scenario to the data of the copy."""
    check_name(scenario)
    folders = {}
    for key, cfg in load_exposure().items():
        folders[key] = target / key
        shutil.copytree(ROOT / cfg["path"], folders[key], ignore=NOT_COPIED)
    change = SCENARIOS[scenario][1]
    if change:
        files = {key: folder / "data" / "seed.json" for key, folder in folders.items()}
        before = {key: path.read_text(encoding="utf-8") for key, path in files.items()}
        data = {key: json.loads(text) for key, text in before.items()}
        change(data["game_backend"], data["support_desk"])
        for key, path in files.items():
            if data[key] != json.loads(before[key]):          # a data set the scenario leaves alone stays byte-identical
                path.write_text(json.dumps(data[key], indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return folders


def _free_ports(count: int) -> list[int]:
    """Ports nobody listens on. All sockets stay bound until every port is chosen, so the ports differ."""
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def _make_live(root: Path) -> None:
    """Add the live endpoint to the copy and let Synapse rebuild the copy's game server."""
    target = live_update.app_file().resolve()
    if root.resolve() not in target.parents:          # never edit the committed backend from here
        raise SystemExit(f"refusing to change {target}: it is outside the isolated copy")
    live_update.apply()
    build = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_mcp.py"), "--system", "game_backend", "--discover", "--include-live"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if build.returncode != 0:
        raise SystemExit("Could not build the live MCP server in the isolated copy:\n" + scrub((build.stdout or "") + (build.stderr or "")))


@contextmanager
def running(scenario: str = "original", live: bool = False, reload: bool = False):
    """Both systems of a scenario, running from a temporary copy. Yields an Instance; cleans up afterwards.

    While the block runs, the <KEY>_DIR / <KEY>_PORT variables of this process (and of the processes it
    starts) point at the copy. live=True adds the live endpoint to the copy and rebuilds its game server
    with `synapse build --discover` (needs the Synapse CLI). reload=True starts the APIs with auto-reload.
    """
    check_name(scenario)
    root = Path(tempfile.mkdtemp(prefix="blast-radius-"))
    names = [f"{key.upper()}_{suffix}" for key in load_exposure() for suffix in ("DIR", "PORT", "URL")]
    saved = {name: os.environ.get(name) for name in names}
    procs: list = []
    try:
        folders = create(root, scenario)
        instance = Instance(root, folders, dict(zip(folders, _free_ports(len(folders)))), scenario, live)
        for name in names:
            os.environ.pop(name, None)                 # a URL override from outside would point somewhere else
        os.environ.update(instance.env)
        if live:
            _make_live(root)
        procs = run_apis.start(reload=reload, quiet=True)
        run_apis.wait_ready()
        yield instance
    finally:
        run_apis.stop(procs)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    print("Scenarios for --scenario (eval.scripted_check, eval.agent_eval):\n")
    for name, (description, _) in SCENARIOS.items():
        print(f"  {name:17s} {description}")


if __name__ == "__main__":
    main()
