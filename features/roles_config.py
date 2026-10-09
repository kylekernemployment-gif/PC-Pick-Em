"""Reaction roles (ported from the PC-Roles bot's config.js)."""

EMBED_TITLE = "‼️React to Get Your Roles!"
EMBED_DESCRIPTION = "\n".join([
    "React below to receive your roles:",
    "",
    "🏀 — NBA",
    "🏈 — NFL",
    "🎮 — Gaming",
    "🚨 — YouTube Alerts",
    "✅ — PC Community",
])
EMBED_COLOR = 0x5865F2

# emoji -> role ID
REACTION_ROLES = {
    "🏀": 1489305484111642687,
    "🏈": 1489305457943253002,
    "🎮": 1489305402679103699,
    "🚨": 1489304199576686642,
    "✅": 1489304154282393751,
}

# The roles message that's already in your server. /setuproles posts a new one
# and the bot remembers its ID from then on.
DEFAULT_MESSAGE_ID = 1489501492057608365
