# Sleeper Fantasy League API

Read-only FastAPI service that sits between a [Sleeper](https://sleeper.com) fantasy
football league and an external client (Claude, a script, whatever), exposing the whole
league behind a handful of fixed URLs with **player IDs already resolved to names**,
positions, NFL teams and injury status.

It also folds in four free external sources — advanced usage stats, betting lines,
injury practice reports and stadium weather — each cached on disk at its own cadence.

Sleeper's public API returns rosters, matchups and transactions as raw numeric player
IDs, and the file that maps those IDs to names weighs ~5MB (Sleeper asks that you fetch
it at most once a day). This service does that cross-referencing for you and caches the
player file on disk.

## Endpoints

| Method | Path | Auth | What it returns |
| --- | --- | --- | --- |
| `GET` | `/health` | none | Liveness probe for Coolify + player cache status |
| `GET` | `/snapshot` | `X-API-Key` | The full league: teams, rosters, standings, matchups, transactions |
| `GET` | `/league-settings` | `X-API-Key` | League configuration translated into plain language |
| `GET` | `/roster/{manager}` | `X-API-Key` | One resolved roster, found by username or team name |
| `GET` | `/advanced-stats/{player_id}` | `X-API-Key` | nflverse usage and efficiency for one player |
| `GET` | `/odds/{week}` | `X-API-Key` | Betting lines for a week (game script signal) |
| `GET` | `/injury-report/{player_id}` | `X-API-Key` | ESPN injury detail for one player |
| `GET` | `/injury-report?team={abbr}` | `X-API-Key` | ESPN injury report for a whole team |
| `GET` | `/weather/{week}` | `X-API-Key` | Kickoff weather for the week's outdoor venues |
| `GET` | `/stadiums` | `X-API-Key` | The static stadium/dome reference used for weather |
| `GET` | `/docs` | none (schema only) | Interactive OpenAPI docs |

### `GET /snapshot`

Query params:

- `week` (optional, 1-22) — week to pull matchups for. Defaults to the current NFL week
  from `/v1/state/nfl`.
- `days` (optional, 1-120, default `7`) — how far back to include transactions.
- `include` (optional) — comma-separated external sources to attach:
  `advanced_stats`, `odds`, `injury_report`, `weather`. Omitted by default so the
  plain roster response stays small. See [External sources in `/snapshot`](#external-sources-in-snapshot).

Returns:

- `league` — id, name, season, status, team count, roster positions
- `nfl_state` — season, season type, current week
- `standings` — every team ranked by wins then points for, with record, points for/against,
  waiver position and FAAB spent
- `teams[]` — each manager (`display_name`, `username`, `team_name`, co-owners) with their
  roster split into:
  - `starters[]` — one entry per lineup slot from the league's `roster_positions`
    (`QB`, `RB`, `WR`, `TE`, `FLEX`, `K`, `DEF`, …), each with the resolved player or
    `"empty": true` for an unfilled slot
  - `bench[]`, `injured_reserve[]`, `taxi_squad[]`
- `matchups[]` — grouped head-to-head, each side with total points, the resolved starting
  lineup with per-player points, and who is ahead by how much
- `transactions` — last `days` days, each with type (`waiver` / `free_agent` / `trade` /
  `commissioner`), who made it, teams involved, resolved players added/dropped, FAAB bid,
  traded picks and budget, plus a one-line English `summary`
- `players_cache` — how old the cached player file is

Every player looks like this:

```json
{
  "player_id": "4034",
  "name": "Christian McCaffrey",
  "position": "RB",
  "nfl_team": "SF",
  "status": "Active",
  "injury_status": "Questionable",
  "injury_body_part": "Achilles",
  "resolved": true
}
```

`"resolved": false` means the ID was not in the cached player file (very rare — a brand
new signing before the daily refresh); the name falls back to `Unknown player (<id>)`
rather than the request failing.

### `GET /league-settings`

Scoring, roster slots, playoff format, trade deadline and waiver rules as clean JSON —
`0.04` becomes *"Points per passing yard"*, `waiver_type` + `waiver_budget` become
*"FAAB blind bidding ($100 season budget)"*, and so on. Scoring keys are grouped
(`passing`, `rushing`, `receiving`, `kicking`, `defense_special_teams`, `bonuses`, …) and
the untouched `raw` settings are included too. Unknown or newly added Sleeper keys are
never dropped — they get a generated description and land in the `other` group.

### `GET /roster/{manager}`

Flexible, case-insensitive lookup against username, display name, team name and
co-owners. Exact match wins, then prefix, then substring — so `/roster/tacos` finds
*Los Tacos Voladores*.

- `404` if nothing matches, with the list of available teams in the response
- `409` if the query is ambiguous, with the candidates

## External data sources

Four free sources sit behind the endpoints above. Only one of them needs an API key;
the rest work out of the box. Each has its own disk cache and refresh cadence, chosen
around how fast the underlying data actually moves.

| Source | Key needed | Cache TTL | What it adds |
| --- | --- | --- | --- |
| [nflverse](https://github.com/nflverse/nflverse-data) | no | 24h | Snap %, target share, air yards, red zone touches, EPA |
| [The Odds API](https://the-odds-api.com) | **yes** (`ODDS_API_KEY`) | 24h | Spread, total, moneyline, favourite |
| [ESPN](https://site.api.espn.com) | no | 3h | Injury status + practice participation |
| [Open-Meteo](https://open-meteo.com) | no | 12h / 1h on game day | Wind, temperature, precipitation at the stadium |

### `GET /advanced-stats/{player_id}` — nflverse

`player_id` is the Sleeper id that appears in `/snapshot` rosters. Three nflverse
releases are combined and keyed by `gsis_id`, which Sleeper carries on every player:

- `stats_player/stats_player_week_<season>.csv` — targets, target share, air yards
  share, WOPR, RACR, EPA, PPR points
- `snap_counts/snap_counts_<season>.csv` — offensive snaps and snap %
- `pbp/play_by_play_<season>.csv` — red zone carries, targets, receptions and TDs

`snap_counts` is keyed by Pro-Football-Reference id rather than `gsis_id`, so
`players/players.csv` is pulled as the crosswalk between the two. This is the one join
that is easy to get silently wrong — a missing crosswalk drops snap counts entirely
rather than attaching them to the wrong player.

The response carries a season average, a **last-three-week average**, and the delta
between them, which is the earliest read on a role change:

```json
{
  "player": { "name": "Justin Jefferson", "position": "WR", "nfl_team": "MIN" },
  "gsis_id": "00-0036322",
  "season": 2025,
  "sources": ["stats_player_week", "snap_counts", "play_by_play (red zone)"],
  "stats": {
    "season_averages": { "snap_pct": 0.64, "target_share": 0.218, "red_zone_touches": 2.0 },
    "recent_averages": { "weeks": [3, 4, 5], "snap_pct": 0.543, "target_share": 0.183 },
    "trend_vs_season": { "snap_pct": -0.097, "target_share": -0.035 },
    "role_note": "snap share trending down 10 points vs season average; target share trending down 4 points",
    "by_week": [ ... ]
  }
}
```

Two behaviours worth knowing:

- **The trend is withheld, not zeroed, early in the season.** With three games or
  fewer the "recent" window is the whole season, so the delta would always be `0.0` —
  which reads as *"role is stable"* when it actually means *"not enough data"*. In that
  case `trend` is `{}` and a `trend_note` explains why.
- **Red zone usage is the only stat that needs the ~98MB play-by-play file.** It is
  streamed to disk, aggregated in one pass (a couple of seconds, no pandas) and then
  deleted; only the aggregate is cached. Set `NFLVERSE_INCLUDE_RED_ZONE=false` to skip
  that download and keep every other stat.

Players with no `gsis_id` (team defenses, and anyone who has never played an NFL game)
return `404` with an explanation rather than an empty body. If nflverse has not
published the current season yet — which is the case for the first weeks of a new
season — it falls back to the previous season and says so in `season`.

### `GET /odds/{week}` — The Odds API

Spread and total are the cleanest public proxy for game script: big favourites run the
ball late, big underdogs throw. Per game you get home/away teams (with abbreviations),
the consensus spread, the total, moneylines, who is favoured, the implied team totals,
and a one-line `game_script` read.

The consensus line is the **median across the returned bookmakers**, so one outlier book
cannot skew it. `bookmakers_counted` says how many went into it.

The free tier allows roughly 500 requests a month, so responses are cached for a full
day and the quota the API reports back is passed through on every response:

```json
"quota": { "requests_remaining": "473", "requests_used": "27", "last_request_cost": "1" }
```

The Odds API returns upcoming games rather than NFL week numbers, so each game is
matched to a week by kickoff date (week 1 starts on the Thursday after Labor Day).
Without `ODDS_API_KEY` this endpoint returns `503` and every other endpoint keeps
working.

### `GET /injury-report/{player_id}` and `GET /injury-report?team={abbr}` — ESPN

ESPN publishes practice participation in the injury note, which is often fresher and
more granular than the `injury_status` baked into Sleeper's once-a-day player file. The
free-text comment is parsed into `practice_participation`: `full`, `limited` or
`did_not_practice`.

Players are matched on `espn_id` from the Sleeper player file, falling back to an exact
name match within the player's own team. The per-player response puts ESPN's view next
to Sleeper's so you can see when they disagree:

```json
{
  "player": { "name": "Christian McCaffrey", "nfl_team": "SF" },
  "listed": true,
  "sleeper_injury_status": "Questionable",
  "espn_report": {
    "status": "Questionable",
    "practice_participation": "limited",
    "injury_type": "Achilles",
    "comment": "McCaffrey was a limited participant in practice Wednesday."
  }
}
```

> **This API is unofficial and unversioned.** ESPN does not document or guarantee it, so
> the parsing here is deliberately defensive: every field is looked for in more than one
> place, unrecognised payloads yield an empty list instead of a `500`, and the raw
> comment text is always passed through so you can see what was parsed from what. The
> shapes it handles were **not** verified against the live API — see
> [What was and wasn't verified](#what-was-and-wasnt-verified).

### `GET /weather/{week}` — Open-Meteo

Weather only matters for kickers and the deep passing game in open-air stadiums.
**Domes and fixed-roof venues short-circuit to `indoor: true` without any API call at
all** — 11 of the 32 stadiums, so that is a third of the requests saved every week.

Team → stadium coordinates and roof type live in `app/teams.py` as a static dictionary
(they only change when a team moves or opens a new building); `GET /stadiums` returns
it. The week's fixtures come from ESPN's scoreboard, and the forecast hour closest to
kickoff is picked from Open-Meteo's hourly series. A kickoff more than 12 hours outside
the returned window reports `available: false` rather than quietly returning the wrong
hour.

Each game gets a `fantasy_impact` block with a `severity` of `none`, `moderate` or
`high`, driven mostly by wind (the variable that actually moves field goals and deep
passing), plus precipitation, snow and cold:

```json
{
  "game": "DET @ KC", "indoor": false, "roof": "open",
  "weather": {
    "wind_mph": 22.5, "wind_gusts_mph": 31.0, "temperature_f": 72.0,
    "fantasy_impact": {
      "severity": "high", "affects_kickers": true,
      "notes": ["31 mph wind: meaningful drag on field goals and deep passing",
                "70% chance of precipitation"]
    }
  }
}
```

Forecasts are cached for 12h during the week and **1h once kickoff is within a day**,
when the forecast is actually worth re-checking.

### External sources in `/snapshot`

All four attach to `/snapshot` as opt-in blocks, so the default response stays the cheap
Sleeper-only payload:

```bash
curl -H "X-API-Key: $API_KEY" \
  "https://your-domain.example/snapshot?include=advanced_stats,odds,injury_report,weather"
```

They are fetched **concurrently**, and each lands under `external.<name>`. Advanced
stats and injuries are keyed by Sleeper player id so they line up with the roster
entries in the same response; `advanced_stats` also includes a
`biggest_role_changes` list, and `weather` an `outdoor_games_with_concerns` list.

**A failing source never fails the snapshot.** Each block reports its own state, so a
missing odds key or an ESPN outage costs you that block and nothing else:

```json
"external": {
  "advanced_stats": { "available": true,  "players": { "6794": { ... } } },
  "odds":           { "available": false, "error": "ODDS_API_KEY is not configured; ..." },
  "injury_report":  { "available": false, "error": "ESPN was unreachable for all 8 teams." },
  "weather":        { "available": true,  "games": [ ... ] }
}
```

An unknown `include` value is rejected with `400` and the list of valid ones rather than
being silently ignored.

## Authentication

Every endpoint except `/health` requires the shared secret in a header:

```bash
curl -H "X-API-Key: $API_KEY" https://your-domain.example/snapshot
```

The service **fails closed**: if `API_KEY` is not set in the environment, the protected
endpoints return `503` instead of serving data openly. Comparison is constant-time.
CORS is wide open by default, which is fine for a read-only API (the key is still
required).

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `LEAGUE_ID` | **yes** | — | Sleeper league id, from `sleeper.com/leagues/<LEAGUE_ID>` |
| `API_KEY` | **yes** | — | Shared secret expected in the `X-API-Key` header |
| `ODDS_API_KEY` | no | — | Free key from [the-odds-api.com](https://the-odds-api.com). Without it `/odds` returns `503`; everything else works |
| `PLAYERS_CACHE_PATH` | no | `data/players_cache.json` (`/data/players_cache.json` in Docker) | Where the player file is cached |
| `PLAYERS_CACHE_TTL_HOURS` | no | `20` | Refresh the player file only after this many hours |
| `CACHE_DIR` | no | the player cache's directory | Where the other sources' cache files live |
| `NFLVERSE_CACHE_TTL_HOURS` | no | `24` | How often the nflverse releases are re-pulled |
| `NFLVERSE_INCLUDE_RED_ZONE` | no | `true` | Set `false` to skip the ~98MB play-by-play download (loses only red zone usage) |
| `NFLVERSE_DOWNLOAD_TIMEOUT` | no | `300` | Timeout for the large nflverse downloads |
| `ODDS_CACHE_TTL_HOURS` | no | `24` | How long betting lines are cached (protects the free quota) |
| `ODDS_REGIONS` | no | `us` | The Odds API regions |
| `ODDS_BOOKMAKERS` | no | all in region | Restrict to specific books, e.g. `draftkings,fanduel` |
| `ESPN_CACHE_TTL_HOURS` | no | `3` | How long injury reports are cached |
| `WEATHER_CACHE_TTL_HOURS` | no | `12` | Forecast cache during the week |
| `WEATHER_GAMEDAY_CACHE_TTL_HOURS` | no | `1` | Forecast cache once kickoff is within a day |
| `SLEEPER_BASE_URL` | no | `https://api.sleeper.app/v1` | Sleeper API base URL |
| `HTTP_TIMEOUT` | no | `20` | Per-request timeout (seconds) for Sleeper calls |
| `PLAYERS_HTTP_TIMEOUT` | no | `120` | Timeout for the ~5MB player file download |
| `HTTP_MAX_RETRIES` | no | `3` | Attempts per Sleeper call (exponential backoff on 429/5xx/timeouts) |
| `CORS_ORIGINS` | no | `*` | Comma-separated allowed origins |
| `LOG_LEVEL` | no | `INFO` | Python log level |

Copy `.env.example` to `.env` and fill it in.

## Running it

### Docker Compose (the quick way)

```bash
cp .env.example .env
# edit .env: set LEAGUE_ID and a long random API_KEY
docker compose up --build
```

```bash
curl localhost:8000/health
curl -H "X-API-Key: <your key>" localhost:8000/snapshot | jq
```

### Locally, without Docker

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export LEAGUE_ID=1390746710426255360 API_KEY=dev-key
uvicorn main:app --reload
```

### Tests

Offline tests that exercise the resolution layer against fixture payloads (no network):

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Deploying on Coolify

1. **New Resource → Application → Docker Compose** (or *Dockerfile* if you prefer;
   both are in the repo) and point it at this Git repository, branch of your choice.
2. **Environment variables** — add at minimum:
   - `LEAGUE_ID` = your Sleeper league id
   - `API_KEY` = a long random string (`openssl rand -hex 32`)
   - `PLAYERS_CACHE_PATH` = `/data/players_cache.json`
   - `CACHE_DIR` = `/data`
   - `ODDS_API_KEY` = your free key from the-odds-api.com (optional; skip it and
     `/odds` returns `503` while every other endpoint works)
3. **Persistent storage** — add a volume mounted at `/data`. Without it the ~5MB player
   file is re-downloaded from Sleeper after every restart, and the nflverse aggregate is
   rebuilt from ~120MB of source files. The compose file already declares the
   `players-cache` volume if you deploy that way.
4. **Port** — the container listens on `8000`; Coolify's proxy maps it to your domain.
5. **Healthcheck** — `GET /health` (unauthenticated, no upstream calls). Already wired
   into both the Dockerfile and the compose file.
6. **Domain + HTTPS** — set your FQDN in Coolify and let it issue the certificate.
7. Deploy, then verify:
   ```bash
   curl https://your-domain.example/health
   curl -H "X-API-Key: <your key>" https://your-domain.example/league-settings
   curl -H "X-API-Key: <your key>" https://your-domain.example/injury-report?team=KC
   ```

To reuse the project for a different league later, change `LEAGUE_ID` and redeploy —
nothing about the league is hardcoded.

## What was and wasn't verified

Being straight about this, because two of these sources could not be reached from the
machine this was built on:

- **Verified against the live service.** nflverse: the column names, the `gsis_id` /
  `pfr_id` join, the red zone aggregation and the season fallback were all built and
  tested against the real release files. A cold build (four files, ~120MB) takes about
  8 seconds and produces a ~5MB cache.
- **Built from documented/observed shapes, not live-verified.** The Odds API, ESPN and
  Open-Meteo were unreachable from the build environment, so their parsing was written
  against their documented (Odds API, Open-Meteo) and community-observed (ESPN) response
  shapes and covered by offline tests using sample payloads. ESPN in particular is
  unofficial and unversioned, so **its parsing is the most likely thing to need a tweak
  on first contact with the live API** — it is written to degrade to empty results
  rather than error, and the raw comment text is always passed through.

The first real call to `/injury-report?team=KC` is the one worth eyeballing: if
`injuries` comes back empty while ESPN's site shows a report, the envelope shape moved
and `parse_injuries` in `app/espn.py` needs a new key added to `_candidate_lists`.

## How it behaves against Sleeper

- **Player file**: fetched at most once per `PLAYERS_CACHE_TTL_HOURS` (20h by default),
  stored on disk with a timestamp, trimmed to the fields actually served, and written
  atomically. On startup it is loaded from disk without touching the network; the refresh
  happens lazily on the first request that needs it, guarded by a lock so concurrent
  requests never trigger two downloads. If the refresh fails but a cached copy exists,
  the cached copy is served and a warning is logged rather than failing the request.
- **Everything else** is fetched live per request — no database. `/snapshot` issues its
  calls concurrently (`asyncio.gather`), so a full snapshot is a handful of parallel
  requests.
- **Transactions** are per-week in Sleeper, so the `days` window is translated into the
  weeks that could contain it and then filtered by timestamp.
- **Errors**: timeouts and `429`/`5xx` responses are retried with exponential backoff,
  then surfaced as `504`; other upstream failures become `502`. Sleeper's ~1000 req/min
  limit is never close to being hit by this service.

## How it behaves against the external sources

- **Every source has its own cache file and TTL**, keyed inside the file (by season,
  week or team) so one stale key never forces a full refresh. Concurrent requests for
  the same key share a single refresh.
- **A failed refresh serves the stale copy** with a `refresh_failed` note in the
  response's `cache` block, rather than failing the request. Only a failure with nothing
  cached at all propagates.
- **Cache files are versioned.** Changing what gets stored bumps a schema version, and a
  file written by an older build is discarded and refetched instead of being served with
  missing fields. (Adding `gsis_id`/`espn_id` to the Sleeper player cache did exactly
  this, so the first request after deploying re-pulls the player file once.)
- **Quota discipline**: The Odds API is the only metered source. Its 24h cache means at
  most ~31 calls a month against a ~500 call allowance, and `quota` in the response
  shows where you stand.
- **Large downloads are streamed to a temp file, parsed off the event loop** with
  `asyncio.to_thread`, and deleted. Nothing large is ever held in memory or left on
  disk — the nflverse aggregate on disk is ~5MB, down from ~120MB of source files.

## Project layout

```
main.py                 FastAPI app, routes, CORS, lifespan
app/config.py           Environment-driven settings
app/security.py         X-API-Key dependency (fails closed)
app/http.py             Shared retry/timeout helpers + streaming downloads
app/cache.py            Keyed disk caches with per-source TTLs
app/sleeper.py          Async Sleeper client: timeouts, retries, error mapping
app/players.py          Disk-backed player cache + ID resolution + cross-source ids
app/humanize.py         Sleeper setting/scoring keys -> plain language
app/services.py         Roster, matchup, transaction and snapshot assembly
app/nflverse.py         nflverse releases: download, join on gsis_id, aggregate
app/odds.py             The Odds API: consensus lines and game script
app/espn.py             ESPN injuries and schedule (defensive parsing)
app/weather.py          Open-Meteo forecasts, domes short-circuited
app/teams.py            Static stadium coordinates, roof types, name aliases
app/enrichment.py       The optional ?include= blocks on /snapshot
tests/                  Offline tests against fixture payloads
Dockerfile              Multi-stage build, non-root, healthcheck
docker-compose.yml      Example deployment with a persistent cache volume
```

## Not included (by design)

User authentication, multi-league support, a frontend, or any write access to Sleeper or
to any of the external sources.
