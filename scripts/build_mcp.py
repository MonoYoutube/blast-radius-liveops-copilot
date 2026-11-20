"""Generate both MCP servers with the Synapse CLI.

    python scripts/build_mcp.py                      # local Auto, reuse last scan
    python scripts/build_mcp.py --discover           # rescan the code first
    python scripts/build_mcp.py --discover --include-live
    python scripts/build_mcp.py --fresh              # drop the scan cache first (canonical output)
    python scripts/build_mcp.py --mode hosted        # needs SYNAPSE_API_KEY / synapse init

The script only chooses endpoints and prints what it runs. The MCP servers
themselves are produced by `synapse build`; nothing here edits them.

Verified against Synapse CLI 0.1.9:
* `--local --auto` calls no model, but the CLI still wants a "ready" provider.
  The default (Anthropic) fails without a key, so we pass `--provider ollama`,
  which needs no key and no running Ollama.
* A local rebuild does not overwrite synapse_auto_server.py: it writes
  synapse_auto_server_2.py next to it. We park the old file during the build so
  the MCP client config keeps pointing at one stable path.
* After a partial rescan the tools of re-parsed files move to the end of the
  generated file. `--fresh` deletes .synapse/surface_cache.json first (the CLI
  documents this as always safe), which gives byte-identical output for
  identical code.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.servers import GENERATED_NAME, HOSTED_FILE, ROOT, endpoints_for, load_dotenv, load_servers, scrub, utf8_stdio  # noqa: E402


def synapse_command() -> list[str]:
    """Locate the Synapse CLI. Prefer `node <index.js>`: no .cmd quoting issues on Windows."""
    override = os.environ.get("SYNAPSE_BIN")
    if override:
        return ["node", override] if override.endswith(".js") else [override]
    npm = shutil.which("npm")
    if npm:
        try:
            root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, check=True).stdout.strip()
            entry = Path(root) / "@2ndbrainlabs-ai" / "synapse-cli" / "dist" / "index.js"
            if entry.exists() and shutil.which("node"):
                return ["node", str(entry)]
        except (subprocess.CalledProcessError, OSError):
            pass
    found = shutil.which("synapse")
    if found:
        return [found]
    raise SystemExit("Synapse CLI not found. Install it: npm install -g @2ndbrainlabs-ai/synapse-cli")


def show(cmd: list[str]) -> str:
    return " ".join(f'"{part}"' if (" " in part or "{" in part) else part for part in cmd)


def build_one(spec, mode: str, discover: bool, include_live: bool, smart_names: bool, fresh: bool = False) -> dict:
    endpoints = endpoints_for(spec.key, include_live=include_live)
    if fresh:
        (spec.path / ".synapse" / "surface_cache.json").unlink(missing_ok=True)
    cmd = synapse_command() + ["--yes", "build"]
    if mode == "local":
        cmd += ["--local", "--provider", os.environ.get("SYNAPSE_LOCAL_PROVIDER", "ollama")]
    cmd += ["--auto", "--endpoints", ",".join(endpoints), "--server-name", spec.server_name]
    if mode == "hosted":
        cmd += ["--base-url", spec.base_url]
    if discover:
        cmd += ["--discover"]
    if smart_names:
        cmd += ["--smart-names"]

    # Park the previous server so Synapse writes synapse_auto_server.py again
    # (otherwise it writes synapse_auto_server_2.py). Restored if the build fails.
    parked = spec.path / (GENERATED_NAME + ".prev")
    if mode == "local":
        for stale in spec.path.glob("synapse_auto_server_[0-9]*.py"):
            stale.unlink()
        if spec.generated_file.exists():
            spec.generated_file.replace(parked)

    printable = ["synapse"] + cmd[cmd.index("--yes"):]
    env = {**os.environ, "SYNAPSE_TELEMETRY": os.environ.get("SYNAPSE_TELEMETRY", "0")}
    if mode == "hosted" and not (spec.path / ".synapse" / "config.json").exists():
        # A hosted build needs an initialised project; the key comes from SYNAPSE_API_KEY or ~/.synapse.
        print(f"\n[{spec.key}] $ cd {spec.path.relative_to(ROOT)} && synapse --yes init")
        init = subprocess.run(synapse_command() + ["--yes", "init"], cwd=spec.path, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if init.returncode != 0:
            raise SystemExit(f"synapse init failed for {spec.key} (exit {init.returncode}). Output:\n{scrub((init.stdout or '') + (init.stderr or ''))}")
    print(f"\n[{spec.key}] $ cd {spec.path.relative_to(ROOT)} && {show(printable)}")
    proc = subprocess.run(cmd, cwd=spec.path, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = scrub((proc.stdout or "") + (proc.stderr or ""))
    for line in output.splitlines():
        if any(mark in line for mark in ("Discover", "Extracted", "Detected", "Ready", "Output", "Tool ", "rror", "✖")):
            print("   ", line.strip(" │"))
    if proc.returncode != 0 or (mode == "local" and not spec.generated_file.exists()):
        if parked.exists():
            parked.replace(spec.generated_file)
            print(f"    build failed: kept the previous {GENERATED_NAME}")
        raise SystemExit(f"synapse build failed for {spec.key} (exit {proc.returncode}). Full output:\n{output}")
    if parked.exists():
        parked.unlink()

    result = {"key": spec.key, "endpoints": endpoints}
    if mode == "local":
        if not spec.generated_file.exists():
            print(output)
            raise SystemExit(f"{GENERATED_NAME} was not written for {spec.key}")
        tools = re.findall(r'@mcp\.tool\(name="([^"]+)"', spec.generated_file.read_text(encoding="utf-8"))
        result["tools"] = tools
        print(f"    -> {spec.generated_file.relative_to(ROOT)}: {len(tools)} tools: {', '.join(tools)}")
    else:
        match = re.search(r'"SYNAPSE_SERVER_ID"\s*:\s*"([0-9a-fA-F-]{8,})"', output)
        if not match:
            print(output)
            raise SystemExit("Could not find SYNAPSE_SERVER_ID in the CLI output")
        result["server_id"] = match.group(1)
        print(f"    -> hosted server id {result['server_id']}")
    return result


def write_client_config(mode: str) -> None:
    """Write a ready-to-paste config for Claude Desktop / Cursor (machine-specific, git-ignored)."""
    servers = {}
    for spec in load_servers(mode):
        env = dict(spec.env)
        if "SYNAPSE_API_KEY" in env:
            env["SYNAPSE_API_KEY"] = "PASTE_YOUR_SYNAPSE_API_KEY"
        servers[spec.server_name] = {"command": spec.command, "args": spec.args, "env": env}
    target = ROOT / "mcp" / "claude_desktop_config.json"
    target.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"\nMCP client config written to {target.relative_to(ROOT)} (merge it into your client's mcpServers).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["local", "hosted"], default="local")
    parser.add_argument("--system", choices=["game_backend", "support_desk", "all"], default="all")
    parser.add_argument("--discover", action="store_true", help="rescan the code (synapse build --discover)")
    parser.add_argument("--fresh", action="store_true", help="delete the scan cache first, for canonical output")
    parser.add_argument("--include-live", action="store_true", help="also expose the endpoints listed under live_demo")
    parser.add_argument("--smart-names", action="store_true", help="let Synapse rename/describe tools from handler source (needs a model)")
    args = parser.parse_args()

    utf8_stdio()
    load_dotenv()
    hosted_ids = json.loads(HOSTED_FILE.read_text(encoding="utf-8")) if HOSTED_FILE.exists() else {}
    for spec in load_servers("local"):
        if args.system not in ("all", spec.key):
            continue
        result = build_one(spec, args.mode, args.discover, args.include_live, args.smart_names, args.fresh)
        if args.mode == "hosted":
            # Saved after every system, so an id is not lost if the next build fails.
            hosted_ids[spec.key] = {"server_id": result["server_id"], "server_name": spec.server_name}
            HOSTED_FILE.write_text(json.dumps(hosted_ids, indent=2) + "\n", encoding="utf-8", newline="\n")
    write_client_config(args.mode)


if __name__ == "__main__":
    main()
