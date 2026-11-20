"""Model adapters behind one small interface.

Two wire formats are supported, chosen by LLM_API_STYLE (responses | chat | auto):

* responses - OpenAI Responses API (POST {base}/responses). Default for
  api.openai.com. Conversation state is kept on our side: every model output
  item (reasoning items included) is appended to the input and sent back with
  the tool results, as the function-calling guide requires for reasoning models.
* chat      - Chat Completions (POST {base}/chat/completions). Works with any
  OpenAI-compatible endpoint: OpenRouter, Groq, Ollama, LM Studio.

Environment (see .env.example):
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
    LLM_API_STYLE          responses | chat | auto      (default auto)
    LLM_REASONING_EFFORT   e.g. low | medium | high     (responses only, default medium; "off" omits it)
    LLM_TEMPERATURE        number                       (chat only; omitted unless set)
    LLM_TIMEOUT            seconds                      (default 180)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import requests

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    error: str = ""          # set when the model produced arguments that are not valid JSON


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    incomplete: str = ""     # why the provider cut the response short, if it did


class LLMError(RuntimeError):
    pass


def _parse_arguments(raw) -> tuple[dict, str]:
    if isinstance(raw, dict):
        return raw, ""
    if raw is None or raw == "":
        return {}, ""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {}, f"arguments were not valid JSON ({exc})"
    if not isinstance(value, dict):
        return {}, "arguments must be a JSON object"
    return value, ""


class _HttpLLM:
    path = ""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.calls = 0

    def _post(self, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last = ""
        for attempt in range(3):
            try:
                resp = requests.post(self.base_url + self.path, headers=headers, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    self.calls += 1
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:600]}"
                if resp.status_code not in RETRY_STATUS:
                    break
            time.sleep(2 * (attempt + 1))
        raise LLMError(f"{self.model} at {self.base_url}{self.path} failed: {last}")


class ResponsesLLM(_HttpLLM):
    """OpenAI Responses API with client-side conversation state."""

    path = "/responses"

    def __init__(self, *args, reasoning_effort: str = "medium", **kwargs):
        super().__init__(*args, **kwargs)
        self.reasoning_effort = reasoning_effort
        self.instructions = ""
        self.items: list[dict] = []
        self.tools: list[dict] = []

    def start(self, system: str, user: str, tools: list[dict]) -> None:
        self.instructions = system
        self.items = [{"role": "user", "content": user}]
        self.tools = [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
                "strict": False,   # Synapse passthrough tools have untyped arguments
            }
            for tool in tools
        ]

    def next(self) -> Turn:
        body = {"model": self.model, "instructions": self.instructions, "input": self.items}
        if self.tools:
            body["tools"] = self.tools
        if self.reasoning_effort and self.reasoning_effort != "off":
            body["reasoning"] = {"effort": self.reasoning_effort}
        data = self._post(body)
        if data.get("error"):
            raise LLMError(f"{self.model} returned an error: {json.dumps(data['error'])[:400]}")
        output = data.get("output") or []
        self.items.extend(output)          # reasoning items must travel back with the tool outputs
        turn = Turn(usage=data.get("usage") or {})
        if data.get("status") == "incomplete":
            turn.incomplete = str((data.get("incomplete_details") or {}).get("reason") or "incomplete")
        for item in output:
            kind = item.get("type")
            if kind == "function_call":
                arguments, error = _parse_arguments(item.get("arguments"))
                turn.tool_calls.append(ToolCall(id=item.get("call_id", ""), name=item.get("name", ""), arguments=arguments, error=error))
            elif kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        turn.text += part.get("text", "")
                    elif part.get("type") == "refusal":
                        turn.text += part.get("refusal", "")
        return turn

    def add_tool_result(self, call: ToolCall, output: str) -> None:
        self.items.append({"type": "function_call_output", "call_id": call.id, "output": output})


class ChatLLM(_HttpLLM):
    """Chat Completions for OpenAI-compatible endpoints."""

    path = "/chat/completions"

    def __init__(self, *args, temperature: float | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.temperature = temperature
        self.messages: list[dict] = []
        self.tools: list[dict] = []

    def start(self, system: str, user: str, tools: list[dict]) -> None:
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        self.tools = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    def next(self) -> Turn:
        body = {"model": self.model, "messages": self.messages}
        if self.tools:
            body["tools"] = self.tools
        if self.temperature is not None:
            body["temperature"] = self.temperature
        data = self._post(body)
        if data.get("error"):
            raise LLMError(f"{self.model} returned an error: {json.dumps(data['error'])[:400]}")
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        self.messages.append(message)
        turn = Turn(text=message.get("content") or "", usage=data.get("usage") or {})
        if choice.get("finish_reason") == "length":
            turn.incomplete = "max_output_tokens"
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments, error = _parse_arguments(function.get("arguments"))
            turn.tool_calls.append(ToolCall(id=call.get("id", ""), name=function.get("name", ""), arguments=arguments, error=error))
        return turn

    def add_tool_result(self, call: ToolCall, output: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call.id, "content": output})


def make_llm():
    """Build the adapter described by the environment."""
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not base_url or not model:
        raise SystemExit("LLM_BASE_URL and LLM_MODEL are required. Copy .env.example to .env and fill it in.")
    style = os.environ.get("LLM_API_STYLE", "auto").strip().lower()
    if style == "auto":
        style = "responses" if "api.openai.com" in base_url else "chat"
    timeout = float(os.environ.get("LLM_TIMEOUT", "180"))
    if style == "responses":
        effort = os.environ.get("LLM_REASONING_EFFORT", "medium").strip().lower()
        return ResponsesLLM(base_url, api_key, model, timeout, reasoning_effort=effort)
    if style == "chat":
        raw = os.environ.get("LLM_TEMPERATURE", "").strip()
        return ChatLLM(base_url, api_key, model, timeout, temperature=float(raw) if raw else None)
    raise SystemExit(f"LLM_API_STYLE must be responses, chat or auto (got {style!r})")
