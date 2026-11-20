"""Wire-format checks for both model adapters against a local stub server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent.llm import ChatLLM, LLMError, ResponsesLLM

TOOLS = [{"name": "game__get_incident", "description": "[game] Get one incident.", "parameters": {"type": "object", "properties": {"incident_id": {}}}}]


class Stub(BaseHTTPRequestHandler):
    seen = []
    replies = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.seen.append((self.path, self.headers.get("Authorization"), body))
        status, payload = Stub.replies.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub():
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Stub.seen, Stub.replies = [], []
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_responses_adapter_round_trip(stub):
    Stub.replies = [
        (200, {"output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "game__get_incident", "arguments": "{\"incident_id\": \"INC-1\"}"},
        ], "usage": {"total_tokens": 10}}),
        (200, {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}]}),
    ]
    llm = ResponsesLLM(stub, "sk-test", "some-model", 5, reasoning_effort="medium")
    llm.start("SYSTEM", "QUESTION", TOOLS)

    first = llm.next()
    assert first.tool_calls[0].name == "game__get_incident" and first.tool_calls[0].arguments == {"incident_id": "INC-1"}
    llm.add_tool_result(first.tool_calls[0], '{"ok": true}')
    assert llm.next().text == "Done."

    path, auth, body = Stub.seen[0]
    assert path == "/v1/responses" and auth == "Bearer sk-test"
    assert body["instructions"] == "SYSTEM" and body["input"] == [{"role": "user", "content": "QUESTION"}]
    assert body["reasoning"] == {"effort": "medium"}
    assert body["tools"][0] == {"type": "function", "name": "game__get_incident", "description": "[game] Get one incident.",
                                "parameters": TOOLS[0]["parameters"], "strict": False}
    for forbidden in ("temperature", "top_p", "logprobs", "top_logprobs"):
        assert forbidden not in body

    second = Stub.seen[1][2]["input"]
    assert [item.get("type") for item in second] == [None, "reasoning", "function_call", "function_call_output"]
    assert second[-1] == {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'}


def test_responses_adapter_can_omit_reasoning(stub):
    Stub.replies = [(200, {"output": []})]
    llm = ResponsesLLM(stub, "", "m", 5, reasoning_effort="off")
    llm.start("S", "Q", [])
    llm.next()
    _, auth, body = Stub.seen[0]
    assert "reasoning" not in body and "tools" not in body and auth is None


def test_chat_adapter_round_trip(stub):
    Stub.replies = [
        (200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "game__get_incident", "arguments": "{bad json"}}]}}]}),
        (200, {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}),
    ]
    llm = ChatLLM(stub, "key", "m", 5)
    llm.start("SYSTEM", "QUESTION", TOOLS)
    first = llm.next()
    assert first.tool_calls[0].error and first.tool_calls[0].arguments == {}
    llm.add_tool_result(first.tool_calls[0], "rejected")
    assert llm.next().text == "Done."

    path, _, body = Stub.seen[0]
    assert path == "/v1/chat/completions" and "temperature" not in body
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert body["tools"][0]["function"]["name"] == "game__get_incident"
    assert Stub.seen[1][2]["messages"][-1] == {"role": "tool", "tool_call_id": "t1", "content": "rejected"}


def test_http_errors_surface_without_the_key(stub):
    Stub.replies = [(401, {"error": {"message": "bad key"}})]
    llm = ResponsesLLM(stub, "sk-secret-value", "m", 5)
    llm.start("S", "Q", [])
    with pytest.raises(LLMError) as err:
        llm.next()
    assert "401" in str(err.value) and "sk-secret-value" not in str(err.value)


def test_truncated_and_error_responses_are_not_mistaken_for_answers(stub):
    Stub.replies = [
        (200, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
               "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Seven players"}]}]}),
        (200, {"error": {"message": "model overloaded"}, "output": []}),
    ]
    llm = ResponsesLLM(stub, "k", "m", 5)
    llm.start("S", "Q", [])
    turn = llm.next()
    assert turn.incomplete == "max_output_tokens" and turn.text == "Seven players"
    with pytest.raises(LLMError) as err:
        llm.next()
    assert "model overloaded" in str(err.value)

    Stub.replies = [(200, {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "Seven"}}]})]
    chat = ChatLLM(stub, "k", "m", 5)
    chat.start("S", "Q", [])
    assert chat.next().incomplete == "max_output_tokens"
