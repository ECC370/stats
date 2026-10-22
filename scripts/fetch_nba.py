"""Pull NBA box scores from ESPN's public (unofficial) JSON feed and keep a
running game log in data/nba_gamelog.csv.

How it works each night:
  1. Look at ESPN's scoreboard for each day since the last day we already have.
  2. For every finished game we have not stored yet, download its box score.
  3. Add the player rows to data/nba_raw.csv (the permanent store).
  4. Rebuild data/nba_gamelog.csv and data/nba_players.csv from the store.

The first run backfills from --since (default: start of last season), which
takes several minutes because it is one request per game. Later runs only fetch
new games. ESPN's feed is unofficial, so if it ever changes shape, the parse
step will skip games and say so in the log.
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
import extras  # noqa: E402
from schema import NBA_COLS, NBA_PARTS, PLAYER_COLS, write_parts  # noqa: E402  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "nba_raw.csv"
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
SCOREBOARD = BASE + "/scoreboard?dates={d}&limit=100"
SUMMARY = BASE + "/summary?event={e}"
SKIP_SEASON_TYPES = {1, 4}          # 1 = preseason; 4 = offseason / all-star style
RAW_COLS = ["game_id", "date", "season", "player_id", "player", "pos", "team",
            "opp", "home", "starter", "team_pts", "opp_pts",
            "MIN", "PTS", "REB", "AST", "3PM", "STL", "BLK", "TOV"]
HEADERS = {"User-Agent": "Mozilla/5.0 (personal stats sheet)"}


def season_of(date):
    """2026-27 season -> 2027 (the year the season ends)."""
    return date.year + 1 if date.month >= 8 else date.year


def get_json(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code} for {url}")
            if r.status_code in (403, 404):
                return None
        except Exception as exc:  # network hiccup
            print(f"  error {exc!r} for {url}")
        time.sleep(1.5 * (i + 1))
    return None


def num(text):
    """'34' -> 34.0, '12:30' -> 12.5, '--' -> None"""
    if text is None:
        return None
    s = str(text).strip()
    if not s or s in ("--", "-"):
        return None
    if ":" in s:
        m, sec = s.split(":", 1)
        try:
            return int(m) + int(sec) / 60
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def made(text):
    """'3-7' -> 3"""
    if text is None:
        return None
    s = str(text)
    if "-" in s:
        return num(s.split("-", 1)[0])
    return num(s)


def parse_summary(js, game_date):
    """Turn one ESPN box score JSON into a list of player rows (dicts)."""
    rows = []
    comp = js["header"]["competitions"][0]
    teams = {}
    for c in comp["competitors"]:
        tid = str(c["team"]["id"])
        teams[tid] = {"abbr": c["team"].get("abbreviation", tid),
                      "home": 1 if c.get("homeAway") == "home" else 0,
                      "score": num(c.get("score"))}
    if len(teams) != 2:
        return rows
    tids = list(teams)
    for tblock in js["boxscore"]["players"]:
        tid = str(tblock["team"]["id"])
        if tid not in teams:
            continue
        other = teams[tids[1] if tid == tids[0] else tids[0]]
        me = teams[tid]
        for grp in tblock.get("statistics", []):
            labels = grp.get("labels") or grp.get("names") or []
            for a in grp.get("athletes", []):
                if a.get("didNotPlay"):
                    continue
                stats = a.get("stats") or []
                if not stats or len(stats) != len(labels):
                    continue
                rec = dict(zip(labels, stats))
                minutes = num(rec.get("MIN"))
                if not minutes or minutes <= 0:
                    continue
                ath = a.get("athlete", {})
                pos = (ath.get("position") or {}).get("abbreviation", "")
                rows.append({
                    "player_id": ath.get("id"),
                    "player": ath.get("displayName"),
                    "pos": pos,
                    "team": me["abbr"],
                    "opp": other["abbr"],
                    "home": me["home"],
                    "starter": 1 if a.get("starter") else 0,
                    "team_pts": me["score"],
                    "opp_pts": other["score"],
                    "MIN": round(minutes, 1),
                    "PTS": num(rec.get("PTS")) or 0,
                    "REB": num(rec.get("REB")) or 0,
                    "AST": num(rec.get("AST")) or 0,
                    "3PM": made(rec.get("3PT")) or 0,
                    "STL": num(rec.get("STL")) or 0,
                    "BLK": num(rec.get("BLK")) or 0,
                    "TOV": num(rec.get("TO")) or 0,
                })
    return rows


def load_raw():
    if RAW.exists():
        return pd.read_csv(RAW, dtype={"game_id": str, "player_id": str})
    return pd.DataFrame(columns=RAW_COLS)


def fetch_new(raw, since, until):
    known = set(raw.game_id.astype(str)) if len(raw) else set()
    new_rows, skipped = [], 0
    d = since
    while d <= until:
        sb = get_json(SCOREBOARD.format(d=d.strftime("%Y%m%d")))
        time.sleep(0.2)
        for ev in (sb or {}).get("events", []):
            eid = str(ev["id"])
            stype = (ev.get("season") or {}).get("type")
            name = ev.get("name", "") + ev.get("shortName", "")
            done = ((ev.get("status") or {}).get("type") or {}).get("completed")
            if eid in known or not done or stype in SKIP_SEASON_TYPES \
                    or "All-Star" in name or "All Star" in name:
                continue
            js = get_json(SUMMARY.format(e=eid))
            time.sleep(0.25)
            if not js:
                skipped += 1
                continue
            try:
                rows = parse_summary(js, d)
            except Exception as exc:
                print(f"  could not read game {eid}: {exc!r}")
                skipped += 1
                continue
            if not rows:
                print(f"  game {eid} on {d}: no player rows found")
                skipped += 1
                continue
            for r in rows:
                r.update(game_id=eid, date=d.isoformat(), season=season_of(d))
            new_rows.extend(rows)
            known.add(eid)
        d += dt.timedelta(days=1)
    print(f"fetched {len(new_rows)} new player rows, skipped {skipped} games")
    if not new_rows:
        return raw
    add = pd.DataFrame(new_rows)[RAW_COLS]
    return pd.concat([raw, add], ignore_index=True) if len(raw) else add


def build_log(raw):
    d = raw.copy()
    d["date"] = pd.to_datetime(d["date"])
    # unique names: if two different players share a name, add the team
    ids = d.groupby("player")["player_id"].nunique()
    dup = ids[ids > 1].index
    if len(dup):
        m = d.player.isin(dup)
        d.loc[m, "player"] = d.loc[m, "player"] + " (" + d.loc[m, "team"] + ")"
    d["abs_margin"] = (d.team_pts - d.opp_pts).abs()

    # back to back: did the team play the day before?
    sched = d[["team", "date"]].drop_duplicates()
    played = set(zip(sched.team, sched.date))
    prev = d.date - pd.Timedelta(days=1)
    d["b2b"] = [1 if (t, p) in played else 0 for t, p in zip(d.team, prev)]

    d = d.sort_values(["player", "date"], ascending=[True, False])
    d = d.drop_duplicates(["player", "game_id"])
    d["rk"] = d.groupby("player").cumcount() + 1
    d["key"] = d.player + "|" + d.rk.astype(str)
    d["THREES"] = d["3PM"]
    d["PRA"] = d.PTS + d.REB + d.AST
    d["PR"] = d.PTS + d.REB
    d["PA"] = d.PTS + d.AST
    d["RA"] = d.REB + d.AST
    d["STL+BLK"] = d.STL + d.BLK
    d["date"] = d.date.dt.strftime("%Y-%m-%d")
    return d[NBA_COLS]


def norm_status(text):
    t = (text or "").lower()
    if "out" in t:
        return "Out"
    if "doubt" in t:
        return "Doubtful"
    if "quest" in t or "day" in t or "gtd" in t:
        return "Questionable"
    return text or ""


def espn_injuries():
    """All teams' injury report from ESPN -> DataFrame(team, player, status, detail)."""
    teams = {}
    tj = get_json(BASE + "/teams") or {}
    for sp in tj.get("sports", []):
        for lg in sp.get("leagues", []):
            for t in lg.get("teams", []):
                t = t.get("team", t)
                teams[str(t.get("displayName"))] = t.get("abbreviation")
    js = get_json(BASE + "/injuries") or {}
    rows = []
    for blk in js.get("injuries", []):
        abbr = teams.get(str(blk.get("displayName")), blk.get("displayName"))
        for i in blk.get("injuries", []):
            ath = i.get("athlete") or {}
            name = ath.get("displayName")
            if not name:
                continue
            rows.append([abbr, name, norm_status(i.get("status")),
                         (i.get("shortComment") or i.get("longComment") or "")[:140]])
    return pd.DataFrame(rows, columns=["team", "player", "status", "detail"])


