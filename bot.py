"""PC Pick Em' — daily NBA pick'em Discord bot."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks

from db import Database
from features import antiscam, eightytwo, verify, youtube
from features.roles import ReactionRoles
from nba import EASTERN, NBAClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pcpickem")


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip().strip("'\"").strip()
    if not value:
        raise SystemExit(f"Missing environment variable {name}. Add it in Render -> Environment.")
    return value


def _required_id(name: str) -> int:
    value = _required(name)
    if not value.isdigit():
        raise SystemExit(f"{name} must be a number (right-click -> Copy ID in Discord), got {value!r}")
    return int(value)


def _database_url() -> str | None:
    # Accept what Neon's "Connect" box shows, e.g. psql 'postgresql://...'
    url = os.getenv("DATABASE_URL", "").strip()
    if url.lower().startswith("psql"):
        url = url[4:].strip()
    url = url.strip("'\"").strip()
    if url and not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit("DATABASE_URL should start with postgresql:// (copy it from Neon -> Connect)")
    return url or None


TOKEN = _required("DISCORD_TOKEN")
GUILD_ID = _required_id("GUILD_ID")
CHANNEL_ID = _required_id("PICKEM_CHANNEL_ID")
MOD_ROLE = os.getenv("MOD_ROLE_NAME", "Lead Moderator")
MEMBER_ROLE = os.getenv("MEMBER_ROLE_NAME", "YouTube Member")
LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID", "").strip() or 0) or None
DAILY_LEADERBOARD_HOUR = int(os.getenv("DAILY_LEADERBOARD_HOUR", "10"))  # US Eastern, 24h clock
DAILY_LEADERBOARD_SIZE = 25
DAILY_LEADERBOARD = _env_bool("DAILY_LEADERBOARD", "true")  # 10am Top 25 post in the pick'em channel
LIVE_BOARD_EVERY_SECS = 10 * 60
POINTS_PER_WIN = int(os.getenv("POINTS_PER_WIN", "100"))
POST_HOURS_BEFORE = float(os.getenv("POST_HOURS_BEFORE", "12"))
INCLUDE_PRESEASON = _env_bool("INCLUDE_PRESEASON", "true")
DATABASE_URL = _database_url()
DB_PATH = os.getenv("DB_PATH", "pickem.db")

SCHEDULE_REFRESH_SECS = 60 * 60     # re-read the NBA schedule hourly
RESULT_CHECK_AFTER_SECS = 90 * 60   # start checking for a final score 1.5h after tip
RESULT_CHECK_EVERY_SECS = 3 * 60    # ...then every 3 minutes until it's final
SELF_PING_EVERY_SECS = 10 * 60

ONE = "1\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}"
TWO = "2\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}"
EMOJIS = {1: ONE, 2: TWO}  # 1 = away team, 2 = home team
CHOICE_BY_EMOJI = {ONE: 1, TWO: 2}

NO_PINGS = discord.AllowedMentions.none()


class PickEmBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # needed for !commands
        intents.members = verify.is_configured()  # needed to see people join (captcha)
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(DATABASE_URL, DB_PATH)
        self.http_session: aiohttp.ClientSession | None = None
        self.nba: NBAClient | None = None
        self.game_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_schedule_refresh = 0.0
        self.last_result_check: dict[str, float] = {}
        self.last_self_ping = 0.0
        self.last_board_update = 0.0
        self.last_board_text: str | None = None
        self.bg_tasks: set[asyncio.Task] = set()
        self.polls_upgraded = False

    async def setup_hook(self) -> None:
        await self.db.init()
        log.info("Database ready (%s)", "Postgres" if self.db.pg else f"SQLite: {DB_PATH}")
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.nba = NBAClient(self.http_session, INCLUDE_PRESEASON)
        self.add_view(PollView())  # team buttons keep working after restarts
        await self.load_features()
        try:
            synced = await self.tree.sync(guild=discord.Object(id=GUILD_ID))
            log.info("Synced %d slash command(s)", len(synced))
        except discord.Forbidden:
            # The rest of the bot still works; only /givepcpoints is missing.
            log.error("Couldn't register slash commands (Missing Access). Re-invite the bot with the "
                      "'bot' AND 'applications.commands' scopes, and check GUILD_ID is your server ID.")
        ticker.start()

    async def load_features(self) -> None:
        guild = discord.Object(id=GUILD_ID)
        await self.add_cog(ReactionRoles(self, GUILD_ID), guild=guild)
        if youtube.is_configured():
            await self.add_cog(youtube.YouTubeLive(self), guild=guild)
            log.info("YouTube live notifications on")
        else:
            log.info("YouTube live notifications off (set YOUTUBE_API_KEY, YOUTUBE_CHANNEL_ID, "
                     "YOUTUBE_NOTIFY_CHANNEL_ID to turn on)")
        if verify.is_configured():
            await self.add_cog(verify.Verification(self, GUILD_ID, MOD_ROLE), guild=guild)
        else:
            log.info("Captcha verification off (set VERIFY_CHANNEL_ID to turn on)")
        if _env_bool("EIGHTYTWO", "true"):
            await self.add_cog(eightytwo.EightyTwoZero(self, MEMBER_ROLE, MOD_ROLE), guild=guild)
        if antiscam.is_enabled():
            await self.add_cog(antiscam.AntiScam(self, GUILD_ID, MOD_ROLE), guild=guild)

    async def points_changed(self) -> None:
        """Features call this after changing PC Points so the live leaderboard updates."""
        await refresh_board_soon()

    async def close(self) -> None:
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = PickEmBot()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def pickem_channel() -> discord.TextChannel:
    return bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)


def fmt_pct(wins: int, losses: int) -> str:
    pct = 100 * wins / (wins + losses)
    return f"{pct:.1f}".rstrip("0").rstrip(".") + "%"


def poll_embed(game: dict, picks: int | None = None) -> discord.Embed:
    away, home, tip = game["away_name"], game["home_name"], game["tip_utc"]
    status = game["status"]
    lines = [
        f"{ONE}  **{away}**" + (f" ({game['away_record']})" if game["away_record"] else ""),
        f"{TWO}  **{home}**" + (f" ({game['home_record']})" if game["home_record"] else ""),
        "",
        f"🕒 Tip-off: <t:{tip}:F> (<t:{tip}:R>)",
    ]
    if status in ("scheduled", "open"):
        color = discord.Color.blue()
        lines.insert(0, "**Who wins?** Tap a team below. You can switch until tip-off.\n")
        lines += ["🔒 Poll closes at tip-off.", f"💰 Correct pick = **+{POINTS_PER_WIN} PC Points**",
                  f"🎟️ {MEMBER_ROLE}s only"]
    elif status == "locked":
        color = discord.Color.orange()
        lines += ["", f"🔒 **Poll closed** — game in progress. {picks or 0} pick(s) locked in."]
    elif status == "final":
        color = discord.Color.green()
        winner = away if game["winner"] == 1 else home
        lines += [
            "",
            f"✅ **Final:** {game['away_tri']} {game['away_score']} – {game['home_score']} {game['home_tri']}",
            f"🏆 **{winner}** win! {EMOJIS[game['winner']]} pickers earned +{POINTS_PER_WIN} PC Points.",
        ]
    else:  # void
        color = discord.Color.dark_grey()
        lines += ["", "⚠️ **Game postponed/cancelled** — poll void, no points awarded."]
    embed = discord.Embed(title=f"🏀 {away} @ {home}", description="\n".join(lines), color=color)
    embed.set_footer(text="PC Pick Em'")
    return embed


async def poll_message(game: dict) -> discord.Message | None:
    if not game.get("message_id"):
        return None
    try:
        channel = bot.get_channel(game["channel_id"]) or await bot.fetch_channel(game["channel_id"])
        return await channel.fetch_message(game["message_id"])
    except discord.HTTPException as e:
        log.warning("Couldn't fetch poll message for %s: %r", game["game_id"], e)
        return None


async def refresh_poll(game: dict) -> discord.Message | None:
    msg = await poll_message(game)
    if msg:
        picks = await bot.db.pick_count(game["game_id"])
        try:
            await msg.edit(embed=poll_embed(game, picks),
                           view=PollView(game) if game["status"] == "open" else None)
        except discord.HTTPException as e:
            log.warning("Couldn't edit poll for %s: %r", game["game_id"], e)
    return msg


class PollView(discord.ui.View):
    """Team buttons on a poll. Registered once at startup so buttons keep
    working after restarts; the clicked message tells us which game it is."""

    def __init__(self, game: dict | None = None) -> None:
        super().__init__(timeout=None)
        for choice, emoji in EMOJIS.items():
            name = (game["away_name"] if choice == 1 else game["home_name"]) if game else f"Team {choice}"
            button = discord.ui.Button(label=name[:80], emoji=emoji, style=discord.ButtonStyle.primary,
                                       custom_id=f"pickem:pick:{choice}")
            button.callback = self._callback(choice)
            self.add_item(button)

    @staticmethod
    def _callback(choice: int):
        async def cb(interaction: discord.Interaction) -> None:
            await handle_pick(interaction, choice)
        return cb


async def handle_pick(interaction: discord.Interaction, choice: int) -> None:
    reply = lambda text: interaction.response.send_message(text, ephemeral=True)
    game = await bot.db.game_by_message(interaction.message.id)
    if not game:
        await reply("This poll isn't active anymore.")
        return
    if not can_play(interaction.user):
        await reply(f"🎟️ PC Pick Em' is for **{MEMBER_ROLE}s** only.")
        return
    gid = game["game_id"]
    async with bot.game_locks[gid]:
        game = await bot.db.get_game(gid)
        if game["status"] != "open" or time.time() >= game["tip_utc"]:
            await reply("⏰ You can't pick after the game starts. This poll closed at tip-off.")
            return
        team = game["away_name"] if choice == 1 else game["home_name"]
        existing = await bot.db.get_pick(gid, interaction.user.id)
        if existing == choice:
            await reply(f"You already picked the **{team}**. Tap the other team if you want to switch.")
            return
        await bot.db.set_pick(gid, interaction.user.id, choice)
    switched = " (switched)" if existing else ""
    await reply(f"✅ You picked the **{team}**{switched}! You can change your pick until tip-off "
                f"<t:{game['tip_utc']}:R>.")


async def reaction_picks(msg: discord.Message) -> dict[int, set[int]]:
    """user_id -> set of choices they reacted with (polls posted before buttons)."""
    chosen: dict[int, set[int]] = defaultdict(set)
    for reaction in msg.reactions:
        choice = CHOICE_BY_EMOJI.get(str(reaction.emoji))
        if choice is None:
            continue
        async for user in reaction.users():
            if not user.bot:
                chosen[user.id].add(choice)
    return chosen


# ---------------------------------------------------------------------------
# Background loop: schedule -> post polls -> lock at tip -> grade finals
# ---------------------------------------------------------------------------

@tasks.loop(seconds=60)
async def ticker() -> None:
    for step in (upgrade_reaction_polls, refresh_schedule, post_due_polls, lock_started_games, resolve_finished_games, daily_leaderboard,
                 update_live_leaderboard, self_ping):
        try:
            await step()
        except Exception:
            log.exception("%s failed", step.__name__)


@ticker.before_loop
async def before_ticker() -> None:
    await bot.wait_until_ready()


async def refresh_schedule() -> None:
    if time.time() - bot.last_schedule_refresh < SCHEDULE_REFRESH_SECS:
        return
    games = await bot.nba.fetch_schedule()
    if games is None:
        log.warning("Couldn't load NBA schedule; will retry next minute")
        return
    bot.last_schedule_refresh = time.time()
    await bot.db.upsert_games(games)
    log.info("Schedule refreshed: %d games", len(games))

    # A posted poll whose game got postponed: pull it and repost once rescheduled.
    for game in await bot.db.games_with_status("open"):
        if game["postponed"]:
            async with bot.game_locks[game["game_id"]]:
                msg = await poll_message(game)
                if msg:
                    try:
                        await msg.edit(embed=poll_embed({**game, "status": "void"}), view=None)
                    except discord.HTTPException:
                        pass
                await bot.db.reopen_later(game["game_id"])
            log.info("Game %s postponed; poll cancelled", game["game_id"])


async def post_due_polls() -> None:
    now = int(time.time())
    due = await bot.db.games_to_post(now, int(POST_HOURS_BEFORE * 3600))
    if not due:
        return
    channel = await pickem_channel()
    for game in due:
        msg = await channel.send(embed=poll_embed(game), view=PollView(game))
        await bot.db.mark_posted(game["game_id"], channel.id, msg.id)
        log.info("Posted poll for %s %s @ %s", game["game_id"], game["away_tri"], game["home_tri"])


async def lock_started_games() -> None:
    now = int(time.time())
    for game in await bot.db.games_with_status("open"):
        if game["tip_utc"] > now:
            continue
        async with bot.game_locks[game["game_id"]]:
            # Picks are saved as people click (old reaction polls were imported at startup).
            await bot.db.lock_game(game["game_id"], None)
            game = await bot.db.get_game(game["game_id"])
            await refresh_poll(game)
        log.info("Locked poll for %s", game["game_id"])


async def resolve_finished_games() -> None:
    now = time.time()
    for game in await bot.db.games_with_status("locked"):
        gid = game["game_id"]
        if now < game["tip_utc"] + RESULT_CHECK_AFTER_SECS:
            continue
        if now - bot.last_result_check.get(gid, 0) < RESULT_CHECK_EVERY_SECS:
            continue
        bot.last_result_check[gid] = now

        result = await bot.nba.fetch_result(game)
        stale = now > game["tip_utc"] + 24 * 3600
        if (result and result.postponed) or (stale and game["postponed"] and not (result and result.final)):
            if await bot.db.void_game(gid):
                await refresh_poll(await bot.db.get_game(gid))
                log.info("Voided %s (postponed)", gid)
            continue
        if not result or not result.final or result.away_score == result.home_score:
            continue

        winner = 1 if result.away_score > result.home_score else 2
        outcome = await bot.db.resolve_game(gid, result.away_score, result.home_score, winner, POINTS_PER_WIN)
        bot.last_result_check.pop(gid, None)
        if outcome is None:
            continue
        winners, total = outcome
        game = await bot.db.get_game(gid)
        msg = await refresh_poll(game)
        await announce_result(game, msg, winners, total)
        await refresh_board_soon()
        log.info("Resolved %s: %d/%d correct", gid, len(winners), total)


async def announce_result(game: dict, msg: discord.Message | None, winners: list[int], total: int) -> None:
    team = game["away_name"] if game["winner"] == 1 else game["home_name"]
    text = (
        f"🏁 **Final:** {game['away_tri']} {game['away_score']} – {game['home_score']} {game['home_tri']}\n"
        f"🏆 **{team}** win! **{len(winners)}/{total}** picked correctly"
    )
    if winners:
        text += f" and earned **+{POINTS_PER_WIN} PC Points**:\n"
        mentions = ""
        for i, uid in enumerate(winners):
            nxt = f"<@{uid}> "
            if len(text) + len(mentions) + len(nxt) > 1900:
                mentions += f"…and {len(winners) - i} more"
                break
            mentions += nxt
        text += mentions
    else:
        text += "."
    channel = msg.channel if msg else await pickem_channel()
    kwargs = {"reference": msg, "mention_author": False} if msg else {}
    await channel.send(text, allowed_mentions=NO_PINGS, **kwargs)


async def update_live_leaderboard(force: bool = False) -> None:
    """Keep one always-current Top 25 message in LEADERBOARD_CHANNEL_ID."""
    if not LEADERBOARD_CHANNEL_ID:
        return
    if not force and time.time() - bot.last_board_update < LIVE_BOARD_EVERY_SECS:
        return
    bot.last_board_update = time.time()
    embed = await leaderboard_embed(DAILY_LEADERBOARD_SIZE, f"🏆 PC Points Leaderboard — Top {DAILY_LEADERBOARD_SIZE}")
    if embed is None:
        embed = discord.Embed(title=f"🏆 PC Points Leaderboard — Top {DAILY_LEADERBOARD_SIZE}",
                              description="Nobody has any PC Points yet. Get picking! 🏀", color=discord.Color.gold())
    if not force and embed.description == bot.last_board_text:
        return  # nothing changed
    embed.set_footer(text="Updates automatically · last updated")
    embed.timestamp = discord.utils.utcnow()
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID) or await bot.fetch_channel(LEADERBOARD_CHANNEL_ID)
    msg_id = await bot.db.get_setting("leaderboard_message_id")
    if msg_id:
        try:
            await channel.get_partial_message(int(msg_id)).edit(embed=embed)
            bot.last_board_text = embed.description
            return
        except discord.NotFound:
            pass  # deleted: post a new one
    msg = await channel.send(embed=embed, allowed_mentions=NO_PINGS)
    await bot.db.set_setting("leaderboard_message_id", str(msg.id))
    bot.last_board_text = embed.description
    log.info("Posted live leaderboard in #%s", getattr(channel, "name", LEADERBOARD_CHANNEL_ID))


async def refresh_board_soon() -> None:
    try:
        await update_live_leaderboard(force=True)
    except Exception:
        log.exception("Live leaderboard update failed")


async def daily_leaderboard() -> None:
    """Post the Top 25 once a day at DAILY_LEADERBOARD_HOUR (US Eastern)."""
    now = datetime.now(EASTERN)
    today = now.date().isoformat()
    # Two-hour window so a restart around 10am doesn't skip the day, but a
    # restart in the afternoon doesn't post a late one.
    if not DAILY_LEADERBOARD or not DAILY_LEADERBOARD_HOUR <= now.hour < DAILY_LEADERBOARD_HOUR + 2:
        return
    if await bot.db.get_setting("daily_leaderboard_date") == today:
        return
    await bot.db.set_setting("daily_leaderboard_date", today)
    embed = await leaderboard_embed(DAILY_LEADERBOARD_SIZE, f"☀️ Daily PC Points Leaderboard — Top {DAILY_LEADERBOARD_SIZE}")
    if embed is None:
        return
    channel = await pickem_channel()
    await channel.send(embed=embed, allowed_mentions=NO_PINGS)
    log.info("Posted daily leaderboard")


async def self_ping() -> None:
    """Keep a free Render web service awake by hitting our own public URL."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url or time.time() - bot.last_self_ping < SELF_PING_EVERY_SECS:
        return
    bot.last_self_ping = time.time()
    try:
        async with bot.http_session.get(url.rstrip("/") + "/health") as resp:
            await resp.read()
    except Exception as e:
        log.debug("Self-ping failed: %r", e)


