# Avenue Guard

Avenue Guard is the Discord utility bot for GD Avenue. It handles moderation guardrails, weekly activity rewards, staff tickets, DM help flows, forum reminders, sticky notices, configurable auto-responses, and a few community fun commands.

The bot is intentionally built around one configured server. Most behavior is controlled from `config.json`, with message-trigger responses in `responses.json` and persistent state in `data/bot.db`.

## Core Features

### Server Guardrails
- Restricts the bot to one guild using `guild.allowed_guild_id`.
- Auto-deletes messages and reactions in the configured creator-points proof channel.
- Applies a restriction role to users who post/react where they should not.
- Allows configured whitelist roles to bypass that restriction flow.
- Sends configurable DMs when users gain watched roles.

### Weekly Activity Requests
- Counts eligible member messages per Madrid-time week.
- Skips configured roles and channels.
- Provides `/tracking top`, `/tracking me`, `/tracking reset`, and `/tracking force_dm`.
- DMs weekly winners with the request format.
- Supports claim, decline confirmation, timeout, reminders, and automatic offer to the next eligible member.
- Logs weekly request events to SQLite and optionally to a log channel.

### Help Menu And Staff Tickets
- DMs members a persistent help menu.
- Supports FAQ, punishment appeals, user reports, bot issue reports, weekly status checks, transcript requests, and staff contact tickets.
- Creates private ticket channels for the requester and staff.
- Tracks ticket inactivity and prompts staff to close stale tickets.
- Saves transcripts before deleting tickets.
- Lets staff approve or deny transcript requests.

### Forum And Sticky Automation
- Posts sticky reminder messages at the bottom of configured text channels.
- Sends first-message reminder embeds in configured forum channels.
- Supports tag-specific forum reminder embeds.
- Can enforce a required word in forum post title/body.
- If the required word is missing, Avenue Guard DMs the thread owner and deletes the forum thread.
- Admins can view or change the required word with `/forum required_word`.

### Configurable Auto-Responses
- Uses `responses.json` for message-triggered replies.
- Supports whole-message or contains matching.
- Supports plain messages or embeds.
- Supports channel filters and per-user cooldowns.
- Stops after the first matching rule.

### Background Utilities
- Optional rotating bot status with placeholders like `{members}`, `{online}`, `{week_msgs}`, `{week_top}`, `{open_tickets}`, and `{today_msgs}`.
- Optional daily server summary embeds.
- Tracks daily messages, edits, deletes, reactions, joins, leaves, bans, boosts, voice minutes, command usage, and top channels/users.
- Includes a small keepalive HTTP server for hosted environments.

### Fun Commands
- `/dance` sends the configured GIF.
- `/rock-paper-scissors` runs a button-based game with per-user cooldown and optional streak reward role.
- `/gambling` runs a small slot animation with optional rare reward role.

## Main Slash Commands

- `/tracking top` shows the current weekly leaderboard.
- `/tracking me` shows your weekly count and rank.
- `/tracking reset` resets this week's tracking data. Admins/owners only.
- `/tracking force_dm` manually sends a weekly request DM. Admins/owners only.
- `/ticket close` closes the current ticket channel. Mods only.
- `/forum required_word` views or changes the forum required word. Discord administrators only.
- `/resync` reloads config and response rules without restarting. Admins/owners only.
- `/restart` exits the bot so the host can restart it. Admins/owners only.
- `/dance`, `/rock-paper-scissors`, `/gambling` are public fun commands.

## Required-Word Forum Enforcement

Required-word checks live in `forum_first_message.entries[]` in `config.json`.

Example:

```json
{
  "forum_channel_id": "1104487618026143754",
  "required_word": "cubical",
  "missing_required_word_dm": "Your thread \"{thread_name}\" was removed because it did not include \"{required_word}\".",
  "required_word_delete_delay_seconds": 5,
  "templates": {
    "default": {
      "title": "Make sure your collab follows our format!",
      "description": "Your collab must have a name, theme, song, appeal, and purpose!",
      "color": "blurple"
    }
  }
}
```

`missing_required_word_dm` supports:
- `{required_word}`
- `{thread_name}`
- `{guild}`

To change the word without editing files, use:

```text
/forum required_word word:cubical
```

If multiple forum channels are configured, provide the forum channel ID:

```text
/forum required_word word:cubical forum_channel_id:1104487618026143754
```

Use `off`, `disable`, `none`, or `clear` as the word to disable enforcement for that forum.

## Configuration Files

- `config.json` controls guild IDs, roles, channels, weekly tracking, tickets, sticky messages, forum reminders, role DMs, fun rewards, help menu FAQ, and background summaries.
- `responses.json` controls automatic message responses.
- `data/bot.db` stores persistent bot data such as weekly counts, tickets, cooldowns, transcript pointers, reminders, and daily stats.

## Running The Bot

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set the bot token:

```bash
export DISCORD_TOKEN="your-token"
```

3. Start Avenue Guard:

```bash
python main.py
```

## Discord Intents And Permissions

Enable these in the Discord Developer Portal:
- Server Members Intent
- Message Content Intent

Useful bot permissions:
- Administrator, or at minimum:
- Manage Roles
- Manage Channels
- Manage Threads
- Manage Messages
- Read Message History
- Send Messages
- Embed Links
- Attach Files

## Persistence Notes

SQLite is stored at `data/bot.db`. If the bot is hosted somewhere with ephemeral storage, mount persistent storage or move the database path to a persistent disk.

## Local Testing

Use `TEST_CHECKLIST.md` for the full server-side test flow. It covers startup, moderation, tracking, help sessions, ticket closure, transcript requests, sticky messages, forum reminders, required-word deletion, and fun commands.
