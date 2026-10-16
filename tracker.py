"""Public-source Pump.fun update watcher: no credential bypass or private API probing."""
from __future__ import annotations
import asyncio
import datetime as dt
import difflib
import hashlib
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import discord
import httpx
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
LOG = logging.getLogger("pumpwatch")
APP_ID = "6717572591"
APP_URL = f"https://apps.apple.com/us/app/id{APP_ID}"
USER_AGENT = "PumpFunPublicUpdateMonitor/1.0 (+public metadata research)"
INTERVAL = max(2, int(os.getenv("CHECK_INTERVAL_MINUTES", "5")))
DB_PATH = os.getenv("DATABASE_PATH", "./pumpfun.sqlite3")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID") or "0")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID") or "0")
ENABLE_WEB_ASSETS = os.getenv("ENABLE_WEB_ASSETS", "true").lower() == "true"
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "pump-fun").strip()
GITHUB_AUTO_DISCOVER = os.getenv("GITHUB_AUTO_DISCOVER", "true").lower() == "true"
GITHUB_MAX_REPOS = max(1, min(50, int(os.getenv("GITHUB_MAX_REPOS", "15"))))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
ASSET_MAX_FILES = max(1, min(20, int(os.getenv("ASSET_MAX_FILES", "10"))) )
ASSET_MAX_BYTES = max(100_000, min(6_000_000, int(os.getenv("ASSET_MAX_BYTES", "2500000"))))
FEATURE_TERMS = re.compile(r"(?i)(?:perpetuals?|perps?|futures?|leverag(?:e|ed)|long.?short|short.?long|long.?position|short.?position|stocks?.?trading|stop.?loss|limit.?order|take.?profit|copy.?trad(?:e|ing)|auto.?sell|auto.?buy|fee.?shar(?:e|ing)|creator.?fee|livestream|live.?stream|cash.?out|cross.?chain|bridge.?swap|launch.?token|token.?launch|migration|watchlist|notification.?setting|price.?alert)")
JS_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]{2,90}")


def feature_clues(source: str) -> dict[str, str]:
    """Conservative public-bundle hints. No credentials, claims of launches, or raw bundle dumps."""
    found = {}
    for match in FEATURE_TERMS.finditer(source):
        start, end = match.span()
        snippet = source[max(0, start - 85):min(len(source), end + 85)]
        # Require a likely flag, UI label, API route or named symbol, not an isolated buzzword.
        contextual = bool(re.search(r"(?i)(?:feature|enabled|flag|modal|button|route|endpoint|/api/|create|settings|toggle|is[A-Z]|show[A-Z]|\btitle\b|\blabel\b)", snippet))
        if not contextual:
            continue
        term = re.sub(r"[^a-z0-9]", "", match.group().lower())
        if term in found:
            continue
        # Only include safe short, bounded evidence (no full source, no query strings).
        excerpt = re.sub(r"[\x00-\x1f]+", " ", snippet)
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:175]
        found[term] = excerpt
        if len(found) >= 25:
            break
    return found




# Public website copy is compared separately from asset fingerprints. A changed
# counter, timestamp, token price or marketing layout should never page Discord.
ANNOUNCEMENT_TERMS = {
    "Perpetuals / leveraged trading": re.compile(r"(?i)\b(?:perps?|perpetuals?|futures?|leverage|leveraged|long\s*(?:/|or|and)\s*short|short\s*(?:/|or|and)\s*long)\b"),
    "Trading features": re.compile(r"(?i)\b(?:limit orders?|stop.loss|take.profit|copy.trad(?:e|ing)|margin.trading|stock.trading)\b"),
    "Product release": re.compile(r"(?i)\b(?:launch(?:ed|ing)?|introduc(?:e|ed|ing)|now available|new feature|roll(?:ed|ing)? out|just added|now live)\b"),
}

