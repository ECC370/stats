"""Pull NFL weekly player stats (free, from the nflverse project on GitHub),
reshape into the game log layout, and write data/nfl_gamelog.csv and
data/nfl_players.csv. Safe to run every night: it rebuilds from scratch.

Games where a player recorded no stats at all are not in the source data, so
very low lines (like 1+ reception) look a little better than they really are.
"""
import datetime as dt
import io
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from schema import NFL_COLS, NFL_PARTS, PLAYER_COLS, write_parts  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
STATS_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
             "stats_player/stats_player_week_{season}.csv")
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
POSITIONS = {"QB", "RB", "WR", "TE"}


def current_season(today=None):
    today = today or dt.date.today()
    return today.year if today.month >= 3 else today.year - 1


def get_csv(url):
    r = requests.get(url, timeout=120)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return pd.read_csv(io.BytesIO(r.content), low_memory=False)


def build(seasons, games):
    frames = []
    for s in seasons:
        d = get_csv(STATS_URL.format(season=s))
        if d is None:
            print(f"season {s}: no file yet, skipping")
            continue
        frames.append(d)
    if not frames:
        raise SystemExit("no NFL stats downloaded")
    d = pd.concat(frames, ignore_index=True)
    d = d[d.position.isin(POSITIONS) & d.season_type.isin(["REG", "POST"])].copy()

    g = games[["game_id", "gameday", "home_team", "away_team", "home_score",
               "away_score", "spread_line", "total_line"]].copy()
    d = d.merge(g, on="game_id", how="left")
    # keep only games that have been played
    d = d[d.home_score.notna() & d.away_score.notna()].copy()

    is_home = d.team == d.home_team
    d["home"] = is_home.astype(int)
    own = d.home_score.where(is_home, d.away_score)
    oth = d.away_score.where(is_home, d.home_score)
    d["abs_margin"] = (own - oth).abs().astype(int)
    # spread_line > 0 means the home team is favoured
    d["fav_by"] = d.spread_line.where(is_home, -d.spread_line)
    d["total_line"] = d.total_line

    f = lambda c: d[c].fillna(0)  # noqa: E731
    out = pd.DataFrame({
        "season": d.season.astype(int),
        "week": d.week.astype(int),
        "date": d.gameday,
        "player": d.player_display_name,
        "team": d.team,
        "opp": d.opponent_team,
        "home": d.home,
        "pos": d.position,
        "abs_margin": d.abs_margin,
        "fav_by": d.fav_by,
        "total_line": d.total_line,
        "PASS_YDS": f("passing_yards"),
        "PASS_TD": f("passing_tds"),
        "INT": f("passing_interceptions"),
        "COMP": f("completions"),
        "ATT": f("attempts"),
        "RUSH_ATT": f("carries"),
        "RUSH_YDS": f("rushing_yards"),
        "RUSH_TD": f("rushing_tds"),
        "REC": f("receptions"),
        "TGT": f("targets"),
        "REC_YDS": f("receiving_yards"),
        "REC_TD": f("receiving_tds"),
        "FPTS_PPR": f("fantasy_points_ppr"),
        "game_id": d.game_id,
        "_pid": d.player_id,
    })
    out["RR_YDS"] = out.RUSH_YDS + out.REC_YDS
    out["TDS"] = out.RUSH_TD + out.REC_TD
    out["OPP"] = out.ATT + out.RUSH_ATT + out.TGT

    # Make player names unique if two different players share a name
    n_ids = out.groupby("player")["_pid"].nunique()
    dup = n_ids[n_ids > 1].index
    if len(dup):
        mask = out.player.isin(dup)
        out.loc[mask, "player"] = out.loc[mask, "player"] + " (" + out.loc[mask, "team"] + ")"

    # One row per player per game
    out = out.sort_values(["player", "date", "week"], ascending=[True, False, False])
    out = out.drop_duplicates(["player", "game_id"])
    out["rk"] = out.groupby("player").cumcount() + 1
    out["key"] = out.player + "|" + out.rk.astype(str)
    out = out[NFL_COLS]
    return out


def main():
    cur = current_season()
    games = get_csv(GAMES_URL)
    log = build([cur - 1, cur], games)
    DATA.mkdir(exist_ok=True)
    write_parts(log, DATA, "nfl_gamelog", NFL_PARTS)

    latest = (log.sort_values(["player", "date"])
                 .groupby("player").tail(1)[["player", "team", "pos"]]
                 .sort_values("player"))
    latest[PLAYER_COLS].to_csv(DATA / "nfl_players.csv", index=False)
    print(f"NFL: {len(log)} rows, {log.player.nunique()} players, "
          f"latest game {log.date.max()}")


if __name__ == "__main__":
    main()
