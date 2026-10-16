# Pump.fun iOS Update Intelligence Bot

A GitHub/Railway-ready Discord bot that watches **public** evidence of Pump.fun changes. It separates confirmed App Store releases from indirect website, API-schema, and repository signals. No Pump.fun account, wallet, private endpoints, or jailbreak required.

**iOS app:** https://apps.apple.com/us/app/pump-fun-speculate-on-trends/id6717572591  
**App ID:** `6717572591`

## Monitors

| Signal | What it compares | Confidence / caveat |
|---|---|---|
| App Store release | Version, release date, release notes | Confirms a public app version / published notes, **not** that every listed feature is live |
| App Store listing | Title, long description, requirements, size | Official listing change |
| App Store media | Artwork and screenshot URLs | Metadata URL changes; an identical URL with replaced image bytes will not be detected |
| Pump.fun website | Visible HTML text, title, meta description | Can expose newly mentioned features; dynamic client-rendered UI isn't fully covered |
| Web build assets | Referenced `.js`/`.css` file paths | Deployment signal, **not** a new iOS feature |
| Public JSON endpoints | Top-level key and type changes | Schema clue only; **does not scan private APIs, authenticated data or live transaction values** |
| Public OpenAPI | Paths, HTTP methods, operation IDs, parameter names | Developer-contract signal; can reveal documented feature surfaces |
| Official public GitHub repo | Latest commit SHA, message and date | Public developer change; not proof of iOS shipment |

An initial successful fetch **seeds the baseline without sending old alerts**. A new alert occurs only when subsequent successful results differ. Failed fetches never overwrite the baseline. Events are stored before Discord delivery and retried if sending fails (rare network edge cases can cause duplicate sends).

### How this differs from iOS binary analysis

Apple's public iTunes Lookup API does **not** provide app package binaries or unreleased feature flags. This bot **cannot** reliably find internal code-level features, changes behind login, private API routes, remotely enabled flags, or in-app UI changes with no public surface. It never implies it can inspect the installed iOS binary. For actual iPhone UI regressions/features you'd need a separately authorized device/automation testing setup.

## Fast setup: GitHub → Railway

1. Create a new **private** GitHub repository (public is fine as long as no secrets are committed). Upload everything in this directory, **except `.env` and `.sqlite3` files**.
2. Go to https://discord.com/developers/applications → **New Application** → **Bot**, obtain the bot token. Keep it secret. Under **OAuth2 → URL Generator**, select scopes **bot** and **applications.commands**, then bot permissions **View Channels**, **Send Messages**, and **Embed Links**. Invite to your Discord server.
3. In Discord, enable **Developer Mode**, right-click your alert channel → **Copy Channel ID**. Optionally copy the server ID for quick slash-command registration.
4. Go to https://railway.com, create project → **Deploy from GitHub repo**. Add the environment variables from `.env.example`:

   | Key | Value |
   |---|---|
   | `DISCORD_TOKEN` | your Discord bot token |
   | `DISCORD_CHANNEL_ID` | numeric channel ID |
   | `DISCORD_GUILD_ID` | optional numeric server ID; makes commands sync quickly in one server |
   | `DATABASE_PATH` | `/data/pumpfun.sqlite3` |
   | `CHECK_INTERVAL_MINUTES` | `5` or more; `5` recommended |

5. In Railway, add a **persistent volume** mounted at `/data`, so updates and redeploys don't reset the SQLite baseline. Use **one replica only**, otherwise multiple workers can issue duplicate alerts.
6. Railway detects `Dockerfile`; `railway.json` starts the worker. Watch deployment logs for `Logged in as ...`.
7. Type `/status`, then `/check` in Discord. First `/check` usually establishes baseline and gives **zero alerts**. Later changes post automatically to the configured channel.