def website_delta(old: dict, new: dict) -> str:
    """Return evidence for actionable new copy; empty suppresses noisy text churn."""
    before = re.sub(r"\s+", " ", " ".join(str(old.get(k, "")) for k in ("title", "description", "text"))).strip()
    after = re.sub(r"\s+", " ", " ".join(str(new.get(k, "")) for k in ("title", "description", "text"))).strip()
    if before == after:
        return ""
    # Compare words, so insertions are shown rather than generic 'text changed'.
    a, b = before.split(), after.split()
    sm = difflib.SequenceMatcher(None, a[:4500], b[:4500], autojunk=False)
    candidates = []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op not in ("insert", "replace"):
            continue
        new_words = b[b0:b1]
        if not new_words:
            continue
        added = " ".join(new_words)
        # Context makes even short inserted labels intelligible.
        context = " ".join(b[max(0, b0-12):min(len(b), b1+16)])
        matches = [name for name, pat in ANNOUNCEMENT_TERMS.items() if pat.search(added)]
        if not matches:
            continue
        # 'long' by itself is ambiguous; our regex only treats paired trading
        # terminology as notable. Maintain a tightly bounded excerpt.
        excerpt = context[:420].strip()
        if excerpt and excerpt not in candidates:
            candidates.append((matches, excerpt))
    if not candidates:
        return ""
    lines = []
    for matches, excerpt in candidates[:3]:
        lines.append(f"Signal: {', '.join(matches)}\nNew website text (with context): {excerpt}")
    return "\n\n".join(lines)[:2200]


