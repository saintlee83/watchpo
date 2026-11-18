"""Tool-calling Chat Completions adapter for local and hosted endpoints."""

from __future__ import annotations

import json
import os

import httpx2

from .analyzer import AnalysisError, ToolAnalyzer, ToolCall, Turn
from .config import AISettings, ConfigError
from .models import Usage


def chat_client(settings: AISettings) -> httpx2.Client:
    headers = {}
    if settings.api_key_env:
        key = os.environ.get(settings.api_key_env)
        if not key:
            raise ConfigError(f"Set {settings.api_key_env} for {settings.provider}.")
        headers["Authorization"] = f"Bearer {key}"
    return httpx2.Client(base_url=settings.base_url.rstrip("/") + "/", headers=headers, timeout=180)


class ChatAnalyzer(ToolAnalyzer):
    def __init__(self, client, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = client

    def _request(self, messages: list[dict]) -> Turn:
        history = [{"role": "system", "content": self.system[0]["text"]}]
        for message in messages:
            content = message.get("content")
            if message["role"] == "user" and isinstance(content, list):
                for block in content:
                    if block["type"] == "tool_result":
                        history.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                        "content": block["content"]})
                    else:
                        history.append({"role": "user", "content": block["text"]})
            else:
                history.append(message)
        tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
        }} for t in self.tools]
        try:
            response = self.client.post("chat/completions", json={
                "model": self.s.model, "messages": history, "tools": tools,
            })
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            assistant = {"role": "assistant", "content": message.get("content")}
            if message.get("reasoning_content") is not None:
                assistant["reasoning_content"] = message["reasoning_content"]
            stop = choice.get("finish_reason")
            calls = []
            if raw_calls := message.get("tool_calls"):
                assistant["tool_calls"] = raw_calls
                for call in raw_calls:
                    f = call["function"]
                    arguments = {} if stop == "length" else json.loads(f["arguments"])
                    calls.append(ToolCall(call["id"], f["name"], arguments))
            tokens = body.get("usage") or {}
            usage = Usage(input_tokens=tokens.get("prompt_tokens", 0),
                          output_tokens=tokens.get("completion_tokens", 0), requests=1)
            if stop == "length":
                stop = "max_tokens"
            if message.get("refusal") or stop == "content_filter":
                stop = "refusal"
            return Turn(assistant, calls, stop, usage, body.get("model") or self.s.model)
        except (httpx2.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise AnalysisError(f"Chat Completions request failed: {e}") from e
