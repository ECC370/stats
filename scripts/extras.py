"""Shared code that turns a player game log into the extra tabs:
slate (hit rates for everyone playing), defense vs position rank, streaks,
team rankings, game list and injuries. Works for NBA and NFL; the fetch
scripts supply the sport specific inputs (todays games, injuries)."""
import numpy as np
import pandas as pd

import re

from schema import (SAFE_CAP_ALL, SAFE_CAP_STAT, SAFE_COLS, SAFE_RULES, SLATE_CAP, SLATE_COLS,  # noqa: E402
                    SLATE_WINDOWS, THRESH)

LIMIT = 900_000
WINDOW_SPEC = {
    "L3": ("rk", 3), "L5": ("rk", 5), "L8": ("rk", 8), "L10": ("rk", 10), "L20": ("rk", 20),
    "SEASON": ("season", 0), "LAST SEASON": ("season", -1),
}
CFG = {
    "NBA": dict(usage="MIN", recent_n=10, def_n=20, factor=0.25, stale_days=21, rank_stat="PTS"),
    "NFL": dict(usage="OPP", recent_n=5, def_n=8, factor=0.25, stale_days=28, rank_stat="PASS_YDS"),
}


def subset(g, window, cur):
    kind, val = WINDOW_SPEC[window]
    return g[g.rk <= val] if kind == "rk" else g[g.season == cur + val]


def eligible(g, sport, today):
    """Is this player relevant enough to show on the slate?"""
    last = g.iloc[0]
    if (pd.Timestamp(today) - pd.Timestamp(last["date"])).days > CFG[sport]["stale_days"]:
        return False
    if sport == "NBA":
        return g.head(10).MIN.mean() >= 15
    return g.head(5).OPP.mean() >= 4


def defense_table(log, sport):
    """{(team, group, stat): (rank, avg allowed per game)}. Rank 1 = allows the most.
    Looks at each defending team's most recent games and adds up what players of
    that position group produced against them."""
    stats = list(THRESH[sport])
    n = CFG[sport]["def_n"]
    d = log[["game_id", "date", "opp", "group"] + stats]
    games = (d[["opp", "game_id", "date"]].drop_duplicates()
             .sort_values(["opp", "date"], ascending=[True, False]).groupby("opp").head(n))
    out = {}
    for grp in sorted(d.group.dropna().unique()):
        sums = d[d.group == grp].groupby(["opp", "game_id"])[stats].sum().reset_index()
        m = games.merge(sums, on=["opp", "game_id"], how="left").fillna(0)
        avg = m.groupby("opp")[stats].mean()
        ranks = avg.rank(ascending=False, method="min")
        for team in avg.index:
            for st in stats:
                out[(team, grp, st)] = (int(ranks.loc[team, st]), round(float(avg.loc[team, st]), 1))
    return out


def make_slate(log, games, today, sport, status=None, rest=None, defense=None):
    """games: {team: {opp, home, fav, total}}. Returns DataFrame in SLATE_COLS."""
    status, rest, defense = status or {}, rest or {}, defense or {}
    cfg, thr = CFG[sport], THRESH[sport]
    cur = int(log.season.max())
    wins = SLATE_WINDOWS[sport]
    rows = []
    for player, g in log.groupby("player"):
        g = g.sort_values("rk")
        last = g.iloc[0]
        if last.team not in games or not eligible(g, sport, today):
            continue
        info = games[last.team]
        grp = last.group
        subs = {w: subset(g, w, cur) for w in wins}
        for stat, ts in thr.items():
            for w in wins:
                x = subs[w]
                if len(x) == 0:
                    continue
                v = x[stat]
                if v.mean() < cfg["factor"] * ts[0]:
                    continue
                hits = [round(float((v >= t).mean()), 3) for t in ts]
                dr, da = defense.get((info["opp"], grp, stat), ("", ""))
                rows.append([None, player, last.get("pos", ""), last.team, info["opp"],
                             "H" if info["home"] else "A", info.get("fav", ""), info.get("total", ""),
                             status.get(player, ""), rest.get(last.team, ""), stat, w, 0, len(x),
                             round(float(v.mean()), 1), round(float(x[cfg["usage"]].mean()), 1), dr, da]
                            + hits + [""] * (8 - len(hits)))
    df = pd.DataFrame(rows, columns=SLATE_COLS)
    if len(df):
        df = df.sort_values(["stat", "window", "avg"], ascending=[True, True, False])
        df["rank"] = df.groupby(["stat", "window"]).cumcount() + 1
        df = df[df["rank"] <= SLATE_CAP[sport]].copy()
        df["key"] = df.stat + "|" + df.window + "|" + df["rank"].astype(str)
    return df


