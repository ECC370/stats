"""Shared column layout. The fetch scripts write these exact columns, in this
order, and the workbook builder reads the same lists. Do not reorder without
rebuilding the workbook."""

NBA_COLS = [
    "key", "season", "date", "player", "team", "opp", "home", "starter",
    "abs_margin", "b2b", "rk",
    "MIN", "PTS", "REB", "AST", "THREES", "STL", "BLK", "TOV",
    "PRA", "PR", "PA", "RA", "STL+BLK", "game_id",
]
NBA_STATS = ["PTS", "REB", "AST", "THREES", "STL", "BLK", "TOV",
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


MAX_PART_BYTES = 900_000   # Google Sheets IMPORTDATA refuses big files
NBA_PARTS = 6
NFL_PARTS = 3


def write_parts(df, outdir, base, nparts, max_bytes=MAX_PART_BYTES):
    """Write df (sorted by player) as base_1.csv ... base_N.csv, never splitting
    one player across two files. Unused files hold only the header row.
    Also removes the old single file so it does not go stale."""
    import os
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
