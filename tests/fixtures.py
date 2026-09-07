"""Small hand-built stand-ins for Sleeper payloads, shaped like the real API."""

LEAGUE = {
    "league_id": "1390746710426255360",
    "name": "Test Keeper League",
    "season": "2025",
    "season_type": "regular",
    "status": "in_season",
    "sport": "nfl",
    "total_rosters": 12,
    "roster_positions": [
        "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF",
        "BN", "BN", "BN", "BN", "BN", "BN", "IR",
    ],
    "scoring_settings": {
        "rec": 1.0,
        "pass_yd": 0.04,
        "pass_td": 4.0,
        "pass_int": -2.0,
        "rush_yd": 0.1,
        "rush_td": 6.0,
        "rec_yd": 0.1,
        "rec_td": 6.0,
        "fum_lost": -2.0,
        "fgm_50p": 5.0,
        "pts_allow_0": 10.0,
        "sack": 1.0,
        "some_future_key": 1.5,
    },
    "settings": {
        "type": 1,
        "max_keepers": 1,
        "num_teams": 12,
        "playoff_teams": 6,
        "playoff_week_start": 15,
        "playoff_round_type": 0,
        "playoff_seed_type": 0,
        "trade_deadline": 13,
        "trade_review_days": 2,
        "pick_trading": 1,
        "disable_trades": 0,
        "waiver_type": 2,
        "waiver_budget": 100,
        "waiver_clear_days": 2,
        "waiver_day_of_week": 2,
        "daily_waivers": 0,
        "reserve_slots": 1,
        "taxi_slots": 0,
        "leg": 5,
        "start_week": 1,
    },
}

USERS = [
    {
        "user_id": "u1",
        "username": "elchubi",
        "display_name": "elchubi",
        "metadata": {"team_name": "Los Tacos Voladores"},
    },
    {
        "user_id": "u2",
        "username": "rival99",
        "display_name": "Rival99",
        "metadata": {"team_name": "Gridiron Goats"},
    },
]

ROSTERS = [
    {
        "roster_id": 1,
        "owner_id": "u1",
        "starters": ["4046", "4034", "0", "6794", "5849", "1466", "4098", "0", "K1", "KC"],
        "players": ["4046", "4034", "6794", "5849", "1466", "4098", "K1", "KC", "9999", "8888"],
        "reserve": ["8888"],
        "taxi": [],
        "settings": {
            "wins": 3, "losses": 2, "ties": 0,
            "fpts": 512, "fpts_decimal": 34,
            "fpts_against": 498, "fpts_against_decimal": 10,
            "waiver_position": 4, "waiver_budget_used": 25, "total_moves": 7,
        },
    },
    {
        "roster_id": 2,
        "owner_id": "u2",
        "starters": ["4034", "4046", "6794", "5849", "1466", "4098", "K1", "KC", "K1", "KC"],
        "players": ["4034", "4046", "6794", "5849", "1466", "4098", "K1", "KC"],
        "reserve": [],
        "taxi": [],
        "settings": {
            "wins": 4, "losses": 1, "ties": 0,
            "fpts": 601, "fpts_decimal": 5,
            "fpts_against": 480, "fpts_against_decimal": 0,
        },
    },
]

PLAYERS_RAW = {
    "4046": {
        "first_name": "Patrick", "last_name": "Mahomes", "full_name": "Patrick Mahomes",
        "position": "QB", "team": "KC", "status": "Active", "number": 15,
        "fantasy_positions": ["QB"], "age": 30, "years_exp": 8,
    },
    "4034": {
        "first_name": "Christian", "last_name": "McCaffrey", "position": "RB",
        "team": "SF", "status": "Active", "injury_status": "Questionable",
        "injury_body_part": "Achilles",
    },
    "6794": {"first_name": "Justin", "last_name": "Jefferson", "position": "WR", "team": "MIN"},
    "5849": {"first_name": "Kyler", "last_name": "Murray", "position": "QB", "team": "ARI"},
    "1466": {"first_name": "Travis", "last_name": "Kelce", "position": "TE", "team": "KC"},
    "4098": {"first_name": "Austin", "last_name": "Ekeler", "position": "RB", "team": "WAS"},
    "K1": {"first_name": "Justin", "last_name": "Tucker", "position": "K", "team": "BAL"},
    "KC": {"first_name": "Kansas City", "last_name": "Chiefs", "position": "DEF", "team": "KC"},
    "9999": {"first_name": "Bench", "last_name": "Guy", "position": "WR", "team": "NYJ"},
    "8888": {"first_name": "Hurt", "last_name": "Player", "position": "RB", "team": "DAL",
             "injury_status": "IR"},
}

MATCHUPS = [
    {
        "roster_id": 1,
        "matchup_id": 1,
        "points": 110.5,
        "starters": ["4046", "4034", "0", "6794", "5849", "1466", "4098", "0", "K1", "KC"],
        "starters_points": [25.1, 12.0, 0, 30.4, 18.0, 9.0, 6.0, 0, 8.0, 2.0],
        "players_points": {"9999": 4.2},
    },
    {
        "roster_id": 2,
        "matchup_id": 1,
        "points": 98.2,
        "starters": ["4034", "4046", "6794", "5849", "1466", "4098", "K1", "KC", "K1", "KC"],
        "starters_points": [12.0, 25.1, 30.4, 18.0, 9.0, 6.0, 8.0, 2.0, 8.0, 2.0],
        "players_points": {},
    },
]