def clock_et(iso):
    try:
        from zoneinfo import ZoneInfo
        t = pd.Timestamp(iso).tz_convert(ZoneInfo("America/Toronto"))
        return t.strftime("%a ") + f"{(t.hour - 1) % 12 + 1}:{t.minute:02d} {'PM' if t.hour >= 12 else 'AM'} ET"
    except Exception:
        return str(iso)


def ml(team_odds):
    if not isinstance(team_odds, dict):
        return ""
    v = team_odds.get("moneyLine", team_odds.get("moneyline", ""))
    return "" if v is None else v


def todays_games(today):
    """Scoreboard for `today`: ({team: {opp, home, fav, total}}, [game dicts])."""
    sb = get_json(SCOREBOARD.format(d=today.strftime("%Y%m%d"))) or {}
    out, glist = {}, []
    for ev in sb.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        odds = (comp.get("odds") or [{}])[0]
        fav, total = odds.get("details") or "", odds.get("overUnder") or ""
        for a, b in ((cs[0], cs[1]), (cs[1], cs[0])):
            out[a["team"]["abbreviation"]] = {"opp": b["team"]["abbreviation"],
                                              "home": 1 if a.get("homeAway") == "home" else 0,
                                              "fav": fav, "total": total}
        home = next(c for c in cs if c.get("homeAway") == "home")
        away = next(c for c in cs if c.get("homeAway") == "away")
        glist.append(dict(when=clock_et(ev.get("date")), away=away["team"]["abbreviation"],
                          home=home["team"]["abbreviation"], spread=fav, total=total,
                          aml=ml(odds.get("awayTeamOdds")), hml=ml(odds.get("homeTeamOdds")), venue=""))
    return out, glist


