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

## Architecture: three services, not one

This repo is actually **three independently deployable services**, split along one line:
is the data specific to *this* fantasy league, or is it the same regardless of who is
asking?

| Service | Directory | Holds | Deployed |
| --- | --- | --- | --- |
| **Public data** | `public_data/` | Player names, advanced stats, betting lines, injury reports, weather, the draft board. Not specific to any league. | **Once** |
| **League backend** | `/` (this directory) | Rosters, matchups, transactions, standings, manager profiles, decisions, one SQLite database per configured league. | **Once**, serving every league in `LEAGUES` |
| **MCP connector** | `mcp_server/` | Nothing - a thin wrapper exposing the league backend's API as tools for Claude.ai. | **Once**, serving every league the backend does |

Player data, snap counts, betting lines and injury reports are the same no matter which
league is asking about them. Computing and caching them separately per league would mean
downloading nflverse's ~120MB of source files once per league, burning through The Odds
API's ~500/month free quota that much faster, and hitting Sleeper's player file more than
the once-a-day it asks for. The public-data service exists so each of those is paid for
exactly once, and every league backend - one, two, or a dozen - shares the result.

Concretely: the league backend never talks to nflverse, The Odds API, ESPN or Open-Meteo
directly any more. Its `/advanced-stats`, `/odds`, `/injury-report*`, `/weather`,
`/draft-class`, `/prospect` endpoints are still there, at the same paths, with the same
behavior - they just proxy to the shared service over Railway's private network instead
of computing it locally. Nothing about calling this API from the outside changes; see
[public_data/README.md](public_data/README.md) for that service on its own, and
[Deploying on Railway](#deploying-on-railway) below for how the three fit together and
how to add a second league.

**One league backend and one MCP connector can serve any number of leagues.** `LEAGUES`
is a comma-separated `slug:sleeper_league_id` list; each slug gets its own SQLite database
and its own `/leagues/{slug}/...` URL prefix, so leagues stay genuinely separate on disk
and in the API even though they share a process, an `API_KEY` and a running container. Add
a league by adding a `slug:id` pair and redeploying - no new service. See
[Adding another league](#adding-another-league) below.

## Endpoints

Base URL is wherever you deployed it. Every endpoint except `/health` requires
`X-API-Key: <API_KEY>`. `/docs` serves the interactive OpenAPI schema.

This backend can serve several leagues at once (see [Architecture](#architecture-three-services-not-one)).
Anything about one specific league lives under `/leagues/{league}/...`, where `league` is a
slug from `GET /leagues`; a handful of endpoints are not about any one league (player-name
resolution, the external sources, the draft board) and stay at a plain path.

### League (Sleeper)

| Method | Endpoint | Params | What it does |
| --- | --- | --- | --- |
| `GET` | `/health` | — | Liveness probe for the platform. No auth, no upstream calls; reports cache state and every configured league slug. |
| `GET` | `/leagues` | — | Which league slugs this backend serves, each with its Sleeper league id. Start here when there is more than one. |
| `GET` | `/leagues/{league}/snapshot` | `week`, `days=7`, `include` | The whole league: teams, rosters (starters by slot / bench / IR), standings, matchups and transactions, with player IDs resolved to names. `include=advanced_stats,odds,injury_report,weather` attaches the external sources. |
| `GET` | `/leagues/{league}/league-settings` | — | League configuration in plain language: scoring, roster slots, playoff format, trade deadline, waiver rules. |
| `GET` | `/leagues/{league}/roster/{manager}` | `manager` | One resolved roster. Flexible lookup by username, display name or team name (exact → prefix → substring). `404` lists the teams, `409` the candidates when ambiguous. |

### External sources

| Method | Endpoint | Params | What it does |
| --- | --- | --- | --- |
| `GET` | `/advanced-stats/{player_id}` | `player_id`, `season` | nflverse: snap %, target share, air yards, red zone touches, EPA. Season average, last-three-week average and the delta — the earliest read on a role change. |
| `GET` | `/odds/{week}` | `week`, `season` | The Odds API: spread, total, moneyline, favourite, implied team totals and a game-script note. Consensus is the median across books. Passes through your remaining quota. |
| `GET` | `/injury-report` | `team` **(required)** | ESPN and nflverse, side by side: a team's full injury report with practice participation (full / limited / did_not_practice). |
| `GET` | `/injury-report/{player_id}` | `player_id` | ESPN and nflverse for one player, next to what Sleeper has cached, so you can see when they disagree. |
| `GET` | `/weather/{week}` | `week`, `season` | Open-Meteo: forecast at the kickoff hour per stadium. Domes return `indoor: true` without any API call. |
| `GET` | `/stadiums` | — | The static reference: coordinates and roof type for all 32 stadiums. |
| `POST` | `/position-points/{position}` | `position`, `season`; body `scoring_settings` **(required)** | Every player at a position, scored week by week under a league's own rules instead of nflverse's fixed PPR column. |
| `POST` | `/points-allowed/{position}` | `position`, `season`; body `scoring_settings` **(required)** | Every NFL defense's fantasy points allowed to a position - strength of schedule for a fantasy roster, not a real-world defensive rank. |
| `GET` | `/schedule/{season}` | `season` | Every team's opponent, week by week - pairs with `/points-allowed` for a per-player schedule-difficulty view. |

Every endpoint in this table is a thin proxy to the shared `public_data/` service (see
[Architecture](#architecture-three-services-not-one)) - same path, same params, same
response, computed once there and reused by every league.

### League-specific edge

| Method | Endpoint | Params | What it does |
| --- | --- | --- | --- |
| `GET` | `/leagues/{league}/managers` | `seasons`, `days` | Every manager's profile: FAAB behaviour (typical bid, max ever, win rate on contested claims), which day they move, activity, draft tendencies by position and round, trade partners. Plus `league_context` to read one against the field. |
| `GET` | `/leagues/{league}/manager/{name}` | `name`, `seasons`, `days` | One manager, with the league context. |
| `GET` | `/leagues/{league}/pressure` | `week`, `horizon=3` | Who is forced to act: bye-week collisions, stacked injuries, positions with no cover. Ranked by urgency. |
| `GET` | `/leagues/{league}/available` | `position`, `limit=25`, `season` | Free agents ranked by recent role trend and points under this league's own `scoring_settings` - not nflverse's generic PPR column. |
| `GET` | `/leagues/{league}/schedule-difficulty/{manager}` | `manager`, `weeks_ahead=4`, `season` | For each of a roster's QB/RB/WR/TE, how many fantasy points its next opponents have allowed at that position. |
| `GET` | `/leagues/{league}/playoff-odds` | `trials=3000` | Monte Carlo playoff odds per team from each team's own scoring history, plus a buyer/bubble/seller read. |
| `GET` | `/leagues/{league}/trade-fits/{manager}` | `manager` | Your thin positions crossed against every other team's surplus there, weighted by their playoff odds and trade history with you. |
| `GET` | `/leagues/{league}/faab-bid/{manager}` | `manager`, `player_id`, `confidence=medium` | A bid recommendation anchored to the league's own bidding history and remaining budgets. |
| `GET` | `/leagues/{league}/briefing/{manager}` | `manager`, `week` | One weekly digest: injury disagreements, upcoming byes, thin positions, trending free agents and weather concerns for your roster. |
| `POST` | `/leagues/{league}/decision` | `kind`, `summary` **(required)**; `reasoning`, `players_involved`, `confidence`, `expected`, `week`, `season` | Log a decision and its reasoning, at the moment you make it. |
| `POST` | `/leagues/{league}/decision/{decision_id}/outcome` | `decision_id`, `outcome` **(required)**; `season` | Record how it turned out. Appended, never edited — the original reasoning stays intact. |
| `GET` | `/leagues/{league}/decisions` | `season`, `week`, `kind`, `pending_only=false` | Read the decision log with outcomes. |

### Rookie draft

| Method | Endpoint | Params | What it does |
| --- | --- | --- | --- |
| `GET` | `/draft-class/{season}` | `season`; `position`, `round_max`, `landing=true` | The full board: draft capital, age, combine measurables and landing spot (how much work vacated at that position on that team). |
| `GET` | `/prospect/{player_id}` | `player_id`, `season` | One prospect's profile. Accepts a Sleeper id or a `gsis_id`. |

### History

| Method | Endpoint | Params | What it does |
| --- | --- | --- | --- |
| `POST` | `/leagues/{league}/backfill` | `refresh=false`, `limit=20` | Walk `previous_league_id` and archive every season's transactions, draft picks and managers. Run once after deploying, then when a season ends. |
| `GET` | `/leagues/{league}/seasons` | `discover=false` | The league's season chain. `discover=true` follows it against Sleeper instead of reading the archive. |
| `POST` | `/capture` | `week`, `season`, `teams`, `refresh=true` | Archive the week's betting lines and injury reports. Not per-league: meant for the cron; rows identical to the last recorded state are skipped. |
| `GET` | `/leagues/{league}/history` | — | Inventory: rows, weeks and file size per source and season, for this league plus the shared odds/injury archive. |
| `GET` | `/leagues/{league}/history/{source}` | `source`; `season`, `week`, `limit` | Read archived rows. `source` is `odds`, `injuries` or `decisions`. The league slug is required in the path even for `odds`/`injuries`, which are shared across leagues. |

Two things worth knowing before you use these:

- Only `odds` and `injuries` are archived. nflverse, Sleeper and Open-Meteo keep their own
  history upstream and are re-fetched on demand.
- The read endpoints archive whatever they pull fresh from upstream automatically (never
  on a cache hit). `HISTORY_AUTO_CAPTURE=false` turns that off.

## The league endpoints in detail

### `GET /leagues/{league}/snapshot`

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

### `GET /leagues/{league}/league-settings`

Scoring, roster slots, playoff format, trade deadline and waiver rules as clean JSON —
`0.04` becomes *"Points per passing yard"*, `waiver_type` + `waiver_budget` become
*"FAAB blind bidding ($100 season budget)"*, and so on. Scoring keys are grouped
(`passing`, `rushing`, `receiving`, `kicking`, `defense_special_teams`, `bonuses`, …) and
the untouched `raw` settings are included too. Unknown or newly added Sleeper keys are
never dropped — they get a generated description and land in the `other` group.

### `GET /leagues/{league}/roster/{manager}`

Flexible, case-insensitive lookup against username, display name, team name and
co-owners. Exact match wins, then prefix, then substring — so `/leagues/main/roster/tacos`
finds *Los Tacos Voladores*.

- `404` if nothing matches, with the list of available teams in the response
- `409` if the query is ambiguous, with the candidates

## External data sources

Four free sources sit behind the endpoints above. Only one of them needs an API key;
the rest work out of the box. Each has its own disk cache and refresh cadence, chosen
around how fast the underlying data actually moves.

**All four now live in `public_data/`, not in this codebase** - see
[Architecture](#architecture-three-services-not-one). This section documents the
behavior, which is unchanged from the outside; `ODDS_API_KEY` and the cache-TTL
variables mentioned below belong to that service's environment now (see
`public_data/.env.example`), not this one's.

| Source | Key needed | Cache TTL | What it adds |
| --- | --- | --- | --- |
| [nflverse](https://github.com/nflverse/nflverse-data) | no | 24h | Snap %, target share, air yards, red zone touches, EPA |
| [The Odds API](https://the-odds-api.com) | **yes** (`ODDS_API_KEY`) | 24h | Spread, total, moneyline, favourite |
| [ESPN](https://site.api.espn.com) | no | 3h | Injury status + practice participation |
| [Open-Meteo](https://open-meteo.com) | no | 12h / 1h on game day | Wind, temperature, precipitation at the stadium |

### `GET /advanced-stats/{player_id}` — nflverse

`player_id` is the Sleeper id that appears in `/leagues/{league}/snapshot` rosters. Three nflverse
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
  "https://your-domain.example/leagues/main/snapshot?include=advanced_stats,odds,injury_report,weather"
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

## Rookie draft board

**Also served by `public_data/`** - the draft board is the same for every league, so it
lives with the other shared sources.

In a keeper league with one keeper you never stash prospects, so a rookie only matters
if he produces **in his rookie season**. That narrows what is worth tracking a great
deal. Roughly in order of how well they predict rookie-season fantasy production:

| Signal | Where it comes from |
| --- | --- |
| **Draft capital** (round and pick) — the strongest single predictor, especially at RB | nflverse `draft_picks` |
| **Age at draft** — younger is better, especially at WR | same file |
| **Landing spot** — how much work actually vacated ahead of him | computed from the snap counts this service already pulls |
| College market share / breakout age | *not included — see below* |
| **Athletic testing** | nflverse `combine` |

Four of the five come from two small nflverse files (~2.5MB together, no API key) plus
data already on disk. The fifth is the only one that needs a college data source, and it
is the weakest of the five, so **no NCAA source is used at all**. Pulling hundreds of
megabytes of college play-by-play to answer something draft capital already answers
better would be starting from the wrong end.

`draft_picks` carries `gsis_id`, so every prospect lines up with the Sleeper rosters in
`/leagues/{league}/snapshot` through the same index `/advanced-stats` uses.

### Landing spot

This is the part nothing else gives you, and it is the most actionable thing on draft
day: a mid-round back walking into an empty backfield is worth more than a higher pick
stuck behind a healthy starter.

For each prospect it takes every player who logged snaps at that position for the
drafting team last season, then checks each one's **current** team in the Sleeper player
file. Anyone whose Sleeper team is no longer the drafting team has left, and their snaps
are vacated. Both halves are data the service already holds.

```json
"landing_spot": {
  "team": "NYJ", "position": "QB", "prior_season": 2025,
  "vacated_share": 1.0,
  "vacated_snap_points": 2.55,
  "returning_snap_points": 0.0,
  "incumbents": [
    { "name": "Brady Cook",  "prior_snap_pct": 0.98, "current_team": "MIA", "still_on_team": false },
    { "name": "Justin Fields","prior_snap_pct": 0.90, "current_team": "KC",  "still_on_team": false }
  ],
  "opportunity": "wide open at QB: 100% of last season's QB snap workload left the team; nobody established returns at the position."
}
```

`vacated_share` is a **proportion of the position's snap workload**, not a sum of
percentages. Three receivers are on the field at once, so their individual snap
percentages add up well past 100% and summing them would be meaningless — an early
version of this reported landing spots at "255% vacated". The raw sums are still
exposed as `*_snap_points` for transparency.

### Usage

```bash
# The whole class, best openings first
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/draft-class/2026"

# Just the running backs taken in the first three rounds
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/draft-class/2026?position=RB&round_max=3"

# One player, by Sleeper id or gsis_id
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/prospect/00-0041027"
```

`?landing=false` skips the landing-spot computation, which is the only part that needs
last season's snap data.

### If you ever want the college half

The missing signal is college market share (dominator rating) and breakout age, from
[collegefootballdata.com](https://collegefootballdata.com) — free, needs a key. One call
a year for ~80 prospects, and a finished college season never changes, so that cache
would need no TTL at all.

The catch: nflverse's `cfb_player_id` is a College Football Reference slug
(`jeremiyah-love-1`), **not** a CFBD id, so the join is name + school + year rather than
a key lookup. Expect a handful of prospects a year to need checking by hand.

## Where the actual edge is

Everyone in the league can ask an AI about players. Stats, projections and rankings are
table stakes — public, aggregated, and available to all twelve of you. An edge has to
come from something the others structurally cannot get.

All of what follows is built here, none of it public.

**Be honest about the size of it.** Fantasy football is dominated by variance and draft
luck. None of this wins you the league. What it does is tilt marginal decisions — how
much to bid, who to approach for a trade, who to sit in a close call — and those compound
over a season. The asymmetry is in the cost: an afternoon of work over data you already
pull, against opponents who will never do this.

### `GET /leagues/{league}/managers` and `/leagues/{league}/manager/{name}` — your opponents' habits

A general fantasy tool has no idea who else is in your league. You play the same eleven
people for years, their habits sit in Sleeper's transaction and draft history, and they
almost certainly have never looked.

Per manager:

| What | Why it matters |
| --- | --- |
| **FAAB behaviour** — typical bid, max ever, win rate on contested claims, share of budget | Somebody who has never bid above $12 loses to $13, not $40 |
| **Bid timing** — which day they move | Whether they claim early or wait for the deadline |
| **Injury reaction** — median days from injury report to drop | That gap is your buy-low window |
| **Activity** — moves, drops, failed claims | A manager who barely touches waivers is free talent and the natural trade target |
| **Draft tendencies** — first position taken, average round per position | Predicts what disappears before your next pick |
| **Trade partners** — who they have actually traded with | Who answers, and who never does |

`league_context` puts one manager against the field: the league's median max bid, and who
the least and most active managers are.

Injury reaction needs the archived injury history to exist — it matches drops against
when a player first appeared on the injury report. Until the archive has some weeks in
it, that one field says so rather than reporting a misleading zero.

### `GET /leagues/{league}/pressure` — who has to move before you do

A manager whose only two startable running backs share a bye week has to act, whether he
has worked that out yet or not. Knowing before he does changes what you can ask for.

For each team, over the next few weeks (`?horizon=`, default 3), it tries to actually
fill the lineup from healthy, non-bye players and reports what it cannot fill:

```json
{
  "team_name": "Gridiron Goats",
  "pressure_score": 4,
  "headline": "cannot fill RB, FLEX in week 6; short at WR.",
  "by_week": [
    { "week": 6, "players_on_bye": ["Christian McCaffrey", "Travis Kelce"],
      "shortfalls": [{"slot": "RB", "eligible_positions": ["RB"]}],
      "can_field_a_lineup": false }
  ],
  "thin_positions": [{"position": "TE", "healthy": 1, "starters_required": 1, "spare": 0}]
}
```

Two details that keep the score meaningful:

- **Dedicated slots are filled before flex slots.** A flex can be covered by three
  positions and a WR slot cannot, so filling greedily in the other order would invent
  shortfalls that do not exist.
- **Kickers and defenses never count as thin.** Carrying exactly one of each is correct
  roster construction and a replacement is always free on waivers. Counting them would
  flag all twelve teams and tell you nothing. Positions with exactly enough bodies are
  still *reported* under `thin_positions`, they just do not raise the score — only a real
  shortfall does.

Bye weeks are derived from the nflverse schedule: a team's bye is the regular-season week
it does not appear in. Verified for 2026 — all 32 teams resolve, one bye each.

### `GET /leagues/{league}/available` — the waiver wire, in this league's own scoring

A general fantasy tool ranks free agents against a generic scoring system, not against
who is actually still on your waiver wire. This computes fantasy points from nflverse's
raw stat counts under **this league's own `scoring_settings`** (see
[`app/scoring.py`](public_data/app/scoring.py) in the public-data service) — 0.5 PPR and
6-point passing touchdowns score differently than someone else's league, and a generic
top-100 list cannot tell the difference. Excludes anyone already rostered anywhere in the
league and ranks by recent-role trend so a role change surfaces before the box score
catches up.

Only offense skill positions (QB/RB/WR/TE) are scored this way — kicking, IDP and defense
scoring keys have no equivalent raw stat in nflverse's weekly file, and rather than
silently ignore them the response says so in `scoring_not_applied`.

### `GET /leagues/{league}/schedule-difficulty/{manager}` — whose slate is softer

For each of a roster's skill players, how many fantasy points its next few opponents have
actually allowed at that position this season — not the opponent's real-world defensive
rank, which does not distinguish "bad against the run" from "bad against pass-catching
backs specifically". The tiebreaker in a close start/sit or a trade-value argument between
two similar players.

### `GET /leagues/{league}/playoff-odds` — Monte Carlo odds and a buy/sell read

Simulates the rest of the regular season thousands of times from each team's own scoring
history (its own mean and spread of points_for so far — not a projection system, not
opponent-specific) to estimate each team's odds of making the playoffs, then classifies
each as a `buyer`, `bubble` or `seller`. A raw win-loss record does not say whether a team
is safely in or needs a miracle; this does. See
[`app/playoffs.py`](app/playoffs.py) for exactly what it does and does not model — it is
deliberately upfront that 12-14 games of history is not enough for more precision than
this.

### `GET /leagues/{league}/trade-fits/{manager}` — who actually has what you need

Crosses your thin positions (no spare healthy body beyond your starters — the same read
`/pressure` uses, via `positional_balance()`, which unlike `thin_positions` also reports
genuine *surplus*) against every other team's surplus at that position, weighted by their
playoff odds and how often they have actually traded with you before. A seller with a
surplus at your weak spot is a far better target than a buyer sitting on the same surplus
as insurance.

### `GET /leagues/{league}/faab-bid/{manager}` — what it actually takes to win a bid

Anchors a bid recommendation to the single most dangerous rival — the manager with both a
track record of bidding high and enough budget left to do it again — rather than a generic
"bid $X for a WR2" rule. `confidence=low/medium/high` scales the margin over that rival's
past ceiling. Honest about its blind spot: there is no signal here about who else actually
wants this specific player, only about what the field has done before.

### `GET /leagues/{league}/briefing/{manager}` — the weekly digest

Merges five otherwise-separate calls — injury disagreements between ESPN and Sleeper,
byes in the next two weeks, thin positions, the top trending free agents, and weather
concerns for your players' games — into one read for your own roster. Same failure
contract as `/snapshot`'s `?include=` blocks: a source that fails reports its own error
rather than failing the whole briefing.

### `POST /leagues/{league}/decision` and `GET /leagues/{league}/decisions` — your own calibration

Log what you decided and why, at the moment you decide it, before you know how it went.
Then record the outcome later.

```bash
curl -X POST -H "X-API-Key: $API_KEY" "https://your-domain.example/leagues/main/decision?\
kind=waiver_bid&summary=Bid 14 on Bench Guy&reasoning=His max bid ever is 12&confidence=medium"
# -> { "decision": { "decision_id": "9066f31e5d30", ... } }

curl -X POST -H "X-API-Key: $API_KEY" \
  "https://your-domain.example/leagues/main/decision/9066f31e5d30/outcome?outcome=Won it at 14, nobody else bid"
```

Outcomes are **appended, not edited**. The original call is preserved exactly as it was
made, which is the part that matters when you go back to check your reasoning against
what happened. `GET /leagues/{league}/decisions?pending_only=true` lists calls still
awaiting an outcome.

This is the slowest of everything on this page to pay off, and the only one that
compounds across seasons: two years of these is the only way to find out whether you
systematically overpay on waivers, or whether your close start/sit calls are coin flips.
No public tool can tell you, because none of them knows what you decided or why.

## Keeping history

The league runs for years and the caches do not, so the question is what would be lost
and what has to be stored.

**Three of the external sources lose nothing.** They keep their own history upstream and
are re-fetched on demand:

| Source | Still available later? | |
| --- | --- | --- |
| nflverse | Yes | A file per season, back to 1999 for play-by-play. **Verified: 2018-2025 all resolve** |
| Open-Meteo | Yes | Free historical archive API |
| **The Odds API** | **No** | The free tier only returns upcoming games. A closing line is gone once the game kicks off |
| **ESPN injuries** | **No** | No historical endpoint exists. Wednesday's "limited" is overwritten by Thursday's "full" |

**Sleeper is the subtle one.** Past seasons stay reachable, but *not* through your league
id: in Sleeper **every season is a separate league**, chained backwards by
`previous_league_id`. A single Sleeper league id only ever reaches the current season,
which is why manager profiling was one season deep until `/backfill` existed.

### Two databases, split the same way as everything else

Each configured league gets its own SQLite file (`league-<slug>.db`, in `CACHE_DIR`),
holding only what is specific to it:

| Table | Holds | Written by |
| --- | --- | --- |
| `seasons`, `managers` | The season chain and who played in each | `/backfill` |
| `transactions` | Every transaction, every season | `/backfill` |
| `draft_picks` | Every draft, every season | `/backfill` |
| `roster_snapshots` | Rosters and standings per week | `/backfill` |
| `decisions` | Your decision log | `/decision` |

Betting lines and injury reports are **not** in these files - they are not specific to
any one league, so they live in `public_data/public.db` instead, shared across every
league this backend serves (see
[Architecture](#architecture-three-services-not-one)). `/leagues/{league}/history` and
`/leagues/{league}/history/{source}` below transparently merge the two: `odds` and
`injuries` are proxied from the shared service, `decisions` is read from that league's own
file.

Both databases use the same design: standard-library `sqlite3`, no ORM, WAL mode so a
read never waits behind a write, versioned migrations applied at startup (`PRAGMA
user_version`), and a database written by a newer build refused rather than downgraded.
Every table also keeps the upstream row verbatim in a `payload` column beside the
extracted ones, so wanting a field later is a query change rather than a migration *plus*
a re-fetch of data that may no longer exist.

Sizing, measured rather than guessed: a 12-team league produces around 360 transactions a
season, so **a decade of one league's complete history is under 1MB**. Reading all of it
and computing every manager profile takes about 60ms. Nothing here is close to needing an
index to be fast; the schema is for structure, not for scale.

### `POST /leagues/{league}/backfill`

Walks `previous_league_id` backwards and archives each season it finds. Transactions,
draft picks and final rosters do not change once a season is over, so they are fetched
once and never requested again — a re-run **skips finished seasons** and only re-reads
the one in progress.

```bash
curl -X POST -H "X-API-Key: $API_KEY" https://your-domain.example/leagues/main/backfill
```

Run it once per league after deploying, then whenever a season ends.
`GET /leagues/{league}/seasons?discover=true` shows what it would pick up before you run
it.

### Multi-season profiling

Once backfilled, `/leagues/{league}/managers` reads from the archive across every season:

```bash
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/leagues/main/managers"
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/leagues/main/managers?seasons=2025,2026"
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/leagues/main/managers?days=30"
```

Omitting both parameters uses everything archived, which is the point. The response says
which `source` it used — `archive`, or `live (current season only)` when nothing has been
backfilled yet, so the endpoint still works before the first run.

### Capturing what evaporates

Odds and injury reports are captured two ways, and they cover each other's gaps.

**Automatically, on every read.** Whenever `/odds`, `/injury-report` or a
`/leagues/{league}/snapshot?include=` pulls data **fresh from upstream**, it is also
archived. It fires
only on an actual fetch, never on a cache hit — archiving a cached response would query
the archive just to conclude nothing changed. It is strictly best-effort: a failure is
logged and swallowed, because a broken archive must never turn a working `/odds` call
into a `500`. Set `HISTORY_AUTO_CAPTURE=false` to switch it off.

The gap it leaves is coverage: a week nobody asks about is a week nobody records.

**On a schedule, via `POST /capture`.** Captures all 32 teams and every game whether or
not anyone asked, which is what makes the archive complete rather than a record of your
browsing. Query params: `week`, `season` (both default to current), `teams` (all 32) and
`refresh` (default `true`, which bypasses the read caches so the archived line is the one
live at capture time). This endpoint is a proxy to the shared public-data service, which
owns the archive - `/capture` is not per-league, so one call covers every league this
backend (or any backend pointed at the same public-data service) serves.

**A row identical to the last recorded state is skipped**, whichever path it came from.
If browsing already recorded Thursday's line, the Thursday cron writes nothing; if nobody
browsed, the cron is the only record. The sequence of rows *is* the history:

```
DET@KC  spread=-9.0     <- Thursday
DET@KC  spread=-7.5     <- Sunday, the line moved

SF  Christian McCaffrey  practice=did_not_practice   <- Wednesday
SF  Christian McCaffrey  practice=limited            <- Friday
```

### Scheduling it

```bash
curl -fsS -X POST -H "X-API-Key: $API_KEY" https://your-domain.example/capture
```

On Railway, add a service in the project running the public-data service, with
**Settings → Cron Schedule** (e.g. `0 23 * * 4` and another for Sunday) hitting that
service's own `/capture` over its private domain directly - it needs no league context,
so there is no reason to route it through a league backend. Twice a week for 18 weeks is
~36 Odds API calls a season against a ~500/month allowance, shared across every league
using that key. The automatic path adds no calls of its own — it only archives fetches
that were going to happen anyway.

### Reading it back

```bash
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/leagues/main/history"
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/leagues/main/history/odds?season=2025&week=2"
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/leagues/main/history/decisions?season=2025"
```

`/leagues/{league}/history` reports that league's database size, schema version and row
counts per table.

After a season or two this answers questions no API will: *how do my RBs score when the
team is favoured by 7+?*, *do players listed limited on Wednesday actually play?*, *has
this manager ever bid above $12?*

## Authentication

Every endpoint except `/health` requires the shared secret in a header:

```bash
curl -H "X-API-Key: $API_KEY" https://your-domain.example/leagues/main/snapshot
```

One `API_KEY` covers every league this backend serves - this is meant for one person's own
leagues, not multiple separate parties, so per-league keys would add real complexity
(routing a key to a league, rotating one without affecting the others) for no isolation
benefit anyone here needs.

The service **fails closed**: if `API_KEY` is not set in the environment, the protected
endpoints return `503` instead of serving data openly. Comparison is constant-time.
CORS is wide open by default, which is fine for a read-only API (the key is still
required).

## Environment variables

These are for **this league's backend**. The public-data service has its own set (see
`public_data/.env.example`) - `ODDS_API_KEY` and every provider/cache-TTL variable now
lives there, not here.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `LEAGUES` | **yes** | — | Comma-separated `slug:sleeper_league_id` pairs, e.g. `main:1390746710426255360,dynasty:9876543210`. Each slug is a `/leagues/{slug}/...` prefix and its own database. |
| `API_KEY` | **yes** | — | Shared secret expected in the `X-API-Key` header, covering every league in `LEAGUES` |
| `PUBLIC_DATA_URL` | **yes** | `http://localhost:8100` | Where the shared public-data service lives - its Railway private domain in production |
| `PUBLIC_DATA_API_KEY` | **yes** | — | That service's own `API_KEY` |
| `PLAYERS_CACHE_PATH` | no | `data/players_cache.json` (`/data/players_cache.json` in Docker) | Where this backend's local player cache lives, hydrated from the public-data service |
| `PLAYERS_CACHE_TTL_HOURS` | no | `20` | Refresh the local player cache only after this many hours |
| `CACHE_DIR` | no | the player cache's directory | Where this backend's other files live, including one `league-<slug>.db` per configured league |
| `HISTORY_AUTO_CAPTURE` | no | `true` | Also archive what read endpoints pull fresh, not just `/capture` |
| `SLEEPER_BASE_URL` | no | `https://api.sleeper.app/v1` | Sleeper API base URL |
| `HTTP_TIMEOUT` | no | `20` | Per-request timeout (seconds) for Sleeper calls |
| `HTTP_MAX_RETRIES` | no | `3` | Attempts per Sleeper call (exponential backoff on 429/5xx/timeouts) |
| `PUBLIC_DATA_TIMEOUT` | no | `60` | Timeout for calls to the public-data service |
| `CORS_ORIGINS` | no | `*` | Comma-separated allowed origins |
| `LOG_LEVEL` | no | `INFO` | Python log level |

Copy `.env.example` to `.env` and fill it in.

## Running it

### Docker Compose (the quick way)

The repo root's `docker-compose.yml` brings up all three services together - the shared
public-data service, the league backend, and its MCP connector:

```bash
cp .env.example .env
# edit .env: set LEAGUES, API_KEY and PUBLIC_DATA_API_KEY (same value in both places),
# plus MCP_URL_TOKEN. ODDS_API_KEY is optional.
docker compose up --build
```

```bash
curl localhost:8100/health                                     # public-data
curl localhost:8000/health                                     # the league backend
curl -H "X-API-Key: <API_KEY>" localhost:8000/leagues | jq
curl -H "X-API-Key: <API_KEY>" localhost:8000/leagues/main/snapshot | jq
curl localhost:8080/healthz                                    # the MCP connector
```

A second league locally just means adding another `slug:id` pair to `LEAGUES` in `.env`
and restarting `sleeper-api` - both `sleeper-api` and `sleeper-mcp` stay single containers.

### Locally, without Docker

Start the public-data service first (see `public_data/README.md`), then:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export LEAGUES=main:1390746710426255360 API_KEY=dev-key
export PUBLIC_DATA_URL=http://localhost:8100 PUBLIC_DATA_API_KEY=<public-data's API_KEY>
uvicorn main:app --reload
```

### Tests

Offline tests that exercise the resolution layer against fixture payloads (no network):

```bash
pip install -r requirements-dev.txt
pytest -q
```

Each service has its own test suite (`tests/` here, `public_data/tests/`,
`mcp_server/tests/`) and they are independent - none of them import across service
boundaries, matching how the services are actually deployed.

## Deploying on Railway

**Three services, not one, and they do not deploy as a unit.** Railway builds one service
per deployment. All three deploy exactly **once**, regardless of how many leagues you add -
a new league is a new `slug:id` pair in the backend's `LEAGUES` variable, not a new service:

| Service | Root Directory | Domain | Healthcheck | How many |
| --- | --- | --- | --- | --- |
| Public data | `/public_data` | **none** — private only | `/health` | **1**, ever |
| League backend | `/` | **none** — private only | `/health` | **1**, ever - serves every league in `LEAGUES` |
| MCP connector | `/mcp_server` | public | `/healthz` | **1**, ever - serves every league the backend does |

**Only the MCP connector needs a public domain.** It is the one thing Claude.ai connects
to. Both backends are reached exclusively over Railway's private network - the
public-data service by the league backend, the league backend by the MCP service and
nothing else. This is the recommended setup, not just an option: it means league data and
every service's `API_KEY` never touch the public internet.

They build and redeploy independently. `docker-compose.yml` at the repo root brings up
all three together for local development, but Railway **ignores that file** and builds
from each directory's own `Dockerfile` and `railway.json` instead.

### 1. The public-data service (deploy this first, once)

1. **New Project → Deploy from GitHub repo**, pick this repo.
2. **Settings → Source → Root Directory**: `/public_data`
3. **Settings → Source → Watch Paths**: `/public_data/**` — a commit to the league
   backend or the MCP service should not rebuild this.
4. **Variables**:
   - `API_KEY` — `openssl rand -hex 32`. The league backend will hold this same value
     as its own `PUBLIC_DATA_API_KEY`.
   - `ODDS_API_KEY` — optional, free key from the-odds-api.com. Shared by every league
     the backend serves.
   - `CACHE_DIR` = `/data`, `DATABASE_PATH` = `/data/public.db`,
     `PLAYERS_CACHE_PATH` = `/data/players_cache.json`
5. **Volume**: attach one mounted at `/data`. Not optional - without it the ~120MB of
   nflverse source downloads happen on every deploy, and the archived betting lines and
   injury reports (which cannot be re-fetched from anywhere) are destroyed.
6. **Networking**: do **not** generate a domain. The league backend reaches this over
   `RAILWAY_PRIVATE_DOMAIN` (below).

### 2. The league backend (deploy this once too)

1. In the **same project** (or a new one, either works - only the private-network
   reachability to the public-data service matters), **New → GitHub Repo**, pick this
   repo again.
2. **Settings → Source → Root Directory**: `/`
3. **Watch Paths**: `/app/**`, `/main.py`, `/Dockerfile`, `/requirements.txt`.
4. **Variables**:
   - `LEAGUES` — every league you want served, e.g.
     `main:1390746710426255360,dynasty:9876543210`. Adding a league later is adding a
     `slug:id` pair here and redeploying - no new service.
   - `API_KEY` — `openssl rand -hex 32`. Shared by every league in `LEAGUES`.
   - `PLAYERS_CACHE_PATH` = `/data/players_cache.json`, `CACHE_DIR` = `/data`
   - `PUBLIC_DATA_URL` = `http://${{public-data.RAILWAY_PRIVATE_DOMAIN}}:${{public-data.PORT}}`
     — substitute the public-data service's actual name for `public-data`
   - `PUBLIC_DATA_API_KEY` = the same value as that service's own `API_KEY` (a reference
     variable if they are in the same project, e.g. `${{public-data.API_KEY}}`; otherwise
     paste it)
5. **Volume**: attach one mounted at `/data`. Not optional - without it, every league's
   transactions, draft picks and decision log are lost on every deploy. (Transactions and
   draft picks can be rebuilt with `/backfill`; a decision log cannot.) One volume holds
   every league's `league-<slug>.db` file, since they are just separate files on the same
   disk.
6. **Networking**: do **not** generate a domain.
7. **After the first deploy**, run the backfill once per league so manager profiling can
   see past seasons. With no public domain there is nothing to `curl` from your machine,
   so do it through the MCP connector instead, once deployed and added to Claude.ai — ask
   Claude to call `history_backfill` with each league's slug. (To test the backend on its
   own first, generate a domain temporarily, run the curls, then remove the domain again.)

### 3. The MCP connector (deploy this once too)

1. **New → GitHub Repo**, same repo again.
2. **Settings → Source → Root Directory**: `/mcp_server`
3. **Watch Paths**: `/mcp_server/**`
4. **Variables**:
   - `BACKEND_URL` = `http://${{league-backend.RAILWAY_PRIVATE_DOMAIN}}:${{league-backend.PORT}}`
     — pointed at the league backend, not the public-data service
   - `BACKEND_API_KEY` = that backend's `API_KEY` (a reference variable, e.g.
     `${{league-backend.API_KEY}}`)
   - `MCP_URL_TOKEN` — `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   - `MCP_ALLOWED_HOSTS` = this connector's domain
   - `DEFAULT_LEAGUE` — optional; set it to one slug from `LEAGUES` to let every tool call
     omit `league` and mean that one. Leave it unset and Claude is told to call
     `league_list` and pass a slug explicitly - the right choice once there is more than
     one league that matters day to day.
5. **Networking**: generate a public domain **for this service**. Claude.ai connects from
   Anthropic's infrastructure, so this is the one thing that must be internet-reachable.

Add the resulting URL (`https://<domain>/<MCP_URL_TOKEN>/mcp`) to Claude.ai as its own
custom connector. One connector reaches every league in `LEAGUES` - Claude picks the
league per tool call (or uses `DEFAULT_LEAGUE`), so there is nothing further to deploy
when you add a league.

> `${{service.RAILWAY_PRIVATE_DOMAIN}}` / `${{service.PORT}}` / `${{service.API_KEY}}`
> are Railway reference variables, resolved from another service's own variables. This is
> standard Railway behavior but was not verified against a live deployment while writing
> this, since Railway's own docs were unreachable from the build environment - if one
> fails to resolve, check the exact variable names in that service's **Variables** tab.

### Adding another league

Once the backend and its MCP connector are running, adding a league costs **no new
services**: add its `slug:sleeper_league_id` pair to the backend's `LEAGUES` variable and
redeploy. The backend opens a new `league-<slug>.db` on the existing volume, `GET
/leagues` picks it up immediately, and the existing MCP connector can reach it the moment
a tool call (or `DEFAULT_LEAGUE`) names its slug - nothing on the MCP side needs to change
unless you want `DEFAULT_LEAGUE` to point somewhere new.

Every league still gets a genuinely separate database - a bug or a bad write in one
league's data can never touch another's. What is shared on purpose is the process, the
`API_KEY`, the MCP connector, the public-data service, and, if you reuse the same
`ODDS_API_KEY`, its monthly quota.

### Two things Railway does differently

**It assigns the port.** Railway injects `PORT` and expects the process to bind it; a
hardcoded port builds and starts fine and then fails its healthcheck forever. All three
services start through `entrypoint.py`, which reads `PORT`.

**Private networking is IPv6-only.** A process bound to `0.0.0.0` is unreachable at
`<service>.railway.internal` — the caller just times out with nothing in either log.
`entrypoint.py` binds `::`, which covers IPv4 too on a dual-stack host, and falls back to
`0.0.0.0` where there is no IPv6 stack (some local Docker setups), so the same image runs
in both places. `HOST` overrides the detection if you need it to.

**A public domain needs `HOST=0.0.0.0` explicitly — confirmed on a live deploy.** The
`::` auto-detection above is right for the backend and public-data, which only ever
answer other services over the private network. The MCP connector is the opposite: it
has a generated public domain and nothing calls it privately, and Railway's public-domain
edge proxy could not reach a container bound to `::` — the app itself came up cleanly
(`Uvicorn running on http://[::]:8080`, logs all healthy) while the public URL returned a
bare `502 Application failed to respond`. Setting `HOST=0.0.0.0` on the MCP service fixed
it immediately. Set it on whichever service ends up with the public domain; leave it unset
on the two that only talk over the private network.

### Verifying

With no public domain on either backend, each one's own health is checked two ways:

- **Railway's dashboard** — each deployment shows healthy/unhealthy from its own
  `railway.json`'s `healthcheckPath`, checked against the container directly over
  Railway's internal network. No domain involved.
- **The `health_check` MCP tool** — proves the whole chain for one league: Claude.ai →
  MCP service → league backend → public-data service. This is the check that actually
  matters, since it is the same path every other tool call takes.

```bash
curl https://mcp.tudominio.com/healthz
```

`/healthz` is the MCP server's own liveness — it reports the backend URL it is pointed at
and whether the key is configured, but deliberately does **not** call the backend, so it
stays green even if the backend is down. Likewise each backend's own `/health` does not
call the public-data service. That is intentional: a platform-level healthcheck should
reflect this process's own state, not a dependency it cannot fix by restarting. Use
`health_check` (the tool, through Claude) for the end-to-end check.

### Running it locally

`docker compose up --build` from the repo root brings up all three services together
(see the root `docker-compose.yml`); each service also has its own `docker-compose.yml`
for running it standalone. Railway does not use any of these files.

## What was and wasn't verified

Being straight about this, because two of these sources could not be reached from the
machine this was built on:

- **Verified offline, end to end.** The SQLite layer: migrations, the season-chain walk,
  multi-season profiling, and that a re-run of `/backfill` skips finished seasons (54
  Sleeper calls on the first run, 18 on the second). Driven against a three-season fake
  league, since Sleeper itself is unreachable from the build environment.
- **Verified the three-service split, end to end.** All three services (public-data,
  the league backend, and a fake upstream standing in for Sleeper/The Odds API/ESPN/
  Open-Meteo) were started as real, independent processes talking over real HTTP - not
  mocked in-process. Confirmed: player names resolve through `/leagues/{league}/snapshot`
  via the public-data service's `/players` endpoint rather than any local Sleeper call;
  `/leagues/{league}/snapshot?include=odds,injury_report,weather` assembles correctly
  through the rewritten `enrichment.py`; `/odds`, `/injury-report*`, `/capture` and
  `/leagues/{league}/history` proxy with byte-for-byte identical error messages; a
  repeated `/capture` correctly skips an unchanged line; and the league backend's own
  `API_KEY` is rejected by the public-data service (401) - proving the two are genuinely,
  separately secured.
- **Verified multiple leagues on one backend process.** With `LEAGUES=main:...,dynasty:...`
  configured, the backend opened two genuinely separate SQLite files (each with its own
  migration log), `GET /leagues` listed both slugs, an unconfigured slug 404'd with the
  list of known ones, and `/leagues/{league}/history` resolved correctly per slug instead
  of crashing on a stale module-level database reference - the bug that motivated
  re-checking every endpoint by hand while doing this rewrite, not just the ones that
  obviously needed a `league` argument.
- **Verified against the live service.** nflverse: the column names, the `gsis_id` /
  `pfr_id` join, the red zone aggregation, the season fallback, and the draft board
  (2026 class: 80 skill picks, all with `gsis_id`, 70 matched to combine data) were all
  built and tested against the real release files. A cold build (four files, ~120MB) takes about
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
and `parse_injuries` in `public_data/app/espn.py` needs a new key added to
`_candidate_lists`.

### Known gap: `/available` can surface a long-retired player

Confirmed on a live deploy: `/leagues/{league}/available` recommended Philip Rivers
(retired since the 2020 season) at QB. `/available` filters out anyone whose Sleeper
`status` is not one of `Active`, `Injured Reserve`, `PUP`, `Non Football Injury`,
`Suspended` or `Practice Squad` - which does catch most stale entries (confirmed it
correctly dropped two other players in the same live check) - but Rivers' own Sleeper
record still reports `status: "Active"`. The nflverse season fallback only ever steps back
one year (2026 → 2025, never further), and 2025 is a complete real season, so this is not
stale-season data leaking through - it is Sleeper's own player record being wrong for this
one player, most likely a bad `gsis_id` cross-reference rather than anything this codebase
computes. A "how old is this player's most recent season" heuristic was considered and
rejected: given the fallback already never goes back more than a year, it would not have
caught this specific case anyway, and risks dropping a legitimately rostered player who
simply missed last season to injury. Flagging it here rather than guessing at a fix that
cannot be verified without live access to Sleeper's and nflverse's real data.

A related, narrower case *was* fixed the same way: `/available` also excludes anyone with
no current `nfl_team` on file, after live testing turned up Tyreek Hill with `status:
"Active"` (so the check above alone would not catch him), `nfl_team: null`, and an ACL
surgery note - a real signal already present in Sleeper's own data, not a guess, which is
why this one was implemented and the season-age heuristic above was not.

### Known gap: ESPN's site API 403s every request from Railway

Confirmed live: `site.api.espn.com` returns `403 Forbidden` for every request this app's
Railway deployment makes to it, including the schedule/scoreboard endpoint that
`/weather/{week}` depends on for its list of games. The first fix attempted was a
realistic browser `User-Agent` (`espn.py`'s `_BROWSER_HEADERS`), on the theory that ESPN's
unofficial API was flagging the app's honest bot User-Agent - it did not help; the 403
persisted identically after that shipped and redeployed (same error, confirmed against
Railway's deploy logs showing the new commit running). That rules out the request itself
and points to an IP-range block on Railway's outbound traffic instead, which is not
something a header change can fix. A VPN or consumer proxy was considered and rejected:
most run into the same problem (VPN exit IPs are commonly blocked for exactly this reason),
and running one inside a Railway container needs `NET_ADMIN`/`/dev/net/tun` access that a
managed container platform does not grant. A paid rotating-residential-proxy service would
likely work, but is an ongoing cost and a new piece of infrastructure for what is currently
a "nice to have" (ESPN's practice-participation detail on top of what Sleeper's own
`injury_status` already provides), so it was not built.

The actual fix: `request_json` takes an `allow_403` flag (`public_data/app/http.py`) that,
for a source confirmed to reject Railway's traffic outright, returns `None` instead of
raising - the same treatment a 404 already gets. Both of `EspnProvider`'s call sites pass
`allow_403=True`, so `team_report()` and `schedule()` degrade to `source_available: false`
(and `/weather/{week}` to `available: false`) instead of a 502 taking down the whole
endpoint. Every other source keeps failing loudly on a 401/403, since for them it usually
means a real misconfiguration (a bad API key) worth surfacing, not hiding.

**A second, independent source was added rather than waiting on ESPN.** nflverse also
publishes the NFL's own official weekly injury report (`injuries/injuries_<season>.csv` -
same GitHub Releases mechanism as `stats_player`/`snap_counts`, which is what let it work
from Railway when ESPN's live API doesn't: a static file download isn't a scraped request a
site can bot-detect). `NflverseProvider.team_injuries()` and `.player_injury_report()`
(`public_data/app/nflverse.py`) serve it, and `/injury-report` and `/injury-report/{id}`
now return both `espn_report`/`espn_id`-keyed data and a `nflverse_report` field side by
side - kept as two sources rather than one replacing the other, so if ESPN's block ever
lifts there is no follow-up migration needed. The trade-off: nflverse's report is
weekly-cadence (the league's official filing), not live like ESPN's scrape, so
`team_injuries()` serves each player's *most recently published* week, which may lag a day
or two behind a mid-week practice-report update. Same caveat as ESPN's own parsing: this is
built from nflreadr's documented `load_injuries()` column names, not live-verified against
the real release file (unreachable from the build environment - see "What was and wasn't
verified"), so the first real call is worth eyeballing.

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

This describes the public-data service's internals, which every league backend proxies
to - see [Architecture](#architecture-three-services-not-one).

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

This repo holds all three services (see
[Architecture](#architecture-three-services-not-one)):

```
League backend (this directory - one process, serves every league in LEAGUES)
main.py                 FastAPI app, routes, CORS, lifespan
app/config.py           Environment-driven settings
app/security.py         X-API-Key dependency (fails closed)
app/sleeper.py          Async Sleeper client for this league's own data
app/public_client.py    HTTP client for the shared public-data service
app/players.py          Local player cache, hydrated from public-data
app/humanize.py         Sleeper setting/scoring keys -> plain language
app/services.py         Roster, matchup, transaction and snapshot assembly
app/enrichment.py       The optional ?include= blocks on /snapshot (proxies)
app/managers.py         Opponent profiles from transaction and draft history
app/pressure.py         Structural gaps: bye collisions, injuries, no cover
app/teams.py            Static stadium coordinates, roof types, name aliases
app/db.py               SQLite connection, schema and versioned migrations
app/store.py            Typed reads and writes over this league's database
app/backfill.py         Walks previous_league_id and archives each season
app/history.py          The decision log
tests/                  Offline tests against fixture payloads
Dockerfile, docker-compose.yml, railway.json

public_data/            Shared service - deploy once (own README)
  app/nflverse.py       nflverse releases: download, join on gsis_id, aggregate
  app/odds.py           The Odds API: consensus lines and game script
  app/espn.py           ESPN injuries and schedule (defensive parsing)
  app/weather.py        Open-Meteo forecasts, domes short-circuited
  app/draft.py          Rookie draft board: draft capital, combine, landing spot
  app/schedule.py       Bye weeks derived from the nflverse schedule
  app/players.py        The canonical Sleeper player cache
  app/db.py, app/store.py, app/history.py   Odds/injury archive
  main.py, Dockerfile, docker-compose.yml, railway.json

mcp_server/             MCP connector exposing every configured league to Claude.ai (own README)

docker-compose.yml      Brings up all three together, for local development
```

## MCP connector

`mcp_server/` is a separate service that wraps the league backend as an
[MCP](https://modelcontextprotocol.io) server, so every league the backend serves can be
added to Claude.ai through **one** remote custom connector (Customize → Connectors → Add
custom connector). It exposes 30 tools over Streamable HTTP and keeps `API_KEY` on the
server side so the Claude client never sees it; every league-specific tool takes a
`league` slug argument (`league_list` shows what is available), with `DEFAULT_LEAGUE`
available to skip passing it when the connector is used for one league day to day. Adding
a league to `LEAGUES` on the backend needs no change here - see
[Adding another league](#adding-another-league). See
[`mcp_server/README.md`](mcp_server/README.md).

## Not included (by design)

User authentication (beyond the shared `API_KEY`), per-league isolation between separate
parties (every league on a backend shares one key and one process — see
[Authentication](#authentication)), a frontend, or any write access to Sleeper or to any
of the external sources.
