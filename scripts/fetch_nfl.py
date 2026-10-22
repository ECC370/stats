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
import extras  # noqa: E402
from schema import NFL_COLS, NFL_PARTS, PLAYER_COLS, write_parts  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
STATS_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
             "stats_player/stats_player_week_{season}.csv")
INJ_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
           "injuries/injuries_{season}.csv")
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


def spread_text(r):
    sl = r.spread_line
    if pd.isna(sl):
        return ""
    if sl == 0:
        return "PK"
    return f"{r.home_team} -{sl:g}" if sl > 0 else f"{r.away_team} -{abs(sl):g}"


def rest_label(n):
    if pd.isna(n):
        return ""
    n = int(n)
    return f"{n}d short" if n <= 5 else (f"{n}d bye" if n >= 13 else f"{n}d")


def clock(day, t):
    try:
        d = pd.Timestamp(day)
        h, m = str(t).split(":")[:2]
        h = int(h)
        return f"{d.strftime('%a')} {(h - 1) % 12 + 1}:{m} {'PM' if h >= 12 else 'AM'} ET"
    except Exception:
        return str(day)


def slot_of(day, t):
    """TNF, SNF or MNF for primetime games, blank for the rest."""
    try:
        wd = pd.Timestamp(day).weekday()
        h = int(str(t).split(":")[0])
    except Exception:
        return ""
    return "TNF" if wd == 3 else ("MNF" if wd == 0 else ("SNF" if wd == 6 and h >= 19 else ""))


