# Sleeper Fantasy League API

Read-only FastAPI service that sits between a [Sleeper](https://sleeper.com) fantasy
football league and an external client (Claude, a script, whatever), exposing the whole
league behind a handful of fixed URLs with **player IDs already resolved to names**,
positions, NFL teams and injury status.

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
| `GET` | `/docs` | none (schema only) | Interactive OpenAPI docs |

### `GET /snapshot`

Query params:

- `week` (optional, 1-22) — week to pull matchups for. Defaults to the current NFL week
  from `/v1/state/nfl`.
- `days` (optional, 1-120, default `7`) — how far back to include transactions.

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
| `PLAYERS_CACHE_PATH` | no | `data/players_cache.json` (`/data/players_cache.json` in Docker) | Where the player file is cached |
| `PLAYERS_CACHE_TTL_HOURS` | no | `20` | Refresh the player file only after this many hours |
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
3. **Persistent storage** — add a volume mounted at `/data`. Without it the ~5MB player
   file is re-downloaded from Sleeper after every restart. The compose file already
   declares the `players-cache` volume if you deploy that way.
4. **Port** — the container listens on `8000`; Coolify's proxy maps it to your domain.
5. **Healthcheck** — `GET /health` (unauthenticated, no upstream calls). Already wired
   into both the Dockerfile and the compose file.
6. **Domain + HTTPS** — set your FQDN in Coolify and let it issue the certificate.
7. Deploy, then verify:
   ```bash
   curl https://your-domain.example/health
   curl -H "X-API-Key: <your key>" https://your-domain.example/league-settings
   ```

To reuse the project for a different league later, change `LEAGUE_ID` and redeploy —
nothing about the league is hardcoded.

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

## Project layout

```
main.py                 FastAPI app, routes, CORS, lifespan
app/config.py           Environment-driven settings
app/security.py         X-API-Key dependency (fails closed)
app/sleeper.py          Async Sleeper client: timeouts, retries, error mapping
app/players.py          Disk-backed player cache + ID resolution
app/humanize.py         Sleeper setting/scoring keys -> plain language
app/services.py         Roster, matchup, transaction and snapshot assembly
tests/                  Offline tests against fixture payloads
Dockerfile              Multi-stage build, non-root, healthcheck
docker-compose.yml      Example deployment with a persistent cache volume
```

## Not included (by design)

User authentication, multi-league support, a frontend, or any write access to Sleeper.
