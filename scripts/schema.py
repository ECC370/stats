"""Shared column layout. The fetch scripts write these exact columns, in this
order, and the workbook builder reads the same lists. Do not reorder without
rebuilding the workbook."""

NBA_COLS = [
    "key", "season", "date", "player", "team", "opp", "home", "starter",
    "abs_margin", "b2b", "rk",
    "MIN", "PTS", "REB", "AST", "THREES", "STL", "BLK", "TOV",
    "PRA", "PR", "PA", "RA", "STL+BLK", "game_id", "DBL_DBL", "TRP_DBL",
    "FGA", "FTA", "FG3A",
]
NBA_STATS = ["PTS", "REB", "AST", "THREES", "STL", "BLK", "TOV",
             "PRA", "PR", "PA", "RA", "DBL_DBL", "TRP_DBL", "MIN"]

NFL_COLS = [
    "key", "season", "week", "date", "player", "team", "opp", "home", "pos",
    "abs_margin", "fav_by", "total_line", "rk",
    "PASS_YDS", "PASS_TD", "INT", "COMP", "ATT",
    "RUSH_ATT", "RUSH_YDS", "RUSH_TD",
    "REC", "TGT", "REC_YDS", "REC_TD",
    "RR_YDS", "TDS", "OPP", "FPTS_PPR", "game_id",
    "SNAP", "TGT_SH", "AY_SH",
]
NFL_STATS = ["PASS_YDS", "PASS_TD", "INT", "COMP", "ATT",
             "RUSH_ATT", "RUSH_YDS", "RUSH_TD",
             "REC", "TGT", "REC_YDS", "REC_TD", "RR_YDS", "TDS", "FPTS_PPR"]

PLAYER_COLS = ["player", "team", "pos"]


DROP_COLS = ["STL+BLK", "game_id"]   # kept in memory, not written (saves file size)
MAX_PART_BYTES = 900_000   # Google Sheets IMPORTDATA refuses big files
NBA_PARTS = 4
NFL_PARTS = 2


def write_parts(df, outdir, base, nparts, max_bytes=MAX_PART_BYTES):
    """Write df (sorted by player) as base_1.csv ... base_N.csv, never splitting
    one player across two files. Unused files hold only the header row.
    Also removes the old single file so it does not go stale."""
    import os
    df = df.drop(columns=[c for c in DROP_COLS if c in df.columns]).copy()
    for c in df.columns:
        if str(df[c].dtype).startswith("float"):
            r = df[c].round(2)
            if (r.dropna() % 1 == 0).all():
                df[c] = r.astype("Int64")
            else:
                df[c] = r
    header = ",".join(df.columns) + "\n"
    lines = df.to_csv(index=False, header=False, lineterminator="\n").split("\n")[:-1]
    players = df["player"].tolist()
    parts, cur, size = [], [], len(header)
    for i, line in enumerate(lines):
        b = len(line.encode()) + 1
        if cur and size + b > max_bytes and players[i] != players[i - 1]:
            parts.append(cur); cur, size = [], len(header)
        cur.append(line); size += b
    parts.append(cur)
    if len(parts) > nparts:
        raise SystemExit(f"{base}: needs {len(parts)} files but the sheet has {nparts} slots")
    for n in range(1, nparts + 1):
        body = parts[n - 1] if n <= len(parts) else []
        with open(os.path.join(outdir, f"{base}_{n}.csv"), "w", newline="") as f:
            f.write(header + "".join(l + "\n" for l in body))
    old = os.path.join(outdir, f"{base}.csv")
    if os.path.exists(old):
        os.remove(old)
    print(f"{base}: {len(lines)} rows in {len(parts)} file(s)")