def save_slate(df, path, sport):
    """Write the slate, trimming ranks until it fits Google's import limit."""
    cap = SLATE_CAP[sport]
    while True:
        text = df[df["rank"] <= cap].to_csv(index=False)
        if len(text.encode()) <= LIMIT or cap <= 20:
            break
        cap -= 5
    with open(path, "w", newline="") as f:
        f.write(text)
    return cap


def streaks(log, games, today, sport, status=None, min_len=5):
    """For players on the slate: the highest line each has cleared in a row,
    for at least min_len straight games."""
    status = status or {}
    thr, cfg = THRESH[sport], CFG[sport]
    rows = []
    for player, g in log.groupby("player"):
        g = g.sort_values("rk")
        last = g.iloc[0]
        if last.team not in games or not eligible(g, sport, today):
            continue
        info = games[last.team]
        for stat, ts in thr.items():
            v = g[stat].to_numpy()
            for t in sorted(ts, reverse=True):
                arr = v >= t
                run = len(arr) if arr.all() else int(np.argmin(arr))
                if run >= min_len:
                    l20 = g.head(20)[stat]
                    rows.append([player, last.get("pos", ""), last.team, info["opp"], status.get(player, ""),
                                 stat, f"{t}+", run, round(float((l20 >= t).mean()), 2),
                                 round(float(g.head(run)[stat].mean()), 1)])
                    break
    df = pd.DataFrame(rows, columns=["Player", "Pos", "Team", "Opp", "Status", "Stat", "Line", "Streak",
                                      "L20 hit", "Avg in streak"])
    return df.sort_values(["Streak", "Player"], ascending=[False, True]).head(400)


def team_table(tg, sport, extra=None):
    """tg: one row per team game with team, date, season, pf, pa. Points based
    ranks over the recent window and the season (not pace adjusted)."""
    n = CFG[sport]["recent_n"]
    tg = tg.sort_values(["team", "date"], ascending=[True, False]).copy()
    tg["rk"] = tg.groupby("team").cumcount() + 1
    cur = int(tg.season.max())
    def block(x, tag):
        a = x.groupby("team").agg(gp=("pf", "size"), pf=("pf", "mean"), pa=("pa", "mean"))
        a["net"] = a.pf - a.pa
        a["off_rk"] = a.pf.rank(ascending=False, method="min").astype(int)
        a["def_rk"] = a.pa.rank(ascending=True, method="min").astype(int)
        a = a.round(1)
        a.columns = [f"{tag}_{c}" for c in a.columns]
        return a
    out = block(tg[tg.rk <= n], f"L{n}").join(block(tg[tg.season == cur], "season"), how="outer")
    if extra is not None:
        out = out.join(extra, how="left")
    out = out.reset_index().rename(columns={"team": "Team"})
    return out.sort_values(f"L{n}_net", ascending=False)


def last_meetings(results, a, b, k=3):
    """results: DataFrame with date, home, away, hs, as_ (scores). Text of the last k meetings."""
    m = results[((results.home == a) & (results.away == b)) | ((results.home == b) & (results.away == a))]
    m = m.sort_values("date", ascending=False).head(k)
    return " | ".join(f"{r.date} {r.away} {int(r.as_)} at {r.home} {int(r.hs)}" for r in m.itertuples())


def form_text(tg, team, k=5):
    x = tg[tg.team == team].sort_values("date", ascending=False).head(k)
    return "".join("W" if r.pf > r.pa else "L" for r in x.itertuples())