**Railway cost:** depends on your plan and resource usage. Bot polls only public metadata (no paid Pump.fun or Apple key required). If GitHub API responds with rate limits, configure `GITHUB_TOKEN` using a minimally scoped GitHub token (currently code doesn't use this variable: public unauthenticated access is the implemented mode). The monitoring loop continues when individual sources fail.

## Slash commands

- `/status` — last scan, baseline count, error count
- `/check` — force a full scan; requires **Manage Server**
- `/history` — last ten change events
- `/watch url:https://pump.fun/... kind:web` — monitor an additional public webpage
- `/watch url:https://api.pump.fun/api/openapi.json kind:spec` — monitor published OpenAPI schema, if publicly accessible without authorization
- `/watch url:https://frontend-api-v3.pump.fun/... kind:json` — monitor response *shape* on an unauthenticated endpoint, if publicly accessible
- `/watches` — list additional URL watches
- `/unwatch url:https://pump.fun/...` — delete extra watch

The `/watch` command accepts only public `https://pump.fun` and `https://*.pump.fun` URLs. No arbitrary target hosts are allowed. Redirects are not followed. Do not use protected endpoints or any URL that requires credentials. The monitor does not inject authorization headers.

## Additional monitoring sources

Set optional Railway variables:

```env
WEB_URLS=https://pump.fun/
PUBLIC_JSON_URLS=
OPENAPI_URLS=
GITHUB_REPOS=pump-fun/pump-public-docs
ENABLE_WEB_ASSETS=true
```

Values in URL and repository lists are separated by commas. Public OpenAPI or API discovery endpoints are **opt-in**, because availability, paths and permissions can change. `https://api.pump.fun/api/openapi.json` is a *candidate* mentioned in package documentation, not a guaranteed currently public endpoint. The bot records failure in its logs rather than treating authentication errors as feature changes.

For best quality, watch low-noise public pages and **published documentation schemas**. Watching the `/` website might produce noisy changes due to personalized, time-varying HTML. Avoid dynamic trades/coin listings; the JSON monitor intentionally ignores changing business data values.

## Run locally

Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: configure DISCORD_TOKEN, DISCORD_CHANNEL_ID, DATABASE_PATH=./pumpfun.sqlite3
python -m tracker
```

Run tests with `python -m unittest discover -s tests -v`.

## What an alert means

- **APP STORE / version:** Apple public listing reports a different released version or notes.
- **APP STORE / listing/media:** Apple public listing metadata changed.
- **WEBSITE / WEB ASSETS:** Public website or referenced deployment assets changed. Might be cosmetic or unrelated to iOS.
- **PUBLIC API / API SPEC:** Public schema/contracts changed. Might indicate a new surface, but requires investigation.
- **PUBLIC REPO:** Public upstream source has a new latest commit. Not a promise of release.

The alert includes changed fields and a source link. It does **not** generate unsupported feature claims or automatically trade anything.

## Operational notes

- Site-specific HTML can have false positives (A/B tests, locale changes, dynamic content). Tune watched pages and interval accordingly.
- Apple lookup and GitHub are third-party services: may throttle, lag, or become unavailable. Each failure is logged and skipped safely.
- Avoid aggressive request frequency; interval minimum is 2 minutes, default 5 minutes.
- Published app release notes can be generic (e.g. “bug fixes”). The bot cannot infer undisclosed details.
- Sources may return different content from Railway IPs; do not attempt to evade access restrictions.
- For production, add external logging/health checks and optionally rotate SQLite backups for disaster recovery.

## Repository layout

```
tracker.py          # async polling, comparisons, SQLite, Discord commands
requirements.txt    # dependencies
Dockerfile          # Railway container build
railway.json        # Railway worker start and restart rules
Procfile            # alternative worker declaration
.env.example        # configuration template; never put secrets in git
.gitignore
README.md
tests/test_tracker.py
```

MIT license; unofficial, not affiliated with Pump.fun or Apple.

## GitHub monitoring (expanded)

The tracker now discovers publicly visible repositories under `GITHUB_OWNER` (default: `pump-fun`), alerts when the **list of public repositories changes**, and checks the latest commit, release, and tags for up to `GITHUB_MAX_REPOS` recently updated repositories. Explicit `GITHUB_REPOS` entries are always checked too. This watches public GitHub activity only; it cannot see private code, unreleased iOS builds, or prove that a change was deployed to the iPhone app.

Railway environment variables:

```env
GITHUB_OWNER=pump-fun
GITHUB_AUTO_DISCOVER=true
GITHUB_MAX_REPOS=15
GITHUB_REPOS=pump-fun/pump-public-docs
# Optional: use a fine-grained read-only GitHub token to check on every regular scan
GITHUB_TOKEN=
```

GitHub limits unauthenticated API requests to 60/hour, so without `GITHUB_TOKEN` repository checks are automatically spaced **at least one hour** apart; the normal App Store and webpage scans remain at the configured interval. With a GitHub token, repo checks run on the normal interval (ensure your token permits reading the public repositories). Keep tokens only in Railway variables, not in GitHub files. With many repos, you may want to reduce `GITHUB_MAX_REPOS` to avoid excessive polling.

**Pump.fun HTML fix:** Website page fetches now stream with an 8 MB upper bound (previously 2 MB). The limit still protects the bot from unexpectedly large responses. A `Response exceeds 8 MB limit` warning indicates the site is bigger still; you can temporarily set `WEB_URLS` empty to skip the web monitor without affecting GitHub/App Store.

**Updating an existing Railway bot:** Replace `tracker.py` with the new copy and update `.env.example` / `README.md` in GitHub. Set the GitHub variables above in Railway. Keep the existing `/data` volume and `DATABASE_PATH` intact, so your baselines/history are preserved. GitHub repo discovery and new GitHub categories create baselines on their first successful run and alert only on later differences.

## V3: quieter, evidence-based website JS alerts

- **Website asset inventory changes are saved silently**, avoiding unhelpful `files (list 107 → 107)` alerts.
- On first observation, the bot captures a baseline and does **not** announce already-existing findings.
- Later, newly referenced first-party `.js` files are inspected, within strict size and count limits, for a **small conservative set of feature-related phrases with contextual clues**.
- A new clue can generate a **possible feature reference** alert with a source asset URL and short excerpt. This is **not confirmation of an announced, released, or iOS feature**. It can also be a dead-code string or a previously existing feature.
- Known clue names are stored on the existing persistent SQLite volume, so a rebuild that mentions the same clue does not re-alert.
- `ASSET_MAX_FILES=10` controls how many new JS files are inspected per scan (1–20); `ASSET_MAX_BYTES=2500000` caps each JS download. Bigger bundles are skipped and **may contain changes the bot cannot detect**. Only public first-party `pump.fun` subdomains are fetched.
- Asset URLs added/removed are written to Railway logs for debugging; noise does not hit Discord. No source-code diffing of every line is performed, and changes to a reused, unchanged asset URL are not detected.
- The App Store, GitHub and configured public API monitors continue to run as before.

### Upgrade V2 → V3

Upload **all V3 project files** to the root of your existing GitHub repository, replacing V2, and let Railway redeploy. Keep existing environment variables, Discord application, and persistent `/data` volume. Do not delete the database. The existing asset snapshot can be reused, while the new feature-clue collection will quietly establish its own baseline.

## V4: actionable website announcements

V4 replaces generic `~ text changed` website alerts with a bounded excerpt showing **what new text appeared** and a feature category, only when meaningful feature-related terms are found. Changes to ordinary numbers, counters and unrelated website copy are saved silently. This monitor observes only the pages configured in `WEB_URLS` or added via `/watch`; it **does not monitor X/Twitter posts** or reliably inspect a dynamically rendered iOS app screen. If a feature is announced only on social media or behind an authenticated UI, this monitor may miss it. An alert is a public-source *signal*, not confirmation that an iOS release occurred.

Existing Railway environment variables and `/data` storage continue to work. Upload the latest `tracker.py` to the same GitHub repository; Railway redeploys automatically. Previously queued generic website alerts are skipped during message sending. No database reset is necessary.
