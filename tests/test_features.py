from features.antiscam import LINK, SCAM_PATTERNS
from features.verify import CODE_CHARS, make_captcha
from features.youtube import parse_latest_video_id, parse_playlist_latest, parse_video_status, uploads_playlist_id

RSS = """<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
 xmlns="http://www.w3.org/2005/Atom"><entry><yt:videoId>abc123</yt:videoId></entry>
 <entry><yt:videoId>older</yt:videoId></entry></feed>"""


def test_youtube_parsing():
    assert parse_latest_video_id(RSS) == "abc123"
    live = {"items": [{"snippet": {"liveBroadcastContent": "live", "title": "Stream",
                                   "thumbnails": {"high": {"url": "t.jpg"}}},
                       "liveStreamingDetails": {"actualStartTime": "x"}}]}
    r = parse_video_status(live, "abc123")
    assert r["status"] == "live" and r["url"].endswith("abc123") and r["thumbnail"] == "t.jpg"
    assert parse_video_status({"items": [{"snippet": {"liveBroadcastContent": "upcoming"}}]}, "x")["status"] == "upcoming"
    up = parse_video_status({"items": [{"snippet": {"liveBroadcastContent": "none", "title": "Vid"}}]}, "x")
    assert up["status"] == "upload" and up["title"] == "Vid" and up["url"].endswith("x")
    vod = {"items": [{"snippet": {"liveBroadcastContent": "none"},
                      "liveStreamingDetails": {"actualStartTime": "a", "actualEndTime": "b"}}]}
    assert parse_video_status(vod, "x")["status"] == "vod"
    assert parse_video_status({"items": []}, "x")["status"] == "none"


def test_scam_filter():
    scams = ["FREE NITRO here https://x.co", "claim at discord.gift/abc", "dlscord.com/nitro",
             "https://steamcommunlty.com/gift", "@everyone check this https://bit.ly/x", "free steam keys"]
    fine = ["Lakers in 6", "check https://www.nba.com/game", "join discord.gg/abc", "discord.com is down?",
            "i got nitro for my birthday"]
    assert all(SCAM_PATTERNS.search(t) for t in scams)
    assert not any(SCAM_PATTERNS.search(t) for t in fine)
    assert LINK.search("https://x.com") and LINK.search("discord.gg/x") and not LINK.search("no links")


def test_captcha_image():
    assert not set("0O1IL") & set(CODE_CHARS)
    png = make_captcha("A7K2Q").getvalue()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000


def test_youtube_api_fallback_parsing():
    assert uploads_playlist_id("UCE1FN3sPMeDo253rgZJZd2A") == "UUE1FN3sPMeDo253rgZJZd2A"
    assert parse_playlist_latest({"items": [{"contentDetails": {"videoId": "xyz"}}]}) == "xyz"
    assert parse_playlist_latest({"items": []}) is None
