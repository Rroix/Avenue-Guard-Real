from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta
from typing import Optional, List, Tuple

import discord
from discord.ext import commands

from utils.checks import ensure_allowed_guild_id
from utils.timeutils import now_madrid, week_start_sunday, next_sunday_midnight, TZ
from utils.views import TrackingDeclineConfirmView

REQUEST_DM_TEXT = (
    "Congratulations! You have been the most active member this week, so you have earned a **level request**. "
    "Please answer this message with the following format:\n"
    "> Level Name:\n"
    "> Level ID:\n"
    "> Creator:\n"
    "> Video (required for demons and platformers):\n"
    "> Notes (Optional):\n"
    "If you don’t want to claim this request, please answer with “I do not want this request”\n"
    "If you have any problems, please contact any of the Admins/Owners.\n"
    "Thanks for bringing so much dedication to our community!"
)

class TrackingCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._started = False
        self._weekly_task: Optional[asyncio.Task] = None
        self._timeout_task: Optional[asyncio.Task] = None

    async def start_background(self):
        if self._started:
            return
        self._started = True
        # Ensure schema for weekly run tracking exists
        try:
            await self.bot.db.connect()
            await self.bot.db.execute(
                """CREATE TABLE IF NOT EXISTS weekly_runs(
                    guild_id INTEGER NOT NULL,
                    week_start TEXT NOT NULL,
                    ran_ts INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, week_start)
                );"""
            )
        except Exception:
            pass
        self._weekly_task = asyncio.create_task(self._weekly_loop())
        self._timeout_task = asyncio.create_task(self._timeout_loop())

    def on_config_reload(self) -> None:
        # no cached config
        pass

    async def user_in_weekly_process(self, user_id: int) -> bool:
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if not allowed_guild_id:
            return False
        # active session any week_start
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM weekly_sessions WHERE guild_id=? AND user_id=? AND active=1 LIMIT 1",
            (allowed_guild_id, user_id)
        )
        return row is not None

def _cfg_int_list(self, section: str, key: str) -> list[int]:
    """Config int-list helper compatible with older Config implementations."""
    cfg = self.bot.config
    try:
        vals = cfg.get_int_list(section, key)
    except TypeError:
        # some Config variants accept only (section, key)
        vals = cfg.get_int_list(section, key)
    except Exception:
        vals = None
    if not vals:
        return []
    out: list[int] = []
    for v in vals:
        try:
            out.append(int(v))
        except Exception:
            continue
    return out

