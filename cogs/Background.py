from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, Optional, List, Tuple

import discord
from discord.ext import commands, tasks

from utils.checks import ensure_allowed_guild_id
from utils.timeutils import TZ, now_madrid, week_start_sunday

def _day_key(dt: Optional[datetime] = None) -> str:
    dt = dt or now_madrid()
    return dt.strftime("%Y-%m-%d")

def _parse_hhmm(value: str, default: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
    try:
        parts = value.strip().split(":")
        if len(parts) != 2:
            return default
        hh = max(0, min(23, int(parts[0])))
        mm = max(0, min(59, int(parts[1])))
        return hh, mm
    except Exception:
        return default

def _fmt_minutes(total_minutes: int) -> str:
    if total_minutes <= 0:
        return "0m"
    hours, mins = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"

@dataclass
class DailyStats:
    messages: int = 0
    edits: int = 0
    deletes: int = 0
    reactions: int = 0

    joins: int = 0
    leaves: int = 0
    bans: int = 0
    unbans: int = 0

    boosts: int = 0
    unboosts: int = 0

    voice_minutes: int = 0
    peak_voice_users: int = 0
    peak_online_members: int = 0

    commands: int = 0
    command_errors: int = 0
    commands_by_name: Dict[str, int] = field(default_factory=dict)

    by_channel: Dict[int, int] = field(default_factory=dict)
    by_user: Dict[int, int] = field(default_factory=dict)

class BackgroundCog(commands.Cog):
    """Background tasks & daily telemetry (config-driven).

    This cog is intentionally "low drama":
    - It never DMs users.
    - It only posts a daily summary embed (optional) to a configured channel.
    - It rotates the bot's presence (optional).
    - It persists daily stats to SQLite for future features.
    """

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._started = False
        self._current_day = _day_key()
        self.stats = DailyStats()
        self.voice_sessions: Dict[int, int] = {}  # user_id -> unix_ts join
        self._status_index = -1  # start at -1 so first rotation shows the first status
        self._last_status_swap = 0.0

    async def start_background(self):
        if self._started:
            return
        self._started = True

        # Ensure DB schema exists
        try:
            await self.bot.db.connect()
            await self.bot.db.execute(
                """CREATE TABLE IF NOT EXISTS daily_stats(
                    guild_id INTEGER NOT NULL,
                    day_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_ts INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, day_key)
                );"""
            )
        except Exception:
            pass

        # Initialize voice sessions from current state (best-effort)
        cfg = self.bot.config
        allowed = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed) if allowed else None
        if guild:
            now_ts = int(time.time())
            for m in guild.members:
                try:
                    if m.bot:
                        continue
                    if m.voice and m.voice.channel:
                        self.voice_sessions[m.id] = now_ts
                except Exception:
                    continue

        # Start loops
        if self._daily_summary_enabled():
            self._start_daily_report_loop()

        if self._status_rotation_enabled():
            try:
                if not self.rotate_status.is_running():
                    self.rotate_status.start()
            except Exception:
                pass

        try:
            if not self.update_snapshot.is_running():
                self.update_snapshot.start()
                if not self.rotate_status.is_running():
                    self.rotate_status.start()
        except Exception:
            pass

    def on_config_reload(self) -> None:
        # Restart daily loop if time changed
        try:
            if self.daily_report.is_running():
                self.daily_report.cancel()
        except Exception:
            pass
        if self._started and self._daily_summary_enabled():
            self._start_daily_report_loop()

    # --------------------
    # Config helpers
    # --------------------
    def _excluded_channels(self) -> set[int]:
        ids = self.bot.config.get_int_list("background", "exclude_channel_ids")
        return set(ids)

    def _status_rotation_enabled(self) -> bool:
        return bool(self.bot.config.get("background", "status_rotation", "enabled", default=False))

    def _status_rotation_interval(self) -> int:
        return int(self.bot.config.get("background", "status_rotation", "interval_seconds", default=600) or 600)

    def _status_list(self) -> List[dict]:
        items = self.bot.config.get("background", "status_rotation", "statuses", default=[]) or []
        if not isinstance(items, list):
            return []
        out: List[dict] = []
        for it in items:
            if isinstance(it, dict):
                t = str(it.get("type", "playing")).lower()
                txt = str(it.get("text", "")).strip()
                if txt:
                    out.append({"type": t, "text": txt})
            elif isinstance(it, str) and it.strip():
                out.append({"type": "playing", "text": it.strip()})
        return out

    async def _render_status_text(self, guild: discord.Guild, text: str) -> str:
        """Replace supported placeholders inside status rotation text."""
        class _SafeDict(dict):
            def __missing__(self, key):
                return "0"

        now = now_madrid()
        members = guild.member_count or len(getattr(guild, 'members', []) or [])
        # online can be approximate depending on intents/Discord caching
        try:
            online = sum(1 for m in guild.members if (not m.bot) and m.status != discord.Status.offline)
        except Exception:
            online = 0

        ws_iso = week_start_sunday(now).isoformat()
        week_msgs = 0
        week_top = ""
        open_tickets = 0
        today_msgs = int(getattr(self.stats, 'messages', 0) or 0)

        try:
            await self.bot.db.connect()
            row = await self.bot.db.fetchone(
                "SELECT COALESCE(SUM(count),0) AS total FROM activity_counts WHERE guild_id=? AND week_start=?",
                (guild.id, ws_iso)
            )
            week_msgs = int(row["total"]) if row else 0
            row2 = await self.bot.db.fetchone(
                "SELECT user_id FROM activity_counts WHERE guild_id=? AND week_start=? ORDER BY count DESC LIMIT 1",
                (guild.id, ws_iso)
            )
            if row2:
                uid = int(row2["user_id"])
                mem = guild.get_member(uid)
                week_top = mem.display_name if mem else str(uid)
            row3 = await self.bot.db.fetchone(
                "SELECT COUNT(*) AS c FROM tickets WHERE guild_id=? AND status IN ('open','closing_prompted')",
                (guild.id,)
            )
            open_tickets = int(row3["c"]) if row3 else 0
        except Exception:
            pass

        mapping = _SafeDict({
            "members": str(members),
            "online": str(online),
            "week_msgs": str(week_msgs),
            "week_top": str(week_top),
            "open_tickets": str(open_tickets),
            "today_msgs": str(today_msgs),
        })
        try:
            return str(text).format_map(mapping)
        except Exception:
            return str(text)

    def _daily_summary_enabled(self) -> bool:
        return bool(self.bot.config.get("background", "daily_summary", "enabled", default=False))

    def _daily_summary_channel_id(self) -> int:
        cid = self.bot.config.get_int("background", "daily_summary", "channel_id")
        if cid:
            return cid
        return self.bot.config.get_int("channels", "general_logging_channel_id") or 0

    def _daily_reset_after_report(self) -> bool:
        return bool(self.bot.config.get("background", "daily_summary", "reset_after_report", default=True))

    def _rollover_if_needed(self):
        today = _day_key()
        if today != self._current_day:
            self._current_day = today
            self.stats = DailyStats()
            self.voice_sessions.clear()

    # --------------------
    # Event listeners
    # --------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(message.guild, allowed):
            return
        if message.channel.id in self._excluded_channels():
            return

        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.messages += 1
        snapshot.by_channel[message.channel.id] = snapshot.by_channel.get(message.channel.id, 0) + 1
        snapshot.by_user[message.author.id] = snapshot.by_user.get(message.author.id, 0) + 1

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild is None or after.author.bot:
            return
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(after.guild, allowed):
            return
        if after.channel.id in self._excluded_channels():
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.edits += 1

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or (message.author and message.author.bot):
            return
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(message.guild, allowed):
            return
        if message.channel and message.channel.id in self._excluded_channels():
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.deletes += 1

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot or reaction.message.guild is None:
            return
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(reaction.message.guild, allowed):
            return
        if reaction.message.channel.id in self._excluded_channels():
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.reactions += 1

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if member.bot or not ensure_allowed_guild_id(member.guild, allowed):
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.joins += 1

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if member.bot or not ensure_allowed_guild_id(member.guild, allowed):
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.leaves += 1

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(guild, allowed):
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.bans += 1

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(guild, allowed):
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.unbans += 1

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if after.bot or not ensure_allowed_guild_id(after.guild, allowed):
            return
        self._rollover_if_needed()
        snapshot = self.stats
        if before.premium_since is None and after.premium_since is not None:
            snapshot.boosts += 1
        elif before.premium_since is not None and after.premium_since is None:
            snapshot.unboosts += 1

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if member.bot or not ensure_allowed_guild_id(member.guild, allowed):
            return
        self._rollover_if_needed()
        snapshot = self.stats
        now_ts = int(time.time())

        if before.channel and not after.channel:
            joined_ts = self.voice_sessions.pop(member.id, None)
            if joined_ts:
                minutes = int((now_ts - joined_ts) // 60)
                if minutes > 0:
                    snapshot.voice_minutes += minutes
        elif after.channel and not before.channel:
            self.voice_sessions[member.id] = now_ts

        # Peak voice users snapshot
        try:
            in_voice = sum(1 for m in member.guild.members if (not m.bot) and m.voice and m.voice.channel)
            snapshot.peak_voice_users = max(snapshot.peak_voice_users, in_voice)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_application_command_completion(self, ctx: discord.ApplicationContext):
        if ctx.guild is None:
            return
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(ctx.guild, allowed):
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.commands += 1
        name = getattr(ctx.command, "qualified_name", None) or getattr(ctx.command, "name", "unknown")
        snapshot.commands_by_name[str(name)] = snapshot.commands_by_name.get(str(name), 0) + 1

    @commands.Cog.listener()
    async def on_application_command_error(self, ctx: discord.ApplicationContext, error: Exception):
        if ctx.guild is None:
            return
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(ctx.guild, allowed):
            return
        self._rollover_if_needed()
        snapshot = self.stats
        snapshot.command_errors += 1

    # --------------------
    # Tasks
    # --------------------
    @tasks.loop(minutes=5)
    async def update_snapshot(self):
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed) if allowed else None
        if guild is None:
            return
        self._rollover_if_needed()
        snapshot = self.stats
        try:
            online = sum(1 for m in guild.members if (not m.bot) and m.status != discord.Status.offline)
            snapshot.peak_online_members = max(snapshot.peak_online_members, online)
        except Exception:
            pass

    @update_snapshot.before_loop
    async def _before_snapshot(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=10)
    async def rotate_status(self):
        if not self._status_rotation_enabled():
            return
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed) if allowed else None
        if guild is None:
            return
        interval = max(10, self._status_rotation_interval())
        now = time.time()
        if now - self._last_status_swap < interval:
            return
        self._last_status_swap = now

        statuses = self._status_list()
        if not statuses:
            return

        self._status_index = (self._status_index + 1) % len(statuses)
        item = statuses[self._status_index]
        t = item["type"]
        txt = item["text"]
        txt = await self._render_status_text(guild, txt)

        atype = discord.ActivityType.playing
        if t == "watching":
            atype = discord.ActivityType.watching
        elif t == "listening":
            atype = discord.ActivityType.listening
        elif t == "competing":
            atype = discord.ActivityType.competing

        try:
            await self.bot.change_presence(activity=discord.Activity(type=atype, name=txt))
        except Exception:
            pass

    @rotate_status.before_loop
    async def _before_rotate(self):
        await self.bot.wait_until_ready()

    def _start_daily_report_loop(self):
        hh, mm = _parse_hhmm(str(self.bot.config.get("background", "daily_summary", "time", default="00:00") or "00:00"))
        target = dtime(hour=hh, minute=mm, tzinfo=TZ)

        try:
            self.daily_report.change_interval(time=target)
        except Exception:
            pass

        try:
            if not self.daily_report.is_running():
                self.daily_report.start()
        except Exception:
            pass

    @tasks.loop(time=dtime(hour=0, minute=0, tzinfo=TZ))
    async def daily_report(self):
        allowed = self.bot.config.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed) if allowed else None
        if guild is None:
            return

        # Report the day we have been collecting, then roll over to today.
        today = _day_key()
        report_day = self._current_day
        snapshot = self.stats
        if today != self._current_day:
            # day changed since last event; report yesterday's snapshot
            report_day = self._current_day
            snapshot = self.stats
        day_key = report_day

        embed = discord.Embed(
            title="Daily Server Summary",
            description=f"Date (Madrid): **{day_key}**",
            timestamp=now_madrid(),
        )

        embed.add_field(name="Messages", value=str(snapshot.messages), inline=True)
        embed.add_field(name="Edits / Deletes", value=f"{snapshot.edits} / {snapshot.deletes}", inline=True)
        embed.add_field(name="Reactions", value=str(snapshot.reactions), inline=True)

        embed.add_field(name="Joins / Leaves", value=f"{snapshot.joins} / {snapshot.leaves}", inline=True)
        embed.add_field(name="Bans / Unbans", value=f"{snapshot.bans} / {snapshot.unbans}", inline=True)
        embed.add_field(name="Boosts / Unboosts", value=f"{snapshot.boosts} / {snapshot.unboosts}", inline=True)

        embed.add_field(name="Voice minutes", value=_fmt_minutes(snapshot.voice_minutes), inline=True)
        embed.add_field(name="Peak in voice", value=str(snapshot.peak_voice_users), inline=True)
        embed.add_field(name="Peak online", value=str(snapshot.peak_online_members), inline=True)

        embed.add_field(name="Commands", value=f"{snapshot.commands} (errors: {snapshot.command_errors})", inline=False)

        top_channels = sorted(snapshot.by_channel.items(), key=lambda kv: kv[1], reverse=True)[:5]
        if top_channels:
            embed.add_field(
                name="Top channels",
                value="\n".join([f"<#{cid}> — **{cnt}**" for cid, cnt in top_channels]),
                inline=False
            )

        top_users = sorted(snapshot.by_user.items(), key=lambda kv: kv[1], reverse=True)[:5]
        if top_users:
            embed.add_field(
                name="Top members",
                value="\n".join([f"<@{uid}> — **{cnt}**" for uid, cnt in top_users]),
                inline=False
            )

        top_cmds = sorted(snapshot.commands_by_name.items(), key=lambda kv: kv[1], reverse=True)[:5]
        if top_cmds:
            embed.add_field(
                name="Top commands",
                value="\n".join([f"`/{name}` — **{cnt}**" for name, cnt in top_cmds]),
                inline=False
            )

        ch_id = self._daily_summary_channel_id()
        channel = guild.get_channel(ch_id) if ch_id else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

        # persist
        try:
            payload = {
                "day_key": day_key,
                "messages": snapshot.messages,
                "edits": snapshot.edits,
                "deletes": snapshot.deletes,
                "reactions": snapshot.reactions,
                "joins": snapshot.joins,
                "leaves": snapshot.leaves,
                "bans": snapshot.bans,
                "unbans": snapshot.unbans,
                "boosts": snapshot.boosts,
                "unboosts": snapshot.unboosts,
                "voice_minutes": snapshot.voice_minutes,
                "peak_voice_users": snapshot.peak_voice_users,
                "peak_online_members": snapshot.peak_online_members,
                "commands": snapshot.commands,
                "command_errors": snapshot.command_errors,
                "by_channel": snapshot.by_channel,
                "by_user": snapshot.by_user,
                "commands_by_name": snapshot.commands_by_name,
            }
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO daily_stats(guild_id, day_key, payload_json, created_ts) VALUES(?,?,?,?)",
                (guild.id, day_key, json.dumps(payload, separators=(',', ':')), int(time.time()))
            )
        except Exception:
            pass

        if self._daily_reset_after_report():
            self._current_day = _day_key()
            self.stats = DailyStats()
            self.voice_sessions.clear()

    @daily_report.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_ready()

def setup(bot: discord.Bot):
    bot.add_cog(BackgroundCog(bot))
