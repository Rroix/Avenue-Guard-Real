from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from utils.checks import ensure_allowed_guild_id, basic_color

class MessageResponsesCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._rules: List[Dict[str, Any]] = []
        self._cooldown: Dict[int, float] = {}
        self.load_rules()

    def load_rules(self) -> None:
        cfg = self.bot.config
        path = cfg.get_str("responses", "rules_path", default="responses.json")
        p = Path(path)
        if not p.exists():
            self._rules = []
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                # strip pseudo-comments at rule-level
                self._rules = [{k: v for k, v in r.items() if not str(k).startswith("_")} for r in data if isinstance(r, dict)]
            else:
                self._rules = []
        except Exception:
            self._rules = []

    def on_config_reload(self) -> None:
        self.load_rules()
        self._cooldown.clear()

    def _cooldown_ok(self, user_id: int) -> bool:
        cfg = self.bot.config
        cd = int(cfg.get("responses", "cooldown_seconds", default=15) or 15)
        now = time.time()
        last = self._cooldown.get(user_id, 0.0)
        if now - last < cd:
            return False
        self._cooldown[user_id] = now
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if not ensure_allowed_guild_id(message.guild, allowed_guild_id):
            return

        if not self._rules:
            return

        if not self._cooldown_ok(message.author.id):
            return

        content = (message.content or "").strip()
        lowered = content.casefold()

        for rule in self._rules:
            try:
                trigger = str(rule.get("Content", "")).strip()
                if not trigger:
                    continue
                whole = bool(rule.get("Whole_message", False))
                trigger_l = trigger.strip().casefold()

                match = (lowered == trigger_l) if whole else (trigger_l in lowered)
                if not match:
                    continue

                # channel filter
                chans = rule.get("Channels", [])
                if isinstance(chans, list) and chans:
                    try:
                        chan_ids = {int(x) for x in chans}
                    except Exception:
                        chan_ids = set()
                    if chan_ids and message.channel.id not in chan_ids:
                        continue

                respond = bool(rule.get("Respond", False))
                use_embed = bool(rule.get("Embed", False))
                use_msg = bool(rule.get("Message", False))

                if use_embed:
                    et = rule.get("Embed_text", {}) or {}
                    title = str(et.get("title", "") or "")
                    desc = str(et.get("description", "") or "")
                    color = basic_color(str(et.get("color", "") or ""))
                    embed = discord.Embed(title=title or None, description=desc or None, color=color)
                    if respond:
                        await message.reply(embed=embed, mention_author=False)
                    else:
                        await message.channel.send(embed=embed)
                elif use_msg:
                    text = str(rule.get("Message_text", "") or "")
                    if respond:
                        await message.reply(text, mention_author=False)
                    else:
                        await message.channel.send(text)

                # first match only
                break
            except Exception:
                continue

def setup(bot: discord.Bot):
    bot.add_cog(MessageResponsesCog(bot))
