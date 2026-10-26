"""The daily brief: a chat model reads the day's evidence, checks what it needs with two tools run by this code, and
writes a short brief plus prioritised actions in strict JSON.

Tools:
- read_page: one page of the site, as the facts the crawl found plus its first 6,000 characters of text
- google: a live Google check through Serper (counts against the run's search cap)

The model is told what the site is and what it must never claim (the site file's `about` and `rules`), and to cite
evidence for every action; the action queue dedupes across days (db.action_key), so a suggestion repeated tomorrow
updates its row."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from . import llm
from .errors import FatalError, LimitReached, SearchError
from .serp import Serper, read_result
from .settings import Settings

log = logging.getLogger(__name__)
PAGE_CHARS = 6000


class Action(BaseModel):
    area: Literal["SEO", "AEO", "GEO", "Technical", "Content"] = Field(description="Which kind of work this is")
    priority: int = Field(description="1 = do this week, 2 = soon, 3 = when there's time")
    title: str = Field(description="Imperative, under 90 characters, e.g. 'Put \"invoice template\" in the /templates/invoice title'")
    why: str = Field(description="One or two sentences: the evidence and the expected effect")
    how: str = Field(description="Concrete steps: what to change, where, with the exact new text when it's copy")
    url: str = Field(description="The page it concerns (full URL or path), or empty")
    keyword: str = Field(description="The keyword it targets, or empty")
    effort: Literal["S", "M", "L"] = Field(description="S under an hour, M a day, L more")
    evidence: str = Field(description="The numbers this rests on, e.g. 'Serper: #12 for \"invoice template\"; crawl: title 74 chars'")


class Idea(BaseModel):
    keyword: str = Field(description="The search or AI prompt to win")
    page_type: str = Field(description="guide | comparison | FAQ block | landing page | blog post")
    angle: str = Field(description="What the page says that the current first page doesn't")
    questions: List[str] = Field(description="Questions the page must answer, from People also ask or the AI answers")


class Brief(BaseModel):
    headline: str = Field(description="One line on today, e.g. 'Two pages lost their canonical; /pricing now #8'")
    summary: str = Field(description="Three to five plain sentences: where SEO, AEO and GEO stand and what changed")
    wins: List[str] = Field(description="Things that improved, each with its number")
    risks: List[str] = Field(description="Things that got worse or are broken, each with its number")
    actions: List[Action] = Field(description="Three to eight actions, most valuable first")
    content_ideas: List[Idea] = Field(description="Up to three pages worth writing next")


SYSTEM = """You are the SEO, AEO and GEO analyst for {host}.{about}

- SEO = ranking in Google. AEO = being the quoted answer (featured snippets, AI Overviews, People also ask).
  GEO = being named by ChatGPT, Perplexity, Gemini and Claude.
- Base every point on the evidence given or on what your tools return. Quote the numbers. If data is missing,
  say so instead of guessing.
- Actions must be specific and doable by one person: which page, which words, which keyword. No generic advice
  ("improve content quality", "build backlinks") without a named page, site or keyword.
{rules}- Prefer actions on pages already at positions 4-20 (cheapest clicks), broken things (errors, false claims,
  pages blocked from crawlers), and the easy keywords not yet ranking.
- The SEO Desk scores say whether the site is improving week to week and month to month: say which way they
  moved and which part moved them, and aim actions at the weakest part.
