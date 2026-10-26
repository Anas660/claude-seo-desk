"""The chat model behind the daily brief and the AI-visibility probes: any OpenAI-style chat completions API.

- Default: OpenRouter (https://openrouter.ai), one key for hundreds of models, named "vendor/model". The default model
  is deepseek/deepseek-v4.1-flash, about $0.15 in / $0.60 out per million tokens; a brief costs about a cent.
- Anything else that speaks the same API works too: set llm.base_url in config.yaml (OpenAI, a local Ollama or
  LM Studio, a company gateway) and llm.model to a name it knows.
- Strict structured output (response_format json_schema) keeps the brief in its exact shape; the answer is validated
  again here, with one retry.
- On OpenRouter only: routing to hosts that support every parameter sent, that don't keep or train on the data, and
  that run the model at fp8 or better; the real cost of each call comes back in usage.cost.

The key is LLM_API_KEY in .env (OPENROUTER_API_KEY also works)."""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, Optional, Type

import httpx
from pydantic import BaseModel

from .settings import OPENROUTER

KEY_ENVS = ("LLM_API_KEY", "OPENROUTER_API_KEY")
PROJECT_URL = "https://github.com/Anas660/claude-seo-desk"
# USD per million tokens (input, output), used only when the API doesn't report the cost.
PRICES = {"deepseek/deepseek-v4.1-flash": (0.15, 0.60)}
FALLBACK_PRICE = (0.3, 1.2)
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
PROVIDER = {"require_parameters": True, "data_collection": "deny", "quantizations": ["fp8", "fp16", "bf16", "fp32", "unknown"]}


class LLMError(Exception):
    pass


def api_key() -> str:
    return next((os.environ[k] for k in KEY_ENVS if os.environ.get(k)), "")


def available() -> bool:
    return bool(api_key())


def is_openrouter(base_url: str) -> bool:
    return base_url.rstrip("/") == OPENROUTER


def strict_schema(schema: Type[BaseModel]) -> Dict[str, Any]:
    """Pydantic's JSON schema in the form strict structured output needs: every object closed and every field required."""
    s = copy.deepcopy(schema.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list((node.get("properties") or {}).keys())
            node.pop("default", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    return s


def extract_json(text: str) -> str:
    """The JSON object in a reply, without code fences or text around it."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def call_cost(model: str, data: Dict[str, Any]) -> float:
    usage = data.get("usage") or {}
    if isinstance(usage.get("cost"), (int, float)):
        return float(usage["cost"])
    price_in, price_out = PRICES.get(model, FALLBACK_PRICE)
    return ((usage.get("prompt_tokens") or 0) * price_in + (usage.get("completion_tokens") or 0) * price_out) / 1e6


class Client:
    def __init__(self, base_url: str = OPENROUTER, client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/")
        self.client = client

    def body(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """The request as this API wants it. `effort` becomes OpenRouter's reasoning setting; other APIs don't all
        accept a reasoning parameter, so it's dropped there."""
        body = dict(body)
        effort = body.pop("effort", None)
        if not is_openrouter(self.base_url):
            return body
        out = {"usage": {"include": True}, "provider": PROVIDER, **body}
        if effort:
            out["reasoning"] = {"effort": EFFORT.get(effort, "medium")}
        return out

    async def chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        key = api_key()
        if not key:
            raise LLMError("LLM_API_KEY (or OPENROUTER_API_KEY) isn't set in .env")
        body = self.body(body)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if is_openrouter(self.base_url):
            headers.update({"HTTP-Referer": PROJECT_URL, "X-Title": "SEO Desk"})
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0))
        try:
            for attempt in range(2):
                try:
                    r = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                except httpx.HTTPError as e:
                    raise LLMError(f"couldn't reach {self.base_url}: {e}") from e
                if r.status_code == 404 and attempt == 0 and "provider" in body and "endpoint" in r.text.lower():
                    # no OpenRouter host met every preference: keep the parameter support, drop the rest
                    body = {**body, "provider": {"require_parameters": True}}
                    continue
                break
        finally:
            if self.client is None:
                await client.aclose()
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or "choices" not in data:
            err = data.get("error") if isinstance(data, dict) else None
            raise LLMError(f"the model API answered {r.status_code}: {json.dumps(err or r.text[:300])[:300]}")
        return data
