"""NBA schedule + score fetching.

Schedule:  NBA's official schedule feed (cdn.nba.com).
Scores:    NBA's official live boxscore feed, with ESPN's public scoreboard as a
           backup in case the NBA feed is down or slow to flip a game to Final.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger("pcpickem.nba")

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BOXSCORE_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}

EASTERN = ZoneInfo("America/New_York")

# gameId prefixes: 001 preseason, 002 regular season, 003 All-Star,
# 004 playoffs, 005 play-in, 006 NBA Cup championship.
REAL_GAME_PREFIXES = ("002", "004", "005", "006")
PRESEASON_PREFIX = "001"

# ESPN abbreviations that differ from NBA tricodes.
ESPN_TO_NBA = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}


@dataclass
class Game:
    game_id: str
    tip_utc: int  # unix seconds
    away_tri: str
    away_name: str
    away_record: str
    home_tri: str
    home_name: str
    home_record: str
    postponed: bool = False
    time_tbd: bool = False


@dataclass
class Result:
    final: bool
    away_score: int = 0
    home_score: int = 0
    postponed: bool = False


def _team_name(team: dict) -> str:
    return f"{team.get('teamCity', '')} {team.get('teamName', '')}".strip()


def _parse_utc(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _is_postponed(text: str) -> bool:
    t = (text or "").lower()
    return "ppd" in t or "postponed" in t or "cancel" in t or "suspended" in t


def parse_schedule(data: dict, include_preseason: bool = False) -> list[Game]:
    prefixes = REAL_GAME_PREFIXES + ((PRESEASON_PREFIX,) if include_preseason else ())
    games: list[Game] = []
    for day in data.get("leagueSchedule", {}).get("gameDates", []):
        for g in day.get("games", []):
            gid = str(g.get("gameId", ""))
            if not gid.startswith(prefixes):
                continue
            home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
            # Playoff games before matchups are decided have no teams yet.
            if not home.get("teamTricode") or not away.get("teamTricode"):
                continue
            when = g.get("gameDateTimeUTC")
            if not when:
                continue
            status_text = g.get("gameStatusText") or ""
            games.append(Game(
                game_id=gid,
                tip_utc=_parse_utc(when),
                away_tri=away["teamTricode"],
                away_name=_team_name(away),
                away_record=f"{away.get('wins', 0)}-{away.get('losses', 0)}",
                home_tri=home["teamTricode"],
                home_name=_team_name(home),
                home_record=f"{home.get('wins', 0)}-{home.get('losses', 0)}",
                postponed=_is_postponed(status_text),
                time_tbd="tbd" in status_text.lower(),
            ))
    return games


def parse_boxscore(data: dict) -> Result | None:
    g = (data or {}).get("game")
    if not g:
        return None
    if _is_postponed(g.get("gameStatusText", "")):
        return Result(final=False, postponed=True)
    final = g.get("gameStatus") == 3
    return Result(
        final=final,
        away_score=int(g.get("awayTeam", {}).get("score") or 0),
        home_score=int(g.get("homeTeam", {}).get("score") or 0),
    )


def parse_espn(data: dict, away_tri: str, home_tri: str) -> Result | None:
    for event in (data or {}).get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        teams = {}
        for c in comp.get("competitors", []):
            abbr = c.get("team", {}).get("abbreviation", "")
            teams[c.get("homeAway")] = (ESPN_TO_NBA.get(abbr, abbr), c.get("score"))
        if teams.get("home", ("",))[0] != home_tri or teams.get("away", ("",))[0] != away_tri:
            continue
        status = (comp.get("status") or event.get("status") or {}).get("type", {})
        name = status.get("name", "")
        if "POSTPONED" in name or "CANCELED" in name or "SUSPENDED" in name:
            return Result(final=False, postponed=True)
        final = bool(status.get("completed")) and status.get("state") == "post"
        return Result(
            final=final,
            away_score=int(teams["away"][1] or 0),
            home_score=int(teams["home"][1] or 0),
        )
    return None


class NBAClient:
    def __init__(self, session: aiohttp.ClientSession, include_preseason: bool = False):
        self.session = session
        self.include_preseason = include_preseason

    async def _get_json(self, url: str, params: dict | None = None) -> dict | None:
        try:
            async with self.session.get(url, params=params, headers=HEADERS) as resp:
                if resp.status != 200:
                    # The NBA boxscore feed returns 403 until a game has started.
                    log.debug("GET %s -> HTTP %s", url, resp.status)
                    return None
                return await resp.json(content_type=None)
        except Exception as e:  # network blips shouldn't crash the loop
            log.warning("GET %s failed: %r", url, e)
            return None

    async def fetch_schedule(self) -> list[Game] | None:
        data = await self._get_json(SCHEDULE_URL)
        if data is None:
            return None
        return parse_schedule(data, self.include_preseason)

    async def fetch_result(self, game: dict) -> Result | None:
        """Best available result for a game: NBA feed first, ESPN as backup."""
        nba = parse_boxscore(await self._get_json(BOXSCORE_URL.format(game_id=game["game_id"])))
        if nba and (nba.final or nba.postponed):
            return nba

        et_date = datetime.fromtimestamp(game["tip_utc"], tz=timezone.utc).astimezone(EASTERN)
        espn = parse_espn(
            await self._get_json(ESPN_SCOREBOARD_URL, {"dates": et_date.strftime("%Y%m%d")}),
            game["away_tri"], game["home_tri"],
        )
        if espn and (espn.final or espn.postponed):
            return espn
        return nba or espn
