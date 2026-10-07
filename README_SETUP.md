# AmiriPicks Winning Slips Tracker

This Discord bot tracks **image posts only** in your configured `#winning-slips` channel.

## What it does

- Counts **one contribution per message** containing an image attachment.
- Ignores text-only messages.
- Ignores every other channel.
- Tracks:
  - Weekly leaderboard
  - Monthly leaderboard
  - All-time leaderboard
- Removes a contribution if the original Discord message is deleted.
- Re-checks edited messages.
- Can scan old posts with `/backfill_slips`.

## Slash commands

- `/leaderboard period:Weekly`
- `/leaderboard period:Monthly`
- `/leaderboard period:All Time`
- `/slipcount member:@User period:...`
- `/my_slipcount period:...`
- `/backfill_slips` — admin-only; scans existing channel history.

## Your configured Discord IDs

- Server: `1327705127582171240`
- Winning slips channel: `1327718864888533064`

## Railway setup

### 1. Put these files in your GitHub repo

Upload:

- `bot.py`
- `requirements.txt`
- `Procfile`
- `.gitignore`

You do **not** need to upload `.env.example`, but you can.

### 2. Add PostgreSQL in Railway

Inside the same Railway project:

1. Click **+ New**
2. Choose **Database**
3. Choose **PostgreSQL**
4. Railway should expose a `DATABASE_URL` to your bot service. If it does not automatically reference it, add a variable to the bot service named `DATABASE_URL` using the Postgres service's connection URL.

PostgreSQL keeps the leaderboard data from disappearing across bot redeploys.

### 3. Add your Discord token safely

In Railway, open your bot service, then go to **Variables**.

Add:

- `DISCORD_TOKEN` = your bot token
- `GUILD_ID` = `1327705127582171240`
- `WINNING_SLIPS_CHANNEL_ID` = `1327718864888533064`

**Do not paste your bot token into Discord chat, GitHub, or the source code.**

### 4. Deploy

Railway should detect the `Procfile` and run:

```text
python bot.py
```

Once the deployment log says something similar to:

```text
Logged in as YourBotName
Tracking image posts in channel 1327718864888533064
```

the bot is live.

### 5. Count old winning slips

Because bots normally begin counting from when they start running, use this once after deployment:

```text
/backfill_slips
```

You need **Manage Server** permission to run it.

That command scans the full history of the winning-slips channel and adds all image posts it finds.

## Discord permissions the bot needs

- View Channels
- Send Messages
- Embed Links
- Read Message History
- Use Application Commands

`Attach Files` is not required for tracking, but it is harmless if already enabled.

`Manage Roles` is not required for this version.
