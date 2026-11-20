"""Add (or remove) the endpoint used in the live part of the demo.

    python scripts/live_update.py status
    python scripts/live_update.py apply     # appends demo/new_endpoint_snippet.py to the game backend
    python scripts/live_update.py revert    # restores the original file

`apply` only edits the backend source, exactly as a developer would by pasting
the snippet. Making the agent see it is Synapse's job:

    python scripts/build_mcp.py --system game_backend --discover --include-live
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "systems" / "game_backend" / "incidents.py"
SNIPPET = ROOT / "demo" / "new_endpoint_snippet.py"
START, END = "# >>> live-demo endpoint", "# <<< live-demo endpoint"


def is_applied(source: str) -> bool:
    return START in source


def apply() -> bool:
    source = APP.read_text(encoding="utf-8")
    if is_applied(source):
        return False
    APP.write_text(source.rstrip("\n") + "\n\n\n" + SNIPPET.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return True


def revert() -> bool:
    source = APP.read_text(encoding="utf-8")
    if not is_applied(source):
        return False
    head = source[: source.index(START)].rstrip("\n") + "\n"
    tail = source[source.index(END) + len(END):].lstrip("\n")
    APP.write_text(head + (("\n\n" + tail) if tail.strip() else ""), encoding="utf-8", newline="\n")
    return True


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "apply":
        print("endpoint added to systems/game_backend/incidents.py" if apply() else "already applied")
        print("next: python scripts/build_mcp.py --system game_backend --discover --include-live")
    elif action == "revert":
        print("endpoint removed" if revert() else "nothing to revert")
        print("next: python scripts/build_mcp.py --system game_backend --fresh")
    elif action == "status":
        print("applied" if is_applied(APP.read_text(encoding="utf-8")) else "not applied")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
