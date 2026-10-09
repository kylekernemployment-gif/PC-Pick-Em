"""Build the 82-0 game's player data from Basketball-Reference CSVs.

Usage:
    git clone --depth 1 https://github.com/sumitrodatta/bball-reference-datasets bbref
    python tools/build_8202_data.py bbref/Data features/data/eighty_two_zero.json.gz

For every franchise (today's 30 teams, including their old names) and decade
(1960s-2020s) it keeps up to 40 players who played there, each with their
per-game averages for that team in that decade (all those seasons, weighted
by games played), both raw and era-adjusted.
"""

from __future__ import annotations

import csv
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

# Historical abbreviation -> today's franchise.
FRANCHISE = {
    "ATL": "ATL", "STL": "ATL", "MLH": "ATL", "TRI": "ATL",
    "BOS": "BOS",
    "BRK": "BRK", "NJN": "BRK", "NYN": "BRK",
    "CHO": "CHO", "CHA": "CHO", "CHH": "CHO",
    "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
    "DET": "DET", "FTW": "DET",
    "GSW": "GSW", "SFW": "GSW", "PHW": "GSW",
    "HOU": "HOU", "SDR": "HOU",
    "IND": "IND",
    "LAC": "LAC", "SDC": "LAC", "BUF": "LAC",
    "LAL": "LAL", "MNL": "LAL",
    "MEM": "MEM", "VAN": "MEM",
    "MIA": "MIA", "MIL": "MIL", "MIN": "MIN",
    "NOP": "NOP", "NOH": "NOP", "NOK": "NOP",
    "NYK": "NYK",
    "OKC": "OKC", "SEA": "OKC",
    "ORL": "ORL",
    "PHI": "PHI", "SYR": "PHI",
    "PHO": "PHO", "POR": "POR",
    "SAC": "SAC", "KCK": "SAC", "KCO": "SAC", "CIN": "SAC", "ROC": "SAC",
    "SAS": "SAS", "TOR": "TOR",
    "UTA": "UTA", "NOJ": "UTA",
    "WAS": "WAS", "WSB": "WAS", "CAP": "WAS", "BAL": "WAS", "CHZ": "WAS", "CHP": "WAS",
}
DECADES = [f"{d}s" for d in range(1960, 2030, 10)]
STATS = ("pts", "trb", "ast", "stl", "blk")
MIN_GAMES = 20
PLAYERS_PER_ROSTER = 40


def f(x: str) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def decade_of(season: int) -> str:
    return f"{(season - 1) // 10 * 10}s"  # season 1987 = 1986-87 -> 1980s


def season_label(season: int) -> str:
    return f"{season - 1}-{str(season)[-2:]}"


