# Pycord Bot (Render-ready)

## What this includes
- One-guild-only bot (configured in `config.json`)
- SQLite persistence (hardcoded to `data/bot.db`)
- Cogs:
  - Mod (autodelete/restrict + auto-DM on role gain)
  - Tracking (weekly top 20, DM claim flow, timeout, forwarding)
  - Help (DM help menu + ticket creation + inactivity close prompt + transcript)
  - MessageResponses (config-driven responses with cooldown; first match only)
  - Sticky (sticky bottom messages + forum thread first-message embeds by tag + optional required-word enforcement)
  - Commands (slash commands: tracking, ticket, resync, restart, dance, rps, gambling)
- Keepalive HTTP server for Render + UptimeRobot

## Setup
1. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variable:
   - `DISCORD_TOKEN` = your bot token

3. Edit `config.json` with your server/channel/role IDs.

4. Run:
   ```bash
   python main.py
   ```

## Render notes
- Web Service (Python)
- Start command: `python main.py`
- Add Environment Variable:
  - `DISCORD_TOKEN`
- Optional: set `PORT` (Render sets it automatically)
- SQLite file lives at `data/bot.db` inside the project directory.
  - If you want persistence across deploys, mount a persistent disk and point the project folder there,
    or modify code to place DB on the mounted disk. (You asked to hardcode to `data/bot.db`, so it is.)

## Discord Developer Portal
Enable privileged intents for:
- Server Members Intent
- Message Content Intent
Presence intent is optional.

## Responses configuration
Message response rules are in `responses.json`. Order matters: only the first matching rule is executed.

## Forum required word
For forum reminder threads, set `required_word`, `missing_required_word_dm`, and `required_word_delete_delay_seconds`
inside a `forum_first_message.entries[]` item in `config.json`. If `required_word` is empty or omitted, enforcement is disabled for that forum.

## Help menu options
- FAQ
- Appeal punishment
- Report a user/message (optional false-report warning)
- Report a bot issue
- Check weekly status
- Request transcript (staff approval)
- Mod contact (ticket)
