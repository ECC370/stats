"""Shared column layout. The fetch scripts write these exact columns, in this
order, and the workbook builder reads the same lists. Do not reorder without
rebuilding the workbook."""

NBA_COLS = [
    "key", "season", "date", "player", "team", "opp", "home", "starter",
    "abs_margin", "b2b", "rk",
    "MIN", "PTS", "REB", "AST", "3PM", "STL", "BLK", "TOV",
    "PRA", "PR", "PA", "RA", "STL+BLK", "game_id",
]
NBA_STATS = ["PTS", "REB", "AST", "3PM", "STL", "BLK", "TOV",
             "PRA", "PR", "PA", "RA", "STL+BLK", "MIN"]

NFL_COLS = [
    "key", "season", "week", "date", "player", "team", "opp", "home", "pos",
    "abs_margin", "fav_by", "total_line", "rk",
    "PASS_YDS", "PASS_TD", "INT", "COMP", "ATT",
    "RUSH_ATT", "RUSH_YDS", "RUSH_TD",
    "REC", "TGT", "REC_YDS", "REC_TD",
    "RR_YDS", "TDS", "OPP", "FPTS_PPR", "game_id",
]
NFL_STATS = ["PASS_YDS", "PASS_TD", "INT", "COMP", "ATT",
             "RUSH_ATT", "RUSH_YDS", "RUSH_TD",
             "REC", "TGT", "REC_YDS", "REC_TD", "RR_YDS", "TDS", "FPTS_PPR"]

PLAYER_COLS = ["player", "team", "pos"]
