"""Shared code that turns a player game log into the extra tabs:
slate (hit rates for everyone playing), defense vs position rank, streaks,
team rankings, game list and injuries. Works for NBA and NFL; the fetch
scripts supply the sport specific inputs (todays games, injuries)."""
import numpy as np
import pandas as pd

import re

from schema import (SAFE_RISK, MAX_CUSHION, MAXH, SAFE_CAP_ALL, SAFE_CAP_STAT, SAFE_COLS, SAFE_RULES, SAFE_SHOW, SAFE_THRESH,  # noqa: E402
                    SAFE_GAME_CAP, SAFE_GAME_CAP_STAT, SLATE_CAP, SLATE_COLS, SLATE_WINDOWS, STAT_LABELS, THRESH)

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


EARLY = {}   # sport -> longer stale limit, set in the first weeks of a season when some teams have not played yet


def eligible(g, sport, today):
    """Is this player relevant enough to show on the slate?"""
    last = g.iloc[0]
    if (pd.Timestamp(today) - pd.Timestamp(last["date"])).days > EARLY.get(sport, CFG[sport]["stale_days"]):
        return False
    if sport == "NBA":
        return g.head(10).MIN.mean() >= 15
    return g.head(5).OPP.mean() >= 4


def defense_table(log, sport):
    """{(team, group, stat): (rank, avg allowed per game)}. Rank 1 = best defense (allows the fewest).
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
        ranks = avg.rank(ascending=True, method="min")
        for team in avg.index:
            for st in stats:
                out[(team, grp, st)] = (int(ranks.loc[team, st]), round(float(avg.loc[team, st]), 1))
    return out


ROLE_RULES = {"NFL": [dict(metric="SNAP", recent=2, prior=2, gap=15),     # snap share, percentage points
                      dict(metric="TGT_SH", recent=3, prior=2, gap=10)],  # share of team targets, percentage points
              "NBA": [dict(metric="MIN", recent=3, prior=3, gap=5)]}     # minutes


def role_changes(log, sport):
    """'up' or 'down' when a player's recent usage moved a lot versus his earlier games this season.
    NFL: snap share over the last 2 games, or share of team targets over the last 3. NBA: minutes over the last 3.
    If one signal says up and another says down, no flag. Returns {player: 'up' or 'down'}."""
    cur = log.season.max()
    x = log[log.season == cur]
    votes = {}
    for r in ROLE_RULES[sport]:
        if r["metric"] not in x.columns:
            continue
        y = x.assign(m=pd.to_numeric(x[r["metric"]], errors="coerce"))
        if r["metric"] == "TGT_SH":
            y = y[y.pos.isin(["WR", "TE", "RB"])] if "pos" in y.columns else y
        for player, g in y.sort_values("rk").groupby("player"):
            m = g.m.dropna()
            if len(m) < r["recent"] + r["prior"]:
                continue
            diff = m.iloc[:r["recent"]].mean() - m.iloc[r["recent"]:].mean()
            if diff >= r["gap"]:
                votes.setdefault(player, set()).add("up")
            elif diff <= -r["gap"]:
                votes.setdefault(player, set()).add("down")
    return {p: v.pop() for p, v in votes.items() if len(v) == 1}


def make_slate(log, games, today, sport, status=None, rest=None, defense=None, role=None):
    """games: {team: {opp, home, fav, total}}. Returns DataFrame in SLATE_COLS."""
    status, rest, defense, role = status or {}, rest or {}, defense or {}, role or {}
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
                hits = [round(float((v >= t).mean()), 2) for t in ts]
                dr, da = defense.get((info["opp"], grp, stat), ("", ""))
                rows.append([None, None, player, last.get("pos", ""), last.team, info["opp"],
                             "H" if info["home"] else "A", info.get("fav", ""), info.get("total", ""),
                             status.get(player, ""), rest.get(last.team, ""), stat, w, 0, len(x),
                             round(float(v.mean()), 1), round(float(x[cfg["usage"]].mean()), 1), dr, da, role.get(player, "")]
                            + hits + [""] * (MAXH - len(hits)) + [game_label(last.team, info)])
    df = pd.DataFrame(rows, columns=SLATE_COLS + ["game"])
    if not len(df):
        return df.assign(grank=0)
    df = df.sort_values(["stat", "window", "avg"], ascending=[True, True, False]).copy()
    df["rank"] = df.groupby(["stat", "window"]).cumcount() + 1
    df["grank"] = df.groupby(["game", "stat", "window"]).cumcount() + 1
    return df


def game_label(team, info):
    """Away at Home, the same text the Games tab builds, so the Slate and Safe game pickers match."""
    return f"{info['opp']} at {team}" if info["home"] else f"{team} at {info['opp']}"


GAME_CAP = {"NFL": 12, "NBA": 15}    # players kept per game, per stat and window, for the game picker


def with_keys(df, cap, gcap):
    """Rows needed for the ALL list (rank <= cap) or for a single game list (grank <= gcap).
    One row can serve both lists, so the game picker costs one extra column, not a second copy."""
    d = df[(df["rank"] <= cap) | (df["grank"] <= gcap)].copy()
    d["key"] = ""
    ok = d["rank"] <= cap
    d.loc[ok, "key"] = "ALL|" + d.stat[ok] + "|" + d.window[ok] + "|" + d["rank"][ok].astype(str)
    d["gkey"] = ""
    gk = d["grank"] <= gcap
    d.loc[gk, "gkey"] = d.game[gk] + "|" + d.stat[gk] + "|" + d.window[gk] + "|" + d["grank"][gk].astype(str)
    return d[SLATE_COLS]


SLATE_PARTS = 2          # the slate is written as 2 files so per game lists do not get trimmed
SLATE_MAX_ROWS = 11500   # the sheet reads up to row 12000


def save_slate(df, path, sport):
    """Write the slate as name_1.csv and name_2.csv (each under Google's import limit), trimming ranks only if
    even two files are not enough. Also writes the old single file (trimmed to fit) for sheets built before the split."""
    import os
    path = str(path)
    stem = path[:-4]
    def fit(limit, cap, gcap, rows_max=10**9):
        while True:
            d = with_keys(df, cap, gcap) if len(df) else pd.DataFrame(columns=SLATE_COLS)
            text = d.to_csv(index=False)
            if (len(text.encode()) <= limit and len(d) <= rows_max) or cap <= 20:
                return text, cap
            cap -= 5
            gcap = max(8, min(gcap, cap // 5))
    # old single file
    text1, _ = fit(LIMIT, SLATE_CAP[sport], min(GAME_CAP[sport], 10))
    with open(path, "w", newline="") as f:
        f.write(text1)
    # split files
    text, cap = fit(SLATE_PARTS * LIMIT - 20_000, SLATE_CAP[sport], GAME_CAP[sport], SLATE_MAX_ROWS)
    lines = text.split("\n")
    header, body = lines[0], [l for l in lines[1:] if l]
    parts, cur, size = [], [], len(header) + 1
    for l in body:
        b = len(l.encode()) + 1
        if cur and size + b > LIMIT - 5_000 and len(parts) < SLATE_PARTS - 1:
            parts.append(cur); cur, size = [], len(header) + 1
        cur.append(l); size += b
    parts.append(cur)
    for i in range(SLATE_PARTS):
        rows = parts[i] if i < len(parts) else []
        with open(f"{stem}_{i + 1}.csv", "w", newline="") as f:
            f.write(header + "\n" + "".join(r + "\n" for r in rows))
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
    def block(x, tag, mingp=1):
        a = x.groupby("team").agg(gp=("pf", "size"), pf=("pf", "mean"), pa=("pa", "mean"))
        a = a[a.gp >= mingp]   # one game is noise, leave it blank
        a["net"] = a.pf - a.pa
        a["off_rk"] = a.pf.rank(ascending=False, method="min").astype(int)
        a["def_rk"] = a.pa.rank(ascending=True, method="min").astype(int)
        a = a.round(1)
        a.columns = [f"{tag}_{c}" for c in a.columns]
        return a
    out = block(tg[tg.rk <= n], f"L{n}").join(block(tg[tg.season == cur], "season", 3), how="outer")
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


def games_table(glist, tg, results, ranks, inj, units=None):
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
                     last_meetings(results, a, h)]
                    + ([", ".join(units.get(t, {}).get(k, [])) for k in ("OL", "DB", "FRONT") for t in (a, h)] if units is not None else []))
    cols = ["When", "Away", "Home", "Spread", "Total", "Away ML", "Home ML", "Away rest", "Home rest", "Venue",
            "Away out", "Away questionable", "Home out", "Home questionable", "Away form", "Home form",
            "Away off rank", "Away def rank", "Home off rank", "Home def rank", "Last meetings"]
    if units is not None:
        cols += ["Away OL out", "Home OL out", "Away DB out", "Home DB out", "Away front out", "Home front out"]
    return pd.DataFrame(rows, columns=cols)


def ordinal(n):
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


COMBO_STATS = {"PRA", "PR", "PA", "RA", "RR_YDS"}


def safe_plays(log, games, today, sport, status=None, rest=None, defense=None, team_info=None, haircut=0.15, units=None, role=None):
    """For each player on the slate, each stat and each risk level (Safest, Balanced, Reach): the
    HIGHEST line that cleared often enough in the recent and the longer window.
    Grade A = near perfect hit rates and no red flags. B = very high long rate with at most one flag.
    C = long rate of 75%+. D = the rest (a reach). Players listed Out or Doubtful are left out."""
    status, rest, defense, team_info = status or {}, rest or {}, defense or {}, team_info or {}
    rules, thr = SAFE_RULES[sport], SAFE_THRESH[sport]
    nt = len({k[0] for k in defense}) or 30      # teams ranked: rank 1 = best defense, nt = worst
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
        who = player + (f" ({last.get('pos')})" if sport == "NFL" and last.get("pos") else "")
        game = f"{last.team} {'vs' if info['home'] else 'at'} {info['opp']}"
        slot = game_label(last.team, info)
        for stat, ts in thr.items():
            if r[stat].mean() < 0.25 * ts[0]:
                continue
            for risk, (min_r, min_l) in SAFE_RISK[sport].items():
                for t in sorted(ts, reverse=True):
                    hr, hl = float((r[stat] >= t).mean()), float((lg[stat] >= t).mean())
                    if hr < min_r or hl < min_l:
                        continue
                    avg = float(lg[stat].mean())
                    cushion = (avg - t) / avg if avg else 0
                    if cushion > MAX_CUSHION:
                        break
                    dr, da = defense.get((info["opp"], grp, stat), ("", ""))
                    tag = "" if dr == "" else ("Soft" if dr >= nt - 7 else ("Tough" if dr <= nt - 22 else "Mid"))
                    flags, issues = [], 0
                    if tag == "Tough":
                        issues += 1; flags.append("tough matchup")
                    if "B2B" in rt or "3in4" in rt or "short" in rt:
                        issues += 1; flags.append(rt)
                    if st == "Questionable":
                        issues += 1; flags.append("questionable")
                    elif st == "DNP":
                        issues += 1; flags.append("missed practice")
                    my_u = (units or {}).get(last.team, {}) if units else {}
                    op_u = (units or {}).get(info["opp"], {}) if units else {}
                    pass_stat = stat in ("PASS_YDS", "PASS_TD", "COMP", "ATT", "INT", "REC", "TGT", "REC_YDS", "REC_TD", "RR_YDS")
                    rush_stat = stat.startswith("RUSH") or stat in ("RR_YDS", "TDS")
                    n_ol = len(my_u.get("OL", []))
                    if n_ol >= 2:
                        issues += 1; flags.append(f"{n_ol} OL starters out")
                    elif n_ol == 1:
                        flags.append("1 OL starter out")
                    if pass_stat and op_u.get("DB"):
                        flags.append(f"+ {len(op_u['DB'])} opp DBs out")
                    if rush_stat and op_u.get("FRONT"):
                        flags.append(f"+ {len(op_u['FRONT'])} opp front out")
                    rl = (role or {}).get(player, "")
                    if rl == "down":
                        issues += 1; flags.append("role down")
                    elif rl == "up":
                        flags.append("+ role up")
                    if st == "Check roster":
                        issues += 1; flags.append("last season stats, check roster")
                    if spread >= 10:
                        issues += 1; flags.append("big spread, game script risk")
                    if hr >= 1 and hl >= 0.95 and issues == 0:
                        grade = "A"
                    elif issues <= 1 and hl >= 0.9:
                        grade = "B"
                    elif hl >= 0.75:
                        grade = "C"
                    else:
                        grade = "D"
                    score = (hr + hl) / 2 + (0.05 if tag == "Soft" else 0) - 0.04 * issues
                    p = min(((hr + hl) / 2) * (1 - haircut), 0.82)   # backtest: "safe" lines really hit about 77 to 82%
                    rows.append([None, grade, who, game, (STAT_LABELS[stat].capitalize() if stat in ("DBL_DBL", "TRP_DBL") else f"Over {t - 0.5:g} {STAT_LABELS[stat]}"), stat, round(avg, 1),
                                 f"{int(round(hr * len(r)))} of {len(r)}", f"{int(round(hl * len(lg)))} of {len(lg)}",
                                 "" if not tag else f"{tag} ({ordinal(dr)})", round(1 / p, 2), "; ".join(flags),
                                 team_info.get(info["opp"], ""), round(score, 3), round(cushion, 2), player, t, risk, None, slot])
                    break
    df = pd.DataFrame(rows, columns=SAFE_COLS + ["slot"])
    if not len(df):
        return df[SAFE_COLS]
    order = {"A": 0, "B": 1, "C": 2, "D": 3}
    df["_g"] = df.grade.map(order)
    df = df.sort_values(["_g", "score", "cushion"], ascending=[True, False, True])
    allowed = {"A only": {"A"}, "A and B": {"A", "B"}, "A to C": {"A", "B", "C"}, "All": {"A", "B", "C", "D"}}
    parts = []
    for risk in SAFE_RISK[sport]:
        dr_ = df[df.risk == risk]
        for show in SAFE_SHOW:
            d = dr_[dr_.grade.isin(allowed[show])]
            # best single stat and best combined stat per player (combined stats often have no alternate ladder)
            combo = d.stat.isin(COMBO_STATS)
            keep = set(d[~combo].drop_duplicates("player").index) | set(d[combo].drop_duplicates("player").index)
            best = d[d.index.isin(keep)].copy()
            lists = [("ALL", best)] + [(stat, x) for stat, x in d.groupby("stat")]
            for stat, x in lists:
                x = x.copy()
                x["i"] = range(1, len(x) + 1)
                x["gi"] = x.groupby("slot").cumcount() + 1
                capn = SAFE_CAP_ALL if stat == "ALL" else SAFE_CAP_STAT
                gcap = SAFE_GAME_CAP if stat == "ALL" else SAFE_GAME_CAP_STAT
                x = x[(x.i <= capn) | (x.gi <= gcap)].copy()
                x["key"] = ""
                ok = x.i <= capn
                x.loc[ok, "key"] = [f"ALL|{stat}|{risk}|{show}|{i}" for i in x.i[ok]]
                x["gkey"] = ""
                gk = x.gi <= gcap
                x.loc[gk, "gkey"] = [f"{g}|{stat}|{risk}|{show}|{i}" for g, i in zip(x.slot[gk], x.gi[gk])]
                parts.append(x)
    return pd.concat(parts)[SAFE_COLS]
