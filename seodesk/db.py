"""SQLite store: each run's snapshots (crawl, GEO checks, app stores, DataForSEO, AI visibility), the pages it crawled,
the rank checks, the daily brief, the action queue and the day's SEO / AEO / GEO scores, plus runs and money spent.
One file, data/seo.db, for every site: each row carries its site's slug, and a DB object reads and writes one site
(DB.scoped gives another site over the same connection)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',     -- running | ok | partial | failed
    note TEXT NOT NULL DEFAULT '',
    llm_usd REAL NOT NULL DEFAULT 0,
    serper_calls INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    new_actions INTEGER NOT NULL DEFAULT 0,
    site TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    usd REAL NOT NULL,
    site TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- One JSON document per run and kind: crawl | geo | stores | volumes | ranked | probes
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    site TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS snapshots_kind ON snapshots(kind, id);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    status INTEGER NOT NULL,
    ms INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    canonical TEXT NOT NULL DEFAULT '',
    h1 TEXT NOT NULL DEFAULT '',
    words INTEGER NOT NULL DEFAULT 0,
    schema_types TEXT NOT NULL DEFAULT '[]',
    aeo TEXT NOT NULL DEFAULT '{}',
    issues TEXT NOT NULL DEFAULT '[]',
    site TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS pages_run ON pages(run_id);
CREATE TABLE IF NOT EXISTS serp (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    keyword TEXT NOT NULL,
    country TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT '',
    position INTEGER,                            -- null = not in the results checked
    url TEXT NOT NULL DEFAULT '',
    top TEXT NOT NULL DEFAULT '[]',              -- first results' domains
    questions TEXT NOT NULL DEFAULT '[]',        -- Google's "People also ask"
    site TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS serp_kw ON serp(keyword, country, date);
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    data TEXT NOT NULL,
    site TEXT NOT NULL DEFAULT ''
);
-- One row per run: the scores progress.py works out from that run's crawl, GEO checks and rank checks.
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    site TEXT NOT NULL,
    run_id INTEGER NOT NULL UNIQUE,
    date TEXT NOT NULL,                          -- the run's day (UTC)
    seo INTEGER,
    aeo INTEGER,
    geo INTEGER,
    overall INTEGER,
    parts TEXT NOT NULL DEFAULT '{}'             -- the components and the raw numbers behind them
);
CREATE INDEX IF NOT EXISTS scores_site_date ON scores(site, date);
CREATE INDEX IF NOT EXISTS runs_site ON runs(site, id);
CREATE INDEX IF NOT EXISTS spend_site ON spend(site, at);
CREATE INDEX IF NOT EXISTS snapshots_site_kind ON snapshots(site, kind, id);
CREATE INDEX IF NOT EXISTS pages_site_run ON pages(site, run_id);
CREATE INDEX IF NOT EXISTS serp_site_kw ON serp(site, keyword, country, date);
CREATE INDEX IF NOT EXISTS briefs_site ON briefs(site, id);
"""

ACTIONS = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,                           -- same title + page = same action across days (per site)
    area TEXT NOT NULL,                          -- SEO | AEO | GEO | Technical | Content
    priority INTEGER NOT NULL DEFAULT 2,         -- 1 = do first
    title TEXT NOT NULL,
    why TEXT NOT NULL DEFAULT '',
    how TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    keyword TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',         -- open | done | dismissed  (set from the dashboard)
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    run_id INTEGER,
    updated_at TEXT,
    site TEXT NOT NULL DEFAULT '',
    UNIQUE(site, key)
);
"""

ACTION_STATUSES = {"open", "done", "dismissed"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def norm_url(url: str) -> str:
    """'https://www.example.com/about/' and '/about' are the same page; with several URLs, the first counts.
    Actions belong to one site, so any origin is dropped."""
    first = (url or "").strip().split()[0] if (url or "").strip() else ""
    first = re.sub(r"^https?://[^/\s]+", "", first.rstrip(",;"), flags=re.I)
    return first.rstrip("/").lower() or ("/" if first else "")


def action_key(title: str, url: str) -> str:
    """Same action on the same page = same key, so a re-suggested action updates its row instead of piling up."""
    norm = re.sub(r"[^a-z0-9]+", " ", f"{title.lower()} {norm_url(url)}").strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


_STOP = set("""a an the and or of to on in into for from with by at as is are be it its this that these those our we us
your one two three all any more most new add fix get make use do via page pages live ai seo aeo geo""".split())


def _words(title: str) -> set:
    """Key words, cut to five letters so 'index' and 'indexation', 'claim' and 'claims' meet."""
    return {w[:5] for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and w not in _STOP}


def same_task(a: str, b: str) -> bool:
    """Two titles for one task: at least half of the shorter title's key words are in the other."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.5


