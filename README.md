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
| `GET` | `/draft-class/{season}` | `X-API-Key` | Rookie draft board: capital, age, combine, landing spot |
| `GET` | `/prospect/{player_id}` | `X-API-Key` | Draft profile for one player |
| `GET` | `/managers` | `X-API-Key` | Behavioural profile of every manager in the league |
| `GET` | `/manager/{name}` | `X-API-Key` | One manager, read against the field |
| `GET` | `/pressure` | `X-API-Key` | Which teams are structurally forced to act |
| `POST` | `/decision` | `X-API-Key` | Log a decision and the reasoning behind it |
| `POST` | `/decision/{id}/outcome` | `X-API-Key` | Record how a logged decision turned out |
| `GET` | `/decisions` | `X-API-Key` | Read the decision log |
| `POST` | `/capture` | `X-API-Key` | Archive this week's betting lines and injury reports |
| `GET` | `/history` | `X-API-Key` | What is in the archive, per source and season |
| `GET` | `/history/{source}` | `X-API-Key` | Read archived rows (`odds`, `injuries`, `decisions`) |
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

## Rookie draft board

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
`/snapshot` through the same index `/advanced-stats` uses.

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

Three things qualify, and all three are built here.

**Be honest about the size of it.** Fantasy football is dominated by variance and draft
luck. None of this wins you the league. What it does is tilt marginal decisions — how
much to bid, who to approach for a trade, who to sit in a close call — and those compound
over a season. The asymmetry is in the cost: an afternoon of work over data you already
pull, against opponents who will never do this.

### `GET /managers` and `/manager/{name}` — your opponents' habits

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

### `GET /pressure` — who has to move before you do

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

### `POST /decision` and `GET /decisions` — your own calibration

Log what you decided and why, at the moment you decide it, before you know how it went.
Then record the outcome later.

```bash
curl -X POST -H "X-API-Key: $API_KEY" "https://your-domain.example/decision?\
kind=waiver_bid&summary=Bid 14 on Bench Guy&reasoning=His max bid ever is 12&confidence=medium"
# -> { "decision": { "decision_id": "9066f31e5d30", ... } }

curl -X POST -H "X-API-Key: $API_KEY" \
  "https://your-domain.example/decision/9066f31e5d30/outcome?outcome=Won it at 14, nobody else bid"
```

Outcomes are **appended, not edited**. The original call is preserved exactly as it was
made, which is the part that matters when you go back to check your reasoning against
what happened. `GET /decisions?pending_only=true` lists calls still awaiting an outcome.

This is the slowest of the three to pay off and the only one that compounds across
seasons: two years of these is the only way to find out whether you systematically
overpay on waivers, or whether your close start/sit calls are coin flips. No public tool
can tell you, because none of them knows what you decided or why.

## Keeping history

The league runs for years; the caches above do not. They are overwritten on every
refresh, so the question is what would actually be lost.

**Three of the five sources lose nothing.** They keep their own history upstream and are
re-fetched on demand:

| Source | Still available later? | |
| --- | --- | --- |
| nflverse | Yes | A file per season, back to 1999 for play-by-play. **Verified: 2018-2025 all resolve** |
| Sleeper | Yes | Past seasons chain through `previous_league_id`; past weeks stay queryable |
| Open-Meteo | Yes | Free historical archive API |
| **The Odds API** | **No** | The free tier only returns upcoming games. A closing line is gone once the game kicks off (historical odds are a paid add-on) |
| **ESPN injuries** | **No** | No historical endpoint exists. Wednesday's "limited" is overwritten by Thursday's "full", and after the week there is no record either happened |

So this service archives **only the two that evaporate**. Re-storing the other three
would duplicate public archives that are better maintained than anything kept here, and
leave a schema to migrate for years.

Capture happens two ways, and they cover each other's gaps.

### 1. Automatically, on every read

Whenever `/odds`, `/injury-report` or a `/snapshot?include=odds,injury_report` pulls
data **fresh from upstream**, that data is also archived. Nothing to schedule, and
anything you look at is recorded by the act of looking at it.

It fires only on an actual upstream fetch, never on a cache hit — archiving a cached
response would re-scan the archive just to conclude nothing changed. It is also
strictly best-effort: a failure is logged and swallowed, because a broken archive must
never turn a working `/odds` call into a `500`.

The gap this leaves is coverage: a week nobody asks about is a week nobody records.
Hence the second path.

