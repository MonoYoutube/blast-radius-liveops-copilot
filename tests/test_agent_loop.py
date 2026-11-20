"""The copilot loop with a scripted model: no network, real MCP servers."""
import asyncio
import json

from agent.copilot import PROMPT_SHA256, answer, exit_code, find_llm_error, is_read_only, print_step, say, tool_specs
from agent.llm import LLMError, ToolCall, Turn
from agent.mcp_hub import McpHub
from agent.servers import load_servers


class ScriptedLLM:
    """Plays back a fixed list of turns and records what it was given."""

    model = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0
        self.results = []
        self.tools = []

    def start(self, system, user, tools):
        self.system, self.user, self.tools = system, user, tools

    def next(self):
        self.calls += 1
        return self.turns.pop(0)

    def add_tool_result(self, call, output):
        self.results.append((call.name, output))


def test_loop_calls_both_systems_and_returns_answer(apis):
    llm = ScriptedLLM([
        Turn(tool_calls=[ToolCall("c1", "game__list_incident_affected_players", {"incident_id": "INC-2026-11-18-EU2"})]),
        Turn(tool_calls=[ToolCall("c2", "desk__search_tickets", {"requester_emails": ["marco.r@example.com"], "created_from": "2026-11-18T18:10:00Z", "status": None})]),
        Turn(text="IronTulip (P-1004) wrote T-1043 and is still waiting.\nNot verified: nothing"),
    ])
    steps = []
    run = asyncio.run(answer("who?", llm, on_step=steps.append))
    assert run["completed"] and "T-1043" in run["answer"]
    assert run["systems_used"] == ["desk", "game"]
    assert [s["tool"] for s in steps] == ["game__list_incident_affected_players", "desk__search_tickets"]
    assert all(s["ok"] for s in steps)
    assert "T-1043" in llm.results[1][1]
    assert len(llm.tools) >= 10 and all(t["name"].count("__") == 1 for t in llm.tools)
    assert "only bridge" in llm.system
    assert run["system_prompt_sha256"] == PROMPT_SHA256 and len(PROMPT_SHA256) == 64      # a saved run names its prompt


def test_loop_reports_bad_calls_back_to_the_model(apis):
    llm = ScriptedLLM([
        Turn(tool_calls=[
            ToolCall("c1", "game__get_incident", {"incident_id": "INC-NOPE"}),
            ToolCall("c2", "game__get_player", {}, error="arguments were not valid JSON"),
            ToolCall("c3", "desk__reply_to_ticket", {"ticket_id": "T-1042", "text": "hi"}),
        ]),
        Turn(text="Could not find that incident.\nNot verified: everything"),
    ])
    run = asyncio.run(answer("?", llm))
    assert [s["ok"] for s in run["steps"]] == [False, False, False]
    assert "404" in llm.results[0][1]
    assert "rejected" in llm.results[1][1]
    assert "blocked" in llm.results[2][1]          # a write tool is never run without approval
    assert run["completed"]


def test_loop_stops_after_max_rounds(apis):
    endless = [Turn(tool_calls=[ToolCall(f"c{i}", "game__list_incidents", {})]) for i in range(5)]
    run = asyncio.run(answer("?", ScriptedLLM(endless), max_rounds=3))
    assert not run["completed"] and len(run["steps"]) == 3
    assert run["stop_reason"] == "max_rounds" and exit_code(run) == 2


def test_empty_and_truncated_answers_are_not_complete(apis):
    empty = asyncio.run(answer("?", ScriptedLLM([Turn(text="   ")])))
    assert not empty["completed"] and empty["stop_reason"] == "empty_answer" and exit_code(empty) == 2
    cut = asyncio.run(answer("?", ScriptedLLM([Turn(text="Seven players were", incomplete="max_output_tokens")])))
    assert not cut["completed"] and cut["stop_reason"] == "incomplete:max_output_tokens" and cut["answer"] == "Seven players were"
    done = asyncio.run(answer("?", ScriptedLLM([Turn(text="Nobody.")])))
    assert done["completed"] and done["stop_reason"] == "answered" and exit_code(done) == 0


class FailingLLM(ScriptedLLM):
    """Works for the scripted turns, then the provider call fails."""

    def next(self):
        if not self.turns:
            raise LLMError("some-model at https://api.example/v1/responses failed: HTTP 401: key sk-live-secret-0001 rejected")
        return super().next()


def test_model_failure_keeps_the_trace_and_never_leaks_the_key(apis, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-live-secret-0001")
    llm = FailingLLM([Turn(tool_calls=[ToolCall("c1", "game__list_incidents", {})])])
    run = asyncio.run(answer("?", llm))                       # must not raise, even inside the MCP task groups
    assert not run["completed"] and run["stop_reason"] == "model_error" and exit_code(run) == 3
    assert len(run["steps"]) == 1 and run["steps"][0]["ok"]   # what was observed before the failure is kept
    assert "HTTP 401" in run["error"] and "sk-live-secret-0001" not in run["error"]
    assert find_llm_error(ExceptionGroup("outer", [ExceptionGroup("inner", [LLMError("boom")])])) is not None
    assert find_llm_error(ExceptionGroup("outer", [ValueError("x")])) is None


def test_nothing_secret_reaches_the_console_or_the_record(apis, monkeypatch, capsys):
    secret = "sk-console-secret-4242"
    monkeypatch.setenv("LLM_API_KEY", secret)
    llm = ScriptedLLM([
        Turn(tool_calls=[
            ToolCall("c1", "game__get_incident", {"incident_id": f"INC-{secret}"}),          # echoed back in the 404 text
            ToolCall("c2", "desk__reply_to_ticket", {"ticket_id": "T-1042", "text": secret}),
        ]),
        Turn(text=f"The key is {secret}.\nNot verified: nothing"),
    ])
    run = asyncio.run(answer(f"what is {secret}?", llm, on_step=print_step))
    say(f"Q: {run['question']}")
    say(run["answer"])
    printed = capsys.readouterr().out
    assert secret not in printed and "[redacted]" in printed
    assert secret not in json.dumps(run["steps"]) and secret not in run["answer"]
    assert all(secret not in output for _, output in llm.results)       # nor is it sent back to the model


def test_usage_is_recorded_per_model_call(apis):
    llm = ScriptedLLM([
        Turn(tool_calls=[ToolCall("c1", "game__list_incidents", {})], usage={"input_tokens": 100, "output_tokens": 20, "cached": True}),
        Turn(text="Three incidents.", usage={"input_tokens": 150, "output_tokens": 30}),
    ])
    run = asyncio.run(answer("?", llm))
    assert run["llm_usage"] == [{"input_tokens": 100, "output_tokens": 20, "cached": True}, {"input_tokens": 150, "output_tokens": 30}]
    assert run["usage_total"] == {"input_tokens": 250, "output_tokens": 50}


def test_read_only_rule_and_specs(apis):
    assert is_read_only("game__list_incidents") and is_read_only("desk__search_tickets")
    assert not is_read_only("desk__reply_to_ticket") and not is_read_only("game__erase_player")

    async def specs():
        async with McpHub(load_servers("local")) as hub:
            return tool_specs(hub)

    by_name = {s["name"]: s for s in asyncio.run(specs())}
    search = by_name["desk__search_tickets"]
    assert set(search["parameters"]["properties"]) == {"requester_emails", "created_from", "status"}
    assert search["description"].startswith("[desk] ")
