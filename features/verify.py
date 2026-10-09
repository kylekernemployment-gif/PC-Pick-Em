"""Captcha verification for new members.

New members only see the verify channel. They click "Verify", read a code
from an image only they can see, and type it in. Success gives them the
Verified role (which unlocks the server); 3 wrong tries, an account that's too
new, or not verifying in time gets them kicked. They can rejoin and try again.
"""

from __future__ import annotations

import io
import os
import random
import string
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .common import env_id, log, mod_log

CODE_CHARS = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL5S8B")
MAX_TRIES = 3


def is_configured() -> bool:
    return env_id("VERIFY_CHANNEL_ID") is not None


def make_captcha(code: str) -> io.BytesIO:
    w, h = 320, 110
    img = Image.new("RGB", (w, h), (32, 34, 40))
    draw = ImageDraw.Draw(img)
    for _ in range(8):  # background noise lines
        draw.line([(random.randint(0, w), random.randint(0, h)), (random.randint(0, w), random.randint(0, h))],
                  fill=(random.randint(70, 140),) * 3, width=2)
    try:
        font = ImageFont.load_default(size=56)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    x = 22
    for ch in code:
        glyph = Image.new("RGBA", (70, 90), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((8, 8), ch, font=font,
                                   fill=(random.randint(170, 255), random.randint(170, 255), random.randint(170, 255)))
        glyph = glyph.rotate(random.randint(-25, 25), expand=False, resample=Image.BICUBIC)
        img.paste(glyph, (x, random.randint(0, 22)), glyph)
        x += 56
    for _ in range(250):  # speckles
        draw.point((random.randint(0, w - 1), random.randint(0, h - 1)), fill=(200, 200, 200))
    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


class CodeModal(discord.ui.Modal, title="Enter the code from the image"):
    code = discord.ui.TextInput(label="Code", min_length=5, max_length=5, placeholder="e.g. A7K2Q")

    def __init__(self, cog: "Verification") -> None:
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.check_answer(interaction, self.code.value)


class EnterCodeView(discord.ui.View):
    def __init__(self, cog: "Verification") -> None:
        super().__init__(timeout=600)
        self.cog = cog

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary, emoji="⌨️")
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CodeModal(self.cog))