async def _log_weekly(self, guild: discord.Guild, week_start: str, user_id: int, event: str, detail: str = "") -> None:
    """Best-effort weekly logging: DB (if table exists) + optional log channel."""
    cfg = self.bot.config
    # DB log (optional)
    try:
        await self.bot.db.execute(
            "INSERT INTO weekly_dm_log(guild_id, week_start, user_id, event, detail, ts) VALUES(?,?,?,?,?,?)",
            (guild.id, week_start, int(user_id), str(event), str(detail)[:500], int(time.time()))
        )
    except Exception:
        pass

    # channel log (optional)
    try:
        log_channel_id = 0
        try:
            log_channel_id = int(cfg.get("tracking", "log_channel_id", default=0) or 0)
        except Exception:
            # older Config may not support default kwarg
            try:
                log_channel_id = int(cfg.get("tracking", "log_channel_id") or 0)
            except Exception:
                log_channel_id = 0

        if not log_channel_id:
            # fall back to general logging if configured
            try:
                log_channel_id = int(cfg.get("channels", "general_logging_channel_id") or 0)
            except Exception:
                log_channel_id = 0

        ch = guild.get_channel(log_channel_id) if log_channel_id else None
        if isinstance(ch, discord.TextChannel):
            emb = discord.Embed(title="Weekly request log", description=f"**{event}**\n{detail}".strip())
            emb.add_field(name="Week", value=week_start, inline=True)
            if user_id:
                emb.add_field(name="User", value=f"<@{user_id}> ({user_id})", inline=True)
            await ch.send(embed=emb)
    except Exception:
        pass

    # --- Activity counting ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")

        # DM handling for weekly request process
        if message.guild is None:
            await self._handle_dm(message)
            return

        if not ensure_allowed_guild_id(message.guild, allowed_guild_id):
            return

        # count activity with exclusions + per-user cooldown
        excluded_role_ids = set(self._cfg_int_list("roles", "excluded_tracking_role_id"))
        if excluded_role_ids:
            m = message.guild.get_member(message.author.id)
            if m and any(r.id in excluded_role_ids for r in m.roles):
                return

        excluded_channels = set(
            self._cfg_int_list('channels', 'excluded_tracking_channel_ids')
            + self._cfg_int_list('channels', 'bot_commands_channel_ids')
        )
        if message.channel.id in excluded_channels:
            return

        cd = int(cfg.get("tracking", "count_cooldown_seconds", default=10) or 10)
        now = int(time.time())
        db = self.bot.db

        row = await db.fetchone(
            "SELECT last_counted_ts FROM activity_last_counted WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id)
        )
        if row and now - int(row["last_counted_ts"]) < cd:
            return

        ws = week_start_sunday(now_madrid())
        ws_iso = ws.isoformat()
        await db.execute(
            "INSERT INTO activity_counts(guild_id,user_id,week_start,count) VALUES(?,?,?,1) "
            "ON CONFLICT(guild_id,user_id,week_start) DO UPDATE SET count=count+1",
            (message.guild.id, message.author.id, ws_iso)
        )
        await db.execute(
            "INSERT INTO activity_last_counted(guild_id,user_id,last_counted_ts) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET last_counted_ts=excluded.last_counted_ts",
            (message.guild.id, message.author.id, now)
        )

    async def _handle_dm(self, message: discord.Message):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
        if guild is None:
            return
        if guild.get_member(message.author.id) is None:
            return

        # find active session for this user (latest week_start)
        rows = await self.bot.db.fetchall(
            "SELECT week_start, stage, expires_ts FROM weekly_sessions WHERE guild_id=? AND user_id=? AND active=1 ORDER BY week_start DESC LIMIT 1",
            (allowed_guild_id, message.author.id)
        )
        if not rows:
            return

        sess = rows[0]
        expires_ts = int(sess["expires_ts"])
        if int(time.time()) > expires_ts:
            # expired: timeout handler will take it
            return

        content = (message.content or "").strip()
        if content.casefold() == "i do not want this request".casefold():
            # send confirm embed with persistent view
            embed = discord.Embed(title="Are you sure?", description="If you confirm, the request will be offered to the next eligible member")
            try:
                await message.channel.send(embed=embed, view=TrackingDeclineConfirmView())
                await self.bot.db.execute(
                    "UPDATE weekly_sessions SET stage='confirm_decline' WHERE guild_id=? AND user_id=? AND week_start=?",
                    (allowed_guild_id, message.author.id, sess["week_start"])
                )
            except Exception:
                pass
            return

        # if in confirm stage, ignore typed messages until button press
        if sess["stage"] == "confirm_decline":
            return

        # forgiving parser: contains name, creator, id
        low = content.casefold()
        if ("name" in low) and ("creator" in low) and ("id" in low):
            await self._record_request(guild, message.author.id, sess["week_start"], content)
            return

        # not valid: nudge
        try:
            await message.channel.send("Please send your request using the format provided (Name, Creator, and ID)")
        except Exception:
            pass

    async def _record_request(self, guild: discord.Guild, user_id: int, week_start_iso: str, content: str):
        cfg = self.bot.config
        weekly_channel_id = cfg.get_int("channels", "weekly_request_channel_ID")
        channel = guild.get_channel(weekly_channel_id) if weekly_channel_id else None

        # rank info
        row = await self.bot.db.fetchone(
            "SELECT rank FROM weekly_claims WHERE guild_id=? AND week_start=? AND user_id=?",
            (guild.id, week_start_iso, user_id)
        )
        rank = int(row["rank"]) if row else None

        # Send to weekly request channel
        if isinstance(channel, discord.TextChannel):
            week_start_dt = discord.utils.parse_time(week_start_iso)
            # parse_time may return None for tz-aware; fallback:
            ws = week_start_iso
            embed = discord.Embed(title="Weekly Request Submitted")
            embed.add_field(name="User", value=f"<@{user_id}> ({user_id})", inline=False)
            if rank:
                embed.add_field(name="Rank", value=f"#{rank}", inline=True)
            embed.add_field(name="Week start", value=week_start_iso, inline=False)
            embed.add_field(name="Content", value=content[:1024], inline=False)
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

        # mark claimed & close session
        now = int(time.time())
        await self.bot.db.execute(
            "UPDATE weekly_claims SET status='claimed' WHERE guild_id=? AND week_start=? AND user_id=?",
            (guild.id, week_start_iso, user_id)
        )
        await self.bot.db.execute(
            "UPDATE weekly_sessions SET active=0 WHERE guild_id=? AND week_start=? AND user_id=?",
            (guild.id, week_start_iso, user_id)
        )
        await self._log_weekly(guild, week_start_iso, user_id, "request_recorded", f"rank={rank if rank is not None else 'unknown'}")
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send("Thanks! Your request has been recorded.")
        except Exception:
            pass

    # --- Decline confirm button handler ---
    async def handle_decline_confirm(self, interaction: discord.Interaction, confirmed: bool):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
        if guild is None:
            try:
                await interaction.response.send_message("Guild not found.", ephemeral=True)
            except Exception:
                pass
            return

        # ensure the interaction user actually has an active confirm session
        row = await self.bot.db.fetchone(
            "SELECT week_start FROM weekly_sessions WHERE guild_id=? AND user_id=? AND active=1 AND stage='confirm_decline' ORDER BY week_start DESC LIMIT 1",
            (guild.id, interaction.user.id)
        )
        if not row:
            try:
                await interaction.response.send_message("No pending confirmation found.", ephemeral=True)
            except Exception:
                pass
            return

        week_start_iso = row["week_start"]

        if not confirmed:
            try:
                await interaction.response.send_message("Request resumed, please send your request with the format!", ephemeral=True)
            except Exception:
                pass
            await self.bot.db.execute(
                "UPDATE weekly_sessions SET stage='awaiting_request' WHERE guild_id=? AND user_id=? AND week_start=?",
                (guild.id, interaction.user.id, week_start_iso)
            )
            return

        # confirmed decline
        await self.bot.db.execute(
            "UPDATE weekly_claims SET status='declined' WHERE guild_id=? AND week_start=? AND user_id=?",
            (guild.id, week_start_iso, interaction.user.id)
        )
        await self._log_weekly(guild, week_start_iso, interaction.user.id, "declined", "User confirmed decline")
        await self.bot.db.execute(
            "UPDATE weekly_sessions SET active=0 WHERE guild_id=? AND week_start=? AND user_id=?",
            (guild.id, week_start_iso, interaction.user.id)
        )
        try:
            await interaction.response.send_message("Confirmed. Offering the request to the next eligible member.", ephemeral=True)
        except Exception:
            pass
        await self._contact_next_eligible(guild, week_start_iso)

    # --- Weekly loops ---
    async def _weekly_loop(self):
        # Robust scheduler: checks frequently so restarts don't skip Sunday runs.
        await self.bot.wait_until_ready()
        while True:
            try:
                await asyncio.sleep(60)
                now = now_madrid()

                allowed_guild_id = self.bot.config.get_int("guild", "allowed_guild_id")
                guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
                if guild is None:
                    continue

                # This Sunday's 00:00 (start of current week)
                this_sunday = week_start_sunday(now)
                this_sunday_iso = this_sunday.isoformat()

                # If we haven't processed this Sunday's rollover yet, do it now.
                row = await self.bot.db.fetchone(
                    "SELECT ran_ts FROM weekly_runs WHERE guild_id=? AND week_start=?",
                    (guild.id, this_sunday_iso)
                )
                if row is not None:
                    continue

                # Run weekly job for the week that ended right before this Sunday (previous week)
                prev_week_start = week_start_sunday(this_sunday - timedelta(seconds=1)).isoformat()
                await self.run_weekly_job(prev_week_start)

                # Mark processed for this Sunday
                await self.bot.db.execute(
                    "INSERT OR REPLACE INTO weekly_runs(guild_id, week_start, ran_ts) VALUES(?,?,?)",
                    (guild.id, this_sunday_iso, int(time.time()))
                )

                # Reset the old week's activity counts so the new week starts fresh
                await self.bot.db.execute(
                    "DELETE FROM activity_counts WHERE guild_id=? AND week_start=?",
                    (guild.id, prev_week_start)
                )
            except asyncio.CancelledError:
                return
            except Exception:
                # don't kill the loop; retry next minute
                continue

    async def _timeout_loop(self):
        while True:
            try:
                await asyncio.sleep(600)  # every 10 minutes
                await self._process_timeouts()
                await self._process_reminders()
            except asyncio.CancelledError:
                return
            except Exception:
                continue

    async def _process_timeouts(self):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
        if guild is None:
            return
        now_ts = int(time.time())

        rows = await self.bot.db.fetchall(
            "SELECT week_start, user_id FROM weekly_sessions WHERE guild_id=? AND active=1 AND expires_ts<=?",
            (guild.id, now_ts)
        )
        for r in rows:
            week_start_iso = r["week_start"]
            user_id = int(r["user_id"])

            # DM timed out
            try:
                user = await self.bot.fetch_user(user_id)
                await user.send("Request timed out")
                await self._log_weekly(guild, week_start_iso, user_id, "timeout_dm_sent", "")
            except Exception:
                pass

            await self.bot.db.execute(
                "UPDATE weekly_claims SET status='timed_out' WHERE guild_id=? AND week_start=? AND user_id=?",
                (guild.id, week_start_iso, user_id)
            )
            await self._log_weekly(guild, week_start_iso, user_id, "timed_out", "No reply before deadline")
            await self.bot.db.execute(
                "UPDATE weekly_sessions SET active=0 WHERE guild_id=? AND week_start=? AND user_id=?",
                (guild.id, week_start_iso, user_id)
            )

            await self._contact_next_eligible(guild, week_start_iso)

    # --- Weekly job execution ---
    async def run_weekly_job(self, week_start_iso: str):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
        if guild is None:
            return

        top_limit = int(cfg.get("tracking", "top_limit", default=20) or 20)
        winners_to_dm = int(cfg.get("tracking", "winners_to_dm", default=1) or 1)
        timeout_h = int(cfg.get("tracking", "dm_timeout_hours", default=48) or 48)

        excluded_role_ids = set(self._cfg_int_list("roles", "excluded_tracking_role_id"))
        await self._log_weekly(guild, week_start_iso, 0, "weekly_job_start", f"top_limit={top_limit} winners_to_dm={winners_to_dm} timeout_h={timeout_h}")

        # top users
        rows = await self.bot.db.fetchall(
            "SELECT user_id, count FROM activity_counts WHERE guild_id=? AND week_start=? ORDER BY count DESC LIMIT ?",
            (guild.id, week_start_iso, top_limit)
        )
        ranked: List[int] = []
        for r in rows:
            uid = int(r["user_id"])
            member = guild.get_member(uid)
            if member is None:
                continue
            if excluded_role_ids and any(role.id in excluded_role_ids for role in member.roles):
                continue
            ranked.append(uid)

        # store a ranked list indirectly via weekly_claims ranks; DM first N that are not contacted yet
        contacted = 0
        for idx, uid in enumerate(ranked, start=1):
            # ensure claim row exists with rank even if not contacted (optional). We'll insert on contact only.
            if contacted >= winners_to_dm:
                break
            ok = await self._contact_user_for_week(guild, week_start_iso, uid, rank=idx, timeout_hours=timeout_h)
            if ok:
                contacted += 1

        await self._log_weekly(guild, week_start_iso, 0, "weekly_job_done", f"contacted={contacted} eligible_ranked={len(ranked)}")




    async def _process_reminders(self):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
        if guild is None:
            return

        reminder_after_h = int(cfg.get("tracking", "reminder_after_hours", default=24) or 24)
        repeat_h = int(cfg.get("tracking", "reminder_repeat_hours", default=0) or 0)

        now_ts = int(time.time())
        rows = await self.bot.db.fetchall(
            "SELECT s.week_start AS week_start, s.user_id AS user_id, s.expires_ts AS expires_ts, c.contacted_ts AS contacted_ts "
            "FROM weekly_sessions s "
            "JOIN weekly_claims c ON c.guild_id=s.guild_id AND c.week_start=s.week_start AND c.user_id=s.user_id "
            "WHERE s.guild_id=? AND s.active=1 AND s.stage='awaiting_request' AND c.status='pending'",
            (guild.id,)
        )

        for r in rows:
            week_start_iso = r["week_start"]
            user_id = int(r["user_id"])
            expires_ts = int(r["expires_ts"])
            contacted_ts = int(r["contacted_ts"])

            if expires_ts <= now_ts:
                continue

            if now_ts < contacted_ts + reminder_after_h * 3600:
                continue

            prev = await self.bot.db.fetchone(
                "SELECT reminded_ts FROM weekly_reminders WHERE guild_id=? AND week_start=? AND user_id=?",
                (guild.id, week_start_iso, user_id)
            )
            if prev is not None:
                if repeat_h <= 0:
                    continue
                if now_ts < int(prev["reminded_ts"]) + repeat_h * 3600:
                    continue

            try:
                user = await self.bot.fetch_user(user_id)
                await user.send(self._build_reminder_text(expires_ts))
                await self.bot.db.execute(
                    "INSERT OR REPLACE INTO weekly_reminders(guild_id,week_start,user_id,reminded_ts) VALUES(?,?,?,?)",
                    (guild.id, week_start_iso, user_id, now_ts)
                )
                await self._log_weekly(guild, week_start_iso, user_id, "reminder_sent", f"expires={self._format_deadline(expires_ts)}")
            except Exception as e:
                await self._log_weekly(guild, week_start_iso, user_id, "reminder_failed", type(e).__name__)

    async def _contact_user_for_week(self, guild: discord.Guild, week_start_iso: str, user_id: int, rank: int, timeout_hours: int) -> bool:
        # don't contact if already contacted this week
        row = await self.bot.db.fetchone(
            "SELECT status FROM weekly_claims WHERE guild_id=? AND week_start=? AND user_id=?",
            (guild.id, week_start_iso, user_id)
        )
        if row is not None:
            try:
                await self._log_weekly(guild, week_start_iso, user_id, "skipped_already_contacted", f"status={row['status']}")
            except Exception:
                pass
            return False

        now_ts = int(time.time())
        expires = now_ts + int(timeout_hours) * 3600

        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(self._build_request_dm_text(int(timeout_hours), expires))
            await self._log_weekly(guild, week_start_iso, user_id, "dm_sent", f"rank={rank} timeout_hours={timeout_hours}")
        except Exception as e:
            # DM closed -> record + log
            await self.bot.db.execute(
                "INSERT INTO weekly_claims(guild_id,week_start,user_id,rank,status,contacted_ts) VALUES(?,?,?,?,?,?)",
                (guild.id, week_start_iso, user_id, rank, "dm_closed", now_ts)
            )
            try:
                await self._log_weekly(guild, week_start_iso, user_id, "dm_failed", type(e).__name__)
            except Exception:
                pass

            log_ch_id = self.bot.config.get_int("channels", "dm_fail_log_channel_id")
            log_ch = guild.get_channel(log_ch_id) if log_ch_id else None
            if isinstance(log_ch, discord.TextChannel):
                embed = discord.Embed(title="DM Failed", description=f"Could not DM <@{user_id}> for weekly request.")
                embed.add_field(name="User ID", value=str(user_id), inline=True)
                embed.add_field(name="Week start", value=week_start_iso, inline=False)
                try:
                    await log_ch.send(embed=embed)
                except Exception:
                    pass
            return False

        # mark contacted + create session
        await self.bot.db.execute(
            "INSERT INTO weekly_claims(guild_id,week_start,user_id,rank,status,contacted_ts) VALUES(?,?,?,?,?,?)",
            (guild.id, week_start_iso, user_id, rank, "pending", now_ts)
        )
        await self.bot.db.execute(
            "INSERT INTO weekly_sessions(guild_id,week_start,user_id,stage,expires_ts,active) VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(guild_id,week_start,user_id) DO UPDATE SET stage='awaiting_request', expires_ts=excluded.expires_ts, active=1",
            (guild.id, week_start_iso, user_id, "awaiting_request", expires)
        )
        return True

    async def _contact_next_eligible(self, guild: discord.Guild, week_start_iso: str):
        # get ranked list again (top_limit) and contact first not contacted
        cfg = self.bot.config
        top_limit = int(cfg.get("tracking", "top_limit", default=20) or 20)
        timeout_h = int(cfg.get("tracking", "dm_timeout_hours", default=48) or 48)
        excluded_role_ids = set(self._cfg_int_list("roles", "excluded_tracking_role_id"))
        await self._log_weekly(guild, week_start_iso, 0, "weekly_job_start", f"top_limit={top_limit} winners_to_dm={winners_to_dm} timeout_h={timeout_h}")

        rows = await self.bot.db.fetchall(
            "SELECT user_id, count FROM activity_counts WHERE guild_id=? AND week_start=? ORDER BY count DESC LIMIT ?",
            (guild.id, week_start_iso, top_limit)
        )

        skipped_missing = 0
        skipped_excluded = 0
        skipped_existing = 0

        for idx, r in enumerate(rows, start=1):
            uid = int(r["user_id"])
            member = guild.get_member(uid)
            if member is None:
                skipped_missing += 1
                continue
            if excluded_role_ids and any(role.id in excluded_role_ids for role in member.roles):
                skipped_excluded += 1
                continue

            existing = await self.bot.db.fetchone(
                "SELECT 1 FROM weekly_claims WHERE guild_id=? AND week_start=? AND user_id=?",
                (guild.id, week_start_iso, uid)
            )
            if existing:
                skipped_existing += 1
                continue

            await self._log_weekly(
                guild,
                week_start_iso,
                uid,
                "offered_next",
                f"rank={idx} skipped_missing={skipped_missing} skipped_excluded={skipped_excluded} skipped_existing={skipped_existing}"
            )
            await self._contact_user_for_week(guild, week_start_iso, uid, rank=idx, timeout_hours=timeout_h)
            return

        await self._log_weekly(
            guild,
            week_start_iso,
            0,
            "no_eligible_member",
            f"top_limit={top_limit} skipped_missing={skipped_missing} skipped_excluded={skipped_excluded} skipped_existing={skipped_existing}"
        )
    # helper for /tracking top
    async def get_top(self, guild_id: int, week_start_iso: str, limit: int = 20) -> List[Tuple[int,int]]:
        rows = await self.bot.db.fetchall(
            "SELECT user_id, count FROM activity_counts WHERE guild_id=? AND week_start=? ORDER BY count DESC LIMIT ?",
            (guild_id, week_start_iso, limit)
        )
        return [(int(r["user_id"]), int(r["count"])) for r in rows]



    async def get_member_stats(self, guild: discord.Guild, week_start_iso: str, user_id: int) -> tuple[int, Optional[int], int]:
        """Return (count, rank among eligible, eligible_total). Rank is 1-based, or None if not ranked/eligible."""
        cfg = self.bot.config
        excluded_role_ids = set(self._cfg_int_list("roles", "excluded_tracking_role_id"))

        # fast count lookup
        row = await self.bot.db.fetchone(
            "SELECT count FROM activity_counts WHERE guild_id=? AND week_start=? AND user_id=?",
            (guild.id, week_start_iso, user_id)
        )
        count = int(row["count"]) if row else 0

        member = guild.get_member(user_id)
        if member is None or member.bot:
            return count, None, 0
        if excluded_role_ids and any(r.id in excluded_role_ids for r in member.roles):
            return count, None, 0

        # compute rank among eligible
        rows = await self.bot.db.fetchall(
            "SELECT user_id, count FROM activity_counts WHERE guild_id=? AND week_start=? ORDER BY count DESC",
            (guild.id, week_start_iso)
        )
        rank = None
        eligible_total = 0
        for r in rows:
            uid = int(r["user_id"])
            m = guild.get_member(uid)
            if m is None or m.bot:
                continue
            if excluded_role_ids and any(role.id in excluded_role_ids for role in m.roles):
                continue
            eligible_total += 1
            if uid == user_id:
                rank = eligible_total
                break

        return count, rank, eligible_total

    async def force_dm_for_user(self, guild: discord.Guild, week_start_iso: str, user_id: int, timeout_hours: Optional[int] = None) -> tuple[bool, str]:
        """Force-send the weekly request DM to a user (admin tool). Returns (ok, message)."""
        cfg = self.bot.config
        excluded_role_ids = set(self._cfg_int_list("roles", "excluded_tracking_role_id"))
        timeout_h = int(timeout_hours or cfg.get("tracking", "dm_timeout_hours", default=48) or 48)

        member = guild.get_member(user_id)
        if member is None:
            return False, "User is not in the server."
        if member.bot:
            return False, "Bots cannot receive weekly requests."
        if excluded_role_ids and any(r.id in excluded_role_ids for r in member.roles):
            return False, "That user is excluded from tracking (blacklisted role)."

        existing = await self.bot.db.fetchone(
            "SELECT status FROM weekly_claims WHERE guild_id=? AND week_start=? AND user_id=?",
            (guild.id, week_start_iso, user_id)
        )
        if existing is not None:
            status = str(existing["status"])
            if status != "dm_closed":
                return False, f"Cannot force DM: user already has status '{status}' for this week."
            # allow re-try: clear old record + session
            await self.bot.db.execute(
                "DELETE FROM weekly_claims WHERE guild_id=? AND week_start=? AND user_id=?",
                (guild.id, week_start_iso, user_id)
            )
            await self.bot.db.execute(
                "DELETE FROM weekly_sessions WHERE guild_id=? AND week_start=? AND user_id=?",
                (guild.id, week_start_iso, user_id)
            )

        # estimate rank among eligible (best-effort)
        rows = await self.bot.db.fetchall(
            "SELECT user_id, count FROM activity_counts WHERE guild_id=? AND week_start=? ORDER BY count DESC",
            (guild.id, week_start_iso)
        )
        rank = 1
        for r in rows:
            uid = int(r["user_id"])
            m = guild.get_member(uid)
            if m is None or m.bot:
                continue
            if excluded_role_ids and any(role.id in excluded_role_ids for role in m.roles):
                continue
            if uid == user_id:
                break
            rank += 1

        ok = await self._contact_user_for_week(guild, week_start_iso, user_id, rank=rank, timeout_hours=timeout_h)
        if ok:
            return True, f"Weekly request DM sent (rank {rank})."
        return False, "Could not DM that user (DMs likely closed)."

    async def reset_current_week(self, guild_id: int) -> None:
        # clear current week's counts and last-counted
        ws = week_start_sunday(now_madrid()).isoformat()
        await self.bot.db.execute("DELETE FROM activity_counts WHERE guild_id=? AND week_start=?", (guild_id, ws))
        await self.bot.db.execute("DELETE FROM activity_last_counted WHERE guild_id=?", (guild_id,))
        # Note: does not touch previous weeks or sessions/claims

def setup(bot: discord.Bot):
    bot.add_cog(TrackingCog(bot))
