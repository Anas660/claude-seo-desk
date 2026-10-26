"""GEO checks that don't need an AI engine: can the AI crawlers read the site, are llms.txt and llms-full.txt there,
and does any live page (blog posts included, which a site repo's own tests can't see) still make a claim the site
file rules out. What the AI engines actually say about the site comes from probes.py."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

import httpx

from .settings import Claim

LLMS_FILES = ("/llms.txt", "/llms-full.txt")


def robots_groups(text: str) -> List[Dict[str, Any]]:
    """robots.txt as groups: {agents: [lowercase names], rules: [(allow, pattern)]}. A user-agent line after rules
    starts a new group."""
    groups: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (x.strip() for x in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if not cur or cur["rules"]:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
            cur["agents"].append(value.lower())
        elif field in ("allow", "disallow") and cur:
            if value:  # an empty Disallow allows everything
                cur["rules"].append((field == "allow", value))
    return groups


def _pattern(rule: str) -> re.Pattern:
    """Google's robots.txt wildcards: * is any run of characters, a final $ anchors the end."""
    anchored = rule.endswith("$")
    body = re.escape(rule[:-1] if anchored else rule).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def can_fetch(groups: List[Dict[str, Any]], agent: str, url: str) -> bool:
    """What Google, OpenAI and Anthropic's crawlers do (RFC 9309): the groups naming the crawler (merged), else the
    * groups; the longest matching rule wins, and Allow wins a tie. Python's urllib.robotparser ignores wildcards,
    so `Disallow: /*` would look like no block at all."""
    name = agent.lower()
    mine = [g for g in groups if name in g["agents"]] or [g for g in groups if "*" in g["agents"]]
    parsed = urlparse(url)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    best: Tuple[int, bool] = (-1, True)
    for g in mine:
        for allow, rule in g["rules"]:
            if _pattern(rule).match(path):
                best = max(best, (len(rule), allow))
    return best[1]


def crawler_access(robots_txt: str, site: str, crawlers: List[str]) -> List[Dict[str, Any]]:
    """For each AI crawler: may it fetch the homepage and a blog post?"""
    groups = robots_groups(robots_txt)
    return [{"crawler": c, "home": can_fetch(groups, c, f"{site}/"), "blog": can_fetch(groups, c, f"{site}/blog/x")}
            for c in crawlers]


# A sentence about the site: it says we / our / us. Settings.about_us adds the site's own names.
ABOUT_US = re.compile(r"\bwe\b|\bour\b|\bus\b", re.I)


def sentence_at(text: str, start: int, end: int) -> str:
    """The sentence around a match: from the previous . ! ? or line break to the next one."""
    left = max(text.rfind(ch, 0, start) for ch in ".!?\n")
    rights = [i for i in (text.find(ch, end) for ch in ".!?\n") if i != -1]
    return text[left + 1:(min(rights) if rights else len(text))]


def scan_claims(pages: List[Dict[str, Any]], claims: List[Claim], extra: Dict[str, str],
                about_us: re.Pattern = ABOUT_US) -> List[Dict[str, Any]]:
    """Every banned claim found, with the page and a few words around it. `extra` is {url: text} for files that
    aren't in the crawl (llms.txt, llms-full.txt)."""
    hits: List[Dict[str, Any]] = []
    sources = [(p["url"], p.get("text", "")) for p in pages] + list(extra.items())
    for url, text in sources:
        for c in claims:
            found = 0
            for m in c.pattern.finditer(text or ""):
                if found == 3:
                    break
                sentence = sentence_at(text, m.start(), m.end())
                if c.about_us and not about_us.search(sentence):
                    continue
                if c.unless is not None and c.unless.search(sentence):
                    continue
                found += 1
                start, end = max(0, m.start() - 70), min(len(text), m.end() + 70)
                hits.append({"id": c.id, "why": c.why, "url": url,
                             "context": re.sub(r"\s+", " ", text[start:end]).strip()})
    return hits


async def check(client: httpx.AsyncClient, site: str, crawlers: List[str], pages: List[Dict[str, Any]],
                claims: List[Claim], about_us: re.Pattern = ABOUT_US) -> Dict[str, Any]:
    out: Dict[str, Any] = {"errors": []}
    try:
        r = await client.get(f"{site}/robots.txt")
        out["robots_ok"] = r.status_code == 200
        out["crawlers"] = crawler_access(r.text if r.status_code == 200 else "", site, crawlers)
    except httpx.HTTPError as e:
        out["robots_ok"], out["crawlers"] = False, []
        out["errors"].append(f"robots.txt: {e}")

    files: Dict[str, str] = {}
    out["llms"] = []
    for path in LLMS_FILES:
        try:
            r = await client.get(f"{site}{path}")
        except httpx.HTTPError as e:
            out["llms"].append({"path": path, "status": 0, "bytes": 0, "error": str(e)[:120]})
            continue
        text = r.text if r.status_code == 200 else ""
        files[f"{site}{path}"] = text
        out["llms"].append({"path": path, "status": r.status_code, "bytes": len(text.encode()),
                            "last_modified": r.headers.get("last-modified", "")})
    out["claims"] = scan_claims(pages, claims, files, about_us)
    out["claims_checked"] = len(claims)
    blocked = [c["crawler"] for c in out.get("crawlers", []) if not c["home"]]
    missing = [f["path"] for f in out["llms"] if f["status"] != 200]
    out["summary"] = {
        "crawlers_blocked": blocked,
        "llms_missing": missing,
        "claim_hits": len(out["claims"]),
        "claim_pages": len({h["url"] for h in out["claims"]}),
    }
    return out
