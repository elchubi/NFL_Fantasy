"""Sample payloads shaped like the external sources' real responses.

nflverse rows were taken from the actual release files. The Odds API, ESPN and
Open-Meteo payloads follow those services' documented/observed shapes.
"""

# --- nflverse CSV rows (column names verified against the live release files) --

STATS_PLAYER_WEEK_CSV = """player_id,player_name,player_display_name,position,position_group,season,week,season_type,team,opponent_team,completions,attempts,passing_yards,passing_tds,passing_air_yards,passing_epa,carries,rushing_yards,rushing_tds,rushing_epa,receptions,targets,receiving_yards,receiving_tds,receiving_air_yards,receiving_epa,racr,target_share,air_yards_share,wopr,fantasy_points_ppr
00-0034796,C.McCaffrey,Christian McCaffrey,RB,RB,2025,1,REG,SF,SEA,0,0,0,0,0,NA,18,95,1,3.2,6,7,45,0,12,2.1,3.75,0.21,0.05,0.42,26.0
00-0034796,C.McCaffrey,Christian McCaffrey,RB,RB,2025,2,REG,SF,LAR,0,0,0,0,0,NA,22,110,2,5.1,4,5,30,0,8,1.4,3.75,0.15,0.03,0.31,33.0
00-0036322,J.Jefferson,Justin Jefferson,WR,WR,2025,1,REG,MIN,CHI,0,0,0,0,0,NA,0,0,0,NA,9,13,140,1,155,8.4,0.9,0.32,0.41,0.86,29.0
00-0036322,J.Jefferson,Justin Jefferson,WR,WR,2025,2,REG,MIN,GB,0,0,0,0,0,NA,0,0,0,NA,5,8,60,0,90,1.1,0.67,0.22,0.30,0.58,11.0
00-0036322,J.Jefferson,Justin Jefferson,WR,WR,2025,3,REG,MIN,DET,0,0,0,0,0,NA,0,0,0,NA,8,11,120,1,130,6.2,0.92,0.30,0.38,0.80,26.0
00-0036322,J.Jefferson,Justin Jefferson,WR,WR,2025,4,REG,MIN,SEA,0,0,0,0,0,NA,0,0,0,NA,3,5,25,0,55,0.4,0.45,0.14,0.18,0.36,5.5
00-0036322,J.Jefferson,Justin Jefferson,WR,WR,2025,5,REG,MIN,CLE,0,0,0,0,0,NA,0,0,0,NA,2,4,18,0,40,-0.6,0.45,0.11,0.14,0.29,3.8
"""

SNAP_COUNTS_CSV = """game_id,pfr_game_id,season,game_type,week,player,pfr_player_id,position,team,opponent,offense_snaps,offense_pct,defense_snaps,defense_pct,st_snaps,st_pct
2025_01_SEA_SF,202509070sfo,2025,REG,1,Christian McCaffrey,McCaCh01,RB,SF,SEA,55,0.82,0,0,2,0.08
2025_02_LAR_SF,202509140sfo,2025,REG,2,Christian McCaffrey,McCaCh01,RB,SF,LAR,48,0.70,0,0,1,0.04
2025_01_MIN_CHI,202509070min,2025,REG,1,Justin Jefferson,JeffJu00,WR,MIN,CHI,60,0.95,0,0,0,0
2025_02_GB_MIN,202509140min,2025,REG,2,Justin Jefferson,JeffJu00,WR,MIN,GB,40,0.62,0,0,0,0
2025_03_MIN_DET,202509210det,2025,REG,3,Justin Jefferson,JeffJu00,WR,MIN,DET,38,0.60,0,0,0,0
2025_04_SEA_MIN,202509280min,2025,REG,4,Justin Jefferson,JeffJu00,WR,MIN,SEA,35,0.55,0,0,0,0
2025_05_MIN_CLE,202510050cle,2025,REG,5,Justin Jefferson,JeffJu00,WR,MIN,CLE,30,0.48,0,0,0,0
"""

PLAYERS_CROSSWALK_CSV = """gsis_id,display_name,pfr_id,espn_id,position
00-0034796,Christian McCaffrey,McCaCh01,3117251,RB
00-0036322,Justin Jefferson,JeffJu00,4262921,WR
"""

# Only the handful of columns the red zone aggregation reads.
PBP_CSV = """week,posteam,yardline_100,play_type,rush_attempt,pass_attempt,touchdown,complete_pass,receiver_player_id,rusher_player_id
1,SF,8,run,1,0,1,0,NA,00-0034796
1,SF,15,run,1,0,0,0,NA,00-0034796
1,SF,45,run,1,0,0,0,NA,00-0034796
1,MIN,12,pass,0,1,1,1,00-0036322,NA
1,MIN,5,pass,0,1,0,0,00-0036322,NA
2,SF,3,run,1,0,1,0,NA,00-0034796
"""

# Column names follow nflreadr's documented `load_injuries()` schema, not
# live-verified (same caveat as ESPN - see README's "What was and wasn't
# verified"). Two players, two weeks each, one of them cleared by week 2.
INJURIES_CSV = """season,game_type,team,week,gsis_id,position,full_name,first_name,last_name,report_primary_injury,report_secondary_injury,report_status,practice_primary_injury,practice_secondary_injury,practice_status,date_modified
2025,REG,SF,1,00-0034796,RB,Christian McCaffrey,Christian,McCaffrey,Achilles,,Questionable,Achilles,,Limited Participation in Practice,2025-09-10
2025,REG,SF,2,00-0034796,RB,Christian McCaffrey,Christian,McCaffrey,,,,,,Full Participation in Practice,2025-09-17
2025,REG,MIN,1,00-0036322,WR,Justin Jefferson,Justin,Jefferson,Hamstring,,Doubtful,Hamstring,,Did Not Participate In Practice,2025-09-11
"""

