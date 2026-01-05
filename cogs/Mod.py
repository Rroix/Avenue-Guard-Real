from __future__ import annotations

import discord
from discord.ext import commands

from utils.checks import ensure_allowed_guild_id, member_has_any_role

class ModCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if message.guild is None or not ensure_allowed_guild_id(message.guild, allowed_guild_id):
            return

        target_channel_id = cfg.get_int("channels", "autodelete_channel_id")
        if target_channel_id and message.channel.id != target_channel_id:
            return

        whitelist_roles = cfg.get_int_list("roles", "whitelisted_deletion_ID_roles")
        restriction_role_id = cfg.get_int("roles", "restriction_role_ID")
        if not restriction_role_id:
            return

        member = message.guild.get_member(message.author.id)
        if member is None:
            return

        if member_has_any_role(member, whitelist_roles):
            return

        # delete the message and apply restriction role
        try:
            await message.delete()
        except Exception:
            pass

        try:
            role = message.guild.get_role(restriction_role_id)
            if role and role not in member.roles:
                await member.add_roles(role, reason="Autodeletion restriction")
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if payload.guild_id != allowed_guild_id:
            return

        target_channel_id = cfg.get_int("channels", "autodelete_channel_id")
        if target_channel_id and payload.channel_id != target_channel_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        member = payload.member
        if member is None or member.bot:
            return

        whitelist_roles = cfg.get_int_list("roles", "whitelisted_deletion_ID_roles")
        restriction_role_id = cfg.get_int("roles", "restriction_role_ID")
        if not restriction_role_id:
            return

        if member_has_any_role(member, whitelist_roles):
            return

        # remove reaction and apply restriction role
        try:
            channel = guild.get_channel(payload.channel_id)
            if isinstance(channel, discord.TextChannel):
                msg = await channel.fetch_message(payload.message_id)
                await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

        try:
            role = guild.get_role(restriction_role_id)
            if role and role not in member.roles:
                await member.add_roles(role, reason="Autodeletion reaction restriction")
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        cfg = self.bot.config
        allowed_guild_id = cfg.get_int("guild", "allowed_guild_id")
        if after.guild.id != allowed_guild_id:
            return        # DM-on-role supports one legacy role or multiple entries.
        rules = []
        entries = cfg.get("autoDM", "entries", default=None)
        if isinstance(entries, list):
            for ent in entries:
                if not isinstance(ent, dict):
                    continue
                rid = ent.get("role_id")
                try:
                    rid_int = int(rid)
                except Exception:
                    continue
                msg = str(ent.get("message", "") or "")
                if msg:
                    rules.append((rid_int, msg))

        legacy_role_id = cfg.get_int("roles", "autoDM_watched_role_id")
        if legacy_role_id:
            legacy_msg = cfg.get_str("autoDM", "message", default="Hello {user}!")
            rules.append((legacy_role_id, legacy_msg))

        # no rules configured
        if not rules:
            return

        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}

        for role_id, msg_template in rules:
            if role_id in after_ids and role_id not in before_ids:
                role = after.guild.get_role(role_id)
                txt = str(msg_template).format(
                    user=after.mention,
                    role=(role.name if role else str(role_id)),
                    guild=after.guild.name
                )
                try:
                    await after.send(txt)
                except Exception:
                    pass

def setup(bot: discord.Bot):
    bot.add_cog(ModCog(bot))