- Open actions from earlier days are listed; don't repeat one unless the evidence changed, and say if one looks done.
  When you do repeat one, copy its title and page exactly, so it updates instead of becoming a second row."""


def system_prompt(settings: Settings) -> str:
    rules = f"- Never suggest or claim these; they aren't true or aren't allowed: {settings.rules}\n" if settings.rules else ""
    return SYSTEM.format(host=settings.host, about=f" {settings.about}" if settings.about else "", rules=rules)


TOOLS = [
    {"type": "function", "function": {
        "name": "read_page",
        "description": "Read one page of the site: title, description, H1, schema, AEO signals, issues, and its text.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["url"], "properties": {
            "url": {"type": "string", "description": "Full URL or path on the site"}}}}},
    {"type": "function", "function": {
        "name": "google",
        "description": "Live Google results for a query: our position, the top 10 domains, People also ask.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string"},
            "country": {"type": "string", "description": "2-letter country code: us, gb, in, de"}}}}},
]


class Tools:
    """Runs the model's tool calls against the day's data (no extra cost) and Serper (counted)."""

    def __init__(self, site: str, pages: List[Dict[str, Any]], serper: Optional[Serper], language: str = "en"):
        self.site, self.host = site, urlparse(site).netloc
        self.pages = {urlparse(p["url"]).path.rstrip("/") or "/": p for p in pages}
        self.serper = serper
        self.language = language

    async def run(self, name: str, args: Dict[str, Any]) -> str:
        if name == "read_page":
            return self.read_page(str(args.get("url") or ""))
        if name == "google":
            return await self.google(str(args.get("query") or ""), str(args.get("country") or "us")[:2].lower())
        return f"There's no tool called {name}."

    def read_page(self, url: str) -> str:
        parsed = urlparse(url if "://" in url else f"{self.site}{url if url.startswith('/') else '/' + url}")
        if parsed.netloc and parsed.netloc != self.host:
            return f"Only pages on {self.host} can be read."
        p = self.pages.get(parsed.path.rstrip("/") or "/")
        if not p:
            return "That page wasn't in today's crawl (it may not be in the sitemap)."
        facts = {k: p.get(k) for k in ("url", "status", "title", "description", "canonical", "h1", "words",
                                       "schema_types", "aeo", "issues")}
        return json.dumps(facts, ensure_ascii=False) + "\n\nTEXT:\n" + (p.get("text") or "")[:PAGE_CHARS]

    async def google(self, query: str, country: str) -> str:
        if not query.strip():
            return "Give a query."
        if self.serper is None:
            return "Live Google checks aren't available today."
        try:
            data = await self.serper.search(query, country or "us", language=self.language)
        except LimitReached:
            return "Limit reached: no searches left. Use what you have."
        except (FatalError, SearchError) as e:
            return f"Search failed: {e}"
        return json.dumps(read_result(data, self.host), ensure_ascii=False)


async def write_brief(client: llm.Client, model: str, effort: str, evidence: str, tools: Tools,
                      max_tool_calls: int, budget_usd: float, system: str = "") -> Tuple[Optional[Brief], float]:
    """(brief or None, cost). Stops calling tools when the budget or the tool cap is reached."""
    spent = 0.0
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system or SYSTEM.format(host=tools.host, about="", rules="")},
        {"role": "user", "content": evidence + f"\n\nCheck what you need with the tools (at most {max_tool_calls} "
                                               "calls), then say you're ready to write the brief."}]
    calls_made = 0
    for _ in range(max_tool_calls + 2):
        if spent >= budget_usd * 0.7:
            break
        data = await client.chat({"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                                  "max_tokens": 6000, "effort": effort})
        spent += llm.call_cost(model, data)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            if msg.get("content"):
                messages.append({"role": "assistant", "content": msg["content"]})
            break
        turn: Dict[str, Any] = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        if msg.get("reasoning_details"):
            turn["reasoning_details"] = msg["reasoning_details"]
        messages.append(turn)
        for call in calls:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls_made += 1
            result = (await tools.run(fn.get("name", ""), args) if calls_made <= max_tool_calls
                      else "Tool limit reached. Write the brief with what you have.")
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
    messages.append({"role": "user", "content": "Write today's brief now: one json object matching the schema."})
    for attempt in range(2):
        data = await client.chat({
            "model": model, "messages": messages, "tools": TOOLS, "tool_choice": "none", "max_tokens": 12000,
            "effort": effort,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "Brief", "strict": True, "schema": llm.strict_schema(Brief)}}})
        spent += llm.call_cost(model, data)
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            brief = Brief.model_validate_json(llm.extract_json(content))
            problem = incomplete(brief)
        except ValidationError as e:
            brief, problem = None, f"it wasn't valid for the schema ({str(e)[:600]})"
        if brief is not None and not problem:
            return brief, spent
        log.warning("Brief rejected (attempt %d): %s", attempt + 1, problem[:200])
        messages += [{"role": "assistant", "content": content or "(empty)"},
                     {"role": "user", "content": f"That brief can't be used: {problem}. "
                                                 "Reply again with the complete json object only."}]
    return None, spent


def incomplete(brief: Brief) -> str:
    """Strict JSON can't say "non-empty", so the fields the dashboard leads with are checked here."""
    missing = [name for name in ("headline", "summary") if not getattr(brief, name).strip()]
    if missing:
        return f"{' and '.join(missing)} is empty"
    if not brief.actions:
        return "there are no actions"
    return ""


def normalise_priorities(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Priority is 1-3. A model that numbers its list 1..N instead gets its order kept as 3 / 4 / rest."""
    if all(1 <= int(a.get("priority", 2)) <= 3 for a in actions):
        return actions
    ranked = sorted(range(len(actions)), key=lambda i: int(actions[i].get("priority", 99)))
    out = [dict(a) for a in actions]
    for rank, i in enumerate(ranked):
        out[i]["priority"] = 1 if rank < 3 else 2 if rank < 7 else 3
    return out
