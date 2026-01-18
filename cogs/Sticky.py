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

        # Intentionally used for tag lookup (you asked to keep this pattern).
        # In multi-forum mode we set this per-thread before selecting templates.
        self._forum_templates: Dict[str, Dict[str, Any]] = {}

        # Thread IDs we've already handled this runtime
        self._forum_sent_threads: set[int] = set()

        # Per-thread locks so only one send attempt runs at a time for a thread.
        self._forum_thread_locks: Dict[int, asyncio.Lock] = {}

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
        # Normal path (on_thread_create) should run first. This fallback:
        # - checks if the bot already posted in the thread (manual check)
        # - if yes, does nothing
        # - if no, sends
        try:
            if isinstance(message.channel, discord.Thread) and message.channel.parent_id in self._forum_rules:
                asyncio.create_task(self._forum_first_message_flow(message.channel, prefer_normal=False))
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
    def _get_thread_lock(self, thread_id: int) -> asyncio.Lock:
        lock = self._forum_thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._forum_thread_locks[thread_id] = lock
        return lock

    async def _thread_has_bot_message(self, thread: discord.Thread, limit: int = 25) -> bool:
        """Manual check: if the bot has already posted in this thread, we shouldn't send again."""
        me = self.bot.user
        if me is None:
            return False
        try:
            async for msg in thread.history(limit=limit, oldest_first=True):
                if msg.author and msg.author.id == me.id:
                    return True
        except Exception:
            # If we can't read history, play safe and avoid double posting.
            return True
        return False

    async def _send_forum_first_message(self, thread: discord.Thread) -> bool:
        """Send the configured first-message embed once. Returns True if sent."""
        if thread.guild is None:
            return False

        templates = self._forum_rules.get(thread.parent_id)
        if not templates:
            return False

        # Keep your intentional mapping: assign per-forum templates here
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

        await thread.send(embed=embed)
        return True

    async def _forum_first_message_flow(self, thread: discord.Thread, prefer_normal: bool) -> None:
        """One task at a time per thread.

        - Normal path (on_thread_create) runs first.
        - Fallback (on_message) waits, then checks if the bot already posted; if not, sends.
        """
        if thread.guild is None:
            return
        if thread.parent_id not in self._forum_rules:
            return

        # Fast skip if already handled in this runtime.
        if thread.id in self._forum_sent_threads:
            return

        lock = self._get_thread_lock(thread.id)
        async with lock:
            # Re-check inside lock.
            if thread.id in self._forum_sent_threads:
                return

            # If fallback, give normal path time to send first.
            if not prefer_normal:
                try:
                    await asyncio.sleep(2.0)
                except Exception:
                    pass

            # Manual check: if bot already posted in the thread, don't send again.
            if await self._thread_has_bot_message(thread):
                self._forum_sent_threads.add(thread.id)
                return

            # Try to send with retries (attachment posts can race thread readiness)
            for attempt in range(6):
                try:
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                    sent = await self._send_forum_first_message(thread)
                    if sent:
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

        # Normal path: prefer_normal=True so it doesn't delay.
        try:
            asyncio.create_task(self._forum_first_message_flow(thread, prefer_normal=True))
        except Exception:
            pass


def setup(bot: discord.Bot):
    bot.add_cog(StickyCog(bot))
