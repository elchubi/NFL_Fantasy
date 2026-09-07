# Sleeper Fantasy League — MCP server

An [MCP](https://modelcontextprotocol.io) server that wraps the league REST backend and
exposes it to Claude.ai as a **remote custom connector**. It holds no data of its own:
every tool is a thin, typed wrapper over one backend endpoint.

Transport is **Streamable HTTP**, which is what a remote connector on Claude.ai expects.
It has to be reachable from the public internet — Claude connects from Anthropic's
infrastructure, not from your machine, so a VPN or a firewalled host will not work.

## Setup

### 1. Generate the URL token

The server has no authentication of its own. It is protected by living at an unguessable
path, so generate one long random segment and keep it:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# e.g. 8Kq2vN7xTfR3wLpZ9mB4hJ6yC1sD0aG5eU8iO2nX4tQ
```

Generate it **once** and keep it stable — changing it changes the connector URL, and you
would have to re-add the connector in Claude.ai.

### 2. Configure and run

```bash
cp .env.example .env
# set BACKEND_URL, BACKEND_API_KEY, MCP_URL_TOKEN and MCP_ALLOWED_HOSTS
docker compose up --build
```

```bash
curl localhost:8080/healthz
```

### 3. Add it to Claude.ai

Claude.ai → **Customize → Connectors → Add custom connector**, and paste:

```
https://mcp.tudominio.com/<MCP_URL_TOKEN>/mcp
```

The `/mcp` suffix matters — that is the Streamable HTTP endpoint. Treat the whole URL as
a secret: anyone holding it has full access to the connector.

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `BACKEND_URL` | **yes** | `http://localhost:8000` | Where the REST backend lives. On Railway use the backend's private domain. |
| `BACKEND_API_KEY` | **yes** | — | The backend's `API_KEY`. Stays server-side; the Claude client never sees it. |
| `MCP_URL_TOKEN` | **yes in production** | — | The secret path segment. Without it the server sits at `/mcp` and logs a warning. |
| `MCP_ALLOWED_HOSTS` | strongly recommended | — | Comma-separated public hostnames. See below. |
| `BACKEND_TIMEOUT` | no | `60` | Per-request timeout against the backend. `league_snapshot` with `include=` can be slow on a cold cache. |
| `PORT` | no | `8080` | Port the container listens on. |
| `LOG_LEVEL` | no | `INFO` | Python log level. |

### About `MCP_ALLOWED_HOSTS`

The SDK enables DNS-rebinding protection by default, and it matches the `Host` header
**exactly** — there is no `*` wildcard, and passing `["*"]` does not work. A public
deployment with the default settings answers **421 Misdirected Request** to every
request, which looks exactly like a broken connector.

So set it to your real hostname:

```
MCP_ALLOWED_HOSTS=mcp.tudominio.com
```

The server also registers `mcp.tudominio.com:*` automatically, since some reverse
proxies pass the port through. Left empty, protection is switched off and a warning is
logged at startup — the container still works, and the URL token is what actually guards
it, but listing the host costs nothing.

## Deploying on Railway

This is the **second of two services** in one Railway project — the REST backend is the
other. Railway builds one service per deployment, so they do not deploy together as a
unit; they share the repo and are wired to each other through Railway's private network.

1. In the project that already holds the backend, **New → GitHub Repo**, pick the same
   repo again.
2. **Settings → Source → Root Directory**: `/mcp_server`
3. **Settings → Source → Watch Paths**: `/mcp_server/**`, so a backend-only commit does
   not rebuild this service.
4. **Variables**:
   - `BACKEND_URL` = `http://${{backend.RAILWAY_PRIVATE_DOMAIN}}:${{backend.PORT}}`,
     with your backend service's actual name. Private traffic never leaves Railway, and
     the backend then needs no public domain at all.
   - `BACKEND_API_KEY` = `${{backend.API_KEY}}` — a reference variable, so rotating the
     key on the backend updates this automatically.
   - `MCP_URL_TOKEN` — the secret path segment.
   - `MCP_ALLOWED_HOSTS` — your public MCP hostname.
   - `HOST=0.0.0.0` — **required**, confirmed on a live deploy. `entrypoint.py` defaults
     to `::` (IPv6-only), which is what the backend and public-data need for Railway's
     private network, but the public-domain edge proxy could not reach this service with
     that default: the container came up cleanly (`Uvicorn running on http://[::]:8080`,
     every startup log healthy) while the public URL answered a bare `502 Application
     failed to respond`. This is the only one of the three services with a public domain,
     so it is the only one that needs this set.
5. **Networking → Generate Domain**, then set a custom one if you want. This service
   *must* be publicly reachable: Claude connects from Anthropic's infrastructure, not
   from your machine.
6. **Healthcheck** — `railway.json` already sets `/healthz`. It sits outside the token
   path so the platform can probe it without holding the secret, and it deliberately does
   **not** call the backend, so it stays green if the backend is down. The `health_check`
   tool is what proves the two services can actually talk.

`docker-compose.yml` is ignored by Railway; it stays for local development.

### Two Railway specifics worth knowing

**Railway assigns the port.** It injects `PORT` and expects the process to bind it. A
hardcoded port builds and starts cleanly and then fails its healthcheck forever, which is
a confusing way to lose an afternoon. `entrypoint.py` reads `PORT`.

**Private networking is IPv6-only.** A process bound to `0.0.0.0` cannot be reached at
`<service>.railway.internal`; the caller just times out with nothing in either log.
`entrypoint.py` binds `::`, which also accepts IPv4 on a dual-stack host, and falls back
to `0.0.0.0` where there is no IPv6 stack, so the same image runs locally too. Set `HOST`
to override.

If you would rather not use private networking, point `BACKEND_URL` at the backend's
public URL instead — it works, it just sends the traffic out and back.

## The tools

31 tools, one per backend endpoint. `/docs` is not exposed — it is only the OpenAPI
reference and nothing useful to a model.

### League

| Tool | What it does |
| --- | --- |
| `league_list` | The leagues this backend serves. Call this first if a `league` slug is unknown. |
| `league_snapshot` | The whole league: teams, rosters split into starters/bench/IR, standings, matchups and transactions, with player names resolved. The starting point for most questions. |
| `league_settings` | Scoring, lineup slots, playoff format, trade deadline and waiver rules in plain language. |
| `league_roster` | One team's roster. Flexible search across username, display name and team name. |
| `waivers_available` | Free agents ranked by recent role trend and points under this league's own scoring rules. |
| `schedule_difficulty` | For a roster's skill players, how stingy their next few opponents have been at that position. |
| `manager_schedule` | Who a manager plays every week of the regular season - the fantasy matchup pairing itself, not a strength read. |

### External sources

| Tool | What it does |
| --- | --- |
| `stats_advanced` | Snap share, target share, air yards, red zone touches and EPA for one player, with the last-three-week trend against the season. |
| `stats_odds` | Spread, total, moneyline and implied team totals for a week — the game-script signal. |
| `stats_injury_report_team` | A whole team's injuries from ESPN, with practice participation. |
| `stats_injury_report_player` | One player's injury detail, shown against what Sleeper has cached. |
| `stats_weather` | Kickoff weather per stadium, with domes short-circuited. |
| `stats_stadiums` | Coordinates and roof type for all 32 stadiums. |

### League-specific edge

| Tool | What it does |
| --- | --- |
| `manager_list` | Every manager's FAAB habits, activity, draft tendencies and trade history, across every archived season. |
| `manager_profile` | One manager, read against the field. |
| `manager_pressure` | Which teams are forced to act — bye collisions, stacked injuries, no cover. |
| `manager_playoff_odds` | Monte Carlo playoff odds per team from its own scoring history, plus a buyer/bubble/seller read. |
| `manager_trade_fits` | Your thin positions crossed against every other team's surplus there, weighted by playoff odds and trade history. |
| `manager_faab_bid` | A bid recommendation anchored to the league's own bidding history and remaining budgets. |
| `manager_briefing` | A weekly digest: injury disagreements, upcoming byes, thin positions, trending free agents and weather concerns. |
| `decision_log` | Record a decision and its reasoning. Append-only. |
| `decision_log_outcome` | Record how a logged decision turned out. Append-only. |
| `decision_list` | Read the decision log with outcomes. |

### Rookie draft

| Tool | What it does |
| --- | --- |
| `draft_class` | The board for a draft class: capital, age, combine and landing spot. |
| `draft_prospect` | One prospect. Accepts a Sleeper id or a `gsis_id`. |

### History and health

| Tool | What it does |
| --- | --- |
| `history_backfill` | Walk the season chain and archive every season's transactions and drafts. Run once after deploying. Safe to repeat. |
| `history_seasons` | The league's seasons, or what a backfill would pick up. |
| `history_capture` | Archive the week's lines and injury reports. Safe to repeat. |
| `history_inventory` | What the archive holds, per source and season. |
| `history_source` | Read archived rows for `odds`, `injuries` or `decisions`. |
| `health_check` | Whether the backend is up and how fresh its caches are. |

### Annotations

Every tool declares what it does to state, so any MCP client can tell them apart:

- **All the `GET` tools** are `readOnlyHint: true`.
- **`decision_log` and `decision_log_outcome`** are `readOnlyHint: false` with
  `destructiveHint: false` — they only ever append a row, and can never edit or delete
  one. An outcome is layered on top of the original entry rather than replacing it.
- **`history_capture` and `history_backfill`** are `readOnlyHint: false` with
  `idempotentHint: true` — capture skips rows identical to the last recorded state, and
  backfill skips seasons already archived, so calling either twice in a row records
  nothing the second time.

## How errors reach the model

The backend's error messages are written to be acted on — *"Sleeper has no gsis_id for
this player, so there is nothing to join"*, *"matches more than one team, here are the
candidates"* — so they are passed through as `ToolError`, which puts the text in front of
the model. Letting the exception propagate untouched would report a bare *"Error
executing tool `stats_advanced`"* and lose the part worth reading, usually the part the
model needs to fix its own call.

## Authentication, and what it is not

This version has **no authentication of its own**. The URL token is the only thing
standing between the internet and your league data, and it travels in the URL — in
Claude.ai's connector settings, in your `.env`, in Railway's variables. That is an
acceptable trade for a single-user server and nothing more.

Concretely: anyone with the URL can read your league and write to your decision log.
There is no way to revoke access for one person while keeping it for another, and no
audit trail of who called what.

**If you ever want to share this with other managers in the league, migrate to
OAuth 2.1** — that is what the MCP specification calls for on remote servers, and what
gives you per-user identity and revocable access. Do not simply hand out the URL. The
SDK supports it: `MCPServer` takes `auth`, `auth_server_provider` and `token_verifier`.
Not implemented here on purpose, because it is real complexity that buys nothing for a
server with one user.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Offline throughout — the backend is a mock transport, and the protocol tests drive the
real Streamable HTTP app: tool listing, annotations, argument schemas and defaults, error
propagation, the token path, and the Host header check.

## Project layout

```
server.py            The MCP server: 31 tools, annotations, transport wiring
backend.py           HTTP client for the REST backend; holds BACKEND_API_KEY
entrypoint.py        Binds the platform's PORT, and IPv6 for private networking
railway.json         Railway build and healthcheck config
Dockerfile           Multi-stage build, non-root, healthcheck
docker-compose.yml   Example deployment
tests/               Offline tests, including protocol-level ones
```
