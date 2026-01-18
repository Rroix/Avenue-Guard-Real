from __future__ import annotations

import asyncio
from typing import Dict, Optional, Any, List

import discord
from discord.ext import commands

from utils.checks import ensure_allowed_guild_id, basic_color


class StickyCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._debounce_tasks: Dict[int, asyncio.Task] = {}
        self._sticky_entries: List[Dict[str, Any]] = []

        # forum_channel_id -> templates dict (keys: "default" and tag_id strings)
        self._forum_rules: Dict[int, Dict[str, Dict[str, Any]]] = {}

        # NOTE: You said this is intentional, so we keep using this mapping for tag lookups.
        # At send-time we assign it to the templates for the relevant forum.
        self._forum_templates: Dict[str, Dict[str, Any]] = {}

        # in-memory guard to avoid double-sending per thread runtime
        self._forum_sent_threads: set[int] = set()

        self.reload_from_config()

    def reload_from_config(self) -> None:
        cfg = self.bot.config
        self._sticky_entries = cfg.get("sticky", "entries", default=[]) or []

        # Forum first-message supports either a single config (legacy) or multiple entries.
        self._forum_rules = {}
        entries = cfg.get("forum_first_message", "entries", default=None)
        if isinstance(entries, list) and entries:
            for ent in entries:
                if not isinstance(ent, dict):
                    continue
                ch = ent.get("forum_channel_id")
                try:
                    ch_id = int(ch)
                except Exception:
                    continue
                templates = ent.get("templates", {}) or {}
                if isinstance(templates, dict):
                    self._forum_rules[ch_id] = templates
        else:
            ch_id = cfg.get_int("forum_first_message", "forum_channel_id")
            templates = cfg.get("forum_first_message", "templates", default={}) or {}
            if ch_id and isinstance(templates, dict):
                self._forum_rules[int(ch_id)] = templates

        # If legacy single-forum config is used, keep _forum_templates pointing there.
        if len(self._forum_rules) == 1:
            self._forum_templates = next(iter(self._forum_rules.values()))
        else:
            # Multi-forum: _forum_templates is set per-thread when sending.
            self._forum_templates = {}

    def on_config_reload(self) -> None:
        self.reload_from_config()

    def _get_sticky_for_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        for e in self._sticky_entries:
            try:
                if int(e.get("channel_id")) == channel_id:
                    return e
            except Exception:
                continue
        return None

    # ---------------------------
    # Sticky message feature
    # ---------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(message.guild, allowed_guild_id):
            return

        # Forum-first-message fallback:
        # If on_thread_create raced (common with media attachments) or failed, try again when we see messages in the thread.
        try:
            if isinstance(message.channel, discord.Thread) and message.channel.parent_id in self._forum_rules:
                if message.channel.id not in self._forum_sent_threads:
                    asyncio.create_task(self._send_forum_first_message_retry(message.channel))
        except Exception:
            pass

        entry = self._get_sticky_for_channel(message.channel.id)
        if not entry:
            return

        # debounce per channel
        task = self._debounce_tasks.get(message.channel.id)
        if task and not task.done():
            task.cancel()

        delay = float(entry.get("delay_seconds", 5) or 5)
        self._debounce_tasks[message.channel.id] = asyncio.create_task(
            self._do_sticky(message.channel, message.guild, entry, delay)
        )

    async def _do_sticky(self, channel: discord.TextChannel, guild: discord.Guild, entry: Dict[str, Any], delay: float):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        # delete previous sticky
        db = self.bot.db
        row = await db.fetchone(
            "SELECT last_sticky_message_id FROM sticky_state WHERE guild_id=? AND channel_id=?",
            (guild.id, channel.id),
        )
        last_id = int(row["last_sticky_message_id"]) if row and row["last_sticky_message_id"] else None
        if last_id:
            try:
                msg = await channel.fetch_message(last_id)
                await msg.delete()
            except Exception:
                pass

        text = str(entry.get("message", "") or "")
        if not text:
            return

        try:
            sent = await channel.send(text)
            await db.execute(
                "INSERT INTO sticky_state(guild_id, channel_id, last_sticky_message_id) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, channel_id) DO UPDATE SET last_sticky_message_id=excluded.last_sticky_message_id",
                (guild.id, channel.id, sent.id),
            )
        except Exception:
            pass

    # ---------------------------
    # Forum first-message feature
    # ---------------------------
    async def _send_forum_first_message_retry(self, thread: discord.Thread) -> None:
        """Send the configured first-message embed to a forum thread with delay+retries.

        Forum posts with media attachments can race thread readiness; this prevents silent failures.
        """
        if thread.guild is None:
            return
        if thread.id in self._forum_sent_threads:
            return

        templates = self._forum_rules.get(thread.parent_id)
        if not templates:
            return

        # keep your intentional mapping: assign per-forum templates here
        self._forum_templates = templates

        # choose template by first matching applied tag, else default
        template = templates.get("default", {}) or {}
        try:
            applied = getattr(thread, "applied_tags", []) or []
            for tag in applied:
                t = self._forum_templates.get(str(tag.id))
                if isinstance(t, dict):
                    template = t
                    break
        except Exception:
            pass

        title = str(template.get("title", "") or "")
        desc = str(template.get("description", "") or "")
        color = basic_color(str(template.get("color", "") or "blurple"))
        embed = discord.Embed(title=title or None, description=desc or None, color=color)

        # initial delay helps when the post includes media attachments
        try:
            await asyncio.sleep(1.0)
        except Exception:
            pass

        for attempt in range(6):
            try:
                await thread.send(embed=embed)
                self._forum_sent_threads.add(thread.id)
                return
            except Exception:
                try:
                    await asyncio.sleep(1.0 + attempt * 0.5)
                except Exception:
                    return

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if thread.guild is None or thread.guild.id != allowed_guild_id:
            return

        if thread.parent_id not in self._forum_rules:
            return

        # Schedule sending with delay+retries.
        try:
            asyncio.create_task(self._send_forum_first_message_retry(thread))
        except Exception:
            pass


def setup(bot: discord.Bot):
    bot.add_cog(StickyCog(bot))
