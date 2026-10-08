"""NBA schedule + score fetching (ESPN's public NBA scoreboard).

The NBA's own feeds (cdn.nba.com) block many cloud hosts, Render included,
so everything comes from ESPN, which serves the same games and live scores.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger("pcpickem.nba")

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

EASTERN = ZoneInfo("America/New_York")

# ESPN season types: 1 preseason, 2 regular season, 3 playoffs, 5 play-in.
PRESEASON = 1

# ESPN abbreviations that differ from the usual NBA tricodes.
ESPN_TO_NBA = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}

POSTPONED_STATUSES = ("POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED", "FORFEIT")


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


def _parse_utc(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _competitors(comp: dict) -> dict[str, dict]:
    return {c.get("homeAway"): c for c in comp.get("competitors", [])}


def _tri(c: dict) -> str:
    abbr = c.get("team", {}).get("abbreviation", "")
    return ESPN_TO_NBA.get(abbr, abbr)


def _record(c: dict) -> str:
    for r in c.get("records") or []:
        if r.get("summary"):
            return r["summary"]
    return ""


def _status_type(event: dict, comp: dict) -> dict:
    return (comp.get("status") or event.get("status") or {}).get("type", {})


def _is_postponed(status: dict) -> bool:
    name = (status.get("name") or "").upper()
    return any(s in name for s in POSTPONED_STATUSES)


def parse_schedule(data: dict, include_preseason: bool = False) -> list[Game]:
    games: list[Game] = []
    for event in (data or {}).get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        if event.get("season", {}).get("type") == PRESEASON and not include_preseason:
            continue
        if comp.get("type", {}).get("abbreviation") == "ALLSTAR" or "all-star" in event.get("name", "").lower():
            continue
        teams = _competitors(comp)
        home, away = teams.get("home"), teams.get("away")
        when = comp.get("date") or event.get("date")
        if not home or not away or not when or not _tri(home) or not _tri(away):
            continue
        games.append(Game(
            game_id=str(event["id"]),
            tip_utc=_parse_utc(when),
            away_tri=_tri(away),
            away_name=away.get("team", {}).get("displayName") or _tri(away),
            away_record=_record(away),
            home_tri=_tri(home),
            home_name=home.get("team", {}).get("displayName") or _tri(home),
            home_record=_record(home),
            postponed=_is_postponed(_status_type(event, comp)),
            time_tbd=comp.get("timeValid") is False or event.get("timeValid") is False,
        ))
    return games


def parse_result(data: dict, game_id: str) -> Result | None:
    for event in (data or {}).get("events", []):
        if str(event.get("id")) != str(game_id):
            continue
        comp = (event.get("competitions") or [{}])[0]
        status = _status_type(event, comp)
        if _is_postponed(status):
            return Result(final=False, postponed=True)
        teams = _competitors(comp)
        return Result(
            final=bool(status.get("completed")) and status.get("state") == "post",
            away_score=int(teams.get("away", {}).get("score") or 0),
            home_score=int(teams.get("home", {}).get("score") or 0),
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
                    log.warning("GET %s %s -> HTTP %s", url, params or "", resp.status)
                    return None
                return await resp.json(content_type=None)
        except Exception as e:  # network blips shouldn't crash the loop
            log.warning("GET %s %s failed: %r", url, params or "", e)
            return None

    async def _scoreboard(self, day: datetime) -> dict | None:
        return await self._get_json(SCOREBOARD_URL, {"dates": day.strftime("%Y%m%d")})

    async def fetch_schedule(self) -> list[Game] | None:
        """Games from yesterday through the next 2 days (US Eastern dates)."""
        today = datetime.now(EASTERN)
        games: dict[str, Game] = {}
        for offset in (-1, 0, 1, 2):
            data = await self._scoreboard(today + timedelta(days=offset))
            if data is None:
                return None
            for g in parse_schedule(data, self.include_preseason):
                games[g.game_id] = g
        return list(games.values())

    async def fetch_result(self, game: dict) -> Result | None:
        tip = datetime.fromtimestamp(game["tip_utc"], tz=timezone.utc).astimezone(EASTERN)
        return parse_result(await self._scoreboard(tip), game["game_id"])
