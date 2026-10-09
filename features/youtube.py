"""YouTube live notifications (ported from the PC-YT-Live-Noti bot).

Every 5 minutes: read the channel's RSS feed for the newest video. If it's new
and it's a live stream that's on air, post an @everyone embed.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import discord
from discord.ext import commands, tasks

from .common import env_id, log

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"
PLAYLIST_ITEMS_API = "https://www.googleapis.com/youtube/v3/playlistItems"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
SEEN_KEY = "youtube_last_video_id"


def parse_latest_video_id(xml_text: str) -> str | None:
    entry = ET.fromstring(xml_text).find("atom:entry", NS)
    vid = entry.find("yt:videoId", NS) if entry is not None else None
    return vid.text if vid is not None else None


def uploads_playlist_id(channel_id: str) -> str:
    """A channel's uploads playlist is its ID with UC swapped for UU."""
    return "UU" + channel_id[2:] if channel_id.startswith("UC") else channel_id


def parse_playlist_latest(data: dict) -> str | None:
    items = (data or {}).get("items") or []
    return items[0].get("contentDetails", {}).get("videoId") if items else None


def parse_video_status(data: dict, video_id: str) -> dict:
    items = data.get("items", [])
    if not items:
        return {"status": "none"}
    snippet = items[0].get("snippet", {})
    live = items[0].get("liveStreamingDetails", {})
    broadcast = snippet.get("liveBroadcastContent", "none")
    if broadcast == "live" and live.get("actualStartTime") and not live.get("actualEndTime"):
        return {
            "status": "live",
            "title": snippet.get("title", ""),
            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
        }
    if broadcast == "upcoming":
        return {"status": "upcoming"}
    return {"status": "none"}


def is_configured() -> bool:
    return bool(os.getenv("YOUTUBE_API_KEY") and os.getenv("YOUTUBE_CHANNEL_ID")
                and env_id("YOUTUBE_NOTIFY_CHANNEL_ID"))


class YouTubeLive(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api_key = os.environ["YOUTUBE_API_KEY"].strip()
        self.yt_channel = os.environ["YOUTUBE_CHANNEL_ID"].strip().strip("'\"").strip()
        self.notify_channel_id = env_id("YOUTUBE_NOTIFY_CHANNEL_ID")

    async def cog_load(self) -> None:
        self.check.start()

    async def cog_unload(self) -> None:
        self.check.cancel()

    async def latest_video_id(self) -> str | None:
        """Newest video from the RSS feed, or from the YouTube API if the feed is down."""
        try:
            async with self.bot.http_session.get(RSS_URL.format(channel_id=self.yt_channel), headers=HEADERS) as r:
                if r.status == 200:
                    return parse_latest_video_id(await r.text())
                log.info("[YouTube] RSS -> HTTP %s, using the YouTube API instead", r.status)
        except Exception as e:
            log.info("[YouTube] RSS error (%r), using the YouTube API instead", e)
        params = {"part": "contentDetails", "playlistId": uploads_playlist_id(self.yt_channel),
                  "maxResults": 1, "key": self.api_key}
        try:
            async with self.bot.http_session.get(PLAYLIST_ITEMS_API, params=params) as r:
                if r.status != 200:
                    log.warning("[YouTube] API playlistItems -> HTTP %s: %s", r.status, (await r.text())[:300])
                    return None
                return parse_playlist_latest(await r.json())
        except Exception as e:
            log.warning("[YouTube] API playlistItems error: %r", e)
            return None

    async def video_status(self, video_id: str) -> dict | None:
        params = {"part": "snippet,liveStreamingDetails", "id": video_id, "key": self.api_key}
        try:
            async with self.bot.http_session.get(VIDEOS_API, params=params) as r:
                if r.status != 200:
                    log.warning("[YouTube] API -> HTTP %s", r.status)
                    return None
                return parse_video_status(await r.json(), video_id)
        except Exception as e:
            log.warning("[YouTube] API error: %r", e)
            return None

    async def notify(self, result: dict) -> bool:
        embed = discord.Embed(title=result["title"], url=result["url"],
                              description="🔴 We're live on YouTube! Come watch!", color=0xFF0000)
        embed.set_image(url=result["thumbnail"])
        embed.set_footer(text="Click the title to watch!")
        try:
            channel = self.bot.get_channel(self.notify_channel_id) or await self.bot.fetch_channel(self.notify_channel_id)
            await channel.send(content="@everyone", embed=embed,
                               allowed_mentions=discord.AllowedMentions(everyone=True))
            return True
        except discord.HTTPException as e:
            log.warning("[YouTube] Couldn't send live notification (will retry): %r", e)
            return False

    @tasks.loop(minutes=5)
    async def check(self) -> None:
        try:
            await self._check()
        except Exception:  # keep the loop alive no matter what
            log.exception("[YouTube] check failed")

    async def _check(self) -> None:
        video_id = await self.latest_video_id()
        if not video_id:
            return
        last_seen = await self.bot.db.get_setting(SEEN_KEY)
        if last_seen is None:
            # First run: don't announce whatever is already up (matches the old bot).
            await self.bot.db.set_setting(SEEN_KEY, video_id)
            log.info("[YouTube] Watching for new live streams (latest video %s)", video_id)
            return
        if video_id == last_seen:
            return
        result = await self.video_status(video_id)
        if result is None:
            return  # API hiccup: try again next cycle
        if result["status"] == "live":
            if await self.notify(result):
                await self.bot.db.set_setting(SEEN_KEY, video_id)
                log.info("[YouTube] Live notification sent for %s", video_id)
        elif result["status"] == "upcoming":
            log.info("[YouTube] %s is scheduled; waiting for it to go live", video_id)
        else:
            await self.bot.db.set_setting(SEEN_KEY, video_id)  # regular upload, skip

    @check.before_loop
    async def before_check(self) -> None:
        await self.bot.wait_until_ready()
