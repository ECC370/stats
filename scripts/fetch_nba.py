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
from schema import NBA_COLS, PLAYER_COLS  # noqa: E402

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
    d["PRA"] = d.PTS + d.REB + d.AST
    d["PR"] = d.PTS + d.REB
    d["PA"] = d.PTS + d.AST
    d["RA"] = d.REB + d.AST
    d["STL+BLK"] = d.STL + d.BLK
    d["date"] = d.date.dt.strftime("%Y-%m-%d")
    return d[NBA_COLS]


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
    log.to_csv(DATA / "nba_gamelog.csv", index=False)
    latest = (log.sort_values(["player", "date"]).groupby("player").tail(1))
    pos = raw.dropna(subset=["pos"]).drop_duplicates("player")[["player", "pos"]]
    players = latest[["player", "team"]].merge(pos, on="player", how="left")
    players["pos"] = players.pos.fillna("")
    players.sort_values("player")[PLAYER_COLS].to_csv(DATA / "nba_players.csv", index=False)
    print(f"NBA: {len(log)} rows, {log.player.nunique()} players, latest {log.date.max()}")


if __name__ == "__main__":
    main()
