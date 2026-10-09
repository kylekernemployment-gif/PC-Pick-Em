import random

from features import eightytwo as E


def test_data_loads_all_franchises_and_decades():
    d = E.data()
    assert len(d["teams"]) == 30
    assert d["decades"] == ["1960s", "1970s", "1980s", "1990s", "2000s", "2010s", "2020s"]
    assert E.era_name("OKC", "1990s") == "Seattle SuperSonics"
    names = [p["name"] for p in d["teams"]["CHI"]["decades"]["1990s"]["players"]]
    assert "Michael Jordan" in names


def test_candidates_respect_positions_and_taken():
    c = E.candidates("CHI", "1990s", {"C"}, set())
    assert c and all("C" in p["positions"] for p in c) and len(c) <= 25
    assert all(p["name"] != "Michael Jordan" for p in E.candidates("CHI", "1990s", {"SG", "SF"}, {"Michael Jordan"}))


def test_skips_keep_the_other_half_of_the_spin():
    rng = random.Random(3)
    open_ = set(E.POSITIONS)
    t, d = E.spin(rng, open_, set())
    t2, d2 = E.spin(rng, open_, set(), decade=d, not_team=t)
    assert d2 == d and t2 != t
    t3, d3 = E.spin(rng, open_, set(), team=t, not_decade=d)
    assert t3 == t and d3 != d or (t3, d3) != (t, d)


def test_full_game_and_scoring():
    rng = random.Random(7)
    g = E.Game(user_id=1, mode="classic", rng=rng)
    while not g.finished:
        g.do_spin()
        p = max(g.options, key=E.player_value)
        pos = next(x for x in E.POSITIONS if x in g.open_positions and x in p["positions"])
        g.place(p, pos)
    r = E.evaluate(g.lineup)
    assert 0 <= r["wins"] <= 82 and r["wins"] + r["losses"] == 82
    assert set(g.lineup) == set(E.POSITIONS) and len({p["name"] for p in g.lineup.values()}) == 5


def test_win_curve_and_grades():
    assert E.wins_for(0.05) == 0 and E.wins_for(2) == 82 and E.wins_for(0.235) == 25
    assert E.grade_for(82).startswith("S") and E.grade_for(70) == "A" and E.grade_for(10) == "F"


def test_dream_team_can_go_82_0():
    pick = lambda t, d, n: next(p for p in E.data()["teams"][t]["decades"][d]["players"] if p["name"] == n)
    lineup = {"PG": pick("LAL", "1980s", "Magic Johnson"), "SG": pick("CHI", "1990s", "Michael Jordan"),
              "SF": pick("BOS", "1980s", "Larry Bird"), "PF": pick("SAS", "2000s", "Tim Duncan"),
              "C": pick("HOU", "1990s", "Hakeem Olajuwon")}
    assert E.evaluate(lineup)["wins"] == 82


def test_awards_follow_the_team_and_count():
    find = lambda t, d, n: next(p for p in E.data()["teams"][t]["decades"][d]["players"] if p["name"] == n)
    heat, cavs = find("MIA", "2010s", "LeBron James"), find("CLE", "2010s", "LeBron James")
    assert heat["awards"]["mvp"] == 2 and "mvp" not in cavs["awards"]
    assert E.award_text(heat).startswith("2× MVP")
    wallace = find("DET", "2000s", "Ben Wallace")
    assert E.def_rate(wallace) > 1.5  # 4x DPOY, 5x All-Defense
    no_awards = {**wallace, "awards": {}}
    assert E.player_value(wallace) > E.player_value(no_awards)