# ---------------------------------------------------------------------------
# Polls posted before the switch to buttons
# ---------------------------------------------------------------------------

async def upgrade_reaction_polls() -> None:
    """Once per start: give open reaction-style polls buttons, keeping their picks."""
    if bot.polls_upgraded:
        return
    bot.polls_upgraded = True
    for game in await bot.db.games_with_status("open"):
        msg = await poll_message(game)
        if not msg or msg.components:
            continue
        async with bot.game_locks[game["game_id"]]:
            for user_id, choices in (await reaction_picks(msg)).items():
                if len(choices) == 1 and await bot.db.get_pick(game["game_id"], user_id) is None \
                        and await can_play_id(GUILD_ID, user_id):
                    await bot.db.set_pick(game["game_id"], user_id, next(iter(choices)))
            try:
                await msg.edit(embed=poll_embed(game), view=PollView(game))
                await msg.clear_reactions()
            except discord.HTTPException as e:
                log.warning("Couldn't upgrade poll %s to buttons: %r", game["game_id"], e)
        log.info("Upgraded poll %s to buttons", game["game_id"])


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def leaderboard_embed(limit: int, title: str) -> discord.Embed | None:
    rows = await bot.db.leaderboard(limit)
    if not rows:
        return None
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"{medals.get(i, f'`#{i}`')} <@{uid}> — **{pts:,}** PC Points" for i, (uid, pts) in enumerate(rows, 1)]
    return discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())


