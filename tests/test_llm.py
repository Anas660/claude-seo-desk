"""The chat-model client: what it sends to OpenRouter and to any other OpenAI-compatible API."""
import asyncio
import json

import httpx
import pytest

from seodesk import llm
from seodesk.settings import OPENROUTER


def api(seen, status=200, payload=None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        return httpx.Response(status, json=payload if payload is not None else {"choices": [{"message": {"content": "hi"}}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_openrouter_gets_routing_cost_and_reasoning(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k1")
    seen = []
    asyncio.run(llm.Client(OPENROUTER, api(seen)).chat({"model": "a/b", "messages": [], "effort": "xhigh"}))
    url, headers, body = seen[0]
    assert url == f"{OPENROUTER}/chat/completions" and headers["authorization"] == "Bearer k1"
    assert body["reasoning"] == {"effort": "high"} and body["usage"] == {"include": True} and "provider" in body
    assert "effort" not in body and headers["x-title"] == "SEO Desk"


def test_other_apis_get_a_plain_request(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k2")
    seen = []
    asyncio.run(llm.Client("http://localhost:11434/v1/", api(seen)).chat({"model": "llama3", "messages": [], "effort": "low"}))
    url, headers, body = seen[0]
    assert url == "http://localhost:11434/v1/chat/completions" and headers["authorization"] == "Bearer k2"
    assert body == {"model": "llama3", "messages": []} and "x-title" not in headers


def test_errors_and_a_missing_key_are_llm_errors(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert not llm.available()
    with pytest.raises(llm.LLMError, match="LLM_API_KEY"):
        asyncio.run(llm.Client(OPENROUTER, api([])).chat({"model": "a/b", "messages": []}))
    monkeypatch.setenv("LLM_API_KEY", "k")
    with pytest.raises(llm.LLMError, match="401"):
        asyncio.run(llm.Client(OPENROUTER, api([], 401, {"error": {"message": "bad key"}})).chat({"model": "a/b", "messages": []}))


def test_cost_comes_from_usage_or_the_price_list():
    assert llm.call_cost("x/y", {"usage": {"cost": 0.0123}}) == 0.0123
    assert llm.call_cost("deepseek/deepseek-v4.1-flash", {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}) == 0.15
    assert llm.extract_json('Sure:\n```json\n{"a": 1}\n```') == '{"a": 1}'
