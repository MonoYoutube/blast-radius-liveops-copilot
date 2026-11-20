"""Add (or remove) the endpoint used in the live part of the demo.

    python scripts/live_update.py status
    python scripts/live_update.py apply     # appends demo/new_endpoint_snippet.py to the game backend
    python scripts/live_update.py revert    # removes it again

`apply` only edits the backend source, exactly as a developer would by pasting
the snippet. Making the agent see it is Synapse's job:

    python scripts/build_mcp.py --system game_backend --discover --include-live

Both commands work on bytes and touch nothing but the block itself. `apply`
appends two line breaks and the snippet, in the line endings the file already
uses. `revert` cuts out exactly that. Everything before and after the block
stays byte for byte as it is: CRLF or LF, blank lines at the end, a missing
final line break, lines somebody added below the block in the meantime.

If the block between the two marker lines was edited afterwards it is somebody's
work, so it is left in place unless you ask for it:  python scripts/live_update.py revert --force
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNIPPET = ROOT / "demo" / "new_endpoint_snippet.py"
START, END = "# >>> live-demo endpoint", "# <<< live-demo endpoint"
_START, _END = START.encode(), END.encode()


class BlockModified(Exception):
    """The block between the markers is no longer the snippet `apply` wrote."""


def app_file() -> Path:
    """incidents.py of the game backend: the committed one, or the isolated copy named by GAME_BACKEND_DIR."""
    return Path(os.environ.get("GAME_BACKEND_DIR") or ROOT / "systems" / "game_backend") / "incidents.py"


def shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _line_break(data: bytes) -> bytes:
    """The line ending the file uses: CRLF if most of its lines end that way, LF otherwise."""
    crlf = data.count(b"\r\n")
    return b"\r\n" if crlf * 2 > data.count(b"\n") else b"\n"


def _span(data: bytes) -> tuple[int, int] | None:
    """Byte range of the block: from the start marker to the end of the end-marker line. None if the markers are damaged."""
    begin, stop = data.find(_START), data.find(_END)
    if begin < 0 or stop < begin:
        return None
    end = stop + len(_END)
    for line_break in (b"\r\n", b"\n"):
        if data.startswith(line_break, end):
            end += len(line_break)
            break
    return begin, end


def block_state(source: bytes | str) -> str:
    """absent: no endpoint.  applied: exactly the snippet.  modified: markers present, content changed or damaged."""
    data = source.encode("utf-8") if isinstance(source, str) else source
    if _START not in data and _END not in data:
        return "absent"
    span = _span(data)
    if span and _lf(data[span[0]:span[1]]).strip() == _lf(SNIPPET.read_bytes()).strip():
        return "applied"
    return "modified"


def is_applied(source: bytes | str) -> bool:
    return block_state(source) != "absent"


def apply() -> bool:
    """Append two line breaks and the snippet. The existing content is extended, never rewritten."""
    path = app_file()
    data = path.read_bytes()
    if block_state(data) != "absent":
        return False
    line_break = _line_break(data)
    block = _lf(SNIPPET.read_bytes()).rstrip(b"\n").replace(b"\n", line_break) + line_break
    path.write_bytes(data + line_break * 2 + block)
    return True


def revert(force: bool = False) -> bool:
    """Cut the block out again, together with the two line breaks apply() put in front of it. Nothing else changes."""
    path = app_file()
    data = path.read_bytes()
    state = block_state(data)
    if state == "absent":
        return False
    span = _span(data)
    if state == "modified":
        if not force:
            raise BlockModified(
                f"the block between the markers in {shown(path)} differs from demo/new_endpoint_snippet.py, so it was left in place"
            )
        if span is None:
            raise BlockModified(f"the marker lines in {shown(path)} are damaged; remove the block by hand")
    begin, end = span
    for separator in (b"\r\n\r\n", b"\n\n"):
        if data.endswith(separator, 0, begin):
            begin -= len(separator)
            break
    path.write_bytes(data[:begin] + data[end:])
    return True


def undo(before: bytes, after: bytes) -> tuple[bool, str]:
    """Take back one apply() made by this process, and only that.

    before/after are the bytes of the file right before and right after the apply().
    Returns (restored, what happened). Somebody else's later edits are never overwritten.
    """
    path = app_file()
    current = path.read_bytes()
    if current == after:
        path.write_bytes(before)
        return True, f"{shown(path)} restored byte for byte"
    state = block_state(current)
    if state == "absent":
        return True, f"the endpoint is already gone from {shown(path)}"
    if state == "applied":
        revert()
        return True, f"endpoint removed from {shown(path)}; the other edits made to that file meanwhile were kept"
    return False, f"the endpoint block in {shown(path)} was edited after the demo added it, so it was left in place"


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    path = app_file()
    if action == "apply":
        if block_state(path.read_bytes()) == "modified":
            raise SystemExit(f"{shown(path)} already has an endpoint block, and it was edited. Nothing changed.")
        print(f"endpoint added to {shown(path)}" if apply() else "already applied")
        print("next: python scripts/build_mcp.py --system game_backend --discover --include-live")
    elif action == "revert":
        try:
            print("endpoint removed" if revert(force="--force" in sys.argv[2:]) else "nothing to revert")
        except BlockModified as exc:
            raise SystemExit(f"Not reverted: {exc}.\nTo remove it anyway: python scripts/live_update.py revert --force")
        print("next: python scripts/build_mcp.py --system game_backend --fresh")
    elif action == "status":
        print({"absent": "not applied", "applied": "applied"}.get(
            block_state(path.read_bytes()), "modified (the block between the markers differs from demo/new_endpoint_snippet.py)"))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