def is_mod(member: discord.abc.User) -> bool:
    return any(r.name == MOD_ROLE for r in getattr(member, "roles", []))


def can_play(member: discord.abc.User | None) -> bool:
    return any(r.name in (MEMBER_ROLE, MOD_ROLE) for r in getattr(member, "roles", []))


async def can_play_id(guild_id: int, user_id: int) -> bool:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False
    return can_play(member)


@bot.command(name="leaderboard", aliases=["lb"])
@commands.has_any_role(MEMBER_ROLE, MOD_ROLE)
async def leaderboard_cmd(ctx: commands.Context) -> None:
    embed = await leaderboard_embed(10, "🏆 PC Points Leaderboard — Top 10")
    if embed is None:
        await ctx.send("Nobody has any PC Points yet. Get picking! 🏀")
        return
    await ctx.send(embed=embed, allowed_mentions=NO_PINGS)


@bot.command(name="pcpoints", aliases=["points"])
@commands.has_any_role(MEMBER_ROLE, MOD_ROLE)
async def pcpoints_cmd(ctx: commands.Context, *, member: discord.Member | None = None) -> None:
    if member and member != ctx.author and not is_mod(ctx.author):
        await ctx.send(f"⛔ Only **{MOD_ROLE}** can check someone else's PC Points.")
        return
    member = member or ctx.author
    points, rank = await bot.db.get_points(member.id)
    who = "You have" if member == ctx.author else f"{member.mention} has"
    text = f"💰 {who} **{points:,}** PC Points."
    if rank:
        text += f" (Rank #{rank})"
    await ctx.send(text, allowed_mentions=NO_PINGS)


