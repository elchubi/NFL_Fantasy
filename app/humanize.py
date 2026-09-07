"""Translate Sleeper's terse setting keys into plain language."""

from __future__ import annotations

from typing import Any

# --- Roster slots ------------------------------------------------------------

SLOT_LABELS: dict[str, str] = {
    "QB": "Quarterback",
    "RB": "Running Back",
    "WR": "Wide Receiver",
    "TE": "Tight End",
    "K": "Kicker",
    "DEF": "Team Defense / Special Teams",
    "DL": "Defensive Lineman",
    "LB": "Linebacker",
    "DB": "Defensive Back",
    "IDP_FLEX": "IDP Flex (DL/LB/DB)",
    "FLEX": "Flex (RB/WR/TE)",
    "WRRB_FLEX": "Flex (RB/WR)",
    "REC_FLEX": "Flex (WR/TE)",
    "SUPER_FLEX": "Superflex (QB/RB/WR/TE)",
    "BN": "Bench",
    "IR": "Injured Reserve",
    "TAXI": "Taxi Squad",
}

# Slots that do not take part in the weekly starting lineup.
NON_STARTING_SLOTS = {"BN", "IR", "TAXI"}

# --- League settings ---------------------------------------------------------

LEAGUE_TYPES = {0: "Redraft", 1: "Keeper", 2: "Dynasty"}
PLAYOFF_ROUND_TYPES = {
    0: "One week per round",
    1: "Two weeks per round from the semifinals",
    2: "Two weeks per round, every round",
}
PLAYOFF_SEED_TYPES = {
    0: "Standard re-seeding each round",
    1: "Fixed bracket (no re-seeding)",
}
WAIVER_DAYS = {
    0: "Sunday",
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
}

# --- Scoring -----------------------------------------------------------------

SCORING_GROUPS: dict[str, tuple[str, ...]] = {
    "passing": ("pass_",),
    "rushing": ("rush_",),
    "receiving": ("rec", "bonus_rec_"),
    "kicking": ("fgm", "fgmiss", "xpm", "xpmiss"),
    "defense_special_teams": ("def_", "st_", "pts_allow", "yds_allow", "sack", "int",
                              "safe", "blk_kick", "ff", "fum_rec", "fum_ret"),
    "turnovers": ("fum",),
    "idp": ("idp_",),
    "bonuses": ("bonus_",),
}

