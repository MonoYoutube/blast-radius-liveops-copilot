"""Boundary cases on isolated copies of the two systems. The demo data and the generated servers stay as committed.

partial_grant     a crash grant to a player who is also owed a purchase must not settle the purchase
unrelated_ticket  a ticket from an affected player since the incident started is not always about the incident
"""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys

import pytest

from agent.copilot import answer
from agent.llm import ToolCall, Turn
from agent.servers import ROOT
from eval import ground_truth, instance, scripted_check
from eval.agent_eval import cases, evaluate
from scripts import live_update
from tests.conftest import synapse_available
from tests.helpers.model_stub import ModelStub
from tests.test_agent_loop import ScriptedLLM

INCIDENT = "INC-2026-11-18-EU2"
SEEDS = {
    "systems/game_backend/data/seed.json": "89f128d14fb9dab09e9e1232cb6f4f1a0f3f8cff8392404af47ba6534aa3d9d5",
    "systems/support_desk/data/seed.json": "3c98987e04b6ad9cebdd59db47d9ce948cec5428a312c1cf213551c4e80e22f2",
}
needs_synapse = [pytest.mark.synapse, pytest.mark.skipif(not synapse_available(), reason="Synapse CLI not installed")]


def digest(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def rows(results) -> dict:
    return {label: got for label, passed, got, expected in results}


def row(results, part: str) -> dict:
    found = [got for label, got in rows(results).items() if part in label]
    assert len(found) == 1, f"{part!r} matches {len(found)} rows"
    return found[0]


def assert_all_pass(results) -> None:
    failures = [(label, got, expected) for label, passed, got, expected in results if not passed]
    assert not failures, failures


@pytest.fixture()
def copy_of(tmp_path, monkeypatch):
    """Point the ground truth at an isolated copy of the data (no API involved)."""
    def make(scenario: str) -> dict:
        folders = instance.create(tmp_path / scenario, scenario)
        for key, folder in folders.items():
            monkeypatch.setenv(f"{key.upper()}_DIR", str(folder))
        return folders
    return make


def test_demo_data_is_the_data_the_published_runs_used():
    """The recorded agent runs and the README numbers refer to exactly these two files."""
    assert {relative: digest(relative) for relative in SEEDS} == SEEDS


def test_scenarios_change_only_their_own_copy(tmp_path):
    for name in instance.SCENARIOS:
        folders = instance.create(tmp_path / name, name)
        assert not (folders["game_backend"] / ".synapse").exists()            # no scan cache or account binding travels along
        copied = {key: hashlib.sha256((folder / "data" / "seed.json").read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                  for key, folder in folders.items()}
        changed = {key for key in copied if copied[key] != digest(f"systems/{key}/data/seed.json")}
        assert changed == {"original": set(), "partial_grant": {"game_backend"}, "unrelated_ticket": {"support_desk"}}[name]
        for key, folder in folders.items():                                    # the generated server is used exactly as committed
            assert (folder / "synapse_auto_server.py").read_bytes() == (ROOT / "systems" / key / "synapse_auto_server.py").read_bytes()
    assert {relative: digest(relative) for relative in SEEDS} == SEEDS
    with pytest.raises(SystemExit, match="unknown scenario"):
        instance.create(tmp_path / "x", "no_such_scenario")


def test_partial_grant_a_crash_grant_does_not_settle_a_purchase(copy_of):
    copy_of("partial_grant")
    # Seen player by player, the queue of people "without any compensation" is now empty ...
    assert ground_truth.q3(INCIDENT) == {
        "compensated": ["P-1001", "P-1002", "P-1003", "P-1004"],
        "not_compensated": ["P-1005", "P-1006", "P-1007"],
        "not_compensated_and_awaiting_reply": [],
        "not_compensated_and_silent": ["P-1006", "P-1007"],
    }
    # ... but purchase by purchase PU-5002 (500 Gems, $4.99) is still owed to P-1004, whose ticket is unanswered.
    assert ground_truth.q3_owed(INCIDENT) == {
        "ledger_checked": True,
        "undelivered_purchase_ids": ["PU-5001", "PU-5002", "PU-5003"],
        "settled": {"PU-5001": ["C-9003"]},
        "owed": {"PU-5002": "P-1004", "PU-5003": "P-1007"},
        "owed_players_awaiting_reply": ["P-1004"],
        "grants_without_purchase": {"P-1001": ["C-9001"], "P-1003": ["C-9002"], "P-1004": ["C-9005"]},
    }
    assert ground_truth.q2_resolution("T-1043") == {
        "player_id": "P-1004", "undelivered_purchase_ids": ["PU-5002"],
        "related_compensation_ids": [], "other_grant_ids": ["C-9005"], "still_owed_purchase_ids": ["PU-5002"],
    }
    assert ground_truth.q2_resolution("T-1042") == {                             # T-1042 after C-9003: settled, nothing owed
        "player_id": "P-1002", "undelivered_purchase_ids": ["PU-5001"],
        "related_compensation_ids": ["C-9003"], "other_grant_ids": [], "still_owed_purchase_ids": [],
    }


def test_unrelated_ticket_wrote_since_is_not_reported_the_incident(copy_of):
    copy_of("unrelated_ticket")
    first = ground_truth.q1(INCIDENT)
    assert first["with_tickets"] == {"P-1001": ["T-1041"], "P-1002": ["T-1042"], "P-1004": ["T-1043"], "P-1005": ["T-1044"], "P-1006": ["T-1052"]}
    assert first["awaiting_first_response"] == ["T-1042", "T-1043", "T-1052"]
    assert ground_truth.q1_topics(INCIDENT) == {
        "about_incident": ["T-1041", "T-1042", "T-1043", "T-1044"],
        "other_topics": ["T-1052"],
        "awaiting_about_incident": ["T-1042", "T-1043"],
        "awaiting_other_topics": ["T-1052"],
    }
    assert ground_truth.q1_moment(INCIDENT) == {
        "unanswered_at_incident_end": ["T-1042", "T-1043", "T-1044", "T-1052"],
        "awaiting_first_response_now": ["T-1042", "T-1043", "T-1052"],
    }


def test_tool_chain_on_the_unrelated_ticket_copy():
    """Through the committed MCP servers, against private APIs that serve the changed data."""
    with instance.running("unrelated_ticket") as copy:
        assert copy.ports["game_backend"] not in (8101, 8102) and copy.ports["support_desk"] not in (8101, 8102)
        results = asyncio.run(scripted_check.run("local", "baseline"))
    assert_all_pass(results)
    assert len(results) == 10
    assert row(results, "by topic") == {
        "about_incident": ["T-1041", "T-1042", "T-1043", "T-1044"], "other_topics": ["T-1052"],
        "awaiting_about_incident": ["T-1042", "T-1043"], "awaiting_other_topics": ["T-1052"],
    }
    assert row(results, "incident end vs now") == {
        "unanswered_at_incident_end": ["T-1042", "T-1043", "T-1044", "T-1052"],
        "awaiting_first_response_now": ["T-1042", "T-1043", "T-1052"],
    }


def test_without_the_ledger_settlement_is_reported_as_unknown():
    with instance.running("partial_grant"):
        results = asyncio.run(scripted_check.run("local", "baseline"))
    assert_all_pass(results)
    assert row(results, "settlement unknown") == {
        "ledger_checked": False, "undelivered_purchase_ids": ["PU-5001", "PU-5002", "PU-5003"],
        "settled": None, "owed": None, "owed_players_awaiting_reply": None, "grants_without_purchase": None,
    }


@pytest.mark.parametrize("scenario", ["original", "partial_grant"], ids=lambda name: name)
@pytest.mark.synapse
@pytest.mark.skipif(not synapse_available(), reason="Synapse CLI not installed")
def test_tool_chain_with_the_ledger_decides_purchase_by_purchase(scenario):
    """Live stage in the copy: the endpoint is added there and `synapse build --discover` runs there."""
    untouched = (ROOT / "systems" / "game_backend" / "incidents.py").read_bytes()
    with instance.running(scenario, live=True) as copy:
        assert live_update.app_file().is_relative_to(copy.root)
        results = asyncio.run(scripted_check.run("local", "live"))
    assert (ROOT / "systems" / "game_backend" / "incidents.py").read_bytes() == untouched
    assert_all_pass(results)
    assert len(results) == 13
    owed = row(results, "purchase by purchase")
    assert owed["settled"] == {"PU-5001": ["C-9003"]}
    assert owed["owed"] == {"PU-5002": "P-1004", "PU-5003": "P-1007"}
    assert owed["owed_players_awaiting_reply"] == ["P-1004"]
    gems = row(results, "T-1043")
    assert gems["still_owed_purchase_ids"] == ["PU-5002"] and gems["related_compensation_ids"] == []
    battle_pass = row(results, "T-1042: original charge")
    assert battle_pass["related_compensation_ids"] == ["C-9003"] and battle_pass["still_owed_purchase_ids"] == []
    by_player = row(results, "compensations x tickets")
    if scenario == "partial_grant":
        assert gems["other_grant_ids"] == ["C-9005"] and owed["grants_without_purchase"]["P-1004"] == ["C-9005"]
        assert by_player["not_compensated_and_awaiting_reply"] == []        # the per-player view no longer shows P-1004
    else:
        assert gems["other_grant_ids"] == [] and "P-1004" not in owed["grants_without_purchase"]
        assert by_player["not_compensated_and_awaiting_reply"] == ["P-1004"]


def facts(payload: dict, note: str = "nothing") -> str:
    return f"Answer.\nNot verified: {note}\n```json\n{json.dumps(payload)}\n```"


@pytest.mark.synapse
@pytest.mark.skipif(not synapse_available(), reason="Synapse CLI not installed")
def test_agent_run_on_the_partial_grant_copy_is_judged_purchase_by_purchase():
    """A scripted model walks the T-1043 chain through the live tools of the copy; the evaluator judges two endings."""
    chain = [
        ToolCall("c1", "desk__get_ticket", {"ticket_id": "T-1043"}),
        ToolCall("c2", "game__find_player_by_email", {"email": "marco.r@example.com"}),
        ToolCall("c3", "game__list_player_purchases", {"player_id": "P-1004"}),
        ToolCall("c4", "game__list_incident_compensations", {"incident_id": INCIDENT}),
    ]
    right = {"player_id": "P-1004", "charged_without_delivery": True, "evidence_purchase_ids": ["PU-5002"], "compensation_checked": True,
             "related_compensation_ids": [], "other_grant_ids": ["C-9005"], "still_owed": True}
    fooled = dict(right, related_compensation_ids=["C-9005"], other_grant_ids=[], still_owed=False)   # "they got 300 Gems, so we are even"

    with instance.running("partial_grant", live=True):
        case = next(c for c in cases(True) if c["id"] == "q2_owed")
        assert case["expected"] == right
        runs = {}
        for name, payload in (("right", right), ("fooled", fooled)):
            llm = ScriptedLLM([Turn(tool_calls=[call]) for call in chain] + [Turn(text=facts(payload))])
            runs[name] = asyncio.run(answer(case["question"], llm))
            assert [step["ok"] for step in runs[name]["steps"]] == [True] * 4
            ledger = llm.results[3][1]
            assert '"C-9005"' in ledger and '"C-9003"' in ledger                   # both grants were put in front of the model
        owed_case = next(c for c in cases(True) if c["id"] == "q3_owed")
        assert owed_case["expected"]["owed_purchase_ids"] == ["PU-5002", "PU-5003"] and owed_case["expected"]["settled_purchase_ids"] == ["PU-5001"]

    assert evaluate(case, runs["right"]) == []
    problems = evaluate(case, runs["fooled"])
    assert any("related_compensation_ids" in p for p in problems) and any("still_owed" in p for p in problems)


def test_cli_scenario_run_needs_nothing_else_running():
    def check(*flags):
        return subprocess.run([sys.executable, "-m", "eval.scripted_check", *flags], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)

    result = check("--scenario", "unrelated_ticket", "--stage", "baseline")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PASS  ") == 10 and "FAIL" not in result.stdout
    assert "scenario unrelated_ticket" in result.stdout and "the committed systems are not touched" in result.stdout

    unknown = check("--scenario", "no_such_scenario")
    assert unknown.returncode != 0 and "unknown scenario" in unknown.stderr and "partial_grant" in unknown.stderr
    hosted = check("--scenario", "partial_grant", "--mode", "hosted")
    assert hosted.returncode != 0 and "--mode local" in hosted.stderr


def test_agent_eval_on_a_scenario_from_the_command_line():
    """`python -m eval.agent_eval --scenario ...` end to end, with a local stand-in for the model provider."""
    reported = {"about_incident": ["T-1041", "T-1042", "T-1043", "T-1044"], "other_topics": ["T-1052"],
                "awaiting_about_incident": ["T-1042", "T-1043"]}

    def responder(path, body):
        done = sum(1 for item in body["input"] if item.get("type") == "function_call_output")
        call = lambda name, arguments: {"type": "function_call", "id": f"fc_{done}", "call_id": f"call_{done}", "name": name, "arguments": json.dumps(arguments)}
        if done == 0:
            return 200, {"output": [call("game__list_incident_affected_players", {"incident_id": INCIDENT})]}
        if done == 1:
            return 200, {"output": [call("desk__list_tickets_awaiting_first_response", {})]}
        return 200, {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": facts(reported)}]}]}

    stub = ModelStub(responder)
    env = {**os.environ, "LLM_BASE_URL": stub.url, "LLM_API_KEY": "sk-stub-key-000000", "LLM_MODEL": "stub-model", "LLM_API_STYLE": "responses"}

    def evaluate_on(scenario):
        result = subprocess.run([sys.executable, "-m", "eval.agent_eval", "--scenario", scenario, "--only", "q1_topics", "--runs", "1"],
                                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        saved = re.search(r"details: (runs[\\/]eval-[\w-]+\.json)", result.stdout)
        report = None
        if saved:
            report = json.loads((ROOT / saved.group(1)).read_text(encoding="utf-8"))
            (ROOT / saved.group(1)).unlink()                                 # leave no report of a stub model behind
        return result, report

    try:
        passed, report = evaluate_on("unrelated_ticket")                     # the copy has T-1052: the reported facts are right
        assert passed.returncode == 0, passed.stdout + passed.stderr
        assert "scenario unrelated_ticket" in passed.stdout and "q1_topics  run 1: PASS" in passed.stdout
        assert report["scenario"] == "unrelated_ticket" and report["model"] == "stub-model" and len(report["system_prompt_sha256"]) == 64
        assert report["runs"][0]["run"]["system_prompt_sha256"] == report["system_prompt_sha256"]

        failed, report = evaluate_on("original")                             # the demo data has no T-1052: same answer, now wrong
        assert failed.returncode == 1, failed.stdout + failed.stderr
        assert "q1_topics  run 1: FAIL" in failed.stdout and "other_topics" in failed.stdout
        assert report["scenario"] == "original"
    finally:
        stub.close()

    unknown = subprocess.run([sys.executable, "-m", "eval.agent_eval", "--only", "no_such_case"], cwd=ROOT, env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert unknown.returncode != 0 and "unknown case id" in unknown.stderr
