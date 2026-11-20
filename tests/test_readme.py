"""The README is what a judge copies commands from. It must match the code it describes."""
import asyncio
import re

from agent.servers import ROOT
from eval import scripted_check

README = (ROOT / "README.md").read_text(encoding="utf-8")
COMMAND = re.compile(r"python (scripts/\w+\.py|-m [\w.]+)((?: (?:--[\w-]+|[\w,]+))*)")


def test_expected_output_is_the_real_output_of_the_scripted_check(apis):
    shown = [line[len("PASS  "):] for line in README.splitlines() if line.startswith("PASS  ")]
    results = asyncio.run(scripted_check.run("local", "baseline"))
    assert shown == [label for label, *_ in results]


def test_every_command_names_a_script_and_flags_that_exist():
    commands = COMMAND.findall(README)
    assert len(commands) >= 20
    for target, flags in commands:
        if target.startswith("-m "):
            path = ROOT / (target[3:].replace(".", "/") + ".py")
            if target == "-m pip" or target == "-m pytest" or target == "-m venv":
                continue
        else:
            path = ROOT / target
        assert path.exists(), f"README mentions {target}, which does not exist"
        source = path.read_text(encoding="utf-8")
        for flag in re.findall(r"--[\w-]+", flags):
            assert f'"{flag}"' in source, f"README uses {flag} with {target}, which does not define it"


def code_lines() -> list[str]:
    """Every line inside a fenced block, whatever its language tag or indentation."""
    lines, inside = [], False
    for raw in README.splitlines():
        if raw.strip().startswith("```"):
            inside = not inside
        elif inside:
            lines.append(raw.strip())
    assert not inside, "unbalanced code fence"
    return lines


def test_commands_work_in_a_fresh_terminal_on_linux_and_in_windows_powershell():
    assert "&&" not in README                               # Windows PowerShell 5.1 rejects it
    lines = code_lines()
    assert "source .venv/bin/activate" in lines and r".venv\Scripts\Activate.ps1" in lines
    commands = [line for line in lines if line.startswith(("python", ".venv", "py "))]
    assert len(commands) >= 12
    for line in commands:                                   # after activation every command is plain `python ...`
        assert line.startswith(("python ", "python3 -m venv", "py -3.12 -m venv", ".venv\\Scripts\\Activate.ps1")), line