@bot.command(name="winrate", aliases=["record"])
@commands.has_any_role(MEMBER_ROLE, MOD_ROLE)
async def winrate_cmd(ctx: commands.Context, member: discord.Member | None = None) -> None:
    member = member or ctx.author
    wins, losses = await bot.db.record(member.id)
    who = "You" if member == ctx.author else member.mention
    if wins + losses == 0:
        await ctx.send(f"📊 {who} {'have' if who == 'You' else 'has'} no graded picks yet.",
                       allowed_mentions=NO_PINGS)
        return
    owner = "Your" if member == ctx.author else f"{member.mention}'s"
    await ctx.send(f"📊 {owner} pick'em: **{fmt_pct(wins, losses)} win rate, {wins}-{losses} record**",
                   allowed_mentions=NO_PINGS)


@bot.command(name="pickemhelp")
async def help_cmd(ctx: commands.Context) -> None:
    await ctx.send(
        "**PC Pick Em' commands**\n"
        f"`!leaderboard` — Top 10 PC Point holders ({MEMBER_ROLE} only)\n"
        f"`!pcpoints` — your PC Points total ({MEMBER_ROLE} only)\n"
        f"`!pcpoints [member]` — someone else's PC Points ({MOD_ROLE} only)\n"
        f"`!winrate [@member]` — pick'em win % and record ({MEMBER_ROLE} only)\n"
        f"`/givepcpoints @member amount` — give/take PC Points ({MOD_ROLE} only)\n\n"
        f"Polls go up in <#{CHANNEL_ID}> {POST_HOURS_BEFORE:g} hours before every NBA game. "
        f"Tap a team's button (you can switch until tip-off). Correct pick = +{POINTS_PER_WIN} PC Points.\n"
        + (f"The Top {DAILY_LEADERBOARD_SIZE} leaderboard is posted every day at {DAILY_LEADERBOARD_HOUR % 12 or 12}"
           f"{'am' if DAILY_LEADERBOARD_HOUR < 12 else 'pm'} ET." if DAILY_LEADERBOARD else "")
        + (f"\nLive leaderboard: <#{LEADERBOARD_CHANNEL_ID}>" if LEADERBOARD_CHANNEL_ID else "")
    )