class VerifyView(discord.ui.View):
    """The permanent button in the verify channel (survives restarts)."""

    def __init__(self, cog: "Verification") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅", custom_id="pcverify:start")
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.start_captcha(interaction)


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot, guild_id: int, mod_role: str) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.mod_role = mod_role
        self.channel_id = env_id("VERIFY_CHANNEL_ID")
        self.role_name = os.getenv("VERIFIED_ROLE_NAME", "Verified")
        self.timeout_min = int(os.getenv("VERIFY_TIMEOUT_MINUTES", "30"))
        self.min_age_days = float(os.getenv("MIN_ACCOUNT_AGE_DAYS", "3"))
        self.codes: dict[int, tuple[str, int]] = {}  # user_id -> (code, tries left)

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyView(self))
        self.sweep.start()
        log.info("Captcha verification on (channel %s, role %r)", self.channel_id, self.role_name)

    async def cog_unload(self) -> None:
        self.sweep.cancel()

    def verified_role(self, guild: discord.Guild) -> discord.Role | None:
        return discord.utils.get(guild.roles, name=self.role_name)

    async def kick(self, member: discord.Member, why: str) -> None:
        try:
            await member.send(f"You were removed from **{member.guild.name}**: {why}\n"
                              "You're welcome to rejoin and verify.")
        except discord.HTTPException:
            pass  # DMs closed
        try:
            await member.kick(reason=why)
            await mod_log(self.bot, f"👢 Kicked {member} (`{member.id}`): {why}")
        except discord.HTTPException as e:
            log.warning("Couldn't kick %s (does the bot have Kick Members?): %r", member, e)
        await self.bot.db.remove_pending(member.id)

    # -- joins ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id != self.guild_id or member.bot:
            return
        age_days = (datetime.now(timezone.utc) - member.created_at).total_seconds() / 86400
        if self.min_age_days and age_days < self.min_age_days:
            await self.kick(member, f"Discord account is too new (must be at least {self.min_age_days:g} days old).")
            return
        await self.bot.db.add_pending(member.id, int(time.time()))
        await mod_log(self.bot, f"📥 {member} (`{member.id}`) joined — account {age_days:.0f} days old, waiting to verify.")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild.id == self.guild_id:
            await self.bot.db.remove_pending(member.id)
            self.codes.pop(member.id, None)

    @tasks.loop(minutes=1)
    async def sweep(self) -> None:
        """Kick people who joined but didn't verify in time."""
        try:
            guild = self.bot.get_guild(self.guild_id)
            if not guild:
                return
            role = self.verified_role(guild)
            for user_id in await self.bot.db.pending_before(int(time.time()) - self.timeout_min * 60):
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        await self.bot.db.remove_pending(user_id)
                        continue
                if role and role in member.roles:
                    await self.bot.db.remove_pending(user_id)
                    continue
                await self.kick(member, f"didn't verify within {self.timeout_min} minutes.")
        except Exception:
            log.exception("Verification sweep failed")

    @sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    # -- captcha ----------------------------------------------------------
    async def start_captcha(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        role = self.verified_role(interaction.guild)
        if role is None:
            await interaction.response.send_message(
                f"⚠️ The **{self.role_name}** role doesn't exist yet. Ask a mod to create it.", ephemeral=True)
            return
        if role in member.roles:
            await interaction.response.send_message("✅ You're already verified!", ephemeral=True)
            return
        code = "".join(random.choices(CODE_CHARS, k=5))
        tries = self.codes.get(member.id, (None, MAX_TRIES))[1]
        self.codes[member.id] = (code, tries)
        await interaction.response.send_message(
            f"Type the 5 characters you see in the image (not case-sensitive). Tries left: **{tries}**",
            file=discord.File(make_captcha(code), "captcha.png"),
            view=EnterCodeView(self), ephemeral=True)

    async def check_answer(self, interaction: discord.Interaction, answer: str) -> None:
        member = interaction.user
        entry = self.codes.get(member.id)
        if not entry:
            await interaction.response.send_message("That code expired. Click **Verify** again.", ephemeral=True)
            return
        code, tries = entry
        if answer.strip().upper() == code:
            self.codes.pop(member.id, None)
            try:
                await member.add_roles(self.verified_role(interaction.guild), reason="Passed captcha")
            except discord.HTTPException as e:
                log.warning("Couldn't give Verified role (Manage Roles / role order?): %r", e)
                await interaction.response.send_message("⚠️ Correct, but I couldn't give you the role. "
                                                        "Please ping a mod.", ephemeral=True)
                return
            await self.bot.db.remove_pending(member.id)
            await interaction.response.send_message("✅ Verified — welcome! The rest of the server is now unlocked.",
                                                    ephemeral=True)
            await mod_log(self.bot, f"✅ {member} (`{member.id}`) verified.")
            return
        tries -= 1
        if tries <= 0:
            self.codes.pop(member.id, None)
            await interaction.response.send_message("❌ Wrong code — out of tries.", ephemeral=True)
            await self.kick(member, f"failed the captcha {MAX_TRIES} times.")
            return
        self.codes[member.id] = (code, tries)
        await interaction.response.send_message(
            f"❌ Wrong code. **{tries}** {'try' if tries == 1 else 'tries'} left — click **Enter code** to try again, "
            "or **Verify** for a new image.", ephemeral=True)

    # -- mod commands -----------------------------------------------------
    @app_commands.command(name="setupverify", description="Post the Verify button in this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def setupverify(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🔒 Verify to enter",
            description="Click **Verify** below, then type the code from the picture.\n"
                        f"You have {self.timeout_min} minutes and {MAX_TRIES} tries.",
            color=discord.Color.green())
        await interaction.channel.send(embed=embed, view=VerifyView(self))
        await interaction.response.send_message("✅ Verify button posted.", ephemeral=True)

    @app_commands.command(name="verifyeveryone",
                          description="Give the Verified role to everyone already in the server (run once)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def verifyeveryone(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        role = self.verified_role(guild)
        if role is None:
            await interaction.response.send_message(f"⚠️ Create a role named **{self.role_name}** first.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not guild.chunked:
            await guild.chunk()
        added = failed = 0
        for member in guild.members:
            if member.bot or role in member.roles:
                continue
            try:
                await member.add_roles(role, reason="Existing member (verifyeveryone)")
                added += 1
            except discord.HTTPException:
                failed += 1
        msg = f"✅ Gave **{self.role_name}** to {added} member(s)."
        if failed:
            msg += f" Couldn't for {failed} — check that the bot's role is above {self.role_name}."
        await interaction.followup.send(msg, ephemeral=True)