def csv_env(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


def is_allowed_url(url: str, kind: str = "pump") -> bool:
    """Avoid server-side request forgery: only known public HTTPS destinations."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().rstrip(".")
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return False
        if not host or len(url) > 1000:
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if kind == "apple":
            return host in ("itunes.apple.com", "apps.apple.com")
        if kind == "github":
            return host == "api.github.com"
        return host == "pump.fun" or host.endswith(".pump.fun")
    except ValueError:
        return False


def clean_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path or "/", "", p.query, ""))


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in sorted(value.items()) if k not in ("lastChecked", "requestId")}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def compact_json(value) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(compact_json(value).encode()).hexdigest()


def changes(old, new, prefix="", limit=18):
    """Summarize path changes; never include potentially private response values."""
    out = []
    def walk(a, b, path):
        if len(out) >= limit:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                if len(out) >= limit:
                    break
                p = f"{path}.{key}" if path else str(key)
                if key not in a:
                    out.append(f"+ {p}")
                elif key not in b:
                    out.append(f"- {p}")
                else:
                    walk(a[key], b[key], p)
        elif isinstance(a, list) and isinstance(b, list):
            if a != b:
                out.append(f"~ {path} (list {len(a)} → {len(b)})")
        elif a != b:
            av, bv = str(a), str(b)
            if len(av) < 90 and len(bv) < 90:
                out.append(f"~ {path}: {av[:75]} → {bv[:75]}")
            else:
                out.append(f"~ {path} changed")
    walk(old, new, prefix)
    return out


@dataclass
class Observation:
    source: str
    kind: str
    url: str
    value: object
    label: str


class Store:
    def __init__(self, path=DB_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshots (source TEXT PRIMARY KEY, payload TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, kind TEXT, label TEXT, url TEXT, details TEXT, created_at TEXT, sent INTEGER DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS watches (url TEXT PRIMARY KEY, kind TEXT NOT NULL)")
        self.db.commit()

    def put(self, obs: Observation) -> bool:
        val = compact_json(obs.value)
        digest = hashlib.sha256(val.encode()).hexdigest()
        cur = self.db.execute("SELECT payload,hash FROM snapshots WHERE source=?", (obs.source,))
        prev = cur.fetchone()
        if prev and prev[1] == digest:
            return False
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if prev and obs.kind != "WEB ASSETS":
            if obs.kind == "WEBSITE":
                detail = website_delta(json.loads(prev[0]), json.loads(val))
            elif obs.kind == "WEB FEATURE SIGNALS":
                before = json.loads(prev[0]).get("signals", {})
                after = json.loads(val).get("signals", {})
                fresh = {key: evidence for key, evidence in after.items() if key not in before}
                detail = "\n\n".join(
                    f"POSSIBLE FEATURE REFERENCE: {key}\nAsset: {evidence['asset']}\nExcerpt: {evidence['excerpt']}"
                    for key, evidence in list(fresh.items())[:5]
                )
            else:
                detail = "\n".join(changes(json.loads(prev[0]), json.loads(val))) or "Content fingerprint changed"
            if detail:
                self.db.execute("INSERT INTO events(source,kind,label,url,details,created_at) VALUES(?,?,?,?,?,?)", (obs.source, obs.kind, obs.label, obs.url, detail[:2500], now))
        self.db.execute("INSERT INTO snapshots(source,payload,hash,updated_at) VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload,hash=excluded.hash,updated_at=excluded.updated_at", (obs.source, val, digest, now))
        self.db.commit()
        if obs.kind in ("WEB ASSETS", "WEBSITE"):
            return bool(prev and detail) if obs.kind == "WEBSITE" else False
        if obs.kind == "WEB FEATURE SIGNALS":
            return bool(prev and fresh)
        return bool(prev)

    def get_snapshot(self, source):
        row = self.db.execute("SELECT payload FROM snapshots WHERE source=?", (source,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self, limit=20):
        return self.db.execute("SELECT id,kind,label,url,details,created_at FROM events WHERE sent=0 ORDER BY id LIMIT ?", (limit,)).fetchall()

    def mark_sent(self, event_id: int):
        self.db.execute("UPDATE events SET sent=1 WHERE id=?", (event_id,))
        self.db.commit()

    def recent(self, limit=10):
        return self.db.execute("SELECT id,kind,label,created_at FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def watch(self, url: str, kind: str):
        self.db.execute("INSERT OR REPLACE INTO watches(url,kind) VALUES(?,?)", (url, kind))
        self.db.commit()

    def unwatch(self, url: str):
        cur = self.db.execute("DELETE FROM watches WHERE url=?", (url,))
        self.db.commit()
        return cur.rowcount > 0

    def watches(self):
        return self.db.execute("SELECT url,kind FROM watches ORDER BY url").fetchall()

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]


class Monitor:
    def __init__(self, store: Store):
        self.store = store
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(18), headers={"User-Agent": USER_AGENT}, follow_redirects=False)
        self.lock = asyncio.Lock()
        self.last_run = "never"
        self.failures = {}
        self.last_github_scan = 0.0

    async def close(self):
        await self.client.aclose()

    async def get(self, url: str, kind="pump", json_body=False):
        if not is_allowed_url(url, kind):
            raise ValueError("URL must be a public, allowed HTTPS host")
        # Pump.fun HTML often exceeds 2MB; stream a bounded amount instead of
        # loading unbounded pages in RAM. GitHub/Apple/JSON remain at 2 MB.
        max_bytes = 8_000_000 if kind == "pump" and not json_body else 2_000_000
        headers = {"Accept": "application/json" if json_body else "text/html,application/json"}
        if kind == "github":
            headers["Accept"] = "application/vnd.github+json"
            if GITHUB_TOKEN:
                headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        async with self.client.stream("GET", url, headers=headers) as response:
            if response.is_redirect:
                raise ValueError("Redirect refused; no cross-host redirects followed")
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise ValueError(f"Response exceeds {max_bytes // 1_000_000} MB limit")
                data.extend(chunk)
            encoding = response.encoding or "utf-8"
            text = data.decode(encoding, errors="replace")
            return json.loads(text) if json_body else text

    async def appstore(self) -> list[Observation]:
        url = f"https://itunes.apple.com/lookup?id={APP_ID}&country=us"
        data = await self.get(url, "apple", True)
        results = data.get("results", [])
        if not results:
            raise ValueError("App Store lookup returned no results")
        app = results[0]
        keys = ("trackName", "version", "currentVersionReleaseDate", "releaseNotes", "description", "sellerName", "primaryGenreName", "genres", "price", "fileSizeBytes", "minimumOsVersion", "averageUserRating", "userRatingCount", "artworkUrl512", "screenshotUrls", "ipadScreenshotUrls", "supportedDevices", "languageCodesISO2A", "features")
        observations = []
        # Separate release metadata from volatile ratings, artwork, device lists.
        groups = {
            "version": ("version", "currentVersionReleaseDate", "releaseNotes"),
            "listing": ("trackName", "description", "sellerName", "primaryGenreName", "genres", "price", "minimumOsVersion", "fileSizeBytes"),
            "media": ("artworkUrl512", "screenshotUrls", "ipadScreenshotUrls"),
        }
        for group, fields in groups.items():
            payload = {k: app.get(k) for k in fields if k in app}
            observations.append(Observation(f"apple:{group}", "APP STORE", APP_URL, payload, f"App Store {group}"))
        return observations

    async def webpage(self, url: str) -> list[Observation]:
        html = await self.get(url)
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.find("meta", attrs={"name": "description"})
        description = description_tag.get("content", "") if description_tag else ""
        visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:12000]
        main = Observation(f"web:text:{url}", "WEBSITE", url, {"title": title, "description": description, "text": visible}, f"Website content: {urlparse(url).path or '/'}")
        if not ENABLE_WEB_ASSETS:
            return [main]
        # Parse assets from ORIGINAL HTML; strip query noise and compare exact asset paths.
        raw = BeautifulSoup(html, "html.parser")
        assets = set()
        for tag in raw.find_all(["script", "link"]):
            ref = tag.get("src") or tag.get("href")
            if not ref:
                continue
            abs_url = urljoin(url, ref)
            p = urlparse(abs_url)
            if is_allowed_url(abs_url) and re.search(r"\.(js|css)(?:$|\?)", abs_url, re.I):
                assets.add(urlunparse((p.scheme, p.netloc, p.path, "", "", "")))
        asset_key = f"web:assets:{url}"
        previous = self.store.get_snapshot(asset_key)
        current = sorted(assets)
        old_files = set(previous.get("files", [])) if previous else set()
        added = sorted(assets - old_files)
        removed = sorted(old_files - assets)
        asset_obs = Observation(asset_key, "WEB ASSETS", url, {"files": current},
                                f"Website JS/CSS asset references: {urlparse(url).path or '/'}")
        # Asset fingerprint churn is noise; keep the inventory silently.
        # Run analysis only on newly introduced JS files. First scan initializes
        # evidence silently, and missing source data does not generate an alert.
        signal_key = f"web:features:{url}"
        old_signals = self.store.get_snapshot(signal_key)
        accumulated = dict(old_signals.get("signals", {})) if old_signals else {}
        to_check = [u for u in added if u.endswith(".js")][:ASSET_MAX_FILES]
        for asset_url in to_check:
            try:
                script = await self.get_asset(asset_url)
                for term, excerpt in feature_clues(script).items():
                    # Preserve first occurrence to avoid repeat alerts on every build.
                    accumulated.setdefault(term, {"asset": asset_url, "excerpt": excerpt})
            except Exception as exc:
                LOG.info("Optional JS feature inspection skipped %s: %s", asset_url, str(exc)[:100])
        if added or removed:
            LOG.info("Website assets: %d added, %d removed; inspected %d new JS files", len(added), len(removed), len(to_check))
            for asset_url in added[:5]:
                LOG.info("Added asset: %s", asset_url)
        signals_obs = Observation(signal_key, "WEB FEATURE SIGNALS", url,
                                  {"signals": accumulated}, "Possible feature references in public JS")
        return [main, asset_obs, signals_obs]

    async def get_asset(self, url: str) -> str:
        """Read only bounded public first-party JavaScript; skip oversize bundles."""
        if not is_allowed_url(url) or not urlparse(url).path.endswith(".js"):
            raise ValueError("Not a permitted JavaScript URL")
        async with self.client.stream("GET", url, headers={"Accept": "text/javascript,*/*"}) as response:
            if response.is_redirect:
                raise ValueError("Asset redirect refused")
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > ASSET_MAX_BYTES:
                    raise ValueError("Large JS bundle skipped")
                chunks.extend(chunk)
            return chunks.decode("utf-8", errors="replace")

    async def json_endpoint(self, url: str, kind="PUBLIC API") -> list[Observation]:
        data = await self.get(url, json_body=True)
        if not isinstance(data, (dict, list)):
            raise ValueError("Expected JSON object or array")
        if kind == "API SPEC" and isinstance(data, dict) and isinstance(data.get("paths"), dict):
            # Capture only endpoints, methods, parameters/schema shapes, not dynamic response data.
            paths = {}
            for path, methods in data["paths"].items():
                if isinstance(methods, dict):
                    paths[path] = {m: {"operationId": body.get("operationId"), "parameters": [p.get("name") for p in body.get("parameters", []) if isinstance(p, dict)]} for m, body in methods.items() if isinstance(body, dict) and m.lower() in ("get", "post", "put", "patch", "delete")}
            data = {"info": data.get("info", {}), "paths": paths}
        elif isinstance(data, list):
            # Avoid false feature alerts from live market data: only observe shape, not values.
            sample = data[0] if data else {}
            data = {"type": "array", "count_band": "nonempty" if data else "empty", "item_keys": sorted(sample.keys()) if isinstance(sample, dict) else type(sample).__name__}
        elif isinstance(data, dict):
            # API response status and top-level schema, not mutable balances/trades/coins.
            data = {"top_level_keys": sorted(data.keys()), "field_types": {k: type(v).__name__ for k, v in data.items()}}
        return [Observation(f"api:{kind}:{url}", kind, url, data, f"{kind} schema: {urlparse(url).hostname}")]

    async def github(self, repo: str) -> list[Observation]:
        """Track commits, latest release and tags for each watched public repo."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid public repository")
        base = f"https://api.github.com/repos/{repo}"
        # Commit failures are reported; empty releases/tags are normal (404/[]).
        data = await self.get(f"{base}/commits?per_page=1", "github", True)
        if not isinstance(data, list) or not data:
            raise ValueError("No public commits")
        item = data[0]
        commit = item.get("commit") or {}
        observations = [Observation(
            f"github:commit:{repo}", "GITHUB COMMIT", item.get("html_url") or f"https://github.com/{repo}/commits",
            {"sha": item.get("sha"), "message": (commit.get("message") or "")[:600],
             "date": (commit.get("committer") or {}).get("date")}, f"GitHub commit: {repo}")]
        try:
            releases = await self.get(f"{base}/releases?per_page=1", "github", True)
            if isinstance(releases, list) and releases:
                release = releases[0]
                observations.append(Observation(
                    f"github:release:{repo}", "GITHUB RELEASE",
                    release.get("html_url") or f"https://github.com/{repo}/releases",
                    {"id": release.get("id"), "tag": release.get("tag_name"),
                     "name": release.get("name"), "published": release.get("published_at"),
                     "draft": release.get("draft"), "prerelease": release.get("prerelease")},
                    f"GitHub release: {repo}"))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                LOG.warning("GitHub release check %s: %s", repo, exc)
        try:
            tags = await self.get(f"{base}/tags?per_page=10", "github", True)
            if isinstance(tags, list):
                observations.append(Observation(
                    f"github:tags:{repo}", "GITHUB TAGS", f"https://github.com/{repo}/tags",
                    {"tags": [t.get("name") for t in tags if isinstance(t, dict)]},
                    f"GitHub tags: {repo}"))
        except httpx.HTTPStatusError as exc:
            LOG.warning("GitHub tags check %s: %s", repo, exc)
        return observations

    async def github_discover(self) -> tuple[list[Observation], list[str]]:
        """Find public repos under the configured GitHub owner (including new ones)."""
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", GITHUB_OWNER):
            raise ValueError("Invalid GITHUB_OWNER")
        url = f"https://api.github.com/users/{GITHUB_OWNER}/repos?per_page=100&type=public&sort=updated"
        data = await self.get(url, "github", True)
        if not isinstance(data, list):
            raise ValueError("GitHub repo discovery returned non-list JSON")
        repos = [r for r in data if isinstance(r, dict) and not r.get("fork") and not r.get("archived") and r.get("full_name")]
        # Stable normalized listing; do not alert just because the GitHub sort order changed.
        names = sorted(r["full_name"] for r in repos)
        obs = Observation(f"github:owner:{GITHUB_OWNER.lower()}", "GITHUB REPOSITORIES",
                          f"https://github.com/{GITHUB_OWNER}?tab=repositories",
                          {"repositories": names}, f"GitHub public repositories: {GITHUB_OWNER}")
        # Limit individual repo polling to respect GitHub API rate limits.
        return [obs], [r["full_name"] for r in repos[:GITHUB_MAX_REPOS]]

    async def scan(self):
        async with self.lock:
            tasks = [("App Store", self.appstore())]
            web = set(csv_env("WEB_URLS", "https://pump.fun/"))
            json_urls = set(csv_env("PUBLIC_JSON_URLS"))
            api_specs = set(csv_env("OPENAPI_URLS"))
            for url, kind in self.store.watches():
                (web if kind == "web" else api_specs if kind == "spec" else json_urls).add(url)
            for url in sorted(web):
                if is_allowed_url(url):
                    tasks.append((url, self.webpage(url)))
            for url in sorted(json_urls):
                if is_allowed_url(url):
                    tasks.append((url, self.json_endpoint(url)))
            for url in sorted(api_specs):
                if is_allowed_url(url):
                    tasks.append((url, self.json_endpoint(url, "API SPEC")))
            # Unauthenticated GitHub API has a 60-request/hour cap; check once
            # per hour without a token. With a token, use normal scan interval.
            github_due = (time.monotonic() - self.last_github_scan >= (INTERVAL * 60 if GITHUB_TOKEN else 3600))
            repos = set(csv_env("GITHUB_REPOS", "pump-fun/pump-public-docs"))
            if github_due and GITHUB_AUTO_DISCOVER:
                try:
                    discovery_obs, discovered = await self.github_discover()
                    for obs in discovery_obs:
                        self.store.put(obs)
                    repos.update(discovered)
                except Exception as exc:
                    LOG.warning("GitHub owner discovery failed for %s: %s", GITHUB_OWNER, exc)
                    discovery_failure = str(exc)[:180]
                else:
                    discovery_failure = None
            else:
                discovery_failure = None
            if github_due:
                for repo in sorted(repos):
                    tasks.append((repo, self.github(repo)))
                self.last_github_scan = time.monotonic()
            outcomes = await asyncio.gather(*(coro for _, coro in tasks), return_exceptions=True)
            updated = 0
            self.failures = {}
            if discovery_failure:
                self.failures[f"github-owner:{GITHUB_OWNER}"] = discovery_failure
            for (label, _), result in zip(tasks, outcomes):
                if isinstance(result, BaseException):
                    self.failures[label] = str(result)[:180]
                    LOG.warning("Monitor failed for %s: %s", label, result)
                    continue
                for observation in result:
                    if self.store.put(observation):
                        updated += 1
            self.last_run = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            return updated