def main(src: Path, out: Path) -> None:
    # League averages per season (per team per game) for era adjustment.
    lg: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    names: dict[tuple[int, str], str] = {}
    for r in csv.DictReader(open(src / "Team Stats Per Game.csv")):
        if r["lg"] != "NBA" or r["team"] == "League Average":
            continue
        s = int(r["season"])
        for k in STATS:
            v = f(r[f"{k}_per_game"])
            if v is not None:
                lg[s][k].append(v)
    for r in csv.DictReader(open(src / "Team Abbrev.csv")):
        if r["lg"] == "NBA":
            names[(int(r["season"]), r["abbreviation"])] = r["team"]
    lg_avg = {s: {k: sum(v) / len(v) for k, v in d.items() if v} for s, d in lg.items()}
    base = {k: sum(lg_avg[s][k] for s in range(2001, 2021)) / 20 for k in STATS}  # 2000s-2010s baseline

    rows = [r for r in csv.DictReader(open(src / "Player Per Game.csv"))
            if r["lg"] == "NBA" and r["team"] in FRANCHISE and 1961 <= int(r["season"])]

    # Steals/blocks weren't tracked before 1973-74: fit simple estimates on 1974-1985.
    def fit(target: str, feats: list[str]) -> list[float]:
        import numpy as np
        X, y = [], []
        for r in rows:
            s = int(r["season"])
            if 1974 <= s <= 1985 and f(r["g"]) and f(r["g"]) >= MIN_GAMES:
                vals = [f(r[c]) for c in feats] + [f(r[f"{target}_per_game"])]
                if None not in vals:
                    X.append(vals[:-1] + [1.0])
                    y.append(vals[-1])
        coef, *_ = np.linalg.lstsq(np.array(X), np.array(y), rcond=None)
        return list(coef)
    stl_coef = fit("stl", ["ast_per_game", "mp_per_game"])
    blk_coef = fit("blk", ["trb_per_game", "mp_per_game"])

    career_pos: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r["pos"] in ("PG", "SG", "SF", "PF", "C"):
            career_pos[r["player_id"]].add(r["pos"])

    order = ["PG", "SG", "SF", "PF", "C"]
    groups: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        s = int(r["season"])
        g = f(r["g"]) or 0
        if g <= 0 or s not in lg_avg or not career_pos[r["player_id"]]:
            continue
        raw = {k: f(r[f"{k}_per_game"]) for k in STATS}
        est = False
        if raw["stl"] is None:
            est = True
            raw["stl"] = max(0.0, stl_coef[0] * (raw["ast"] or 0) + stl_coef[1] * (f(r["mp_per_game"]) or 0) + stl_coef[2])
        if raw["blk"] is None:
            est = True
            raw["blk"] = max(0.0, blk_coef[0] * (raw["trb"] or 0) + blk_coef[1] * (f(r["mp_per_game"]) or 0) + blk_coef[2])
        if None in raw.values():
            continue
        adj = {}
        for k in STATS:
            league = lg_avg[s].get(k) or base[k]  # no league stl/blk before 1974 -> no adjustment
            adj[k] = raw[k] * base[k] / league
        key = (FRANCHISE[r["team"]], decade_of(s))
        groups[key][r["player_id"]].append({
            "name": r["player"], "season": s, "g": g, "raw": raw, "adj": adj, "est": est,
            "team_name": names.get((s, r["team"]), r["team"]),
        })

    teams: dict[str, dict] = {}
    for (fr, dec), players in groups.items():
        entries = []
        team_names: dict[str, int] = defaultdict(int)
        for pid, seasons in players.items():
            games = sum(x["g"] for x in seasons)
            for x in seasons:
                team_names[x["team_name"]] += int(x["g"])
            if games < MIN_GAMES:
                continue
            # Per-game averages across every season with this team in this decade, weighted by games.
            avg = lambda key, k: sum(x[key][k] * x["g"] for x in seasons) / games
            first, last = min(x["season"] for x in seasons), max(x["season"] for x in seasons)
            span = season_label(first) if first == last else f"{first - 1}-{str(last)[-2:]}"
            pos = "/".join(p for p in order if p in career_pos[pid])
            entries.append((games, [
                seasons[0]["name"], pos, span,
                *[round(avg("raw", k), 1) for k in STATS],
                *[round(avg("adj", k), 2) for k in STATS],
                int(any(x["est"] for x in seasons)), int(games),
            ]))
        entries.sort(key=lambda e: -e[0])
        if len(entries) < 5:
            continue
        t = teams.setdefault(fr, {"decades": {}})
        t["decades"][dec] = {
            "label": max(team_names, key=team_names.get),
            "players": [e[1] for e in entries[:PLAYERS_PER_ROSTER]],
        }
    for fr, t in teams.items():
        latest = max(s for (s, a) in names if FRANCHISE.get(a) == fr)
        t["name"] = next(n for (s, a), n in names.items() if s == latest and FRANCHISE.get(a) == fr)

    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fields": ["name", "pos", "season", "pts", "reb", "ast", "stl", "blk",
                   "adj_pts", "adj_reb", "adj_ast", "adj_stl", "adj_blk", "est", "games"],
        "decades": DECADES,
        "teams": teams,
        "source": "Basketball-Reference via github.com/sumitrodatta/bball-reference-datasets",
    }
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    n = sum(len(d["players"]) for t in teams.values() for d in t["decades"].values())
    print(f"{len(teams)} franchises, {sum(len(t['decades']) for t in teams.values())} team-decades, {n} players -> {out}")
    print("stl est coef", [round(c, 3) for c in stl_coef], "blk est coef", [round(c, 3) for c in blk_coef])


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
