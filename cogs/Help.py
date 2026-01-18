from __future__ import annotations

import asyncio
import io
import json
import re
import time
from typing import Optional, Dict, Any, Tuple

import discord
from discord.ext import commands

from utils.checks import ensure_allowed_guild_id, is_mod
from utils.views import HelpMenuView, HelpModConfirmView, TicketClosePromptView, TranscriptRequestView
from utils.transcript import build_text_transcript
from utils.timeutils import now_madrid, week_start_sunday


pinged_role_for_tickets = "<@&1462403598028640296>"

def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


class HelpCog(commands.Cog):
    """DM help system + ticket system helpers + transcript requests."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._ticket_scan_task: Optional[asyncio.Task] = None
        self._started = False

    async def start_background(self):
        if self._started:
            return
        self._started = True
        self._ticket_scan_task = asyncio.create_task(self._ticket_scan_loop())

    def on_config_reload(self) -> None:
        pass

    # -----------------------------
    # Ticket inactivity scanning
    # -----------------------------
    async def _ticket_scan_loop(self):
        while True:
            try:
                await asyncio.sleep(600)  # every 10 minutes
                await self._scan_tickets()
            except asyncio.CancelledError:
                return
            except Exception:
                continue

    async def _scan_tickets(self):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
        if guild is None:
            return

        inactivity_hours = int(cfg.get("tickets", "ticket_inactivity_hours", default=24) or 24)
        cutoff = int(time.time()) - inactivity_hours * 3600

        rows = await self.bot.db.fetchall(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND status='open' AND last_user_activity_ts<=?",
            (guild.id, cutoff),
        )
        for r in rows:
            channel_id = int(r["channel_id"])
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send("Do you want to close the ticket?", view=TicketClosePromptView())
                await self.bot.db.execute("UPDATE tickets SET status='closing_prompted' WHERE channel_id=?", (channel_id,))
            except Exception:
                continue

    # -----------------------------
    # Listener: ticket activity + DM help
    # -----------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None

        # Ticket activity in guild
        if message.guild is not None and ensure_allowed_guild_id(message.guild, allowed_guild_id):
            row = await self.bot.db.fetchone("SELECT status FROM tickets WHERE channel_id=?", (message.channel.id,))
            if row and row["status"] in ("open", "closing_prompted"):
                await self.bot.db.execute(
                    "UPDATE tickets SET last_user_activity_ts=?, status='open' WHERE channel_id=?",
                    (int(time.time()), message.channel.id),
                )
            return

        # DM help
        if message.guild is None:
            if guild is None:
                return
            member = guild.get_member(message.author.id)
            if member is None:
                return  # ignore DMs from non-members

            tracking = self.bot.get_cog("TrackingCog")
            if tracking and await tracking.user_in_weekly_process(message.author.id):
                return

            if await self._handle_help_session_message(guild, message):
                return

            embed = discord.Embed(
                title="Help Menu",
                description="Hello! What do you need help with?\nSelect an option below",
            )
            try:
                await message.channel.send(embed=embed, view=HelpMenuView())
            except Exception:
                pass

    # -----------------------------
    # Cooldowns (help actions)
    # -----------------------------
    async def _remaining_help_cooldown(self, guild_id: int, user_id: int, action: str, cooldown_seconds: int) -> int:
        row = await self.bot.db.fetchone(
            "SELECT last_used_ts FROM help_cooldowns WHERE guild_id=? AND user_id=? AND action=?",
            (guild_id, user_id, action),
        )
        if not row:
            return 0
        last_ts = int(row["last_used_ts"])
        return max(0, cooldown_seconds - (int(time.time()) - last_ts))

    async def _touch_help_cooldown(self, guild_id: int, user_id: int, action: str) -> None:
        await self.bot.db.execute(
            "INSERT INTO help_cooldowns(guild_id,user_id,action,last_used_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,user_id,action) DO UPDATE SET last_used_ts=excluded.last_used_ts",
            (guild_id, user_id, action, int(time.time())),
        )

    # -----------------------------
    # Menu handler (DM only)
    # -----------------------------
    async def handle_help_selection(self, interaction: discord.Interaction, value: str):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
        if guild is None:
            return await interaction.response.send_message("Guild not found.")

        if interaction.guild is not None:
            return await interaction.response.send_message("Please DM me to use the help menu.")

        if value == "faq":
            return await self._send_faq(interaction)

        if value == "weekly_status":
            return await self._send_weekly_status(interaction, guild)

        # Cooldowns
        cds = {
            "appeal": 48 * 3600,
            "report_user": 24 * 3600,
            "bot_issue": 6 * 3600,
            "transcript": 8 * 3600,
        }

        if value == "appeal":
            remaining = await self._remaining_help_cooldown(guild.id, interaction.user.id, "appeal", cds["appeal"])
            if remaining:
                embed = discord.Embed(
                    title="On cooldown",
                    description=f"You're on cooldown for **Appeal punishment**.\nTry again in **{_format_duration(remaining)}**.",
                )
                return await interaction.response.send_message(embed=embed)

            await self._touch_help_cooldown(guild.id, interaction.user.id, "appeal")
            await self._start_help_session(interaction.user.id, guild.id, "appeal_punishment", {})
            embed = discord.Embed(
                title="Appeal punishment",
                description="Please fill [this form](https://forms.gle/1fgqKtyo6okiQzjBA) out\n\nType **cancel** to stop.",
            )
            return await interaction.response.send_message(embed=embed)

        if value == "report":
            remaining = await self._remaining_help_cooldown(guild.id, interaction.user.id, "report_user", cds["report_user"])
            if remaining:
                embed = discord.Embed(
                    title="On cooldown",
                    description=f"You're on cooldown for **Report a user/message**.\nTry again in **{_format_duration(remaining)}**.",
                )
                return await interaction.response.send_message(embed=embed)

            await self._touch_help_cooldown(guild.id, interaction.user.id, "report_user")
            warning = bool(cfg.get("help", "report_warning_enabled", default=True))
            text = "Please send the message link (preferred) OR user ID along with the reason for your report with evidence.\n\nType **cancel** to stop."
            if warning:
                text = "⚠️ False reports will lead to punishment.\n\n" + text

            await self._start_help_session(interaction.user.id, guild.id, "report_details", {})
            embed = discord.Embed(title="Report a user", description=text)
            return await interaction.response.send_message(embed=embed)

        if value == "bot_issue":
            remaining = await self._remaining_help_cooldown(guild.id, interaction.user.id, "bot_issue", cds["bot_issue"])
            if remaining:
                embed = discord.Embed(
                    title="On cooldown",
                    description=f"You're on cooldown for **Report a bot issue**.\nTry again in **{_format_duration(remaining)}**.",
                )
                return await interaction.response.send_message(embed=embed)

            await self._touch_help_cooldown(guild.id, interaction.user.id, "bot_issue")
            await self._start_help_session(interaction.user.id, guild.id, "bot_issue_details", {})
            embed = discord.Embed(
                title="Report a bot issue",
                description="Please describe the bot issue/bug, and include screenshots or steps to reproduce/explanation in a single message.\n\nType **cancel** to stop.",
            )
            return await interaction.response.send_message(embed=embed)

        if value == "transcript":
            remaining = await self._remaining_help_cooldown(guild.id, interaction.user.id, "transcript", cds["transcript"])
            if remaining:
                embed = discord.Embed(
                    title="On cooldown",
                    description=f"You're on cooldown for **Request transcript**.\nTry again in **{_format_duration(remaining)}**.",
                )
                return await interaction.response.send_message(embed=embed)

            await self._touch_help_cooldown(guild.id, interaction.user.id, "transcript")
            await self._start_help_session(interaction.user.id, guild.id, "transcript_ticket", {})
            embed = discord.Embed(
                title="Request transcript",
                description="Send your **ticket channel** (mention like <#123> or ID), or your **Ticket ID** (example: `T21`).\n\nType **cancel** to stop.",
            )
            return await interaction.response.send_message(embed=embed)

        if value == "mod_contact":
            return await interaction.response.send_message(
                "Do you want to contact staff? Please only use this if you **need staff**.",
                view=HelpModConfirmView(),
            )

        return await interaction.response.send_message("That option isn't available yet.")

    async def _send_faq(self, interaction: discord.Interaction):
        cfg = self.bot.config
        faq = cfg.get("help", "faq", default={}) or {}
        title = str(faq.get("title", "FAQ") or "FAQ")
        entries = faq.get("entries", [])
        if not isinstance(entries, list):
            entries = []
        desc = "\n".join([f"• {str(x)}" for x in entries][:20]) or "Not available right now, sorry"
        embed = discord.Embed(title=title, description=desc)
        await interaction.response.send_message(embed=embed)

    async def _send_weekly_status(self, interaction: discord.Interaction, guild: discord.Guild):
        cfg = self.bot.config
        excluded_role_id = cfg.get_int("roles", "excluded_tracking_role_id")
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await interaction.response.send_message("You must be in the server...")

        if excluded_role_id and any(r.id == excluded_role_id for r in member.roles):
            return await interaction.response.send_message("You are excluded from weekly tracking.")

        ws = week_start_sunday(now_madrid()).isoformat()
        row = await self.bot.db.fetchone(
            "SELECT count FROM activity_counts WHERE guild_id=? AND user_id=? AND week_start=?",
            (guild.id, member.id, ws),
        )
        count = int(row["count"]) if row else 0

        rows = await self.bot.db.fetchall(
            "SELECT user_id, count FROM activity_counts WHERE guild_id=? AND week_start=? ORDER BY count DESC LIMIT 20",
            (guild.id, ws),
        )
        rank = None
        for i, r in enumerate(rows, start=1):
            if int(r["user_id"]) == member.id:
                rank = i
                break

        embed = discord.Embed(title="Weekly status")
        embed.add_field(name="Messages counted", value=str(count), inline=True)
        embed.add_field(name="Top 20 rank", value=(f"#{rank}" if rank else "Not in top 20"), inline=True)
        await interaction.response.send_message(embed=embed)

    # -----------------------------
    # Help sessions
    # -----------------------------
    async def _start_help_session(self, user_id: int, guild_id: int, stage: str, data: Dict[str, Any]):
        await self.bot.db.execute(
            "INSERT INTO help_sessions(guild_id,user_id,stage,created_ts,data_json) VALUES(?,?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET stage=excluded.stage, created_ts=excluded.created_ts, data_json=excluded.data_json",
            (guild_id, user_id, stage, int(time.time()), json.dumps(data)),
        )

    async def _clear_help_session(self, user_id: int, guild_id: int):
        await self.bot.db.execute("DELETE FROM help_sessions WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    async def _get_help_session(self, user_id: int, guild_id: int) -> Optional[Dict[str, Any]]:
        row = await self.bot.db.fetchone(
            "SELECT stage, data_json FROM help_sessions WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        if not row:
            return None
        try:
            data = json.loads(row["data_json"] or "{}")
        except Exception:
            data = {}
        return {"stage": str(row["stage"]), "data": data}

    async def _handle_help_session_message(self, guild: discord.Guild, message: discord.Message) -> bool:
        sess = await self._get_help_session(message.author.id, guild.id)
        if not sess:
            return False

        stage = sess["stage"]
        data = sess["data"]
        content = (message.content or "").strip()

        if content.casefold() in {"cancel", "stop", "never mind", "nevermind"}:
            await self._clear_help_session(message.author.id, guild.id)
            await message.channel.send("Cancelled.")
            return True

        if stage == "appeal_punishment":
            data["punishment"] = content
            await self._start_help_session(message.author.id, guild.id, "appeal_reason", data)
            await message.channel.send("Why should this punishment be lifted? Please explain.\n\nType **cancel** to stop.")
            return True

        if stage == "appeal_reason":
            data["reason"] = content
            await self._submit_appeal(guild, message.author.id, data)
            await self._clear_help_session(message.author.id, guild.id)
            await message.channel.send("Thanks! We will look into your appeal soon")
            return True

        if stage == "report_details":
            data["report"] = content
            await self._submit_report(guild, message.author.id, data)
            await self._clear_help_session(message.author.id, guild.id)
            await message.channel.send("Thanks! Your report has been sent to our staff team")
            return True

        if stage == "bot_issue_details":
            data["issue"] = content
            await self._submit_bot_issue(guild, message.author.id, data)
            await self._clear_help_session(message.author.id, guild.id)
            await message.channel.send("Thanks! Your bug report has been sent to our developer team")
            return True

        if stage == "transcript_ticket":
            ticket_channel_id, ticket_id = self._parse_ticket_reference(content)
            if ticket_channel_id is None and ticket_id is None:
                await message.channel.send("Couldn't parse that. Send a ticket channel mention/ID or Ticket ID like `T123`. If not, contact any Admins/Owners directly.")
                return True

            row = None
            if ticket_channel_id is not None:
                row = await self.bot.db.fetchone(
                    "SELECT ticket_id, channel_id, creator_id FROM tickets WHERE channel_id=?",
                    (ticket_channel_id,),
                )
            elif ticket_id is not None:
                row = await self.bot.db.fetchone(
                    "SELECT ticket_id, channel_id, creator_id FROM tickets WHERE guild_id=? AND ticket_id=?",
                    (guild.id, ticket_id),
                )

            if not row:
                await message.channel.send("I couldn't find that ticket :/")
                return True

            if int(row["creator_id"]) != message.author.id:
                await message.channel.send("That is nor your ticket though")
                return True

            t_id = int(row["ticket_id"]) if row["ticket_id"] is not None else None
            ch_id = int(row["channel_id"])

            ok, reason = await self._create_transcript_request(
                guild,
                requester_id=message.author.id,
                ticket_channel_id=ch_id,
                ticket_id=t_id,
            )
            await self._clear_help_session(message.author.id, guild.id)

            if ok:
                await message.channel.send("Your transcript request has been sent to staff for approval, thanks for your patience!")
            else:
                await message.channel.send(reason or "Transcript request failed.")
            return True

        await self._clear_help_session(message.author.id, guild.id)
        return False

    def _parse_ticket_reference(self, text: str) -> Tuple[Optional[int], Optional[int]]:
        """Return (channel_id, ticket_id). Only one will be non-None."""
        m = re.search(r"<#(\d{15,25})>", text)
        if m:
            return int(m.group(1)), None

        m = re.search(r"\bT?(\d{1,25})\b", text.strip(), flags=re.I)
        if not m:
            return None, None

        digits = m.group(1)
        if len(digits) >= 15:
            return int(digits), None
        return None, int(digits)

    # -----------------------------
    # Staff submissions
    # -----------------------------
    async def _submit_appeal(self, guild: discord.Guild, user_id: int, data: Dict[str, Any]):
        cfg = self.bot.config
        ch_id = cfg.get_int("channels", "appeals_log_channel_id")
        channel = guild.get_channel(ch_id) if ch_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(title="Punishment Appeal")
        embed.add_field(name="User", value=f"<@{user_id}> ({user_id})", inline=False)
        embed.add_field(name="Punishment", value=str(data.get("punishment", ""))[:1024], inline=False)
        embed.add_field(name="Why lift?", value=str(data.get("reason", ""))[:1024], inline=False)
        await channel.send(embed=embed)

    async def _submit_report(self, guild: discord.Guild, user_id: int, data: Dict[str, Any]):
        cfg = self.bot.config
        ch_id = cfg.get_int("channels", "reports_log_channel_id")
        channel = guild.get_channel(ch_id) if ch_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(title="User/Message Report")
        embed.add_field(name="Reporter", value=f"<@{user_id}> ({user_id})", inline=False)
        embed.add_field(name="Details", value=str(data.get("report", ""))[:1024], inline=False)
        await channel.send(embed=embed)

    async def _submit_bot_issue(self, guild: discord.Guild, user_id: int, data: Dict[str, Any]):
        cfg = self.bot.config
        ch_id = cfg.get_int("channels", "bot_issues_log_channel_id")
        channel = guild.get_channel(ch_id) if ch_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(title="Bot Issue Report")
        embed.add_field(name="Reporter", value=f"<@{user_id}> ({user_id})", inline=False)
        embed.add_field(name="Issue", value=str(data.get("issue", ""))[:1024], inline=False)
        await channel.send(embed=embed)

    # -----------------------------
    # Transcript requests (staff approval)
    # -----------------------------
    async def _create_transcript_request(self, guild: discord.Guild, requester_id: int, ticket_channel_id: int, ticket_id: Optional[int]) -> Tuple[bool, str]:
        cfg = self.bot.config
        req_ch_id = cfg.get_int("channels", "transcript_requests_channel_id")
        channel = guild.get_channel(req_ch_id) if req_ch_id else None
        if not isinstance(channel, discord.TextChannel):
            return False, "Transcript requests channel is not configured."

        if ticket_id is not None:
            existing = await self.bot.db.fetchone(
                "SELECT status FROM transcript_requests WHERE guild_id=? AND ticket_id=? ORDER BY created_ts DESC LIMIT 1",
                (guild.id, ticket_id),
            )
        else:
            existing = await self.bot.db.fetchone(
                "SELECT status FROM transcript_requests WHERE guild_id=? AND ticket_channel_id=? ORDER BY created_ts DESC LIMIT 1",
                (guild.id, ticket_channel_id),
            )

        if existing:
            status = str(existing["status"])
            if status == "pending":
                return False, "There is already a **pending** transcript request for that ticket."
            if status in ("approved", "denied"):
                return False, f"That ticket's transcript request is already **{status}**."

        embed = discord.Embed(title="Transcript Request")
        embed.add_field(name="Requester", value=f"<@{requester_id}> ({requester_id})", inline=False)
        embed.add_field(name="Ticket Channel", value=f"<#{ticket_channel_id}> ({ticket_channel_id})", inline=False)
        if ticket_id is not None:
            embed.add_field(name="Ticket ID", value=f"T{ticket_id}", inline=False)
        embed.set_footer(text="Staff: Approve or Deny")

        msg = await channel.send(embed=embed, view=TranscriptRequestView())

        await self.bot.db.execute(
            "INSERT INTO transcript_requests(guild_id, request_message_id, ticket_channel_id, requester_id, status, created_ts, ticket_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (guild.id, msg.id, ticket_channel_id, requester_id, "pending", int(time.time()), ticket_id),
        )
        return True, ""

    async def handle_transcript_request_decision(self, interaction: discord.Interaction, approved: bool):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if interaction.guild is None or interaction.guild.id != allowed_guild_id:
            return await interaction.response.send_message("Wrong server.", ephemeral=True)

        mod_role_id = cfg.get_int("roles", "MOD_ROLE_ID") or 0
        member = interaction.guild.get_member(interaction.user.id)
        if member is None or not is_mod(member, mod_role_id):
            return await interaction.response.send_message("Only mods can do that.", ephemeral=True)

        row = await self.bot.db.fetchone(
            "SELECT ticket_channel_id, requester_id, status, ticket_id FROM transcript_requests WHERE request_message_id=?",
            (interaction.message.id,),
        )
        if not row:
            return await interaction.response.send_message("Request not found.", ephemeral=True)

        if str(row["status"]) != "pending":
            return await interaction.response.send_message("This request is already processed.", ephemeral=True)

        ticket_channel_id = int(row["ticket_channel_id"])
        requester_id = int(row["requester_id"])
        ticket_id = int(row["ticket_id"]) if row["ticket_id"] is not None else None

        if not approved:
            await self.bot.db.execute("UPDATE transcript_requests SET status='denied' WHERE request_message_id=?", (interaction.message.id,))
            try:
                await interaction.message.edit(content="❌ Denied", view=None)
            except Exception:
                pass
            try:
                await interaction.response.send_message("Denied.", ephemeral=True)
            except Exception:
                pass
            try:
                user = await self.bot.fetch_user(requester_id)
                await user.send(f"Your transcript request was **denied** by staff. (Ticket {('T'+str(ticket_id)) if ticket_id else ticket_channel_id})")
            except Exception:
                pass
            return

        await self.bot.db.execute("UPDATE transcript_requests SET status='approved' WHERE request_message_id=?", (interaction.message.id,))

        try:
            await interaction.response.send_message("Approved. Sending transcript…", ephemeral=True)
        except Exception:
            pass

        ok = await self._dm_transcript(interaction.guild, requester_id, ticket_channel_id, ticket_id)

        try:
            await interaction.message.edit(content=("Approved and sent" if ok else "⚠️ Approved (failed to deliver)"), view=None)
        except Exception:
            pass

    async def _dm_transcript(self, guild: discord.Guild, requester_id: int, ticket_channel_id: int, ticket_id: Optional[int]) -> bool:
        user: Optional[discord.User]
        try:
            user = await self.bot.fetch_user(requester_id)
        except Exception:
            return False

        # If channel exists: build transcript live
        channel = guild.get_channel(ticket_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                transcript_path = await build_text_transcript(channel)
                await user.send(
                    content=f"Here is your transcript for {channel.name} ({channel.id}).",
                    file=discord.File(transcript_path, filename=f"transcript-{ticket_id or channel.id}.txt"),
                )
                return True
            except Exception:
                pass

        # Fallback: fetch stored transcript from log channel
        if ticket_id is None:
            return False

        ptr = await self.bot.db.fetchone(
            "SELECT log_channel_id, log_message_id FROM ticket_transcripts WHERE guild_id=? AND ticket_id=?",
            (guild.id, ticket_id),
        )
        if not ptr:
            return False

        log_ch = guild.get_channel(int(ptr["log_channel_id"]))
        if not isinstance(log_ch, discord.TextChannel):
            return False

        try:
            msg = await log_ch.fetch_message(int(ptr["log_message_id"]))
            if not msg.attachments:
                return False
            att = msg.attachments[0]
            data = await att.read()
            await user.send(
                content=f"Here is your transcript for Ticket T{ticket_id}.",
                file=discord.File(fp=io.BytesIO(data), filename=f"transcript-T{ticket_id}.txt"),
            )
            return True
        except Exception:
            return False

    # -----------------------------
    # Ticket creation (mod contact)
    # -----------------------------
    async def handle_mod_confirm(self, interaction: discord.Interaction, confirmed: bool):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        guild = self.bot.get_guild(allowed_guild_id) if allowed_guild_id else None
        if guild is None:
            return await interaction.response.send_message("Guild not found.", ephemeral=True)

        member = guild.get_member(interaction.user.id)
        if member is None:
            return await interaction.response.send_message("You must be in the server to create a ticket!", ephemeral=True)

        if not confirmed:
            return await interaction.response.send_message("Cancelled.", ephemeral=True)

        cooldown_h = int(cfg.get("tickets", "ticket_creation_cooldown_hours", default=24) or 24)
        row = await self.bot.db.fetchone(
            "SELECT last_created_ts FROM ticket_cooldowns WHERE guild_id=? AND user_id=?",
            (guild.id, member.id),
        )
        now = int(time.time())
        if row and now - int(row["last_created_ts"]) < cooldown_h * 3600:
            remaining = cooldown_h * 3600 - (now - int(row["last_created_ts"]))
            return await interaction.response.send_message(
                f"You can create another ticket in about {_format_duration(remaining)}.",
                ephemeral=True,
            )

        category_id = cfg.get_int("tickets", "ticket_category_id")
        mod_role_id = cfg.get_int("roles", "MOD_ROLE_ID")
        if not category_id or not mod_role_id:
            return await interaction.response.send_message("Ticket system is not configured (Average's fault, please contact him)", ephemeral=True)

        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("Ticket category is missing or invalid ((Average's fault, please contact him)", ephemeral=True)

        mod_role = guild.get_role(mod_role_id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if mod_role:
            overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        ticket_id = await self._next_ticket_id(guild.id)
        name = f"ticket-{ticket_id}-{member.name}".lower().replace(" ", "-")[:90]

        try:
            channel = await guild.create_text_channel(name=name, category=category, overwrites=overwrites, reason="Ticket created")
        except Exception:
            return await interaction.response.send_message("Failed to create ticket (missing permissions?)", ephemeral=True)

        await self.bot.db.execute(
            "INSERT OR REPLACE INTO ticket_cooldowns(guild_id, user_id, last_created_ts) VALUES(?,?,?)",
            (guild.id, member.id, now),
        )
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO tickets(guild_id, channel_id, creator_id, created_ts, last_user_activity_ts, status, ticket_id) "
            "VALUES(?,?,?,?,?, 'open', ?)",
            (guild.id, channel.id, member.id, now, now, ticket_id),
        )

        # DM includes Ticket ID
        try:
            dm = await member.create_dm()
            embed = discord.Embed(
                title="Ticket created",
                description=f"Your ticket has been created -> {channel.mention}\n\nTicket ID: `T{ticket_id}`",
            )
            await dm.send(embed=embed)
        except Exception:
            pass

        try:
            await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
        except Exception:
            pass

        try:
            await channel.send(f"Please say what you need {member.mention}, {pinged_role_for_tickets if pinged_role_for_tickets else 'staff'} will be shortly with you ;)")
        except Exception:
            pass

    async def _next_ticket_id(self, guild_id: int) -> int:
        row = await self.bot.db.fetchone("SELECT next_ticket_id FROM ticket_sequences WHERE guild_id=?", (guild_id,))
        if not row:
            await self.bot.db.execute("INSERT INTO ticket_sequences(guild_id, next_ticket_id) VALUES(?, ?)", (guild_id, 2))
            return 1

        next_id = int(row["next_ticket_id"])
        await self.bot.db.execute("UPDATE ticket_sequences SET next_ticket_id=? WHERE guild_id=?", (next_id + 1, guild_id))
        return next_id

    async def handle_ticket_close_prompt(self, interaction: discord.Interaction, confirmed: bool):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if interaction.guild is None or interaction.guild.id != allowed_guild_id:
            return await interaction.response.send_message("Wrong server.", ephemeral=True)

        mod_role_id = cfg.get_int("roles", "MOD_ROLE_ID")
        if mod_role_id:
            member = interaction.guild.get_member(interaction.user.id)
            if member is None or not is_mod(member, mod_role_id):
                return await interaction.response.send_message("Only staff can close tickets", ephemeral=True)

        if not confirmed:
            await interaction.response.send_message("Keeping ticket open.", ephemeral=True)
            await self.bot.db.execute("UPDATE tickets SET status='open' WHERE channel_id=?", (interaction.channel_id,))
            return

        await interaction.response.send_message("Closing ticket...", ephemeral=True)
        await self.close_ticket_channel(interaction.guild, interaction.channel_id)

    async def close_ticket_channel(self, guild: discord.Guild, channel_id: int):
        cfg = self.bot.config
        log_channel_id = cfg.get_int("channels", "general_logging_channel_id")
        log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
        channel = guild.get_channel(channel_id)

        if not isinstance(channel, discord.TextChannel):
            return

        row = await self.bot.db.fetchone("SELECT ticket_id FROM tickets WHERE channel_id=?", (channel_id,))
        ticket_id = int(row["ticket_id"]) if row and row["ticket_id"] is not None else None

        try:
            transcript_path = await build_text_transcript(channel)
            if isinstance(log_channel, discord.TextChannel):
                sent = await log_channel.send(
                    content=f"Transcript for {channel.name} ({channel.id})" + (f" | Ticket T{ticket_id}" if ticket_id else ""),
                    file=discord.File(transcript_path, filename=f"transcript-{ticket_id or channel.id}.txt"),
                )
                if ticket_id is not None:
                    await self.bot.db.execute(
                        "INSERT OR REPLACE INTO ticket_transcripts(guild_id, ticket_id, log_channel_id, log_message_id, created_ts) "
                        "VALUES(?,?,?,?,?)",
                        (guild.id, ticket_id, sent.channel.id, sent.id, int(time.time())),
                    )
        except Exception:
            pass

        try:
            await self.bot.db.execute("UPDATE tickets SET status='closed' WHERE channel_id=?", (channel_id,))
        except Exception:
            pass

        try:
            await channel.delete(reason="Ticket closed")
        except Exception:
            pass


def setup(bot: discord.Bot):
    bot.add_cog(HelpCog(bot))
