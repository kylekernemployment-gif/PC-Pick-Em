import asyncio

from db import Database
from nba import Game, parse_boxscore, parse_espn, parse_schedule

SCHEDULE = {"leagueSchedule": {"gameDates": [{"gameDate": "10/21/2026 00:00:00", "games": [
    {"gameId": "0022600001", "gameStatus": 1, "gameStatusText": "7:30 pm ET",
     "gameDateTimeUTC": "2026-10-21T23:30:00Z",
     "awayTeam": {"teamCity": "Boston", "teamName": "Celtics", "teamTricode": "BOS", "wins": 0, "losses": 0},
     "homeTeam": {"teamCity": "New York", "teamName": "Knicks", "teamTricode": "NYK", "wins": 0, "losses": 0}},
    {"gameId": "0012600050", "gameStatus": 1, "gameStatusText": "7:00 pm ET",
     "gameDateTimeUTC": "2026-10-10T23:00:00Z",
     "awayTeam": {"teamCity": "Utah", "teamName": "Jazz", "teamTricode": "UTA"},
     "homeTeam": {"teamCity": "Phoenix", "teamName": "Suns", "teamTricode": "PHX"}},
    {"gameId": "0042600101", "gameStatus": 1, "gameStatusText": "TBD",
     "gameDateTimeUTC": "2027-04-18T00:00:00Z",
     "awayTeam": {"teamTricode": ""}, "homeTeam": {"teamTricode": ""}},
    {"gameId": "0022600002", "gameStatus": 1, "gameStatusText": "PPD",
     "gameDateTimeUTC": "2026-10-22T00:00:00Z",
     "awayTeam": {"teamCity": "Golden State", "teamName": "Warriors", "teamTricode": "GSW", "wins": 1, "losses": 0},
     "homeTeam": {"teamCity": "Los Angeles", "teamName": "Lakers", "teamTricode": "LAL", "wins": 0, "losses": 1}},
]}]}}


def test_parse_schedule():
    games = parse_schedule(SCHEDULE)
    assert [g.game_id for g in games] == ["0022600001", "0022600002"]
    g = games[0]
    assert (g.away_tri, g.home_tri, g.away_name, g.home_name) == ("BOS", "NYK", "Boston Celtics", "New York Knicks")
    assert g.tip_utc == 1792625400
    assert games[1].postponed and not games[0].postponed
    assert len(parse_schedule(SCHEDULE, include_preseason=True)) == 3


def test_parse_boxscore():
    r = parse_boxscore({"game": {"gameStatus": 3, "gameStatusText": "Final",
                                 "awayTeam": {"score": 101}, "homeTeam": {"score": 99}}})
    assert r.final and (r.away_score, r.home_score) == (101, 99)
    assert not parse_boxscore({"game": {"gameStatus": 2, "gameStatusText": "Q4 2:00",
                                        "awayTeam": {"score": 90}, "homeTeam": {"score": 88}}}).final
    assert parse_boxscore(None) is None


def test_parse_espn():
    data = {"events": [{"competitions": [{
        "status": {"type": {"name": "STATUS_FINAL", "state": "post", "completed": True}},
        "competitors": [
            {"homeAway": "home", "score": "110", "team": {"abbreviation": "GS"}},
            {"homeAway": "away", "score": "104", "team": {"abbreviation": "LAL"}},
        ]}]}]}
    r = parse_espn(data, "LAL", "GSW")
    assert r.final and (r.away_score, r.home_score) == (104, 110)
    assert parse_espn(data, "BOS", "GSW") is None


def test_full_game_flow(tmp_path):
    async def go():
        db = Database(path=str(tmp_path / "t.db"))
        await db.init()
        g = Game("0022600001", 1000, "BOS", "Boston Celtics", "0-0", "NYK", "New York Knicks", "0-0")
        await db.upsert_games([g])
        assert [x["game_id"] for x in await db.games_to_post(now=500, window=600)] == ["0022600001"]
        assert await db.games_to_post(now=300, window=600) == []  # too early
        await db.mark_posted(g.game_id, 1, 42)
        assert (await db.game_by_message(42))["status"] == "open"

        await db.set_pick(g.game_id, 111, 1)
        await db.set_pick(g.game_id, 222, 2)
        await db.delete_pick(g.game_id, 111, 2)  # wrong emoji: pick survives
        assert await db.get_pick(g.game_id, 111) == 1

        await db.lock_game(g.game_id, {111: 1, 222: 2, 333: 1})
        winners, total = await db.resolve_game(g.game_id, 101, 99, 1, 100)
        assert sorted(winners) == [111, 333] and total == 3
        assert await db.resolve_game(g.game_id, 101, 99, 1, 100) is None  # no double pay

        assert await db.get_points(111) == (100, 1)
        assert await db.record(111) == (1, 0)
        assert await db.record(222) == (0, 1)
        assert await db.add_points(222, 250) == 250
        assert await db.leaderboard() == [(222, 250), (111, 100), (333, 100)]
    asyncio.run(go())
