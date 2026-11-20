"""Where the two Synapse-generated MCP servers live and how to start them.

Everything is derived from mcp/exposure.json so the build script, the copilot
and the tests agree on names, ports and paths.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPOSURE_FILE = ROOT / "mcp" / "exposure.json"
HOSTED_FILE = ROOT / "mcp" / "hosted_servers.json"
GENERATED_NAME = "synapse_auto_server.py"


@dataclass
class ServerSpec:
    key: str                 # "game_backend" / "support_desk"
    namespace: str           # short prefix shown to the model: "game" / "desk"
    server_name: str         # MCP server name given to Synapse
    path: Path               # codebase directory
    port: int
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return os.environ.get(f"{self.key.upper()}_URL", f"http://127.0.0.1:{self.port}")

    @property
    def generated_file(self) -> Path:
        return self.path / GENERATED_NAME


def load_exposure() -> dict:
    return json.loads(EXPOSURE_FILE.read_text(encoding="utf-8"))


def endpoints_for(key: str, include_live: bool = False) -> list[str]:
    cfg = load_exposure()[key]
    rows = list(cfg["expose"]) + (list(cfg.get("live_demo", [])) if include_live else [])
    return [row["endpoint"] for row in rows]


def load_servers(mode: str = "local") -> list[ServerSpec]:
    """Return launch specs for both MCP servers.

    local  -> the Python file written by `synapse build --local --auto`
    hosted -> the @2ndbrainlabs-ai/synapse-mcp runner with the server id that
              `synapse build --auto` stored in the Synapse account
    """
    specs: list[ServerSpec] = []
    hosted = json.loads(HOSTED_FILE.read_text(encoding="utf-8")) if HOSTED_FILE.exists() else {}
    for key, cfg in load_exposure().items():
        spec = ServerSpec(
            key=key,
            namespace=cfg["namespace"],
            server_name=cfg["server_name"],
            path=ROOT / cfg["path"],
            port=int(cfg["port"]),
        )
        if mode == "local":
            spec.command = sys.executable
            spec.args = [str(spec.generated_file)]
            spec.env = {"ENDPOINT_BASE_URL": spec.base_url}
        elif mode == "hosted":
            server_id = hosted.get(key, {}).get("server_id", "")
            if not server_id:
                raise SystemExit(
                    f"No hosted server id for {key}. Run: python scripts/build_mcp.py --mode hosted"
                )
            spec.command = "npx"
            spec.args = ["-y", "@2ndbrainlabs-ai/synapse-mcp"]
            spec.env = {
                "SYNAPSE_API_KEY": os.environ.get("SYNAPSE_API_KEY", ""),
                "SYNAPSE_SERVER_ID": server_id,
                "SYNAPSE_API_URL": os.environ.get("SYNAPSE_API_URL", "https://api.synapse.2ndbrainlabs.ai"),
                "ENDPOINT_BASE_URL": spec.base_url,
            }
        else:
            raise SystemExit(f"unknown mode {mode!r} (use local or hosted)")
        specs.append(spec)
    return specs


def utf8_stdio() -> None:
    """Make prints safe on Windows consoles and pipes (Synapse output and model answers are UTF-8)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def scrub(text: str) -> str:
    """Remove the value of every key-like environment variable from text before it is printed or saved."""
    for name, value in os.environ.items():
        if value and len(value) >= 8 and any(mark in name.upper() for mark in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[redacted]")
    return text


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env reader (KEY=VALUE per line). Existing variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