def nba_rest(raw, games, today):
    """Rest text per team playing today: B2B, 3in4 (3 games in 4 nights) or days of rest."""
    sched = raw[["team", "date"]].drop_duplicates()
    sched["date"] = pd.to_datetime(sched["date"])
    out = {}
    for team in games:
        ds = sched[sched.team == team].date
        if not len(ds):
            continue
        days = (pd.Timestamp(today) - ds.max()).days - 1
        recent = ((ds >= pd.Timestamp(today) - pd.Timedelta(days=3)) & (ds < pd.Timestamp(today))).sum()
        tags = (["B2B"] if days == 0 else []) + (["3in4"] if recent >= 2 else [])
        out[team] = " ".join(tags) or f"{max(days, 0)}d"
    return out


def nba_extras(raw, log, today):
    """Write slate, games, streaks, teams and injuries files."""
    log = log.copy()
    pos = raw.dropna(subset=["pos"]).drop_duplicates("player").set_index("player")["pos"]
    grp = lambda p: "C" if str(p).split("-")[0].upper() == "C" else (  # noqa: E731
        "G" if str(p).split("-")[0].upper() in ("PG", "SG", "G") else "F")
    log["pos"] = log.player.map(pos).fillna("")
    log["group"] = log.pos.map(grp)
    games, glist = todays_games(today)
    inj = espn_injuries()
    inj.rename(columns=dict(team="Team", player="Player", status="Status", detail="Detail")).sort_values(
        ["Team", "Status"]).to_csv(DATA / "nba_injuries.csv", index=False)
    status = dict(zip(inj.player, inj.status))
    # first weeks of a season: teams that have not played yet still have last season's stats only
    cur = int(log.season.max())
    early = (pd.Timestamp(today) - pd.Timestamp(log[log.season == cur].date.min())).days < 30
    if early:
        extras.EARLY["NBA"] = 250
        for p in log[(log.rk == 1) & (log.season < cur)].player:
            status.setdefault(p, "Check roster")
    rest = nba_rest(raw, games, today)
    tgm = raw[["team", "game_id", "date", "season", "team_pts", "opp_pts"]].drop_duplicates(["team", "game_id"])
    tg = tgm.rename(columns={"team_pts": "pf", "opp_pts": "pa"})
    tt = extras.team_table(tg, "NBA")
    tt.to_csv(DATA / "nba_teams.csv", index=False)
    n = extras.CFG["NBA"]["recent_n"]
    ranks = tt.set_index("Team")[[f"L{n}_off_rk", f"L{n}_def_rk"]]
    ranks.columns = ["off_rk", "def_rk"]
    hm = raw[raw.home == 1].drop_duplicates(["game_id"])
    res = pd.DataFrame({"date": hm.date, "home": hm.team, "away": hm.opp, "hs": hm.team_pts, "as_": hm.opp_pts})
    for g in glist:
        g["arest"], g["hrest"] = rest.get(g["away"], ""), rest.get(g["home"], "")
    extras.games_table(glist, tg, res, ranks, inj).to_csv(DATA / "nba_games.csv", index=False)
    defense = extras.defense_table(log, "NBA")
    slate = extras.make_slate(log, games, today, "NBA", status, rest, defense)
    cap = extras.save_slate(slate, DATA / "nba_slate.csv", "NBA")
    extras.streaks(log, games, today, "NBA", status).to_csv(DATA / "nba_streaks.csv", index=False)
    pa = tt.set_index("Team")[f"L{n}_pa"].rank(ascending=False, method="min")
    team_info = {t: f"Pts allowed rk {int(v)}" for t, v in pa.items()}
    safe = extras.safe_plays(log, games, today, "NBA", status, rest, defense, team_info)
    safe.to_csv(DATA / "nba_safe.csv", index=False)
    print(f"NBA extras: {len(games) // 2} games today, slate {len(slate)} rows (cap {cap}), injuries {len(inj)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="first date to fetch (YYYY-MM-DD) if no data yet")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    raw = load_raw()
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date()
    until = today - dt.timedelta(days=1)
    if len(raw):
        since = max(pd.to_datetime(raw.date).max().date() - dt.timedelta(days=2),
                    dt.date(2000, 1, 1))
    elif args.since:
        since = dt.date.fromisoformat(args.since)
    else:
        start_year = today.year - 1 if today.month >= 8 else today.year - 2
        since = dt.date(start_year, 10, 1)
    print(f"fetching games from {since} to {until}")
    raw = fetch_new(raw, since, until)
    if not len(raw):
        raise SystemExit("no NBA data found; ESPN may have blocked the request")
    raw.to_csv(RAW, index=False)
    log = build_log(raw)
    write_parts(log, DATA, "nba_gamelog", NBA_PARTS)
    latest = (log.sort_values(["player", "date"]).groupby("player").tail(1))
    pos = raw.dropna(subset=["pos"]).drop_duplicates("player")[["player", "pos"]]
    players = latest[["player", "team"]].merge(pos, on="player", how="left")
    players["pos"] = players.pos.fillna("")
    players.sort_values("player")[PLAYER_COLS].to_csv(DATA / "nba_players.csv", index=False)
    try:
        nba_extras(raw, log, today)
    except Exception as exc:  # the extras must never break the main data
        import traceback
        traceback.print_exc()
        print(f"NBA extras skipped: {exc!r}")
    print(f"NBA: {len(log)} rows, {log.player.nunique()} players, latest {log.date.max()}")


if __name__ == "__main__":
    main()
