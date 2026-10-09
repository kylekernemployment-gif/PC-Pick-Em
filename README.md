# PC Pick Em' 🏀

One Discord bot for the PC server:

- **NBA pick'em** with PC Points, a leaderboard and win rates (below)
- **Reaction roles** (merged from the PC-Roles bot)
- **YouTube live notifications** (merged from the PC-YT-Live-Noti bot)
- **Captcha verification** for new members, plus an **anti-scam** filter

Each extra feature turns on only when its settings are added in Render. See [Other features](#other-features).

## NBA pick'em

- **12 hours before every NBA game (preseason included)** it posts a poll in your pick'em channel and pre-reacts with 1️⃣ (away team) and 2️⃣ (home team).
- Only members with the **YouTube Member** (or **Lead Moderator**) role can pick. Anyone else's reaction is removed. Members can only pick **one** team. Reacting to the second one removes it and shows *"You must only react for one team!"*. To switch, remove your first reaction, then click the other one.
- **At tip-off the poll locks.** Late reactions are removed.
- When the game goes **Final**, everyone who picked the winner gets **+100 PC Points** and the bot posts the result.
- The schedule and live scores come from ESPN's public NBA scoreboard. The NBA's own feed blocks cloud hosts like Render. Postponed games are voided with no points awarded.

## Commands

| Command | What it does |
|---|---|
| `!leaderboard` | Top 10 PC Point holders. Needs the **YouTube Member** or **Lead Moderator** role. |
| `!pcpoints` | Your PC Points total and rank. Needs the **YouTube Member** or **Lead Moderator** role. |
| `!pcpoints <member>` | **Lead Moderator only.** Someone else's PC Points. Accepts a mention, username or ID. |
| `!winrate [@member]` | Pick'em record, for example `55% win rate, 55-45 record`. Needs the **YouTube Member** or **Lead Moderator** role. |
| `/givepcpoints @member amount` | **Lead Moderator only.** Gives PC Points. A negative amount takes them away. `!givepcpoints` works too. |
| `!pickemhelp` | Lists the commands |

Every day at **10am Eastern** the bot posts the **Top 25** leaderboard in the pick'em channel, or in `LEADERBOARD_CHANNEL_ID` if you set one.

---

## Setup

### 1. Discord bot
1. Go to <https://discord.com/developers/applications>, click **New Application**, name it **PC Pick Em'**, then open **Bot**.
2. Click **Reset Token** and copy the token. This is `DISCORD_TOKEN`.
3. Under **Privileged Gateway Intents**, turn on **Message Content Intent**. The `!` commands need it.
4. Open **OAuth2 → URL Generator**. Select the scopes `bot` and `applications.commands`. Select these bot permissions:
   View Channels, Send Messages, Embed Links, Read Message History, Add Reactions, **Manage Messages** (used to remove a second or late reaction).
   Open the generated URL and invite the bot to your server.
5. In Discord, turn on **User Settings → Advanced → Developer Mode**. Right-click your server and choose **Copy Server ID** (`GUILD_ID`). Right-click the pick'em channel and choose **Copy Channel ID** (`PICKEM_CHANNEL_ID`).
6. Make sure a role named exactly **Lead Moderator** exists. If yours has a different name, set `MOD_ROLE_NAME`.

### 2. Database (free and permanent)
A Render free web service **wipes its disk on every restart and deploy**, so points need to live in a database outside the bot.
1. Create a free Postgres database at <https://neon.tech>. Supabase also works.
2. Copy the connection string (`postgresql://...?sslmode=require`). This is `DATABASE_URL`.

> Render also offers Postgres, but its free database expires after 30 days. Use Neon or Supabase for this.

### 3. Deploy on Render
1. In Render, choose **New → Blueprint** and select this GitHub repo. Render reads `render.yaml`.
2. Fill in `DISCORD_TOKEN`, `GUILD_ID`, `PICKEM_CHANNEL_ID` and `DATABASE_URL` when asked, then deploy.
3. Keep it awake. Free web services go to sleep after about 15 minutes without web traffic. The bot pings its own URL every 10 minutes. For extra safety, add a free monitor at <https://uptimerobot.com> that hits `https://<your-service>.onrender.com/health` every 5 minutes.

> **Heads-up about free hours:** Render gives each workspace about 750 free instance hours per month. That covers one always-on service. If your other 2 bots are also free and always on, the three bots together will run out of hours partway through the month. Options:
> - Put this bot on the $7/month Starter plan (set `plan: starter` in `render.yaml`, which this repo now uses).
> - Run it in a separate Render workspace.

Check the logs. You should see `Logged in as PC Pick Em'...`, `Synced 1 slash command(s)` and `Schedule refreshed: N games`.

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `DISCORD_TOKEN` | — | required |
| `GUILD_ID` | — | required |
| `PICKEM_CHANNEL_ID` | — | required |
| `DATABASE_URL` | — | Postgres URL. If it's missing, the bot uses a local SQLite file, which is fine for testing on your PC. |
| `MOD_ROLE_NAME` | `Lead Moderator` | role allowed to use `/givepcpoints` |
| `MEMBER_ROLE_NAME` | `YouTube Member` | role allowed to use `!leaderboard`, `!pcpoints` and `!winrate` |
| `LEADERBOARD_CHANNEL_ID` | pick'em channel | where the daily Top 25 is posted |
| `DAILY_LEADERBOARD_HOUR` | `10` | hour for the daily leaderboard, US Eastern, 24-hour clock |
| `POINTS_PER_WIN` | `100` | |
| `POST_HOURS_BEFORE` | `12` | |
| `INCLUDE_PRESEASON` | `true` | polls for preseason games too (good for testing). Set to `false` to only do regular season + playoffs |

## Running locally
```bash
pip install -r requirements.txt
cp .env.example .env   # fill it in, then export the values
python bot.py
pytest                 # parser + points logic tests
```

## Other features

### Reaction roles
These are always on. They use the same 5 roles and the same message as the old PC-Roles bot (see `features/roles_config.py`).
- `/setuproles` (Administrator) posts a fresh roles message in the current channel. The bot remembers it from then on.
- The bot needs the **Manage Roles** permission. Its role must sit **above** the NBA, NFL, Gaming, YouTube Alerts and PC Community roles in Server Settings → Roles.

### YouTube live notifications
Every 5 minutes the bot checks your channel. When a new live stream is on air, it posts an `@everyone` embed. Add these in Render → Environment to turn it on:

| Variable | |
|---|---|
| `YOUTUBE_API_KEY` | copy it from the old PC-YT-Live-Noti service |
| `YOUTUBE_CHANNEL_ID` | copy it from the old service. It starts with `UC`. |
| `YOUTUBE_NOTIFY_CHANNEL_ID` | the Discord channel to post in. This was `CHANNEL_ID` on the old service. |

The bot needs the **Mention Everyone** permission in that channel.

### Captcha verification
New members only see the verify channel until they pass a captcha. Turn it on with `VERIFY_CHANNEL_ID`.
1. Developer portal → Bot → turn on **Server Members Intent** → Save.
2. Create a role named **Verified**. Give the bot **Manage Roles** and **Kick Members**, and drag the bot's role above Verified.
3. Create a `#verify` channel. Set `VERIFY_CHANNEL_ID` in Render and redeploy.
4. In `#verify`, run `/setupverify` to post the Verify button.
5. Run `/verifyeveryone` once, so everyone already in the server gets Verified.
6. Lock the server down:
   - Server Settings → Roles → **@everyone** → turn off **View Channels**.
   - **Verified** role → turn on **View Channels**.
   - `#verify` channel → Permissions → **@everyone** → allow **View Channel**.

New members get 3 tries and 30 minutes. Accounts younger than 3 days are removed. Anyone removed can rejoin and try again.

| Variable | Default | |
|---|---|---|
| `VERIFY_CHANNEL_ID` | — | turns verification on |
| `VERIFIED_ROLE_NAME` | `Verified` | |
| `VERIFY_TIMEOUT_MINUTES` | `30` | |
| `MIN_ACCOUNT_AGE_DAYS` | `3` | set to `0` to allow any account |
| `MOD_LOG_CHANNEL_ID` | — | optional private channel that logs joins, verifications, kicks and deleted scams |

### Anti-scam
This is on by default (`ANTI_SCAM=false` turns it off). It deletes free-Nitro, Steam-gift and fake-Discord-link messages and times the sender out for 1 hour. Members who joined in the last 24 hours can't post links (`NEW_MEMBER_LINK_HOURS`). Mods and anyone with Manage Messages are skipped. The bot needs **Manage Messages** and **Moderate Members**.