class DB:
    def __init__(self, path: Path, site: str = ""):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.site = site
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA + ACTIONS)
        self.conn.commit()

    def scoped(self, site: str) -> "DB":
        """The same database, reading and writing `site` (shares this connection)."""
        other = object.__new__(DB)
        other.conn, other.site = self.conn, site
        return other

    def close(self) -> None:
        self.conn.close()

    # ── runs and spend ─────────────────────────────────────────────────────
    def start_run(self, kind: str = "daily") -> int:
        cur = self.conn.execute("INSERT INTO runs(kind, started_at, site) VALUES (?, ?, ?)", (kind, now(), self.site))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, note: str = "", **counts: Any) -> None:
        cols = {k: v for k, v in counts.items() if k in ("llm_usd", "serper_calls", "pages", "new_actions")}
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.conn.execute(f"UPDATE runs SET finished_at = ?, status = ?, note = ?{', ' + sets if sets else ''} WHERE id = ?",
                          (now(), status, note[:500], *cols.values(), run_id))
        self.conn.commit()
        # Fold the WAL into the main file, so a copy of seo.db (a backup, another tool reading it) is up to date.
        self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM runs WHERE site = ? ORDER BY id DESC LIMIT ?",
                                                   (self.site, limit))]

    def run(self, run_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ? AND site = ?", (run_id, self.site)).fetchone()
        return dict(row) if row else None

    def add_spend(self, kind: str, usd: float) -> None:
        if usd > 0:
            self.conn.execute("INSERT INTO spend(at, agent, kind, usd, site) VALUES (?, 'seo', ?, ?, ?)",
                              (now(), kind, usd, self.site))
            self.conn.commit()

    def month_spend(self, all_sites: bool = False) -> float:
        start = datetime.now(timezone.utc).strftime("%Y-%m-01")
        sql, args = "SELECT COALESCE(SUM(usd), 0) FROM spend WHERE at >= ?", [start]
        if not all_sites:
            sql, args = sql + " AND site = ?", [start, self.site]
        return float(self.conn.execute(sql, args).fetchone()[0])

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (key, value))
        self.conn.commit()

    # ── snapshots ─────────────────────────────────────────────────────────
    def save_snapshot(self, run_id: int, kind: str, data: Dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO snapshots(run_id, kind, data, created_at, site) VALUES (?, ?, ?, ?, ?)",
                          (run_id, kind, json.dumps(data, ensure_ascii=False, default=str), now(), self.site))
        self.conn.commit()

    def latest_snapshot(self, kind: str, run_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """The newest snapshot of a kind; with `run_id`, the newest taken in that run or before it."""
        sql, args = "SELECT data, created_at FROM snapshots WHERE site = ? AND kind = ?", [self.site, kind]
        if run_id is not None:
            sql, args = sql + " AND run_id <= ?", [*args, run_id]
        row = self.conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if not row:
            return None
        return {**json.loads(row["data"]), "_at": row["created_at"]}

    def run_snapshot(self, run_id: int, kind: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT data, created_at FROM snapshots WHERE site = ? AND run_id = ? AND kind = ? "
                                "ORDER BY id DESC LIMIT 1", (self.site, run_id, kind)).fetchone()
        return {**json.loads(row["data"]), "_at": row["created_at"]} if row else None

    def snapshot_history(self, kind: str, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT data, created_at FROM snapshots WHERE site = ? AND kind = ? ORDER BY id DESC LIMIT ?",
                                 (self.site, kind, limit)).fetchall()
        return [{**json.loads(r["data"]), "_at": r["created_at"]} for r in reversed(rows)]

    # ── crawl ─────────────────────────────────────────────────────────────
    def save_pages(self, run_id: int, pages: Iterable[Dict[str, Any]]) -> int:
        n = 0
        for p in pages:
            self.conn.execute(
                "INSERT INTO pages(run_id, url, status, ms, title, description, canonical, h1, words, schema_types, aeo, issues, site)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, p["url"], int(p.get("status", 0)), int(p.get("ms", 0)), p.get("title", ""), p.get("description", ""),
                 p.get("canonical", ""), p.get("h1", ""), int(p.get("words", 0)), json.dumps(p.get("schema_types", [])),
                 json.dumps(p.get("aeo", {})), json.dumps(p.get("issues", [])), self.site))
            n += 1
        self.conn.commit()
        return n

    def run_pages(self, run_id: int) -> List[Dict[str, Any]]:
        out = []
        for r in self.conn.execute("SELECT * FROM pages WHERE site = ? AND run_id = ? ORDER BY url", (self.site, run_id)):
            d = dict(r)
            for k in ("schema_types", "aeo", "issues"):
                d[k] = json.loads(d[k] or "null")
            out.append(d)
        return out

    def latest_pages(self) -> List[Dict[str, Any]]:
        row = self.conn.execute("SELECT MAX(run_id) FROM pages WHERE site = ?", (self.site,)).fetchone()
        if not row or row[0] is None:
            return []
        return self.run_pages(row[0])

    def first_seen(self) -> Dict[str, str]:
        """When each URL was first crawled (the start of the earliest run that saw it)."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT p.url, MIN(r.started_at) FROM pages p JOIN runs r ON r.id = p.run_id WHERE p.site = ? GROUP BY p.url",
            (self.site,))}

    # ── rank checks ───────────────────────────────────────────────────────
    def save_serp(self, run_id: int, rows: Iterable[Dict[str, Any]]) -> int:
        n, day = 0, today()
        for r in rows:
            self.conn.execute(
                "INSERT INTO serp(run_id, date, keyword, country, tier, position, url, top, questions, site)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, day, r["keyword"], r["country"], r.get("tier", ""), r.get("position"), r.get("url", ""),
                 json.dumps(r.get("top", [])), json.dumps(r.get("questions", [])), self.site))
            n += 1
        self.conn.commit()
        return n

    def run_serp(self, run_id: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT keyword, country, tier, position FROM serp WHERE site = ? AND run_id = ?", (self.site, run_id))]

    def serp_latest(self) -> List[Dict[str, Any]]:
        """Each keyword's newest check, with the position 7 days before for the trend."""
        rows = self.conn.execute(
            "SELECT s.* FROM serp s JOIN (SELECT keyword, country, MAX(id) id FROM serp WHERE site = ? GROUP BY keyword, country) m"
            " ON m.id = s.id ORDER BY s.tier, s.keyword", (self.site,)).fetchall()
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        out = []
        for r in rows:
            d = dict(r)
            d["top"], d["questions"] = json.loads(d["top"]), json.loads(d["questions"])
            prev = self.conn.execute("SELECT position FROM serp WHERE site = ? AND keyword = ? AND country = ? AND date <= ?"
                                     " ORDER BY id DESC LIMIT 1", (self.site, d["keyword"], d["country"], week_ago)).fetchone()
            d["position_7d"] = prev[0] if prev else None
            out.append(d)
        return out

    def serp_history(self, days: int = 30) -> List[Dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        return [dict(r) for r in self.conn.execute(
            "SELECT date, keyword, country, position FROM serp WHERE site = ? AND date >= ? ORDER BY date", (self.site, since))]

    # ── scores ────────────────────────────────────────────────────────────
    def save_scores(self, run_id: int, date: str, scores: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO scores(site, run_id, date, seo, aeo, geo, overall, parts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_id) DO UPDATE SET date = excluded.date, seo = excluded.seo, aeo = excluded.aeo,"
            " geo = excluded.geo, overall = excluded.overall, parts = excluded.parts",
            (self.site, run_id, date, scores.get("seo"), scores.get("aeo"), scores.get("geo"), scores.get("overall"),
             json.dumps(scores.get("parts") or {}, ensure_ascii=False)))
        self.conn.commit()

    def scores(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """One row per day (that day's last run), oldest first."""
        sql = ("SELECT s.* FROM scores s JOIN (SELECT date, MAX(run_id) run_id FROM scores WHERE site = ? GROUP BY date) d"
               " ON d.run_id = s.run_id WHERE s.site = ?")
        args: List[Any] = [self.site, self.site]
        if since:
            sql, args = sql + " AND s.date >= ?", [*args, since]
        rows = []
        for r in self.conn.execute(sql + " ORDER BY s.date", args):
            d = dict(r)
            d["parts"] = json.loads(d["parts"] or "{}")
            rows.append(d)
        return rows

    def previous_parts(self, run_id: int) -> Dict[str, Any]:
        """The score parts of the last scored run before this one ({} when there's none)."""
        row = self.conn.execute("SELECT parts FROM scores WHERE site = ? AND run_id < ? ORDER BY run_id DESC LIMIT 1",
                                (self.site, run_id)).fetchone()
        return json.loads(row[0] or "{}") if row else {}

    def unscored_runs(self) -> List[Dict[str, Any]]:
        """Finished runs that crawled something and have no scores yet (runs from before scoring existed)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT r.* FROM runs r LEFT JOIN scores s ON s.run_id = r.id WHERE r.site = ? AND s.id IS NULL"
            " AND r.status != 'running' AND r.pages > 0 ORDER BY r.id", (self.site,))]

    def scored_runs(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT r.* FROM runs r JOIN scores s ON s.run_id = r.id WHERE r.site = ? ORDER BY r.id", (self.site,))]

    # ── briefs and actions ────────────────────────────────────────────────
    def save_brief(self, run_id: int, data: Dict[str, Any], model: str, cost: float) -> int:
        cur = self.conn.execute("INSERT INTO briefs(run_id, created_at, model, cost_usd, data, site) VALUES (?, ?, ?, ?, ?, ?)",
                                (run_id, now(), model, cost, json.dumps(data, ensure_ascii=False), self.site))
        self.conn.commit()
        return int(cur.lastrowid)

    def latest_brief(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM briefs WHERE site = ? ORDER BY id DESC LIMIT 1", (self.site,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "run_id": row["run_id"], "created_at": row["created_at"], "model": row["model"],
                "cost_usd": row["cost_usd"], **json.loads(row["data"])}

    def briefs(self, limit: int = 14) -> List[Dict[str, Any]]:
        return [{"id": r["id"], "created_at": r["created_at"], "headline": json.loads(r["data"]).get("headline", "")}
                for r in self.conn.execute("SELECT id, created_at, data FROM briefs WHERE site = ? ORDER BY id DESC LIMIT ?",
                                           (self.site, limit))]

    def brief(self, brief_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM briefs WHERE id = ? AND site = ?", (brief_id, self.site)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "created_at": row["created_at"], "model": row["model"], **json.loads(row["data"])}

    def upsert_actions(self, run_id: int, actions: Iterable[Dict[str, Any]]) -> int:
        """Adds new actions; one suggested again refreshes its text and last_seen but keeps the status you set
        (a dismissed action stays dismissed). Returns how many were new."""
        new, stamp = 0, now()
        for a in actions:
            key = action_key(a["title"], a.get("url", ""))
            row = self.conn.execute("SELECT id FROM actions WHERE site = ? AND key = ?", (self.site, key)).fetchone()
            if not row:  # the same task, worded differently, on the same page
                page = norm_url(a.get("url", ""))
                for r in self.conn.execute("SELECT id, title, url FROM actions WHERE site = ? AND status = 'open'",
                                           (self.site,)).fetchall():
                    if norm_url(r["url"]) == page and same_task(r["title"], a["title"]):
                        row = r
                        break
            fields = (a.get("area", "SEO"), int(a.get("priority", 2)), a["title"], a.get("why", ""), a.get("how", ""),
                      a.get("url", ""), a.get("keyword", ""), a.get("effort", ""), a.get("evidence", ""))
            if row:
                self.conn.execute(
                    "UPDATE actions SET area=?, priority=?, title=?, why=?, how=?, url=?, keyword=?, effort=?, evidence=?,"
                    " last_seen=?, seen_count=seen_count+1, run_id=? WHERE id=?", (*fields, stamp, run_id, row["id"]))
            else:
                self.conn.execute(
                    "INSERT INTO actions(key, area, priority, title, why, how, url, keyword, effort, evidence, first_seen,"
                    " last_seen, run_id, site) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, *fields, stamp, stamp, run_id, self.site))
                new += 1
        self.conn.commit()
        return new

    def merge_duplicates(self) -> int:
        """Folds open actions that are the same task on the same page into the oldest one. Returns rows removed."""
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM actions WHERE site = ? AND status = 'open' ORDER BY first_seen, id", (self.site,))]
        removed, kept = 0, []
        for r in rows:
            twin = next((k for k in kept if norm_url(k["url"]) == norm_url(r["url"]) and same_task(k["title"], r["title"])), None)
            if twin is None:
                kept.append(r)
                continue
            self.conn.execute("UPDATE actions SET seen_count = seen_count + ?, last_seen = MAX(last_seen, ?), "
                              "priority = MIN(priority, ?) WHERE id = ?", (r["seen_count"], r["last_seen"], r["priority"], twin["id"]))
            self.conn.execute("DELETE FROM actions WHERE id = ?", (r["id"],))
            removed += 1
        self.conn.commit()
        return removed

    def actions(self, status: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM actions WHERE site = ?", [self.site]
        if status:
            sql, args = sql + " AND status = ?", [*args, status]
        sql += " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, priority, last_seen DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(sql, (*args, limit))]

    def set_action(self, action_id: int, status: str) -> bool:
        if status not in ACTION_STATUSES:
            raise ValueError(f"status must be one of {', '.join(sorted(ACTION_STATUSES))}")
        cur = self.conn.execute("UPDATE actions SET status = ?, updated_at = ? WHERE id = ? AND site = ?",
                                (status, now(), action_id, self.site))
        self.conn.commit()
        return cur.rowcount > 0

    def action_counts(self) -> Dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM actions WHERE site = ? GROUP BY status",
                                                      (self.site,))}
