from __future__ import annotations

import asyncio
from typing import Dict, Optional, Any, List

import discord
from discord.ext import commands

from utils.checks import ensure_allowed_guild_id, basic_color
from utils.errors import log_error


class StickyCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._debounce_tasks: Dict[int, asyncio.Task] = {}
        self._sticky_entries: List[Dict[str, Any]] = []

        # forum_channel_id -> templates dict (keys: "default" and tag_id strings)
        self._forum_rules: Dict[int, Dict[str, Dict[str, Any]]] = {}
        self._forum_required_rules: Dict[int, Dict[str, Any]] = {}

        # Intentionally used for tag lookup (you asked to keep this pattern).
        # In multi-forum mode we set this per-thread before selecting templates.
        self._forum_templates: Dict[str, Dict[str, Any]] = {}

        # Thread IDs we've already handled this runtime
        self._forum_sent_threads: set[int] = set()
        self._forum_required_checked_threads: set[int] = set()

        # Per-thread locks so only one send attempt runs at a time for a thread.
        self._forum_thread_locks: Dict[int, asyncio.Lock] = {}

        self.reload_from_config()

    def reload_from_config(self) -> None:
        cfg = self.bot.config
        self._sticky_entries = cfg.get("sticky", "entries", default=[]) or []

        # Forum first-message supports either a single config (legacy) or multiple entries.
        self._forum_rules = {}
        self._forum_required_rules = {}
        global_required_rule = self._required_rule_from_config(cfg.get("forum_first_message", default={}) or {})
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
                    required_rule = self._required_rule_from_config(ent, fallback=global_required_rule)
                    if required_rule:
                        self._forum_required_rules[ch_id] = required_rule
        else:
            ch_id = cfg.get_int("forum_first_message", "forum_channel_id")
            templates = cfg.get("forum_first_message", "templates", default={}) or {}
            if ch_id and isinstance(templates, dict):
                self._forum_rules[int(ch_id)] = templates
                required_rule = global_required_rule
                if required_rule:
                    self._forum_required_rules[int(ch_id)] = required_rule

        # If legacy single-forum config is used, keep _forum_templates pointing there.
        if len(self._forum_rules) == 1:
            self._forum_templates = next(iter(self._forum_rules.values()))
        else:
            self._forum_templates = {}

    def on_config_reload(self) -> None:
        self.reload_from_config()

    def _required_rule_from_config(self, source: Dict[str, Any], fallback: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        fallback = fallback or {}
        word = str(source.get("required_word", fallback.get("word", "")) or "").strip()
        if not word:
            return None

        dm_message = str(
            source.get("missing_required_word_dm")
            or source.get("required_word_dm_message")
            or fallback.get("dm_message")
            or "Your thread was removed because it did not include the required word: {required_word}."
        )
        try:
            delay = float(source.get("required_word_delete_delay_seconds", fallback.get("delete_delay_seconds", 10)) or 10)
        except Exception:
            delay = 10.0

        return {
            "word": word,
            "dm_message": dm_message,
            "delete_delay_seconds": max(0.0, delay),
        }

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

    def _schedule_required_word_check(self, thread: discord.Thread) -> None:
        if thread.parent_id not in self._forum_required_rules:
            return
        if thread.id in self._forum_required_checked_threads:
            return
        self._forum_required_checked_threads.add(thread.id)
        try:
            asyncio.create_task(self._enforce_required_word(thread))
        except Exception:
            pass

    async def _thread_contains_required_word(self, thread: discord.Thread, required_word: str) -> bool:
        needle = required_word.casefold()
        text_parts = [thread.name or ""]
        try:
            async for msg in thread.history(limit=10, oldest_first=True):
                if msg.author and msg.author.bot:
                    continue
                if msg.content:
                    text_parts.append(msg.content)
                for embed in msg.embeds:
                    if embed.title:
                        text_parts.append(embed.title)
                    if embed.description:
                        text_parts.append(embed.description)
        except Exception:
            # If history cannot be read, avoid deleting a valid thread by mistake.
            return True

        return needle in "\n".join(text_parts).casefold()

    async def _find_thread_owner(self, thread: discord.Thread) -> Optional[discord.abc.User]:
        owner_id = getattr(thread, "owner_id", None)
        if owner_id:
            member = thread.guild.get_member(owner_id) if thread.guild else None
            if member:
                return member
            try:
                return await self.bot.fetch_user(owner_id)
            except Exception:
                return None

        try:
            async for msg in thread.history(limit=5, oldest_first=True):
                if msg.author and not msg.author.bot:
                    return msg.author
        except Exception:
            return None
        return None

    async def _enforce_required_word(self, thread: discord.Thread) -> None:
        rule = self._forum_required_rules.get(thread.parent_id)
        if not rule:
            return

        delay = float(rule.get("delete_delay_seconds", 10.0) or 10.0)
        if delay:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

        required_word = str(rule.get("word", "") or "").strip()
        if not required_word:
            return

        if await self._thread_contains_required_word(thread, required_word):
            return

        owner = await self._find_thread_owner(thread)
        dm_template = str(rule.get("dm_message", "") or "")
        if owner and dm_template:
            try:
                dm_text = dm_template.format(
                    required_word=required_word,
                    thread_name=thread.name,
                    guild=thread.guild.name if thread.guild else "",
                )
            except Exception:
                dm_text = dm_template
            try:
                await owner.send(dm_text)
            except Exception:
                pass

        try:
            try:
                if getattr(thread, "archived", False) or getattr(thread, "locked", False):
                    await thread.edit(archived=False, locked=False)
            except Exception:
                pass
            await thread.delete()
        except Exception as e:
            await log_error(self.bot, f"Could not delete thread {thread.id} missing required word {required_word!r}: {repr(e)}")

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
            self._schedule_required_word_check(thread)
            return

        lock = self._get_thread_lock(thread.id)
        async with lock:
            # Re-check inside lock.
            if thread.id in self._forum_sent_threads:
                self._schedule_required_word_check(thread)
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
                self._schedule_required_word_check(thread)
                return

            # Try to send with retries (attachment posts can race thread readiness)
            for attempt in range(6):
                try:
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                    sent = await self._send_forum_first_message(thread)
                    if sent:
                        self._forum_sent_threads.add(thread.id)
                        self._schedule_required_word_check(thread)
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