# ---- slate (players on today's / this week's games, hit rates at every line) ----
NBA_THRESH = {   # X+ means a posted line of X minus 0.5. Double and triple double are yes or no (1 = recorded one).
    "PTS": [5, 10, 15, 20, 25, 30, 35, 40, 45, 50],
    "REB": list(range(2, 17)),
    "AST": list(range(2, 15)),
    "THREES": list(range(1, 9)),
    "PRA": [10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60],
    "PR": [10, 15, 20, 25, 30, 35, 40, 45, 50],
    "PA": [10, 15, 20, 25, 30, 35, 40, 45, 50],
    "RA": [5, 10, 15, 20, 25, 30],
    "STL": [1, 2, 3, 4],
    "BLK": [1, 2, 3, 4],
    "DBL_DBL": [1],
    "TRP_DBL": [1],
}
NFL_THRESH = {   # lines as FanDuel offers them (X+ means a posted line of X minus 0.5)
    "PASS_YDS": [125, 150, 175, 200, 225, 250, 275, 300, 325, 350],
    "PASS_TD": [1, 2, 3, 4],
    "COMP": [18, 20, 22, 24, 26, 28, 30],
    "ATT": [25, 28, 31, 34, 37, 40],
    "RUSH_YDS": [5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100, 110],
    "RUSH_ATT": [8, 10, 12, 15, 18, 20, 22, 25],
    "REC": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    "REC_YDS": [5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100, 110, 125],
    "TGT": [3, 4, 5, 6, 7, 8, 9, 10],
    "RR_YDS": [10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100, 110, 125],
    "TDS": [1, 2],
}
THRESH = {"NBA": NBA_THRESH, "NFL": NFL_THRESH}
SLATE_WINDOWS = {"NBA": ["L5", "L10", "L20", "SEASON", "LAST SEASON"],
                 "NFL": ["L3", "L5", "L8", "SEASON", "LAST SEASON"]}
MAXH = 16   # most lines shown in the Slate grid
SLATE_COLS = (["key", "gkey", "player", "pos", "team", "opp", "home", "fav", "total", "status", "rest",
               "stat", "window", "rank", "gp", "avg", "usage", "def_rk", "def_avg", "role"]
              + [f"h{i}" for i in range(1, MAXH + 1)])
SLATE_CAP = {"NBA": 70, "NFL": 90}


# ---- safe plays ----
STAT_LABELS = {
    "PTS": "points", "REB": "rebounds", "AST": "assists", "THREES": "threes", "PRA": "pts+reb+ast",
    "PR": "pts+reb", "PA": "pts+ast", "RA": "reb+ast", "STL": "steals", "BLK": "blocks", "DBL_DBL": "double double", "TRP_DBL": "triple double",
    "PASS_YDS": "pass yards", "PASS_TD": "pass TDs", "COMP": "completions", "ATT": "pass attempts",
    "RUSH_YDS": "rush yards", "RUSH_ATT": "rush attempts", "REC": "receptions", "REC_YDS": "rec yards",
    "TGT": "targets", "RR_YDS": "rush+rec yards", "TDS": "rush+rec TDs",
}
SAFE_COLS = ["key", "grade", "who", "game", "pick", "stat", "avg", "recent", "long", "matchup", "fair",
             "flags", "team_def", "score", "cushion", "player", "line", "risk", "gkey"]
SAFE_RULES = {   # recent window games, long window games, min games, min recent hit, min long hit
    "NBA": dict(recent=10, long=20, min_recent=8, min_long=12, hit_recent=0.9, hit_long=0.85),
    "NFL": dict(recent=8, long=16, min_recent=6, min_long=8, hit_recent=0.85, hit_long=0.8),
}
# Safe plays only use lines the book offers. Safe shows one line per player, so NBA points can step by 1
# (the book's alt points ladder) without cluttering the tab. The Slate grid stays coarse so it stays readable.
SAFE_THRESH = {"NBA": {**NBA_THRESH, "PTS": list(range(2, 51))}, "NFL": NFL_THRESH}
SAFE_SHOW = ["A only", "A and B", "A to C", "All"]
SAFE_RISK = {   # how aggressive the line is: the minimum recent and long hit rate the line must have cleared
    "NBA": {"Safest": (0.90, 0.85), "Balanced": (0.80, 0.75), "Reach": (0.65, 0.60)},
    "NFL": {"Safest": (0.85, 0.80), "Balanced": (0.70, 0.65), "Reach": (0.55, 0.50)},
}
SAFE_RISK_NAMES = ["Safest", "Balanced", "Reach"]
SAFE_CAP_ALL = 120
SAFE_CAP_STAT = 40
SAFE_GAME_CAP = 12        # best plays kept per game (game picker)
SAFE_GAME_CAP_STAT = 6
MAX_CUSHION = 0.40      # ignore lines far below the average (nobody posts them)
