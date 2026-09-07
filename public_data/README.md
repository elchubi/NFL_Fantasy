# Sleeper Fantasy — Public Data Service

Shared NFL data reused by every league backend: player names, advanced usage stats,
betting lines, injury reports, stadium weather, and the rookie draft board. **Nothing
here depends on which fantasy league is asking.**

Deploy this **once**. Every league backend (see the root project's README) points at
it over Railway's private network with `PUBLIC_DATA_URL` + `PUBLIC_DATA_API_KEY`. Adding
a second or third league costs nothing extra here — they all share this one instance,
its caches, and its Odds API quota.

## Why this exists

A league backend needs player names, snap counts, betting lines and injury reports to
answer fantasy questions — but none of that data is specific to *a* league. Running it
once per league would mean: downloading nflverse's ~120MB of source files N times,
burning The Odds API's ~500/month free quota N times faster, and hitting Sleeper's
player file N times instead of once. This service exists so "public" and "per-league"
data are stored exactly once each, matching what each actually is.

## Endpoints

Same shapes as the league backend's own `/advanced-stats`, `/odds`, `/injury-report*`,
`/weather/{week}`, `/stadiums`, `/draft-class/{season}`, `/prospect/{player_id}`,
`/capture`, `/history`, `/history/{source}` — see the root project's README for the full
description of each; the behavior is identical, just served from here.

One endpoint is new and exists only for the league backends to call:

| Method | Endpoint | What it does |
| --- | --- | --- |
| `GET` | `/players` | The full trimmed Sleeper player map. A league backend calls this to hydrate its own local player cache instead of hitting Sleeper's player file directly. |
| `GET` | `/byes/{season}` | Bye week per NFL team, derived from the nflverse schedule. A league backend's `/pressure` endpoint calls this to spot colliding byes. |

## Environment variables

Same provider variables as the root project's `.env.example` (`ODDS_API_KEY`,
`NFLVERSE_*`, `ESPN_*`, `WEATHER_*`), minus everything league-specific (no `LEAGUE_ID`,
no per-league database tables). See `.env.example` here for the full list.

## Deploying

Same Docker/Railway pattern as the other two services in this repo — see the root
README's "Deploying on Railway" section, which covers this service as the shared third
piece: **no public domain needed here either**, unless you want to debug it directly.
Each league backend reaches it over Railway's private network, the same way the MCP
server reaches its league backend.

```bash
docker compose up --build
curl -H "X-API-Key: $API_KEY" localhost:8000/stadiums
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```
