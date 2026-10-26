"""Settings: config.yaml holds what every site shares (the model, the money limits, the AI crawler list, DataForSEO
cadence) and the list of sites; sites/<slug>.yaml says what to check on one site; .env holds the keys.

All of them live in one folder, the SEO Desk home: $SEODESK_HOME, else the current folder. `seodesk init` writes a
starting config there. The run database and logs go in its data/ and logs/ folders. `seodesk audit` needs none of it:
it builds its settings from the URL (adhoc)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

SERPER_USD = 0.001  # per search, for the spend table (Serper's pay-as-you-go price is about $1 per 1,000)
TIERS = ("easy", "medium", "head")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
OPENROUTER = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
# AI crawlers that should be allowed to read the site (GEO), when config.yaml doesn't list its own.
AI_CRAWLERS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "PerplexityBot",
               "Google-Extended", "Applebot-Extended", "CCBot"]
DEFAULT_CRAWL = {"max_pages": 150, "concurrency": 4, "timeout_s": 20}


def home() -> Path:
    """The folder with config.yaml, sites/, .env, data/ and logs/."""
    return Path(os.environ.get("SEODESK_HOME") or ".").resolve()


def config_file() -> Path:
    return home() / "config.yaml"


def data_dir() -> Path:
    return home() / "data"


def log_dir() -> Path:
    return home() / "logs"


@dataclass
class Keyword:
    keyword: str
    country: str
    tier: str
    volume: str = ""
    target: str = ""


@dataclass
class Claim:
    id: str
    pattern: re.Pattern
    why: str
    # Only a sentence that is about us counts (names the brand, or says we/our), so general advice
    # ("insist on SOC 2") isn't flagged; `unless` skips a sentence that denies the claim ("no SOC 2 report").
    about_us: bool = False
    unless: Optional[re.Pattern] = None


@dataclass
class Prompt:
    text: str
    # A prompt that names the site ("What is Acme?") always mentions it, so it doesn't count toward the mention rate;
    # its answer is still checked for false claims.
    branded: bool = False


@dataclass
class Visibility:
    """The site file's `ai_visibility`: questions to ask AI models, and which models (probes.py)."""
    models: List[str]
    prompts: List[Prompt]
    every_days: float = 7.0
    max_calls: int = 20


@dataclass
class Settings:
    site: str                       # the canonical origin, e.g. https://www.example.com
    slug: str = "site"
    name: str = ""
    about: str = ""                 # what the site is, for the brief's analyst
    rules: str = ""                 # what the brief must never suggest
    country: str = "us"             # main market: rank checks default to it, and DataForSEO's ranked keywords use it
    language: str = "en"
    brand_terms: List[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    llm_base_url: str = OPENROUTER
    effort: str = "medium"
    max_tool_calls: int = 10
    max_run_usd: float = 1.0
    max_monthly_usd: float = 4.0    # this site's monthly cap
    max_total_monthly_usd: float = 30.0  # every site together
    max_serper_per_run: int = 40
    crawl: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CRAWL))
    thin_ok: List[str] = field(default_factory=list)
    keywords: List[Keyword] = field(default_factory=list)
    claims: List[Claim] = field(default_factory=list)
    ai_crawlers: List[str] = field(default_factory=lambda: list(AI_CRAWLERS))
    visibility: Optional[Visibility] = None
    # The app-store listings (stores.py): {wordpress: {slug, keywords}, shopify: {handle, must_not_contain}}
    stores: Dict[str, Any] = field(default_factory=dict)
    dataforseo: Dict[str, Any] = field(default_factory=dict)
    serper_key: str = ""
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    db_path: Path = field(default_factory=lambda: data_dir() / "seo.db")

    @property
    def host(self) -> str:
        return re.sub(r"^https?://", "", self.site).split("/")[0]

    @property
    def label(self) -> str:
        return self.name or self.host

    @property
    def about_us(self) -> re.Pattern:
        """A sentence about this site: it names the brand, or says we / our / us."""
        names = {t for t in [self.name.lower(), *self.brand_terms] if t and "." not in t}
        alts = "|".join(re.escape(t) for t in sorted(names, key=len, reverse=True))
        return re.compile((rf"\b(?:{alts})\b|" if alts else "") + r"\bwe\b|\bour\b|\bus\b", re.I)

    @property
    def brand(self) -> re.Pattern:
        """An answer that names the site: its brand terms, its name or its domain."""
        bare = self.host.lower().removeprefix("www.")
        names = {t for t in [self.name.lower(), *self.brand_terms, bare] if t}
        alts = "|".join(re.escape(t) for t in sorted(names, key=len, reverse=True))
        return re.compile(rf"(?<![\w.-])(?:{alts})(?![\w-])", re.I)


def origin(url: str) -> str:
    """'example.com/pricing' -> 'https://example.com'."""
    url = url.strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"not a web address: {url}")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def adhoc(url: str, **over: Any) -> Settings:
    """Settings for one site with no config files (`seodesk audit`): the defaults, plus any keys in the environment."""
    site = origin(url)
    host = urlparse(site).netloc
    base = dict(site=site, slug=re.sub(r"[^a-z0-9-]+", "-", host.removeprefix("www."))[:32].strip("-") or "site",
                serper_key=os.environ.get("SERPER_API_KEY", ""))
    base.update(over)
    return Settings(**base)


def _read(path: Path) -> Dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        hint = " (run `seodesk init` to write a starting config)" if path.name == "config.yaml" else ""
        raise SystemExit(f"{path} is missing{hint}") from None


