import asyncio

from db import Database
from nba import Game, parse_result, parse_schedule


def _event(eid, season_type, when, away, home, status="STATUS_SCHEDULED", state="pre",
           completed=False, scores=("0", "0"), records=True, time_valid=True):
    def team(abbr, name, side, score):
        c = {"homeAway": side, "score": score, "team": {"abbreviation": abbr, "displayName": name}}
        if records:
            c["records"] = [{"name": "overall", "summary": "1-0"}]
        return c
    return {"id": eid, "date": when, "name": f"{away[1]} at {home[1]}", "season": {"type": season_type},
            "competitions": [{"date": when, "timeValid": time_valid,
                              "status": {"type": {"name": status, "state": state, "completed": completed}},
                              "competitors": [team(*home, "home", scores[1]), team(*away, "away", scores[0])]}]}


SCOREBOARD = {"events": [
    _event("401", 2, "2026-10-21T23:30Z", ("BOS", "Boston Celtics"), ("NY", "New York Knicks")),
    _event("402", 1, "2026-10-08T23:00Z", ("UTAH", "Utah Jazz"), ("PHX", "Phoenix Suns"), records=False),
    _event("403", 2, "2026-10-22T00:00Z", ("GS", "Golden State Warriors"), ("LAL", "Los Angeles Lakers"),
           status="STATUS_POSTPONED"),
    _event("404", 3, "2027-04-18T00:00Z", ("OKC", "Oklahoma City Thunder"), ("DEN", "Denver Nuggets"),
           time_valid=False),
]}


def test_parse_schedule():
    games = {g.game_id: g for g in parse_schedule(SCOREBOARD)}
    assert sorted(games) == ["401", "403", "404"]  # preseason excluded
    g = games["401"]
    assert (g.away_tri, g.home_tri, g.away_name, g.home_name) == ("BOS", "NYK", "Boston Celtics", "New York Knicks")
    assert g.tip_utc == 1792625400 and g.away_record == "1-0"
    assert games["403"].postponed and not g.postponed
    assert games["404"].time_tbd and not g.time_tbd
    pre = {g.game_id: g for g in parse_schedule(SCOREBOARD, include_preseason=True)}["402"]
    assert (pre.away_tri, pre.home_tri, pre.away_record) == ("UTA", "PHX", "")


def test_parse_result():
    final = {"events": [_event("401", 2, "2026-10-21T23:30Z", ("BOS", "B"), ("NY", "N"),
                               status="STATUS_FINAL", state="post", completed=True, scores=("101", "99"))]}
    r = parse_result(final, "401")
    assert r.final and (r.away_score, r.home_score) == (101, 99)
    live = {"events": [_event("401", 2, "2026-10-21T23:30Z", ("BOS", "B"), ("NY", "N"),
                              status="STATUS_IN_PROGRESS", state="in", scores=("50", "48"))]}
    assert not parse_result(live, "401").final
    assert parse_result(SCOREBOARD, "403").postponed
    assert parse_result(final, "999") is None
    assert parse_result(None, "401") is None


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


def test_settings(tmp_path):
    async def go():
        db = Database(path=str(tmp_path / "s.db"))
        await db.init()
        assert await db.get_setting("daily_leaderboard_date") is None
        await db.set_setting("daily_leaderboard_date", "2026-10-09")
        await db.set_setting("daily_leaderboard_date", "2026-10-10")
        assert await db.get_setting("daily_leaderboard_date") == "2026-10-10"
    asyncio.run(go())