Set `HISTORY_AUTO_CAPTURE=false` to turn this off and archive only on `/capture`.

### 2. On a schedule, via `POST /capture`

Writes the current week's betting lines and injury reports to an append-only
[JSON Lines](https://jsonlines.org) file, one per source per season, under
`HISTORY_DIR`. Query params: `week`, `season` (both default to current), `teams`
(defaults to all 32) and `refresh` (default `true`).

Unlike the automatic path this captures **all 32 teams and every game**, whether or not
anyone asked about them, which is what makes the archive complete rather than a record
of your browsing.

`refresh=true` bypasses the read caches so the archived line is the one live at capture
time, rather than whatever a browsing request happened to warm the cache with hours
earlier. That is the entire point of capturing on a schedule, so it defaults on and
costs one Odds API call per capture.

### Both together

The two paths share one deduplicated archive, so they never double-write. **A capture
that would write a row identical to the last recorded state is skipped**, whichever path
it came from. If browsing already recorded Thursday's line, the Thursday cron writes
nothing; if nobody browsed, the cron is the only record. Running either more often than
the lines move costs nothing, and every real change is kept:

```
08:33:23  DET@KC  spread=-9.0  total=50.0     <- Thursday
08:41:07  DET@KC  spread=-7.5  total=50.0     <- Sunday, line moved

08:33:23  SF  Christian McCaffrey  practice=did_not_practice   <- Wednesday
08:41:07  SF  Christian McCaffrey  practice=limited            <- Friday
```

The sequence *is* the history: a subject appears again only when something about it
actually changed.

### Scheduling it

Point a scheduler at the endpoint — Coolify's scheduled tasks, or any cron:

```bash
# Thursday evening and Sunday shortly before the early kickoffs
curl -fsS -X POST -H "X-API-Key: $API_KEY" https://your-domain.example/capture
```

Twice a week for 18 weeks is ~36 Odds API calls a season, against a ~500/month free
allowance. The automatic path adds no calls of its own — it only archives fetches that
were going to happen anyway. Storage runs roughly **250KB of odds and ~2MB of injuries per season** — the
same `/data` volume covers it without going near needing a database engine.

### Reading it back

```bash
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/history"
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/history/odds?season=2025&week=2"
curl -H "X-API-Key: $API_KEY" "https://your-domain.example/history/injuries?season=2025"
```

After a season or two this answers questions no API will: *how do my RBs score when the
team is favoured by 7+?*, *do players listed limited on Wednesday actually play?*

An interrupted write can leave a torn final line. The reader skips it with a warning
instead of failing, and the next append starts on a fresh line so the damage stays
confined to that one row.

Deduplication is decided against an in-memory index of the last state per subject, built
from the file the first time a week is touched, so auto-capture does not re-read the
season archive on every request. The app runs single-worker (`--workers 1`); with
several worker processes those indexes could drift and occasionally write a duplicate
row, which is harmless but worth knowing before raising the worker count.

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
| `HISTORY_DIR` | no | `<CACHE_DIR>/history` | Append-only archive of odds and injury reports |
| `HISTORY_AUTO_CAPTURE` | no | `true` | Also archive what read endpoints pull fresh, not just `/capture` |
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
6. **Scheduled task** (recommended, for complete archive coverage) — reads already
   archive themselves; the cron is what covers weeks nobody browsed. Add a Coolify
   scheduled task running
   `curl -fsS -X POST -H "X-API-Key: $API_KEY" http://localhost:8000/capture`, e.g.
   Thursdays and Sundays. See [Keeping history](#keeping-history).
7. **Domain + HTTPS** — set your FQDN in Coolify and let it issue the certificate.
8. Deploy, then verify:
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
app/draft.py            Rookie draft board: draft capital, combine, landing spot
app/managers.py         Opponent profiles from transaction and draft history
app/pressure.py         Structural gaps: bye collisions, injuries, no cover
app/schedule.py         Bye weeks derived from the nflverse schedule
app/teams.py            Static stadium coordinates, roof types, name aliases
app/enrichment.py       The optional ?include= blocks on /snapshot
app/history.py          Append-only JSONL archive + the /capture flow
tests/                  Offline tests against fixture payloads
Dockerfile              Multi-stage build, non-root, healthcheck
docker-compose.yml      Example deployment with a persistent cache volume
```

## Not included (by design)

User authentication, multi-league support, a frontend, or any write access to Sleeper or
to any of the external sources.
