"""AI visibility: ask AI models the questions your customers ask, and record whether each answer names the site,
links to it, which sites it cites instead, and whether it repeats a claim the site file rules out.

Set it up in a site file:

    ai_visibility:
      models: [perplexity/sonar, openai/gpt-5-mini:online]   # OpenRouter names; ":online" adds web search
      prompts:
        - best open source uptime monitor
        - {prompt: "What is Acme?", branded: true}
      every_days: 7       # answers move slowly, and every prompt costs a little
      max_calls: 20       # prompts x models in one run, at most

The prompt goes to the model exactly as a user would type it: no system prompt, nothing that hints at the site.
A model with web search answers from today's web (and cites it); one without answers from what it learned in
training, which shows whether it knows the site at all. Unbranded prompts give the mention rate that feeds the GEO
score; branded ones are there to catch false claims."""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import geo, llm
from .dataforseo import due
from .settings import Settings

log = logging.getLogger(__name__)
EXCERPT = 600
LINK = re.compile(r"https?://[^\s)\]>\"'<]+")


def domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def citations(data: Dict[str, Any], message: Dict[str, Any]) -> List[str]:
    """Every URL the answer cites: OpenRouter's url_citation annotations (web search), Perplexity's own citations
    list, and links written in the text. In order, once each."""
    urls: List[str] = []
    for a in message.get("annotations") or []:
        if isinstance(a, dict) and a.get("type") == "url_citation":
            url = (a.get("url_citation") or {}).get("url") or a.get("url")
            if url:
                urls.append(str(url))
    urls += [str(u) for u in data.get("citations") or [] if isinstance(u, str)]
    urls += [u.rstrip(".,;:") for u in LINK.findall(message.get("content") or "")]
    return list(dict.fromkeys(urls))


def plan(settings: Settings) -> List[str]:
    v = settings.visibility
    return sorted(f"{m}|{p.text}" for m in v.models for p in v.prompts) if v else []


def probes_due(snap: Optional[Dict[str, Any]], settings: Settings) -> bool:
    """Every `every_days`, or sooner when the prompts or models change."""
    if settings.visibility is None:
        return False
    return due(snap, settings.visibility.every_days) or (snap or {}).get("plan") != plan(settings)


def summarise(results: List[Dict[str, Any]], own: str) -> Dict[str, Any]:
    unbranded = [r for r in results if not r["branded"]]
    by_model: Dict[str, Dict[str, int]] = {}
    for r in results:
        m = by_model.setdefault(r["model"], {"answers": 0, "unbranded": 0, "mentions": 0, "cited": 0, "flagged": 0})
        m["answers"] += 1
        m["flagged"] += bool(r["flags"])
        if not r["branded"]:
            m["unbranded"] += 1
            m["mentions"] += r["mentioned"]
            m["cited"] += r["cited"]
    cited = Counter(d for r in unbranded for d in r["citations"] if d != own)
    return {
        "prompts": len(unbranded),                       # unbranded answers (prompts x models)
        "mentions": sum(r["mentioned"] for r in unbranded),
        "cited": sum(r["cited"] for r in unbranded),
        "flagged": sum(1 for r in results if r["flags"]),
        "by_model": by_model,
        "top_cited": [{"domain": d, "answers": n} for d, n in cited.most_common(10)],
    }


async def run(client: llm.Client, settings: Settings, budget_usd: float) -> Dict[str, Any]:
    """Every prompt to every model, until max_calls or the budget runs out. One that fails is an error line."""
    v = settings.visibility
    out: Dict[str, Any] = {"results": [], "errors": [], "cost": 0.0, "calls": 0, "plan": plan(settings)}
    if v is None:
        return out
    own, brand = settings.host.lower().removeprefix("www."), settings.brand
    for prompt in v.prompts:
        for model in v.models:
            if out["calls"] >= v.max_calls:
                out["errors"].append(f"call limit reached ({v.max_calls} answers)")
                break
            if out["cost"] >= budget_usd:
                out["errors"].append("budget used up")
                break
            out["calls"] += 1
            try:
                data = await client.chat({"model": model, "max_tokens": 1500,
                                          "messages": [{"role": "user", "content": prompt.text}]})
            except llm.LLMError as e:
                out["errors"].append(f"{model}: {e}")
                continue
            out["cost"] += llm.call_cost(model, data)
            message = (data.get("choices") or [{}])[0].get("message") or {}
            text = str(message.get("content") or "")
            cites = citations(data, message)
            hits = geo.scan_claims([{"url": model, "text": text}], settings.claims, {}, settings.about_us)
            out["results"].append({
                "model": model, "prompt": prompt.text, "branded": prompt.branded,
                "mentioned": bool(brand.search(text)),
                "cited": any(domain(u) == own or domain(u).endswith("." + own) for u in cites),
                "citations": list(dict.fromkeys(domain(u) for u in cites if domain(u)))[:12],
                "flags": sorted({h["id"] for h in hits}),
                "excerpt": re.sub(r"\s+", " ", text).strip()[:EXCERPT],
            })
        else:
            continue
        break
    out["summary"] = summarise(out["results"], own)
    log.info("AI visibility: %d answers, %d of %d unbranded name the site", len(out["results"]),
             out["summary"]["mentions"], out["summary"]["prompts"])
    return out