@bot.command(name="givepcpoints")
@commands.has_role(MOD_ROLE)
async def givepcpoints_prefix(ctx: commands.Context, member: discord.Member, amount: int) -> None:
    await ctx.send(await give_points(ctx.author, member, amount), allowed_mentions=NO_PINGS)


@bot.tree.command(name="givepcpoints", description="Give (or take, with a negative number) PC Points",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Who gets the points", amount="How many PC Points (negative to remove)")
@app_commands.checks.has_role(MOD_ROLE)
async def givepcpoints_slash(interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
    if amount == 0:
        await interaction.response.send_message("Amount can't be 0.", ephemeral=True)
        return
    await interaction.response.send_message(await give_points(interaction.user, member, amount),
                                            allowed_mentions=NO_PINGS)


async def give_points(giver: discord.abc.User, member: discord.Member, amount: int) -> str:
    total = await bot.db.add_points(member.id, amount)
    task = asyncio.create_task(refresh_board_soon())
    bot.bg_tasks.add(task)
    task.add_done_callback(bot.bg_tasks.discard)
    verb = "gave" if amount > 0 else "took"
    log.info("%s %s %d PC Points %s %s", giver, verb, abs(amount), "to" if amount > 0 else "from", member)
    return (f"✅ {giver.mention} {verb} **{abs(amount):,}** PC Points {'to' if amount > 0 else 'from'} "
            f"{member.mention}. They now have **{total:,}** PC Points.")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingRole):
        msg = f"⛔ Only members with the **{MOD_ROLE}** role can use this."
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "⛔ You need Administrator permission to use this."
    else:
        log.exception("Slash command error", exc_info=error)
        cause = getattr(error, "original", error)
        if isinstance(cause, discord.Forbidden):
            msg = ("⛔ I'm missing a permission for that in this channel or server "
                   f"(Discord said: {cause.text or cause.status}).")
        else:
            msg = f"Something went wrong running that command ({type(cause).__name__}). Check the Render logs."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRole):
        await ctx.send(f"⛔ Only members with the **{MOD_ROLE}** role can use this.")
    elif isinstance(error, commands.MissingAnyRole):
        await ctx.send(f"⛔ You need the **{MEMBER_ROLE}** role to use this.")
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send("Use PC Pick Em' commands in the server, not in DMs.")
    elif isinstance(error, (commands.MemberNotFound, commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.send(f"⚠️ {error}")
    else:
        log.exception("Command error", exc_info=error)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


# ---------------------------------------------------------------------------
# Tiny web server so Render sees an open port (and for uptime pings)
# ---------------------------------------------------------------------------

async def start_web_server() -> None:
    port = os.getenv("PORT")
    if not port:
        return

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="PC Pick Em' bot is running")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Health server listening on :%s", port)


async def main() -> None:
    # Open the port first so Render sees the service as up even while logging in.
    await start_web_server()
    async with bot:
        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            raise SystemExit("DISCORD_TOKEN is invalid. Reset it in the Discord developer portal "
                             "and paste the new one into Render.")
        except discord.PrivilegedIntentsRequired:
            needed = "'Message Content Intent'" + (" and 'Server Members Intent'" if verify.is_configured() else "")
            raise SystemExit(f"Turn on {needed} in the Discord developer portal (Bot page), save, then redeploy.")


if __name__ == "__main__":
    asyncio.run(main())
