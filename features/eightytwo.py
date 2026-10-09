"""82-0: spin a team + decade, draft a player, build a starting five, see your record.

Modes: Classic (stats shown while drafting) and Hoop IQ (stats hidden).
Each game: 5 picks (PG/SG/SF/PF/C, each once), 1 team skip, 1 decade skip.
Players use their per-game averages and awards with that team in that decade.
Going 82-0 pays PC Points once per member per day.
"""

from __future__ import annotations

import asyncio
import gzip
import itertools
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from .common import env_id, log

DATA_PATH = Path(__file__).parent / "data" / "eighty_two_zero.json.gz"
POSITIONS = ["PG", "SG", "SF", "PF", "C"]
CATS = ["pts", "reb", "ast", "stl", "blk"]
CAT_NAMES = {"pts": "Scoring", "reb": "Rebounding", "ast": "Playmaking", "stl": "Perimeter defense (steals)",
             "blk": "Rim protection (blocks)"}
# Era-adjusted team totals that count as "elite", and how much each category matters.
TARGETS = {"pts": 125, "reb": 46, "ast": 30, "stl": 8.5, "blk": 7.5}
WEIGHTS = {"pts": 0.32, "reb": 0.20, "ast": 0.20, "stl": 0.12, "blk": 0.16}
# Rating -> wins (piecewise linear). Calibrated by simulation: random picks average ~25 wins,
# sensible picks ~62, and only great, balanced lineups reach 82-0.
WIN_CURVE = [(0.08, 0), (0.236, 25), (0.770, 62), (0.925, 82)]
MAX_OPTIONS = 25  # Discord select menu limit
EASTERN = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Data + scoring (no Discord here, so it's easy to test)
# ---------------------------------------------------------------------------

_DATA: dict | None = None


def data() -> dict:
    global _DATA
    if _DATA is None:
        with gzip.open(DATA_PATH, "rt", encoding="utf-8") as fh:
            raw = json.load(fh)
        fields = raw["fields"]
        for team in raw["teams"].values():
            for dec in team["decades"].values():
                dec["players"] = [dict(zip(fields, p)) for p in dec["players"]]
                for p in dec["players"]:
                    p["positions"] = p["pos"].split("/")
        _DATA = raw
    return _DATA


def team_name(team: str) -> str:
    return data()["teams"][team]["name"]


def era_name(team: str, decade: str) -> str:
    """'Seattle SuperSonics' for the Thunder in the 1990s, etc."""
    return data()["teams"][team]["decades"][decade]["label"]


def arrangement(lineup: dict[str, dict], new: dict | None = None,
                new_pos: str | None = None) -> dict[str, dict] | None:
    """A valid lineup (position -> player) that adds `new` (at `new_pos` if given), with every
    player at one of his listed positions and as few existing players moved as possible.
    None if he can't fit anywhere."""
    current = list(lineup.items())
    people = [p for _, p in current] + ([new] if new else [])
    best, best_moves = None, 99
    for perm in itertools.permutations(POSITIONS, len(people)):
        if new_pos and new and perm[-1] != new_pos:
            continue
        if any(pos not in person["positions"] for pos, person in zip(perm, people)):
            continue
        moves = sum(cur != pos for (cur, _), pos in zip(current, perm))
        if moves < best_moves:
            best, best_moves = dict(zip(perm, people)), moves
    return best


def moved_players(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    where = {id(p): pos for pos, p in after.items()}
    return [f"{p['name'].split()[-1]} → {where[id(p)]}" for pos, p in before.items() if where[id(p)] != pos]


def candidates(team: str, decade: str, lineup: dict[str, dict]) -> list[dict]:
    """Players from that roster who fit the lineup, shuffling others to their other positions if needed."""
    roster = data()["teams"][team]["decades"].get(decade, {}).get("players", [])
    taken = {p["name"] for p in lineup.values()}
    ok = [p for p in roster if p["name"] not in taken and arrangement(lineup, p) is not None]
    return ok[:MAX_OPTIONS]


def spin(rng: random.Random, lineup: dict[str, dict],
         team: str | None = None, decade: str | None = None,
         not_team: str | None = None, not_decade: str | None = None) -> tuple[str, str]:
    """Random (team, decade) that has someone draftable. Fix team or decade to reroll only the other."""
    teams = data()["teams"]
    pairs = [(t, d) for t, v in teams.items() for d in v["decades"]
             if (team is None or t == team) and (decade is None or d == decade)
             and t != not_team and d != not_decade]
    rng.shuffle(pairs)
    for t, d in pairs:
        if candidates(t, d, lineup):
            return t, d
    if team or decade:  # nothing fits with that constraint: reroll both
        return spin(rng, lineup, not_team=not_team, not_decade=not_decade)
    raise RuntimeError("No draftable players left")


# Awards won with that team in that decade, per season there. Defensive honors
# add to steals/blocks (box scores undersell defenders); star honors add a small
# overall bonus. Kept modest so the stats still decide most of the record.
STAR_WEIGHTS = {"mvp": 3.0, "nba1": 1.5, "nba2": 1.0, "nba3": 0.6, "as": 0.3}
DEF_WEIGHTS = {"dpoy": 2.0, "def1": 1.0, "def2": 0.6}
STAR_BONUS, DEF_BONUS = 0.004, 0.25  # rating per star point; stl & blk per defense point


def star_rate(p: dict) -> float:
    a = p.get("awards") or {}
    return min(sum(w * a.get(k, 0) for k, w in STAR_WEIGHTS.items()) / max(p.get("seasons", 1), 1), 4.0)


def def_rate(p: dict) -> float:
    a = p.get("awards") or {}
    return min(sum(w * a.get(k, 0) for k, w in DEF_WEIGHTS.items()) / max(p.get("seasons", 1), 1), 3.0)


def player_value(p: dict) -> float:
    stats = {c: p[f"adj_{c}"] + (DEF_BONUS * def_rate(p) if c in ("stl", "blk") else 0) for c in CATS}
    return sum(WEIGHTS[c] * stats[c] / TARGETS[c] for c in CATS) + STAR_BONUS * star_rate(p)


AWARD_ORDER = [("mvp", "MVP"), ("dpoy", "DPOY"), ("allnba", "All-NBA"), ("alldef", "All-Def"),
               ("as", "All-Star"), ("roy", "ROY"), ("smoy", "6MOY"), ("mip", "MIP")]


def award_text(p: dict, limit: int | None = None) -> str:
    a = dict(p.get("awards") or {})
    a["allnba"] = a.get("nba1", 0) + a.get("nba2", 0) + a.get("nba3", 0)
    a["alldef"] = a.get("def1", 0) + a.get("def2", 0)
    parts = [f"{a[k]}× {label}" if a[k] > 1 else label for k, label in AWARD_ORDER if a.get(k)]
    return " · ".join(parts[:limit] if limit else parts)


def wins_for(rating: float) -> int:
    if rating <= WIN_CURVE[0][0]:
        return 0
    for (x1, y1), (x2, y2) in zip(WIN_CURVE, WIN_CURVE[1:]):
        if rating <= x2:
            return round(y1 + (y2 - y1) * (rating - x1) / (x2 - x1))
    return 82


def grade_for(wins: int) -> str:
    for cutoff, grade in ((82, "S 🏆"), (75, "A+"), (68, "A"), (60, "B"), (50, "C"), (40, "D")):
        if wins >= cutoff:
            return grade
    return "F"


def lineup_rating(lineup: dict[str, dict]) -> tuple[float, dict[str, float], dict[str, float]]:
    """(rating, category scores, era-adjusted team totals)."""
    totals = {c: sum(p[f"adj_{c}"] for p in lineup.values()) for c in CATS}
    defense = sum(def_rate(p) for p in lineup.values()) * DEF_BONUS
    scored = {c: totals[c] + (defense if c in ("stl", "blk") else 0) for c in CATS}
    scores = {c: min(scored[c] / TARGETS[c], 1.15) for c in CATS}
    rating = sum(WEIGHTS[c] * scores[c] for c in CATS) + STAR_BONUS * sum(star_rate(p) for p in lineup.values())
    weakest = min(scores, key=scores.get)
    if scores[weakest] < 0.7:  # a glaring hole drags the whole team down
        rating -= 0.35 * (0.7 - scores[weakest])
    return rating, scores, totals


def evaluate(lineup: dict[str, dict]) -> dict:
    rating, scores, totals = lineup_rating(lineup)
    wins = wins_for(rating)
    best = max(lineup.items(), key=lambda kv: player_value(kv[1]))
    return {"wins": wins, "losses": 82 - wins, "grade": grade_for(wins), "totals": totals,
            "best": best, "weakness": CAT_NAMES[min(scores, key=scores.get)] if wins < 82 else None}


def stat_line(p: dict) -> str:
    est = "*" if p.get("est") else ""
    return (f"{p['pts']:g} pts · {p['reb']:g} reb · {p['ast']:g} ast · "
            f"{p['stl']:g}{est} stl · {p['blk']:g}{est} blk")


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

@dataclass
class Game:
    user_id: int
    mode: str  # "classic" | "hoopiq"
    rng: random.Random = field(default_factory=random.Random)
    lineup: dict[str, dict] = field(default_factory=dict)  # position -> player (+ team info)
    team: str | None = None
    decade: str | None = None
    options: list[dict] = field(default_factory=list)
    pending: dict | None = None  # player picked, waiting for a position
    team_skip: bool = True
    decade_skip: bool = True
    finished: bool = False

    @property
    def open_positions(self) -> set[str]:
        return {p for p in POSITIONS if p not in self.lineup}

    @property
    def taken(self) -> set[str]:
        return {p["name"] for p in self.lineup.values()}

    moving: bool = False  # showing the "move a player" menu

    def do_spin(self, **kw) -> None:
        self.team, self.decade = spin(self.rng, self.lineup, **kw)
        self.options = candidates(self.team, self.decade, self.lineup)
        self.pending = None

    def placements(self, player: dict) -> list[tuple[str, list[str]]]:
        """Positions he can take now, each with the moves it would cause (e.g. 'Bryant → SF')."""
        out = []
        for pos in POSITIONS:
            if pos in player["positions"]:
                after = arrangement(self.lineup, player, pos)
                if after is not None:
                    out.append((pos, moved_players(self.lineup, after)))
        return out

    def place(self, player: dict, position: str) -> list[str]:
        entry = {**player, "team": self.team, "decade": self.decade, "era_team": era_name(self.team, self.decade)}
        after = arrangement(self.lineup, entry, position)
        moves = moved_players(self.lineup, after)
        self.lineup = after
        self.team = self.decade = None
        self.options, self.pending = [], None
        self.finished = len(self.lineup) == 5
        return moves

    def move_options(self) -> list[tuple[str, str]]:
        """(from, to) moves for drafted players: to an open listed position, or a swap
        with a teammate who can play the other spot."""
        out = []
        for frm, p in self.lineup.items():
            for to in p["positions"]:
                if to == frm:
                    continue
                other = self.lineup.get(to)
                if other is None or frm in other["positions"]:
                    out.append((frm, to))
        return out

    def move(self, frm: str, to: str) -> str:
        p, other = self.lineup.pop(frm), self.lineup.pop(to, None)
        self.lineup[to] = p
        if other:
            self.lineup[frm] = other
            text = f"🔀 Swapped **{p['name']}** ({to}) and **{other['name']}** ({frm})."
        else:
            text = f"🔀 Moved **{p['name']}** to **{to}**."
        if self.team:  # refresh the draft list for the current spin
            self.options = candidates(self.team, self.decade, self.lineup)
        self.moving = False
        return text


# ---------------------------------------------------------------------------
# Discord UI
# ---------------------------------------------------------------------------

MODE_LABEL = {"classic": "Classic (stats shown)", "hoopiq": "Hoop IQ (stats hidden)"}


def lineup_lines(game: Game, reveal: bool) -> list[str]:
    lines = []
    for pos in POSITIONS:
        p = game.lineup.get(pos)
        if not p:
            lines.append(f"`{pos:<2}` —")
            continue
        line = f"`{pos:<2}` **{p['name']}** · {p['era_team']} {p['season']}"
        if reveal:
            line += f"\n      {stat_line(p)}"
            if award_text(p):
                line += f"\n      🏆 {award_text(p)}"
        lines.append(line)
    return lines


def game_embed(game: Game, member: discord.abc.User, status: str | None = None,
               note: str | None = None) -> discord.Embed:
    reveal = game.mode == "classic"
    e = discord.Embed(title="🏀 82-0 Challenge", color=discord.Color.orange())
    e.set_author(name=f"{member.display_name}'s game · {MODE_LABEL[game.mode]}", icon_url=member.display_avatar.url)
    e.add_field(name=f"Starting five ({len(game.lineup)}/5)", value="\n".join(lineup_lines(game, reveal)), inline=False)
    if status:
        e.add_field(name="​", value=status, inline=False)
    elif game.team:
        e.add_field(name="🎰 Your spin", value=f"**{era_name(game.team, game.decade)}** · **{game.decade}**"
                    + (f"  _(today's {team_name(game.team)})_" if era_name(game.team, game.decade) != team_name(game.team) else ""),
                    inline=False)
        if note:
            e.add_field(name="\u200b", value=note, inline=False)
        if game.pending:
            e.add_field(name="Where does he play?",
                        value=f"**{game.pending['name']}** — pick an open position below.", inline=False)
        else:
            e.add_field(name="​", value="Pick a player from the list below.", inline=False)
    else:
        e.add_field(name="​", value=note or "Hit **🎰 Spin** for a team and decade.", inline=False)
    skips = []
    if game.team_skip:
        skips.append("team skip")
    if game.decade_skip:
        skips.append("decade skip")
    e.set_footer(text=f"🔒 Only {member.display_name} can play this game · Skips left: {', '.join(skips) if skips else 'none'}"
                      + (" · * = estimated (not tracked before 1973-74)" if reveal else ""))
    return e


def result_embed(game: Game, member: discord.abc.User, result: dict, reward_note: str) -> discord.Embed:
    perfect = result["wins"] == 82
    e = discord.Embed(
        title=f"🏆 82-0! A perfect season!" if perfect else f"🏀 Final record: {result['wins']}-{result['losses']}",
        color=discord.Color.gold() if perfect else discord.Color.blue())
    e.set_author(name=f"{member.display_name}'s game · {MODE_LABEL[game.mode]}", icon_url=member.display_avatar.url)
    e.add_field(name="Starting five", value="\n".join(lineup_lines(game, reveal=True)), inline=False)
    t = result["totals"]
    e.add_field(name="Team per game (era-adjusted)",
                value=f"{t['pts']:.0f} pts · {t['reb']:.0f} reb · {t['ast']:.0f} ast · {t['stl']:.1f} stl · {t['blk']:.1f} blk",
                inline=False)
    best_pos, best = result["best"]
    e.add_field(name="Grade", value=f"**{result['grade']}**", inline=True)
    e.add_field(name="Best pick", value=f"{best['name']} ({best_pos})", inline=True)
    e.add_field(name="Biggest weakness", value=result["weakness"] or "None — flawless", inline=True)
    if reward_note:
        e.add_field(name="​", value=reward_note, inline=False)
    e.set_footer(text="* = estimated (steals/blocks not tracked before 1973-74)")
    return e


class GameView(discord.ui.View):
    def __init__(self, cog: "EightyTwoZero", game: Game) -> None:
        super().__init__(timeout=15 * 60)
        self.cog, self.game = cog, game
        self.message: discord.Message | None = None
        g = game
        if g.finished:
            self.add_item(self._button("Play again", "🔁", discord.ButtonStyle.success, self.play_again))
            return
        if g.moving:
            moves = g.move_options()
            select = discord.ui.Select(placeholder="Move which player?", min_values=1, max_values=1,
                                       options=[self._move_option(frm, to) for frm, to in moves][:25])
            select.callback = self._owner_only(self._move_cb(select))
            self.add_item(select)
            self.add_item(self._button("Back", "↩️", discord.ButtonStyle.secondary, self.on_cancel_move))
            return
        if not g.team:
            self.add_item(self._button("Spin", "🎰", discord.ButtonStyle.primary, self.on_spin))
            self._maybe_move_button()
            return
        if g.pending:
            for pos, moves in g.placements(g.pending):
                label = pos if not moves else f"{pos} ({', '.join(moves)})"
                self.add_item(self._button(label[:80], None, discord.ButtonStyle.primary, self._place_cb(pos)))
            self.add_item(self._button("Back", "↩️", discord.ButtonStyle.secondary, self.on_back))
            return
        select = discord.ui.Select(placeholder="Draft a player…", min_values=1, max_values=1,
                                   options=[self._option(i, p) for i, p in enumerate(g.options)])
        select.callback = self._owner_only(self._select_cb(select))
        self.add_item(select)
        if g.team_skip:
            self.add_item(self._button("Skip team", "⏭️", discord.ButtonStyle.secondary, self.on_skip_team))
        if g.decade_skip:
            self.add_item(self._button("Skip decade", "⏭️", discord.ButtonStyle.secondary, self.on_skip_decade))
        self._maybe_move_button()

    def _maybe_move_button(self) -> None:
        if self.game.move_options():
            self.add_item(self._button("Move player", "🔀", discord.ButtonStyle.secondary, self.on_move))

    def _move_option(self, frm: str, to: str) -> discord.SelectOption:
        p, other = self.game.lineup[frm], self.game.lineup.get(to)
        label = (f"{p['name']}: {frm} → {to}" if other is None
                 else f"Swap {p['name']} ({frm}) ↔ {other['name']} ({to})")
        return discord.SelectOption(label=label[:100], value=f"{frm}:{to}")

    def _option(self, i: int, p: dict) -> discord.SelectOption:
        if self.game.mode == "classic":
            awards = award_text(p, limit=2)
            label = f"{p['name']} ({p['pos']})" + (f" · 🏆 {awards}" if awards else "")
            desc = f"{p['season']} · {stat_line(p)}"
        else:
            label, desc = f"{p['name']} ({p['pos']})", "Stats and awards hidden — trust your hoop IQ"
        return discord.SelectOption(label=label[:100], value=str(i), description=desc[:100])

    def _button(self, label, emoji, style, cb) -> discord.ui.Button:
        b = discord.ui.Button(label=label, emoji=emoji, style=style)
        b.callback = self._owner_only(cb)
        return b

    def _owner_only(self, cb):
        """Second lock on every control (on top of interaction_check): only the player who
        started this game can press its buttons or use its menus."""
        async def guarded(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.game.user_id:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "🔒 This isn't your game. Start your own with `/82-0`.", ephemeral=True)
                return
            await cb(interaction)
        return guarded

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game.user_id:
            await interaction.response.send_message("🔒 This isn't your game. Start your own with `/82-0`.",
                                                    ephemeral=True)
            return False
        if self.cog.games.get(self.game.user_id) is not self.game and not self.game.finished:
            await interaction.response.send_message("This game was replaced by a newer one.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message and not self.game.finished and self.cog.games.get(self.game.user_id) is self.game:
            self.cog.games.pop(self.game.user_id, None)
            try:
                await self.message.edit(content="⌛ This 82-0 game expired (15 minutes idle).", view=None)
            except discord.HTTPException:
                pass

    async def refresh(self, interaction: discord.Interaction, status: str | None = None,
                      note: str | None = None) -> None:
        view = GameView(self.cog, self.game)
        view.message = self.message
        self.stop()
        await interaction.response.edit_message(embed=game_embed(self.game, interaction.user, status, note),
                                                view=view)

    # -- callbacks --------------------------------------------------------
    async def on_spin(self, interaction: discord.Interaction, note: str | None = None, **kw) -> None:
        g = self.game
        kept_team, kept_decade = kw.get("team"), kw.get("decade")
        await interaction.response.edit_message(
            embed=game_embed(g, interaction.user, "🎰 Spinning…"), view=None)
        teams = list(data()["teams"])
        for _ in range(3):  # quick fake spin; a skip only spins the half being rerolled
            team = f"🔒 **{team_name(kept_team)}**" if kept_team else f"**{team_name(g.rng.choice(teams))}**"
            decade = f"🔒 **{kept_decade}**" if kept_decade else f"**{g.rng.choice(data()['decades'])}**"
            await asyncio.sleep(0.6)
            await interaction.edit_original_response(
                embed=game_embed(g, interaction.user, f"🎰 Spinning… {team} · {decade}"))
        g.do_spin(**kw)
        view = GameView(self.cog, g)
        view.message = self.message
        self.stop()
        await asyncio.sleep(0.6)
        await interaction.edit_original_response(embed=game_embed(g, interaction.user, note=note), view=view)

    async def on_skip_team(self, interaction: discord.Interaction) -> None:
        g = self.game
        g.team_skip = False
        await self.on_spin(interaction, note=f"⏭️ Team skipped. You kept the **{g.decade}**.",
                           decade=g.decade, not_team=g.team)

    async def on_skip_decade(self, interaction: discord.Interaction) -> None:
        g = self.game
        g.decade_skip = False
        await self.on_spin(interaction, note=f"⏭️ Decade skipped. You kept the **{team_name(g.team)}** franchise.",
                           team=g.team, not_decade=g.decade)

    def _select_cb(self, select: discord.ui.Select):
        async def cb(interaction: discord.Interaction) -> None:
            g = self.game
            player = g.options[int(select.values[0])]
            spots = g.placements(player)
            if len(spots) == 1 and not spots[0][1]:  # only one spot and nobody has to move
                await self._finish_pick(interaction, player, spots[0][0])
            else:
                g.pending = player
                await self.refresh(interaction)
        return cb

    def _place_cb(self, pos: str):
        async def cb(interaction: discord.Interaction) -> None:
            await self._finish_pick(interaction, self.game.pending, pos)
        return cb

    async def on_back(self, interaction: discord.Interaction) -> None:
        self.game.pending = None
        await self.refresh(interaction)

    async def on_move(self, interaction: discord.Interaction) -> None:
        self.game.moving = True
        self.game.pending = None
        await self.refresh(interaction, note="🔀 Pick a move. Players can only go to positions they've played.")

    async def on_cancel_move(self, interaction: discord.Interaction) -> None:
        self.game.moving = False
        await self.refresh(interaction)

    def _move_cb(self, select: discord.ui.Select):
        async def cb(interaction: discord.Interaction) -> None:
            frm, to = select.values[0].split(":")
            await self.refresh(interaction, note=self.game.move(frm, to))
        return cb

    async def _finish_pick(self, interaction: discord.Interaction, player: dict, pos: str) -> None:
        g = self.game
        moves = g.place(player, pos)
        if not g.finished:
            moved = f" Moved {', '.join(moves)}." if moves else ""
            await self.refresh(interaction, f"✅ **{player['name']}** at **{pos}**.{moved} Hit **🎰 Spin** for the next pick.")
            return
        result = evaluate(g.lineup)
        reward = await self.cog.reward(interaction.user, result)
        self.cog.games.pop(g.user_id, None)
        view = GameView(self.cog, g)
        view.message = self.message
        self.stop()
        await interaction.response.edit_message(embed=result_embed(g, interaction.user, result, reward), view=view)

    async def play_again(self, interaction: discord.Interaction) -> None:
        await self.cog.start(interaction, self.game.mode)


class EightyTwoZero(commands.Cog):
    def __init__(self, bot: commands.Bot, allowed_roles: tuple[str, ...], members_label: str) -> None:
        self.bot = bot
        self.allowed_roles, self.members_label = allowed_roles, members_label
        self.channel_id = env_id("EIGHTYTWO_CHANNEL_ID")
        self.reward_points = int(os.getenv("EIGHTYTWO_POINTS", "50"))
        self.games: dict[int, Game] = {}

    async def cog_load(self) -> None:
        await asyncio.to_thread(data)  # load the player data up front
        log.info("82-0 game on (%d franchises)", len(data()["teams"]))

    def allowed(self, member: discord.abc.User) -> bool:
        return any(r.name in self.allowed_roles for r in getattr(member, "roles", []))

    @app_commands.command(name="82-0", description="Spin teams and decades, draft a starting five, chase 82-0")
    @app_commands.describe(mode="Classic shows stats while you draft; Hoop IQ hides them")
    @app_commands.choices(mode=[app_commands.Choice(name="Classic (stats shown)", value="classic"),
                                app_commands.Choice(name="Hoop IQ (stats hidden)", value="hoopiq")])
    async def eightytwo(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        if not self.allowed(interaction.user):
            await interaction.response.send_message(f"⛔ 82-0 is for **{self.members_label}** only.", ephemeral=True)
            return
        if self.channel_id and interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"Play 82-0 in <#{self.channel_id}>.", ephemeral=True)
            return
        await self.start(interaction, mode.value)

    async def start(self, interaction: discord.Interaction, mode: str) -> None:
        game = Game(user_id=interaction.user.id, mode=mode)
        self.games[interaction.user.id] = game  # starting again replaces an unfinished game
        view = GameView(self, game)
        await interaction.response.send_message(embed=game_embed(game, interaction.user), view=view)
        view.message = await interaction.original_response()

    async def reward(self, member: discord.abc.User, result: dict) -> str:
        if result["wins"] != 82:
            return ""
        today = datetime.now(EASTERN).date().isoformat()
        key = f"eightytwo_reward:{member.id}"
        if await self.bot.db.get_setting(key) == today:
            return f"🎉 Perfect season! (You already earned today's +{self.reward_points} PC Points — come back tomorrow.)"
        await self.bot.db.set_setting(key, today)
        total = await self.bot.db.add_points(member.id, self.reward_points)
        hook = getattr(self.bot, "points_changed", None)
        if hook:
            asyncio.create_task(hook())
        log.info("82-0: %s went 82-0, +%d PC Points", member, self.reward_points)
        return f"🎉 **+{self.reward_points} PC Points!** You now have **{total:,}**."