# --- The Odds API -------------------------------------------------------------

ODDS_PAYLOAD = [
    {
        "id": "e912304de2b2ce35b473ce2ecd3d1502",
        "sport_key": "americanfootball_nfl",
        "commence_time": "2025-09-14T17:00:00Z",
        "home_team": "Kansas City Chiefs",
        "away_team": "Detroit Lions",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": -350},
                            {"name": "Detroit Lions", "price": 280},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": -110, "point": -8.5},
                            {"name": "Detroit Lions", "price": -110, "point": 8.5},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 49.5},
                            {"name": "Under", "price": -110, "point": 49.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": -112, "point": -9.5},
                            {"name": "Detroit Lions", "price": -108, "point": 9.5},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 50.5},
                            {"name": "Under", "price": -110, "point": 50.5},
                        ],
                    },
                ],
            },
        ],
    }
]

# --- ESPN ---------------------------------------------------------------------

ESPN_INJURIES = {
    "injuries": [
        {
            "id": "1",
            "athlete": {
                "id": "3117251",
                "displayName": "Christian McCaffrey",
                "position": {"abbreviation": "RB"},
            },
            "status": "Questionable",
            "date": "2025-09-12T18:00Z",
            "details": {"type": "Achilles", "side": "Right", "returnDate": "2025-09-14"},
            "longComment": "McCaffrey was a limited participant in practice Wednesday.",
        },
        {
            "id": "2",
            "athlete": {"id": "999999", "displayName": "Someone Else"},
            "status": {"name": "Out"},
            "details": {"type": "Hamstring"},
            "shortComment": "Did not practice Thursday and has been ruled out.",
        },
    ]
}

# The same endpoint has also been observed returning per-team groups.
ESPN_INJURIES_GROUPED = [
    {"team": {"abbreviation": "SF"}, "injuries": ESPN_INJURIES["injuries"]}
]

ESPN_SCOREBOARD = {
    "events": [
        {
            "id": "401671789",
            "name": "Detroit Lions at Kansas City Chiefs",
            "shortName": "DET @ KC",
            "date": "2025-09-14T17:00Z",
            "week": {"number": 2},
            "status": {"type": {"description": "Scheduled"}},
            "competitions": [
                {
                    "venue": {"fullName": "GEHA Field at Arrowhead Stadium", "indoor": False},
                    "competitors": [
                        {"homeAway": "home", "team": {"abbreviation": "KC"}},
                        {"homeAway": "away", "team": {"abbreviation": "DET"}},
                    ],
                }
            ],
        },
        {
            "id": "401671790",
            "shortName": "GB @ MIN",
            "date": "2025-09-14T17:00Z",
            "week": {"number": 2},
            "competitions": [
                {
                    "venue": {"fullName": "U.S. Bank Stadium", "indoor": True},
                    "competitors": [
                        {"homeAway": "home", "team": {"abbreviation": "MIN"}},
                        {"homeAway": "away", "team": {"abbreviation": "GB"}},
                    ],
                }
            ],
        },
    ]
}

# --- Open-Meteo ---------------------------------------------------------------

OPEN_METEO = {
    "latitude": 39.05,
    "longitude": -94.48,
    "hourly": {
        "time": [
            "2025-09-14T15:00",
            "2025-09-14T16:00",
            "2025-09-14T17:00",
            "2025-09-14T18:00",
        ],
        "temperature_2m": [70.1, 71.5, 72.0, 73.2],
        "apparent_temperature": [69.0, 70.2, 71.0, 72.0],
        "precipitation_probability": [5, 10, 70, 65],
        "precipitation": [0.0, 0.0, 0.12, 0.1],
        "wind_speed_10m": [8.0, 12.0, 22.5, 19.0],
        "wind_gusts_10m": [14.0, 20.0, 31.0, 28.0],
        "snowfall": [0.0, 0.0, 0.0, 0.0],
    },
}


# --- nflverse draft_picks / combine (columns verified against the live files) --

DRAFT_PICKS_CSV = """season,round,pick,team,gsis_id,pfr_player_id,cfb_player_id,pfr_player_name,position,college,age
2026,1,3,ARI,00-0041027,LoveJe00,jeremiyah-love-1,Jeremiyah Love,RB,Notre Dame,21
2026,1,4,TEN,00-0041438,TateCa00,carnell-tate-1,Carnell Tate,WR,Ohio St.,21
2026,2,40,GNB,00-0041500,SmitJo00,john-smith-1,John Smith,WR,Oregon,22
2026,3,90,KAN,,BlacKa00,kaelon-black-1,Kaelon Black,RB,Penn St.,24
2026,4,120,NWE,00-0041600,JoneDe00,derek-jones-1,Derek Jones,DE,Alabama,22
2025,1,1,ARI,00-0039999,OldPl00,old-player-1,Old Player,RB,Texas,22
"""

COMBINE_CSV = """season,draft_year,draft_team,draft_round,draft_ovr,pfr_id,cfb_id,player_name,pos,school,ht,wt,forty,bench,vertical,broad_jump,cone,shuttle
2026,2026,ARI,1,3,LoveJe00,jeremiyah-love-1,Jeremiyah Love,RB,Notre Dame,71,212,4.36,,38,124,,
2026,2026,TEN,1,4,,carnell-tate-1,Carnell Tate,WR,Ohio St.,73,195,4.41,,36,,6.9,4.2
2026,2026,GNB,2,40,SmitJo00,,John Smith,WR,Oregon,72,190,4.5,,,,,
2025,2025,ARI,1,1,OldPl00,old-player-1,Old Player,RB,Texas,70,205,4.4,,,,,
"""