def site_slugs(config: Optional[Path] = None) -> List[str]:
    """The sites in config.yaml's `sites` list, in order (the first is the dashboard's default)."""
    slugs = [str(s) for s in _read(config or config_file()).get("sites") or []]
    bad = [s for s in slugs if not SLUG.match(s)]
    if bad:
        raise SystemExit(f"config.yaml: site names must be lowercase letters, digits and dashes: {', '.join(bad)}")
    if not slugs:
        raise SystemExit("config.yaml: `sites` is empty")
    return slugs


def _visibility(raw: Any, where: str) -> Optional[Visibility]:
    if not raw:
        return None
    models = [str(m) for m in raw.get("models") or []]
    prompts = []
    for p in raw.get("prompts") or []:
        prompts.append(Prompt(str(p["prompt"]), bool(p.get("branded", False))) if isinstance(p, dict) else Prompt(str(p)))
    if not models or not prompts:
        raise SystemExit(f"{where}: ai_visibility needs `models` and `prompts`")
    return Visibility(models=models, prompts=prompts, every_days=float(raw.get("every_days", 7)),
                      max_calls=int(raw.get("max_calls", 20)))


def load(slug: Optional[str] = None, config: Optional[Path] = None, sites_dir: Optional[Path] = None) -> Settings:
    config = config or config_file()
    sites_dir = sites_dir or config.parent / "sites"
    load_dotenv(config.parent / ".env")
    raw = _read(config)
    slugs = site_slugs(config)
    slug = slug or slugs[0]
    if slug not in slugs:
        raise SystemExit(f"'{slug}' isn't in config.yaml's sites ({', '.join(slugs)})")
    s = _read(sites_dir / f"{slug}.yaml")
    where = f"sites/{slug}.yaml"
    llm, limits = raw.get("llm", {}) or {}, raw.get("limits", {}) or {}
    site_limits = s.get("limits") or {}
    crawl = {**DEFAULT_CRAWL, **(raw.get("crawl") or {}), **(s.get("crawl") or {})}
    country = str(s.get("country") or "us").lower()
    keywords = []
    for k in s.get("keywords") or []:
        tier = str(k.get("tier", "medium"))
        if tier not in TIERS:
            raise SystemExit(f"{where}: keyword '{k.get('keyword')}' has tier '{tier}' (use {', '.join(TIERS)})")
        keywords.append(Keyword(keyword=str(k["keyword"]), country=str(k.get("country", country)).lower(), tier=tier,
                                volume=str(k.get("volume", "") or ""), target=str(k.get("target", "") or "")))
    claims = []
    for c in s.get("claims") or []:
        try:
            claims.append(Claim(id=str(c["id"]), pattern=re.compile(str(c["pattern"]), re.I), why=str(c.get("why", "")),
                                about_us=bool(c.get("about_us", False)),
                                unless=re.compile(str(c["unless"]), re.I) if c.get("unless") else None))
        except re.error as e:
            raise SystemExit(f"{where}: claim '{c.get('id')}' has a bad pattern: {e}") from e
    base_url = str(llm.get("base_url") or OPENROUTER).rstrip("/")
    model = str(llm.get("model", DEFAULT_MODEL))
    if base_url == OPENROUTER and "/" not in model:
        raise SystemExit("config.yaml: llm.model must be an OpenRouter name like deepseek/deepseek-v4.1-flash")
    site = str(s.get("site") or "").rstrip("/")
    if not re.match(r"^https?://[^/\s]+$", site):
        raise SystemExit(f"{where}: `site` must be an origin like https://www.example.com")
    return Settings(
        slug=slug,
        name=str(s.get("name") or ""),
        site=site,
        about=" ".join(str(s.get("about") or "").split()),
        rules=" ".join(str(s.get("rules") or "").split()),
        country=country,
        language=str(s.get("language") or "en").lower(),
        brand_terms=[str(t).lower() for t in s.get("brand_terms") or []],
        model=model,
        llm_base_url=base_url,
        effort=str(llm.get("effort", "medium")),
        max_tool_calls=int(llm.get("max_tool_calls", 10)),
        max_run_usd=float(site_limits.get("max_run_usd", limits.get("max_run_usd", 1.0))),
        max_monthly_usd=float(site_limits.get("max_monthly_usd", limits.get("site_monthly_usd", 4.0))),
        max_total_monthly_usd=float(limits.get("max_monthly_usd", 30.0)),
        max_serper_per_run=int(site_limits.get("max_serper_per_run", limits.get("max_serper_per_run", 40))),
        crawl=crawl,
        thin_ok=[str(p).rstrip("/") or "/" for p in crawl.get("thin_ok") or []],
        keywords=keywords,
        claims=claims,
        ai_crawlers=[str(a) for a in raw.get("ai_crawlers") or AI_CRAWLERS],
        visibility=_visibility(s.get("ai_visibility"), where),
        serper_key=os.environ.get("SERPER_API_KEY", ""),
        stores=dict(s.get("stores") or {}),
        dataforseo=dict(raw.get("dataforseo") or {}),
        dataforseo_login=os.environ.get("DATAFORSEO_LOGIN", ""),
        dataforseo_password=os.environ.get("DATAFORSEO_PASSWORD", ""),
        db_path=config.parent / "data" / "seo.db",
    )


def load_all(config: Optional[Path] = None, sites_dir: Optional[Path] = None) -> List[Settings]:
    return [load(s, config, sites_dir) for s in site_slugs(config)]
