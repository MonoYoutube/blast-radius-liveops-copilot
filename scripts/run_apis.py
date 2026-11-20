"""Start both source systems.

    python scripts/run_apis.py            # with auto-reload (use this for the demo)
    python scripts/run_apis.py --no-reload

game backend  -> http://127.0.0.1:8101/docs
support desk  -> http://127.0.0.1:8102/docs
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
from importlib.util import cache_from_source
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.servers import ServerSpec, load_servers  # noqa: E402


def _spawn(spec: ServerSpec, reload: bool, quiet: bool) -> subprocess.Popen:
    python = sys.executable
    options = {}
    if sys.platform == "win32":
        # Launch the interpreter directly with its venv, as CPython's venv
        # redirector does. The PID we own is then the actual API process.
        if sys.prefix != sys.base_prefix:
            python = sys._base_executable
            options["env"] = {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = [python, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(spec.port)]
    if reload and sys.platform != "win32":
        cmd.append("--reload")
    if quiet:
        cmd += ["--log-level", "warning"]
    return subprocess.Popen(cmd, cwd=spec.path, **options)


def _source_stamp(folder: Path) -> dict:
    stamp = {}
    for source in folder.rglob("*.py"):
        if source.name.startswith("synapse_auto_server"):
            continue  # The generated MCP clients run separately from these APIs.
        try:
            stamp[source] = source.stat().st_mtime_ns
        except FileNotFoundError:
            pass  # A save can rename a file while the watcher scans it.
    return stamp


class _WindowsReloaders(list):
    """Keep API PIDs independent of Uvicorn's console-wide Ctrl+C reload."""

    def __init__(self):
        super().__init__()
        self.stopping = threading.Event()
        self.watcher = None

    def watch(self, specs, stamps, quiet):
        while not self.stopping.wait(0.25):
            for index, spec in enumerate(specs):
                current = _source_stamp(spec.path)
                if current != stamps[index]:
                    print(f"Reloading {spec.key} after a source change.", flush=True)
                    stop([self[index]])
                    # Timestamp-based .pyc files miss same-size edits within one second.
                    for source, stamp in current.items():
                        if stamps[index].get(source) != stamp:
                            cache = Path(cache_from_source(str(source))).resolve()
                            if cache.is_relative_to(spec.path):
                                cache.unlink(missing_ok=True)
                    stamps[index] = current
                    self[index] = _spawn(spec, reload=False, quiet=quiet)


def start(reload: bool = True, quiet: bool = False) -> list[subprocess.Popen]:
    specs = load_servers("local")
    procs = _WindowsReloaders() if sys.platform == "win32" and reload else []
    try:
        if isinstance(procs, _WindowsReloaders):
            stamps = [_source_stamp(spec.path) for spec in specs]
        for spec in specs:
            procs.append(_spawn(spec, reload, quiet))
        if isinstance(procs, _WindowsReloaders):
            procs.watcher = threading.Thread(target=procs.watch, args=(specs, stamps, quiet), daemon=True)
            procs.watcher.start()
    except BaseException:
        stop(procs)
        raise
    return procs


def wait_ready(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    for spec in load_servers("local"):
        while True:
            try:
                urllib.request.urlopen(f"{spec.base_url}/openapi.json", timeout=2).read()
                break
            except Exception:
                if time.time() > deadline:
                    raise SystemExit(f"{spec.key} did not start on {spec.base_url}")
                time.sleep(0.3)


def stop(procs: list[subprocess.Popen]) -> None:
    if isinstance(procs, _WindowsReloaders):
        procs.stopping.set()
        if procs.watcher is not None and procs.watcher.ident is not None:
            procs.watcher.join()
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main() -> None:
    procs = start(reload="--no-reload" not in sys.argv)
    try:
        wait_ready()
        for spec in load_servers("local"):
            print(f"{spec.key:13s} ready on {spec.base_url}  (docs: {spec.base_url}/docs)")
        print("Ctrl+C to stop.")
        while (procs.watcher.is_alive() if isinstance(procs, _WindowsReloaders)
               else all(proc.poll() is None for proc in procs)):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop(procs)


if __name__ == "__main__":
    main()
