"""The command line as a user or the demo script runs it: exit codes and what reaches the console."""
import os
import subprocess
import sys

import pytest

from agent.servers import ROOT
from scripts.demo import stale_build_verdict
from tests.helpers.model_stub import ModelStub

SECRET = "sk-cli-secret-9999"


def call(name, arguments, call_id="call_1"):
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id, "name": name, "arguments": arguments}


def message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def copilot(stub: ModelStub, question: str = "Which incidents exist?") -> subprocess.CompletedProcess:
    env = {**os.environ, "LLM_BASE_URL": stub.url, "LLM_API_KEY": SECRET, "LLM_MODEL": "stub-model", "LLM_API_STYLE": "responses"}
    return subprocess.run([sys.executable, "-m", "agent.copilot", question], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)


@pytest.fixture()
def stub_factory():
    stubs = []

    def make(responder):
        stubs.append(ModelStub(responder))
        return stubs[-1]

    yield make
    for stub in stubs:
        stub.close()


def test_answer_exits_zero_and_shows_the_trace(apis, stub_factory):
    def responder(path, body):
        answered = any(item.get("type") == "function_call_output" for item in body["input"])
        return 200, {"output": [message("There are three incidents.\nNot verified: nothing")] if answered
                     else [call("game__list_incidents", "{}")]}

    result = copilot(stub_factory(responder))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[game] list_incidents() -> ok" in result.stdout and "There are three incidents." in result.stdout
    assert SECRET not in result.stdout + result.stderr


def test_round_limit_exits_nonzero_with_the_reason(apis, stub_factory):
    counter = {"n": 0}

    def responder(path, body):                                   # the model never stops calling tools
        counter["n"] += 1
        return 200, {"output": [call("game__list_incidents", "{}", f"call_{counter['n']}")]}

    result = copilot(stub_factory(responder))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "No complete answer: max_rounds" in result.stdout
    assert counter["n"] == 16


def test_model_failure_exits_three_without_a_traceback_or_the_key(apis, stub_factory):
    def responder(path, body):
        return 401, {"error": {"message": f"Incorrect API key provided: {SECRET}"}}

    result = copilot(stub_factory(responder))
    output = result.stdout + result.stderr
    assert result.returncode == 3, output
    assert "No complete answer: model_error" in output and "HTTP 401" in output
    assert SECRET not in output and "Traceback" not in output and "ExceptionGroup" not in output


def test_empty_model_answer_exits_nonzero(apis, stub_factory):
    result = copilot(stub_factory(lambda path, body: (200, {"output": [message("")]})))
    assert result.returncode == 2 and "No complete answer: empty_answer" in result.stdout


def test_demo_accepts_only_the_real_stale_cache_failure():
    stale = "i  Discover  Using the endpoints discovered on Nov 20 (8 endpoint(s)).\n✖  Endpoints Not Found\nbuild failed: kept the previous synapse_auto_server.py"
    assert stale_build_verdict(1, stale, server_kept=True) == ""
    assert "succeeded" in stale_build_verdict(0, stale, server_kept=True)
    assert "not kept" in stale_build_verdict(1, stale, server_kept=False)
    assert "other than the missing endpoint" in stale_build_verdict(1, "Using the endpoints discovered on Nov 20\n✖ Quota Exceeded", True)
    assert "before Synapse reused" in stale_build_verdict(1, "Synapse CLI not found", True)
    assert "before Synapse reused" in stale_build_verdict(1, "✖  Endpoints Not Found", True)        # a fresh scan that misses it is a different bug