class TrackerBot(commands.Bot):
    def __init__(self, monitor: Monitor):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.monitor = monitor
        self.loop_task = None

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.loop_task = asyncio.create_task(self.worker())

    async def on_ready(self):
        LOG.info("Logged in as %s", self.user)

    async def close(self):
        if self.loop_task:
            self.loop_task.cancel()
        await self.monitor.close()
        await super().close()

    async def send_pending(self):
        if not CHANNEL_ID:
            LOG.warning("DISCORD_CHANNEL_ID is not configured")
            return
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await self.fetch_channel(CHANNEL_ID)
        for event_id, kind, label, url, detail, created in self.monitor.store.pending():
            if kind == "WEB ASSETS" or (kind == "WEBSITE" and not detail.startswith("Signal:")):
                # Discard legacy asset churn and uninformative generic website alerts.
                self.monitor.store.mark_sent(event_id)
                continue
            embed = discord.Embed(title=f"🔔 {label}", url=url, color=0x17C964, timestamp=dt.datetime.fromisoformat(created))
            embed.add_field(name="Source", value=kind, inline=True)
            if kind == "WEBSITE":
                embed.title = "📣 New public website feature signal"
                embed.add_field(name="What changed", value=discord.utils.escape_mentions(detail[:1000]), inline=False)
                embed.add_field(name="Verify on source", value=f"[Open page]({url})", inline=False)
            elif kind == "WEB FEATURE SIGNALS":
                # URL outside code fences remains clickable in Discord.
                match = re.search(r"Asset: (https://[^\s]+)", detail)
                embed.add_field(name="Possible feature evidence (unverified)",
                                value=discord.utils.escape_mentions(detail[:900]), inline=False)
                if match:
                    embed.add_field(name="Inspect changed JS asset", value=f"[Open public asset]({match.group(1)})", inline=False)
            else:
                embed.add_field(name="Detected changes", value=f"```text\n{detail[:900]}\n```", inline=False)
            embed.set_footer(text="Public website/code evidence • Confirm release against official announcement or app")
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, discord.Forbidden) as e:
                LOG.error("Failed to send event %s: %s", event_id, e)
                break
            self.monitor.store.mark_sent(event_id)
            await asyncio.sleep(0.5)

    async def worker(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.monitor.scan()
                await self.send_pending()
            except Exception:
                LOG.exception("Unexpected scan failure")
            await asyncio.sleep(INTERVAL * 60)


store = Store()
monitor = Monitor(store)
bot = TrackerBot(monitor)


def admin_only(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.user.guild_permissions.manage_guild)


@bot.tree.command(name="status", description="Current scanner status and snapshot counts")
async def status(interaction: discord.Interaction):
    msg = (f"**Last scan:** {monitor.last_run}\n**Interval:** {INTERVAL} min\n"
           f"**Saved baselines:** {store.count()}\n**Watch URLs:** {len(store.watches())}\n"
           f"**Failures in last scan:** {len(monitor.failures)}\n"
           f"**Failure details:** {str(monitor.failures)[:700] if monitor.failures else 'None'}\n"
           "App metadata, website assets, API shapes and public repositories are monitored independently.")
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="check", description="Run all monitors now (server managers only)")
@app_commands.check(admin_only)
async def check(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    changed = await monitor.scan()
    await bot.send_pending()
    await interaction.followup.send(f"Scan complete. {changed} changed sources; {len(monitor.failures)} failed sources. Initial observations set baselines without alerts.", ephemeral=True)


@bot.tree.command(name="history", description="Show the most recent change detections")
async def history(interaction: discord.Interaction):
    rows = store.recent(10)
    content = "\n".join(f"`#{row[0]}` **{row[1]}** {row[2]} — {row[3][:16]}" for row in rows) if rows else "No changes recorded yet; first scan establishes baselines."
    await interaction.response.send_message(content[:1950], ephemeral=True)


@bot.tree.command(name="watch", description="Watch an additional public pump.fun webpage or JSON schema")
@app_commands.describe(url="Public https://pump.fun/... URL", kind="web, json or spec")
@app_commands.choices(kind=[app_commands.Choice(name="Website", value="web"), app_commands.Choice(name="JSON response schema", value="json"), app_commands.Choice(name="OpenAPI specification", value="spec")])
@app_commands.check(admin_only)
async def watch(interaction: discord.Interaction, url: str, kind: app_commands.Choice[str]):
    if not is_allowed_url(url):
        await interaction.response.send_message("Only public HTTPS URLs on pump.fun or its subdomains are permitted.", ephemeral=True)
        return
    store.watch(clean_url(url), kind.value)
    await interaction.response.send_message(f"Added `{kind.value}` watch: {clean_url(url)}. First successful fetch establishes a baseline.", ephemeral=True)


@bot.tree.command(name="unwatch", description="Remove an additional URL watch (server managers only)")
@app_commands.check(admin_only)
async def unwatch(interaction: discord.Interaction, url: str):
    removed = store.unwatch(clean_url(url))
    await interaction.response.send_message("Removed." if removed else "No matching saved watch.", ephemeral=True)


@bot.tree.command(name="watches", description="List additional URL watches")
async def watches(interaction: discord.Interaction):
    rows = store.watches()
    output = "\n".join(f"`{kind}` {url}" for url, kind in rows) or "No extra watches; default monitors are still active."
    await interaction.response.send_message(output[:1950], ephemeral=True)


@bot.tree.error
async def slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = "You need Manage Server permission to run this command."
    else:
        LOG.error("Slash command error: %s", error)
        message = "Command failed; check Railway logs."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN; configure Railway environment variables")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