def nfl_extras(log, games, cur, today):
    """Write slate, games, streaks, teams and injuries files for the coming games."""
    log = log.copy()
    log["group"] = log.pos
    todays = today.isoformat()
    done = games[games.home_score.notna() & games.away_score.notna()]
    up = games[(games.season == cur) & games.home_score.isna() & (games.gameday >= todays)]
    # injuries (latest report for the coming week)
    inj_raw = get_csv(INJ_URL.format(season=cur))
    inj = pd.DataFrame(columns=["team", "pos", "player", "status", "injury", "practice"])
    if inj_raw is not None and len(inj_raw):
        wk = int(up.week.min()) if len(up) else int(inj_raw.week.max())
        r = inj_raw[inj_raw.week == wk] if (inj_raw.week == wk).any() else inj_raw[inj_raw.week == inj_raw.week.max()]
        # official game status wins; before Friday fall back to the practice report
        pr = r.practice_status.fillna("").str.lower()
        prac = pr.map(lambda t: "DNP" if t.startswith("did not") else ("Limited" if t.startswith("limited") else ""))
        r = r.assign(report_status=r.report_status.where(r.report_status.notna(), prac.replace("", None)))
        r = r[r.report_status.notna()]
        inj = pd.DataFrame({"team": r.team, "pos": r.position, "player": r.full_name, "status": r.report_status,
                            "injury": r.report_primary_injury.fillna(""), "practice": r.practice_status.fillna("")})
        inj = inj.sort_values(["team", "status", "pos", "player"])
    inj.rename(columns=dict(team="Team", pos="Pos", player="Player", status="Status", injury="Injury",
                            practice="Practice")).to_csv(DATA / "nfl_injuries.csv", index=False)
    status = dict(zip(inj.player, inj.status))
    # team table (points for and against plus yards allowed)
    tg = pd.concat([
        pd.DataFrame({"team": done.home_team, "date": done.gameday, "season": done.season, "pf": done.home_score, "pa": done.away_score}),
        pd.DataFrame({"team": done.away_team, "date": done.gameday, "season": done.season, "pf": done.away_score, "pa": done.home_score})])
    tg = tg[tg.season >= cur - 1]
    gs = log.groupby(["opp", "game_id", "date"])[["PASS_YDS", "RUSH_YDS"]].sum().reset_index()
    gs = gs.sort_values(["opp", "date"], ascending=[True, False]).groupby("opp").head(8)
    ex = gs.groupby("opp")[["PASS_YDS", "RUSH_YDS"]].mean().round(1)
    ex["pass_rk"] = ex.PASS_YDS.rank(ascending=False, method="min").astype(int)
    ex["rush_rk"] = ex.RUSH_YDS.rank(ascending=False, method="min").astype(int)
    ex.columns = ["Pass yds allowed", "Rush yds allowed", "Pass allowed rank", "Rush allowed rank"]
    ex.index.name = "team"
    # Teams tab: rank 1 = best defense (fewest yards allowed), same direction as the def_rk columns.
    # Safe and Slate keep the other direction internally (1 = most allowed = softest matchup).
    disp = ex[["Pass yds allowed", "Rush yds allowed"]].copy()
    disp["Pass D rank"] = disp["Pass yds allowed"].rank(ascending=True, method="min").astype(int)
    disp["Rush D rank"] = disp["Rush yds allowed"].rank(ascending=True, method="min").astype(int)
    tt = extras.team_table(tg, "NFL", disp)
    tt.to_csv(DATA / "nfl_teams.csv", index=False)
    n = extras.CFG["NFL"]["recent_n"]
    ranks = tt.set_index("Team")[[f"L{n}_off_rk", f"L{n}_def_rk"]]
    ranks.columns = ["off_rk", "def_rk"]
    # coming games
    wkgames = up[up.week == up.week.min()] if len(up) else up
    gdict, glist, rest = {}, [], {}
    # sort the injury list by kickoff, then team, then how serious the status is
    order = {}
    for i, g in enumerate(wkgames.sort_values(["gameday", "gametime"]).itertuples()):
        order[g.home_team] = order[g.away_team] = i
    sev = {"Out": 0, "Doubtful": 1, "Questionable": 2, "DNP": 3, "Limited": 4}
    if len(inj):
        inj = inj.assign(_k=inj.team.map(order).fillna(99), _s=inj.status.map(sev).fillna(9))
        inj = inj.sort_values(["_k", "team", "_s", "pos", "player"]).drop(columns=["_k", "_s"])
        inj.rename(columns=dict(team="Team", pos="Pos", player="Player", status="Status", injury="Injury",
                                practice="Practice")).to_csv(DATA / "nfl_injuries.csv", index=False)
    for r in wkgames.itertuples():
        sp = spread_text(r)
        tot = "" if pd.isna(r.total_line) else r.total_line
        sl_ = slot_of(r.gameday, r.gametime)
        gdict[r.home_team] = dict(opp=r.away_team, home=1, fav=sp, total=tot, slot=sl_)
        gdict[r.away_team] = dict(opp=r.home_team, home=0, fav=sp, total=tot, slot=sl_)
        rest[r.home_team], rest[r.away_team] = rest_label(r.home_rest), rest_label(r.away_rest)
        roof = "" if pd.isna(r.roof) else r.roof
        glist.append(dict(when=clock(r.gameday, r.gametime), away=r.away_team, home=r.home_team, spread=sp, total=tot,
                          aml="" if pd.isna(r.away_moneyline) else int(r.away_moneyline),
                          hml="" if pd.isna(r.home_moneyline) else int(r.home_moneyline),
                          arest=rest[r.away_team], hrest=rest[r.home_team], venue=roof))
    res = pd.DataFrame({"date": done.gameday, "home": done.home_team, "away": done.away_team,
                        "hs": done.home_score, "as_": done.away_score})
    skill = inj[inj.pos.isin(["QB", "RB", "WR", "TE"])]
    extras.games_table(glist, tg, res, ranks, skill).to_csv(DATA / "nfl_games.csv", index=False)
    defense = extras.defense_table(log, "NFL")
    slate = extras.make_slate(log, gdict, today, "NFL", status, rest, defense)
    cap = extras.save_slate(slate, DATA / "nfl_slate.csv", "NFL")
    extras.streaks(log, gdict, today, "NFL", status).to_csv(DATA / "nfl_streaks.csv", index=False)
    team_info = {t: f"Run D {int(disp.loc[t, 'Rush D rank'])} / Pass D {int(disp.loc[t, 'Pass D rank'])}" for t in disp.index}
    safe = extras.safe_plays(log, gdict, today, "NFL", status, rest, defense, team_info)
    safe.to_csv(DATA / "nfl_safe.csv", index=False)
    print(f"safe plays: {len(safe)} rows")
    print(f"NFL extras: {len(gdict) // 2} games, slate {len(slate)} rows (cap {cap}), injuries {len(inj)}")


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
    try:
        nfl_extras(log, games, cur, dt.date.today())
    except Exception as exc:  # the extras must never break the main data
        import traceback
        traceback.print_exc()
        print(f"NFL extras skipped: {exc!r}")
    print(f"NFL: {len(log)} rows, {log.player.nunique()} players, "
          f"latest game {log.date.max()}")


if __name__ == "__main__":
    main()