SCORING_LABELS: dict[str, str] = {
    # Passing
    "pass_yd": "Points per passing yard",
    "pass_td": "Points per passing touchdown",
    "pass_int": "Points per interception thrown",
    "pass_2pt": "Points per passing 2-point conversion",
    "pass_cmp": "Points per completion",
    "pass_inc": "Points per incompletion",
    "pass_att": "Points per pass attempt",
    "pass_fd": "Points per passing first down",
    "pass_sack": "Points per sack taken",
    "pass_int_td": "Points per pick-six thrown",
    "pass_cmp_40p": "Points per completion of 40+ yards",
    "pass_td_40p": "Points per passing touchdown of 40+ yards",
    "pass_td_50p": "Points per passing touchdown of 50+ yards",
    # Rushing
    "rush_yd": "Points per rushing yard",
    "rush_td": "Points per rushing touchdown",
    "rush_2pt": "Points per rushing 2-point conversion",
    "rush_att": "Points per rushing attempt",
    "rush_fd": "Points per rushing first down",
    "rush_40p": "Points per rush of 40+ yards",
    "rush_td_40p": "Points per rushing touchdown of 40+ yards",
    "rush_td_50p": "Points per rushing touchdown of 50+ yards",
    # Receiving
    "rec": "Points per reception (PPR)",
    "rec_yd": "Points per receiving yard",
    "rec_td": "Points per receiving touchdown",
    "rec_2pt": "Points per receiving 2-point conversion",
    "rec_fd": "Points per receiving first down",
    "rec_40p": "Points per reception of 40+ yards",
    "rec_td_40p": "Points per receiving touchdown of 40+ yards",
    "rec_td_50p": "Points per receiving touchdown of 50+ yards",
    "bonus_rec_qb": "Bonus points per reception by a QB",
    "bonus_rec_rb": "Bonus points per reception by a RB",
    "bonus_rec_wr": "Bonus points per reception by a WR",
    "bonus_rec_te": "Bonus points per reception by a TE (TE premium)",
    # Turnovers / fumbles
    "fum": "Points per fumble",
    "fum_lost": "Points per fumble lost",
    "fum_rec": "Points per fumble recovered",
    "fum_rec_td": "Points per fumble recovery touchdown",
    "fum_ret_yd": "Points per fumble return yard",
    # Kicking
    "xpm": "Points per extra point made",
    "xpmiss": "Points per extra point missed",
    "fgm": "Points per field goal made",
    "fgmiss": "Points per field goal missed",
    "fgm_0_19": "Points per field goal made from 0-19 yards",
    "fgm_20_29": "Points per field goal made from 20-29 yards",
    "fgm_30_39": "Points per field goal made from 30-39 yards",
    "fgm_40_49": "Points per field goal made from 40-49 yards",
    "fgm_50p": "Points per field goal made from 50+ yards",
    "fgmiss_0_19": "Points per field goal missed from 0-19 yards",
    "fgmiss_20_29": "Points per field goal missed from 20-29 yards",
    "fgmiss_30_39": "Points per field goal missed from 30-39 yards",
    "fgmiss_40_49": "Points per field goal missed from 40-49 yards",
    "fgmiss_50p": "Points per field goal missed from 50+ yards",
    # Team defense / special teams
    "def_td": "Points per defensive touchdown",
    "def_st_td": "Points per defensive or special teams touchdown",
    "def_st_ff": "Points per special teams forced fumble",
    "def_st_fum_rec": "Points per special teams fumble recovery",
    "def_st_tkl_solo": "Points per special teams solo tackle",
    "def_kr_td": "Points per kick return touchdown",
    "def_pr_td": "Points per punt return touchdown",
    "def_2pt": "Points per defensive 2-point return",
    "def_forced_punts": "Points per forced punt",
    "st_td": "Points per special teams touchdown",
    "st_ff": "Points per special teams forced fumble",
    "st_fum_rec": "Points per special teams fumble recovery",
    "st_tkl_solo": "Points per special teams solo tackle",
    "sack": "Points per sack",
    "sack_yd": "Points per sack yard",
    "int": "Points per interception by the defense",
    "ff": "Points per forced fumble",
    "safe": "Points per safety",
    "blk_kick": "Points per blocked kick",
    "blk_kick_ret_yd": "Points per blocked kick return yard",
    "pts_allow_0": "Points when the defense allows 0 points",
    "pts_allow_1_6": "Points when the defense allows 1-6 points",
    "pts_allow_7_13": "Points when the defense allows 7-13 points",
    "pts_allow_14_20": "Points when the defense allows 14-20 points",
    "pts_allow_21_27": "Points when the defense allows 21-27 points",
    "pts_allow_28_34": "Points when the defense allows 28-34 points",
    "pts_allow_35p": "Points when the defense allows 35+ points",
    "yds_allow_0_100": "Points when the defense allows 0-99 yards",
    "yds_allow_100_199": "Points when the defense allows 100-199 yards",
    "yds_allow_200_299": "Points when the defense allows 200-299 yards",
    "yds_allow_300_349": "Points when the defense allows 300-349 yards",
    "yds_allow_350_399": "Points when the defense allows 350-399 yards",
    "yds_allow_400_449": "Points when the defense allows 400-449 yards",
    "yds_allow_450_499": "Points when the defense allows 450-499 yards",
    "yds_allow_500_549": "Points when the defense allows 500-549 yards",
    "yds_allow_550p": "Points when the defense allows 550+ yards",
    # IDP
    "idp_tkl": "Points per IDP tackle",
    "idp_tkl_solo": "Points per IDP solo tackle",
    "idp_tkl_ast": "Points per IDP assisted tackle",
    "idp_tkl_loss": "Points per IDP tackle for loss",
    "idp_sack": "Points per IDP sack",
    "idp_int": "Points per IDP interception",
    "idp_ff": "Points per IDP forced fumble",
    "idp_fum_rec": "Points per IDP fumble recovery",
    "idp_def_td": "Points per IDP defensive touchdown",
    "idp_safe": "Points per IDP safety",
    "idp_pass_def": "Points per IDP pass defended",
    "idp_blk_kick": "Points per IDP blocked kick",
    # Yardage bonuses
    "bonus_pass_yd_300": "Bonus for 300+ passing yards",
    "bonus_pass_yd_400": "Bonus for 400+ passing yards",
    "bonus_rush_yd_100": "Bonus for 100+ rushing yards",
    "bonus_rush_yd_200": "Bonus for 200+ rushing yards",
    "bonus_rec_yd_100": "Bonus for 100+ receiving yards",
    "bonus_rec_yd_200": "Bonus for 200+ receiving yards",
    "bonus_pass_cmp_25": "Bonus for 25+ completions",
    "bonus_rush_att_20": "Bonus for 20+ rushing attempts",
    "bonus_sack_2p": "Bonus for 2+ sacks",
    "bonus_tkl_10p": "Bonus for 10+ tackles",
}


def scoring_label(key: str) -> str:
    """A readable description for a scoring key, guessed if unknown."""
    if key in SCORING_LABELS:
        return SCORING_LABELS[key]
    pretty = key.replace("_", " ")
    return f"Points for {pretty}"


def scoring_group(key: str) -> str:
    for group, prefixes in SCORING_GROUPS.items():
        for prefix in prefixes:
            if key == prefix or key.startswith(prefix):
                return group
    return "other"


def slot_label(slot: str) -> str:
    return SLOT_LABELS.get(slot, slot)


def waiver_type_label(settings: dict[str, Any]) -> str:
    """Describe the waiver system.

    A non-zero budget is the reliable tell for FAAB; otherwise fall back to
    Sleeper's waiver_type flag.
    """
    budget = settings.get("waiver_budget") or 0
    waiver_type = settings.get("waiver_type")
    if budget:
        return f"FAAB blind bidding (${budget} season budget)"
    mapping = {
        0: "Rolling waiver order (order moves to the back after a claim)",
        1: "Reverse standings waiver order (resets weekly)",
        2: "FAAB blind bidding",
    }
    return mapping.get(waiver_type, f"Unknown waiver type ({waiver_type})")


def trade_deadline_label(settings: dict[str, Any]) -> str:
    deadline = settings.get("trade_deadline")
    if deadline is None:
        return "Not set"
    if deadline >= 18:
        return "No trade deadline"
    return f"End of week {deadline}"


def playoff_summary(settings: dict[str, Any]) -> dict[str, Any]:
    teams = settings.get("playoff_teams")
    start = settings.get("playoff_week_start")
    return {
        "teams_in_playoffs": teams,
        "starts_week": start,
        "round_format": PLAYOFF_ROUND_TYPES.get(
            settings.get("playoff_round_type"),
            f"Unknown ({settings.get('playoff_round_type')})",
        ),
        "seeding": PLAYOFF_SEED_TYPES.get(
            settings.get("playoff_seed_type"),
            f"Unknown ({settings.get('playoff_seed_type')})",
        ),
        "description": (
            f"{teams} teams make the playoffs, starting in week {start}."
            if teams and start
            else "Playoff configuration is not set yet."
        ),
    }