def games_table(glist, tg, results, ranks, inj):
    """glist: list of dicts (when, away, home, spread, total, aml, hml, arest, hrest, venue).
    inj: DataFrame with team, player, status."""
    rows = []
    def names(team, sts):
        if inj is None or len(inj) == 0:
            return ""
        x = inj[(inj.team == team) & (inj.status.isin(sts))]
        return ", ".join(x.player.head(8))
    for g in glist:
        a, h = g["away"], g["home"]
        rk = lambda t, c: ranks.loc[t, c] if t in ranks.index else ""
        rows.append([g["when"], a, h, g["spread"], g["total"], g.get("aml", ""), g.get("hml", ""),
                     g.get("arest", ""), g.get("hrest", ""), g.get("venue", ""),
                     names(a, ["Out", "Doubtful"]), names(a, ["Questionable"]),
                     names(h, ["Out", "Doubtful"]), names(h, ["Questionable"]),
                     form_text(tg, a), form_text(tg, h),
                     rk(a, "off_rk"), rk(a, "def_rk"), rk(h, "off_rk"), rk(h, "def_rk"),
                     last_meetings(results, a, h)])
    cols = ["When", "Away", "Home", "Spread", "Total", "Away ML", "Home ML", "Away rest", "Home rest", "Venue",
            "Away out", "Away questionable", "Home out", "Home questionable", "Away form", "Home form",
            "Away off rank", "Away def rank", "Home off rank", "Home def rank", "Last meetings"]
    return pd.DataFrame(rows, columns=cols)


def safe_plays(log, games, today, sport, status=None, rest=None, defense=None, team_info=None, haircut=0.10):
    """For each player on the slate and each stat, the HIGHEST line that cleared in
    nearly every recent game and almost as often over the longer window. Ranked by a
    simple score: average of the two hit rates, nudged by matchup, rest, injury and
    blowout risk. Players listed Out or Doubtful are left out."""
    status, rest, defense, team_info = status or {}, rest or {}, defense or {}, team_info or {}
    rules, thr = SAFE_RULES[sport], THRESH[sport]
    rows = []
    for player, g in log.groupby("player"):
        g = g.sort_values("rk")
        last = g.iloc[0]
        if last.team not in games or not eligible(g, sport, today):
            continue
        st = status.get(player, "")
        if st in ("Out", "Doubtful"):
            continue
        info = games[last.team]
        grp = last.group
        r, lg = g.head(rules["recent"]), g.head(rules["long"])
        if len(r) < rules["min_recent"] or len(lg) < rules["min_long"]:
            continue
        m = re.search(r"(\d+(?:\.\d+)?)", str(info.get("fav", "")))
        spread = float(m.group(1)) if m else 0.0
        rt = rest.get(last.team, "")
        for stat, ts in thr.items():
            if r[stat].mean() < 0.25 * ts[0]:
                continue
            for t in sorted(ts, reverse=True):
                hr, hl = float((r[stat] >= t).mean()), float((lg[stat] >= t).mean())
                if hr >= rules["hit_recent"] and hl >= rules["hit_long"]:
                    avg = float(lg[stat].mean())
                    dr, da = defense.get((info["opp"], grp, stat), ("", ""))
                    tag = "" if dr == "" else ("Soft" if dr <= 8 else ("Tough" if dr >= 23 else "Mid"))
                    score = (hr + hl) / 2 + (0.05 if tag == "Soft" else -0.05 if tag == "Tough" else 0)
                    notes = []
                    if "B2B" in rt or "3in4" in rt or "short" in rt:
                        score -= 0.03; notes.append(rt)
                    if st == "Questionable":
                        score -= 0.05; notes.append("questionable")
                    if spread >= 10:
                        score -= 0.03; notes.append("big spread, game script risk")
                    p = ((hr + hl) / 2) * (1 - haircut)
                    rows.append([None, player, last.get("pos", ""), last.team, info["opp"], "H" if info["home"] else "A",
                                 info.get("fav", ""), st, rt, stat, f"o{t - 0.5:g}",
                                 f"{int(round(hr * len(r)))}/{len(r)}", f"{int(round(hl * len(lg)))}/{len(lg)}",
                                 round(avg, 1), round((avg - t) / avg, 2) if avg else 0, dr, da, tag,
                                 team_info.get(info["opp"], ""), round(1 / p, 2), round(score, 3), "; ".join(notes)])
                    break
    df = pd.DataFrame(rows, columns=SAFE_COLS)
    if not len(df):
        return df
    df["_c"] = df.cushion
    parts = []
    allx = df.sort_values(["score", "_c"], ascending=False).head(SAFE_CAP_ALL).copy()
    allx["key"] = ["ALL|" + str(i) for i in range(1, len(allx) + 1)]
    parts.append(allx)
    for stat, x in df.groupby("stat"):
        x = x.sort_values(["score", "_c"], ascending=False).head(SAFE_CAP_STAT).copy()
        x["key"] = [f"{stat}|{i}" for i in range(1, len(x) + 1)]
        parts.append(x)
    return pd.concat(parts)[SAFE_COLS]
