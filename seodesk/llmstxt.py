"""Draft an llms.txt (https://llmstxt.org) from a crawl: the site's name and one-line summary, then its pages grouped
by section, each a Markdown link with its meta description.

It's a starting point to edit, not a finished file: say what the site is in your own words, cut pages that wouldn't
help an AI assistant answer a question, and put the most useful ones first."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Set
from urllib.parse import urlparse

SEPARATORS = re.compile(r"\s+[|–—·•:-]\s+")


def _path(url: str) -> str:
    return urlparse(url).path.rstrip("/") or "/"


def site_name(home: Dict[str, Any], host: str) -> str:
    """og:site_name, else the brand end of the homepage title ("Invoicing for freelancers | Acme" -> "Acme"), else
    the domain."""
    if home.get("site_name"):
        return str(home["site_name"])
    parts = [x.strip() for x in SEPARATORS.split(str(home.get("title") or "")) if x.strip()]
    if len(parts) > 1:
        return min(parts[0], parts[-1], key=len)
    return parts[0] if parts else host.removeprefix("www.")


def page_title(page: Dict[str, Any], brands: Set[str]) -> str:
    """The title without the site's name tacked on ("Pricing | Acme" -> "Pricing"). `brands` are the lowercase
    names the site goes by (og:site_name and the brand end of the homepage title can differ)."""
    parts = [x.strip() for x in SEPARATORS.split(str(page.get("title") or page.get("h1") or "")) if x.strip()]
    kept = [x for x in parts if x.lower() not in brands] or parts
    return " - ".join(kept) or _path(page["url"])


def section(url: str) -> str:
    """'/blog/x' -> 'Blog'; a top-level page -> 'Pages'."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    if len(segments) < 2:
        return "Pages"
    return segments[0].replace("-", " ").replace("_", " ").capitalize()


def draft(site: str, pages: List[Dict[str, Any]], name: str = "") -> str:
    host = urlparse(site).netloc
    live = [p for p in pages if p.get("status") == 200
            and not any(i.get("code") == "noindex" for i in p.get("issues") or [])]
    home = next((p for p in live if _path(p["url"]) == "/"), {"url": f"{site}/"})
    name = name or site_name(home, host)
    brands = {name.lower(), site_name({"title": home.get("title")}, host).lower()}
    out = [f"# {name}", ""]
    if home.get("description"):
        out += [f"> {home['description']}", ""]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for p in live:
        if _path(p["url"]) != "/":
            groups.setdefault(section(p["url"]), []).append(p)
    order = sorted(groups, key=lambda g: (g != "Pages", -len(groups[g]), g))
    for g in order:
        out += [f"## {g}", ""]
        for p in groups[g]:
            desc = f": {p['description']}" if p.get("description") else ""
            out.append(f"- [{page_title(p, brands)}]({p['url']}){desc}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
