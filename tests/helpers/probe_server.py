"""A tiny MCP server for tests: reports its own environment and proves when it is alive."""
import os
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("probe")
HEARTBEAT = os.environ.get("PROBE_HEARTBEAT_FILE")


def _beat() -> None:
    while True:
        Path(HEARTBEAT).write_text(str(time.time()), encoding="utf-8")
        time.sleep(0.1)


@mcp.tool(name="get_env", description="Return the value of one environment variable of the server process.")
def get_env(name: str) -> dict:
    return {"name": name, "value": os.environ.get(name)}


if __name__ == "__main__":
    if HEARTBEAT:
        threading.Thread(target=_beat, daemon=True).start()
    mcp.run()
