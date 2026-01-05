from __future__ import annotations

import os
import random
import secrets
import asyncio
import time
from typing import Optional

import discord
from discord.ext import commands

from utils.checks import is_admin_or_owner, is_mod
from utils.timeutils import now_madrid, week_start_sunday
from utils.errors import log_error

class CommandsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        cfg = bot.config
        self.allowed_guild_id = cfg.get_int("guild", "allowed_guild_id") or 0
        # Hardcoded anti-spam cooldowns
        self._gamble_last_ts: dict[int, float] = {}  # user_id -> last /gambling time
        self._rps_last_ts: dict[int, float] = {}  # user_id -> last /rock-paper-scissors time

        # Command groups (guild-scoped for fast sync)
        self.tracking_group = discord.SlashCommandGroup("tracking", "Tracking commands", guild_ids=[self.allowed_guild_id] if self.allowed_guild_id else None)
        self.ticket_group = discord.SlashCommandGroup("ticket", "Ticket commands", guild_ids=[self.allowed_guild_id] if self.allowed_guild_id else None)

        # register commands
        self.tracking_group.command(name="top", description="Show the current week's top 20 active members.")(self.tracking_top)
        self.tracking_group.command(name="reset", description="Reset current week's tracking stats (Admins/Owners only).")(self.tracking_reset)
        self.tracking_group.command(name="me", description="Show your activity stats for this week.")(self.tracking_me)
        self.tracking_group.command(name="force_dm", description="Force-send the weekly request DM to a user (Admins/Owners only).")(self.tracking_force_dm)

        self.ticket_group.command(name="close", description="Close the current ticket channel (Mods only).")(self.ticket_close)

        bot.add_application_command(self.tracking_group)
        bot.add_application_command(self.ticket_group)

        @bot.slash_command(name="resync", description="Reload config, views, and responses without restart.", guild_ids=[self.allowed_guild_id] if self.allowed_guild_id else None)
        async def resync(ctx: discord.ApplicationContext):
            await self._resync(ctx)

        @bot.slash_command(name="restart", description="Restart the bot (Admins/Owners only).", guild_ids=[self.allowed_guild_id] if self.allowed_guild_id else None)
        async def restart(ctx: discord.ApplicationContext):
            await self._restart(ctx)

        @bot.slash_command(name="dance", description="Send a dance GIF.", guild_ids=[self.allowed_guild_id] if self.allowed_guild_id else None)
        async def dance(ctx: discord.ApplicationContext):
            await self._dance(ctx)

        @bot.slash_command(name="rock-paper-scissors", description="Play Rock Paper Scissors.", guild_ids=[self.allowed_guild_id] if self.allowed_guild_id else None)
        async def rps(ctx: discord.ApplicationContext):
            await self._rps(ctx)

        @bot.slash_command(name="gambling", description="Try your luck in a quick slots game.", guild_ids=[self.allowed_guild_id] if self.allowed_guild_id else None)
        async def gambling(ctx: discord.ApplicationContext):
            await self._gambling(ctx)

    def _in_allowed_guild(self, ctx: discord.ApplicationContext) -> bool:
        return ctx.guild is not None and ctx.guild.id == self.allowed_guild_id

    # --- /tracking top ---

    async def tracking_top(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        tracking = self.bot.get_cog("TrackingCog")
        if tracking is None:
            return await ctx.respond("Tracking cog not loaded.", ephemeral=True)

        ws = week_start_sunday(now_madrid()).isoformat()
        raw = await tracking.get_top(ctx.guild.id, ws, limit=50)  # pull more then filter

        if not raw:
            return await ctx.respond("No activity tracked yet this week.", ephemeral=True)

        excluded_role_ids = set(self.bot.config.get_int_list("roles", "excluded_tracking_role_id", default=[]))

        top = []
        for uid, cnt in raw:
            member = ctx.guild.get_member(uid) if ctx.guild else None
            if member is None or member.bot:
                continue
            if excluded_role_ids and any(r.id in excluded_role_ids for r in member.roles):
                continue
            top.append((uid, cnt))
            if len(top) >= 20:
                break

        if not top:
            return await ctx.respond("No eligible members tracked yet this week.", ephemeral=True)

        lines = []
        for i, (uid, cnt) in enumerate(top, start=1):
            lines.append(f"**#{i:02d}**  <@{uid}> — **{cnt}** messages")

        week_label = week_start_sunday(now_madrid()).strftime("%Y-%m-%d")
        embed = discord.Embed(
            title="Weekly Activity Leaderboard",
            description=f"Week starting **{week_label}** — top {len(top)}\n\n" + "\n".join(lines),
        )
        try:
            if ctx.guild and ctx.guild.icon:
                embed.set_thumbnail(url=ctx.guild.icon.url)
        except Exception:
            pass

        embed.set_footer(text="Counts exclude bots, commands, and configured channels/roles.")
        await ctx.respond(embed=embed)


    async def tracking_me(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        tracking = self.bot.get_cog("TrackingCog")
        if tracking is None:
            return await ctx.respond("Tracking cog not loaded.", ephemeral=True)

        ws = week_start_sunday(now_madrid()).isoformat()
        count, rank, eligible_total = await tracking.get_member_stats(ctx.guild, ws, ctx.user.id)

        if rank is None:
            return await ctx.respond("You are not eligible for weekly tracking (or have no tracked messages yet).", ephemeral=True)

        week_label = week_start_sunday(now_madrid()).strftime("%Y-%m-%d")
        embed = discord.Embed(title="Your Weekly Activity")
        embed.add_field(name="Week starting", value=week_label, inline=False)
        embed.add_field(name="Messages", value=str(count), inline=True)
        embed.add_field(name="Rank", value=f"#{rank} of {eligible_total}", inline=True)
        await ctx.respond(embed=embed, ephemeral=True)

    async def tracking_force_dm(self, ctx: discord.ApplicationContext, member: discord.Member):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        admin_roles = self.bot.config.get_int_list("roles", "admin_owner_role_ids")
        invoker = ctx.guild.get_member(ctx.user.id) if ctx.guild else None
        if invoker is None or not is_admin_or_owner(invoker, admin_roles):
            return await ctx.respond("You don't have permission to use this.", ephemeral=True)

        tracking = self.bot.get_cog("TrackingCog")
        if tracking is None:
            return await ctx.respond("Tracking cog not loaded.", ephemeral=True)

        ws = week_start_sunday(now_madrid()).isoformat()
        ok, msg = await tracking.force_dm_for_user(ctx.guild, ws, member.id)
        await ctx.respond(msg, ephemeral=True)

    async def tracking_reset(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        member = ctx.guild.get_member(ctx.user.id)
        admin_roles = self.bot.config.get_int_list("roles", "admin_owner_role_ids")
        if member is None or not is_admin_or_owner(member, admin_roles):
            return await ctx.respond("You don't have permission to use this.", ephemeral=True)

        tracking = self.bot.get_cog("TrackingCog")
        if tracking is None:
            return await ctx.respond("Tracking cog not loaded.", ephemeral=True)

        await tracking.reset_current_week(ctx.guild.id)
        await ctx.respond("Tracking stats for the current week have been reset.", ephemeral=True)

    # --- /ticket close ---
    async def ticket_close(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        member = ctx.guild.get_member(ctx.user.id)
        mod_role_id = self.bot.config.get_int("roles", "MOD_ROLE_ID") or 0
        if member is None or not is_mod(member, mod_role_id):
            return await ctx.respond("Only mods can close tickets.", ephemeral=True)

        # ensure this is a ticket channel
        row = await self.bot.db.fetchone("SELECT status FROM tickets WHERE channel_id=? AND status IN ('open','closing_prompted')", (ctx.channel_id,))
        if not row:
            return await ctx.respond("This isn't an active ticket channel.", ephemeral=True)

        helpcog = self.bot.get_cog("HelpCog")
        if helpcog is None:
            return await ctx.respond("Help cog not loaded.", ephemeral=True)

        await ctx.respond("Closing ticket...", ephemeral=True)
        await helpcog.close_ticket_channel(ctx.guild, ctx.channel_id)

    # --- /resync ---
    async def _resync(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        member = ctx.guild.get_member(ctx.user.id)
        admin_roles = self.bot.config.get_int_list("roles", "admin_owner_role_ids")
        if member is None or not is_admin_or_owner(member, admin_roles):
            return await ctx.respond("You don't have permission to use this.", ephemeral=True)

        await self.bot.config.reload()

        # notify cogs
        for cog in self.bot.cogs.values():
            fn = getattr(cog, "on_config_reload", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

        # re-register persistent views
        try:
            await self.bot.register_persistent_views()
        except Exception:
            pass

        await ctx.respond("Resynced config, views, and responses.", ephemeral=True)

    # --- /restart ---
    async def _restart(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        member = ctx.guild.get_member(ctx.user.id)
        admin_roles = self.bot.config.get_int_list("roles", "admin_owner_role_ids")
        if member is None or not is_admin_or_owner(member, admin_roles):
            return await ctx.respond("You don't have permission to use this.", ephemeral=True)

        await ctx.respond("Restarting...", ephemeral=True)
        await self.bot.close()
        os._exit(0)

    # --- /dance ---
    async def _dance(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)
        url = self.bot.config.get_str("fun", "dance_gif_url", default="")
        if not url:
            return await ctx.respond("Dance GIF not configured.", ephemeral=True)
        await ctx.respond(url)

    # --- /rps ---
    async def _rps(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        # Anti-spam: hardcoded 10s cooldown per user for /rock-paper-scissors
        now_ts = time.time()
        last_ts = self._rps_last_ts.get(ctx.user.id, 0.0)
        if now_ts - last_ts < 10.0:
            remaining = int(10 - (now_ts - last_ts) + 0.999)
            return await ctx.respond(f"Slow down... try again in {remaining}s", ephemeral=True)
        self._rps_last_ts[ctx.user.id] = now_ts

        parent = self
        options = ["Rock", "Paper", "Scissors"]
        nonce = secrets.token_hex(4)

        def outcome(user: str, bot: str) -> str:
            if user == bot:
                return "tie"
            wins = {("Rock", "Scissors"), ("Paper", "Rock"), ("Scissors", "Paper")}
            return "win" if (user, bot) in wins else "lose"

        class RPSView(discord.ui.View):
            def __init__(self, user_id: int):
                super().__init__(timeout=60)
                self.user_id = user_id

                for opt in options:
                    btn = discord.ui.Button(
                        label=opt,
                        style=discord.ButtonStyle.primary,
                        custom_id=f"rps:{nonce}:{opt.lower()}",
                    )
                    btn.callback = self._make_callback(opt)
                    self.add_item(btn)

            def _make_callback(self, choice: str):
                async def _cb(interaction: discord.Interaction):
                    try:
                        if interaction.user.id != self.user_id:
                            return await interaction.response.send_message("This game isn't for you.", ephemeral=True)

                        bot_choice = random.choice(options)
                        o = outcome(choice, bot_choice)

                        guild_id = interaction.guild.id if interaction.guild else parent.allowed_guild_id
                        user_id = interaction.user.id

                        if o == "win":
                            streak = await parent._rps_update_streak(guild_id, user_id, new_value=None, increment=True)
                        elif o == "lose":
                            await parent._rps_update_streak(guild_id, user_id, new_value=0, increment=False)
                            streak = 0
                        else:
                            # Tie: do not reset or increment streak
                            streak = await parent._rps_get_streak(guild_id, user_id)

                        reward_text = ""
                        cfg = parent.bot.config
                        reward_role_id = cfg.get_int("roles", "rps_streak_role_id")
                        if o == "win" and reward_role_id and streak >= 5 and interaction.guild:
                            role = interaction.guild.get_role(reward_role_id)
                            member = interaction.guild.get_member(user_id)
                            if role and member and role not in member.roles:
                                try:
                                    await member.add_roles(role, reason="RPS 5-win streak reward")
                                    reward_text = f"\n\n🏆 **5-win streak!** You earned **{role.name}**."
                                except Exception:
                                    reward_text = "\n\n🏆 **5-win streak!** (Could not assign the role, permissions/role hierarchy.)"
                            # Reset after awarding so it doesn't award forever
                            await parent._rps_update_streak(guild_id, user_id, new_value=0, increment=False)
                            streak = 0

                        if o == "win":
                            result_line = "You **win**!"
                        elif o == "lose":
                            result_line = "You **lose**!"
                        else:
                            result_line = "It's a **tie**!"

                        content = (
                            f"You chose **{choice}**. I chose **{bot_choice}**. {result_line}"
                            f"\nWin streak: **{streak}**"
                            f"{reward_text}"
                        )

                        await interaction.response.defer()
                        await interaction.message.edit(content=content, view=None)
                    except Exception as e:
                        try:
                            if interaction.response.is_done():
                                await interaction.followup.send("Something went wrong.", ephemeral=True)
                            else:
                                await interaction.response.send_message("Something went wrong.", ephemeral=True)
                        except Exception:
                            pass
                        await log_error(parent.bot, f"RPS view error: {repr(e)}")
                return _cb

        await ctx.respond("Choose:", view=RPSView(ctx.user.id))

    async def _rps_get_streak(self, guild_id: int, user_id: int) -> int:
        """Return current RPS win streak without modifying it."""
        await self.bot.db.connect()
        row = await self.bot.db.fetchone(
            "SELECT streak FROM rps_streaks WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )
        return int(row["streak"]) if row else 0

    async def _rps_update_streak(self, guild_id: int, user_id: int, new_value: Optional[int], increment: bool) -> int:
        """Update and return a user's RPS win streak.

        - If increment=True, increments current streak by 1.
        - If new_value is not None, sets streak to that value (used for reset).
        """
        await self.bot.db.connect()

        if new_value is not None:
            await self.bot.db.execute(
                "INSERT INTO rps_streaks(guild_id,user_id,streak,updated_ts) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET streak=excluded.streak, updated_ts=excluded.updated_ts",
                (guild_id, user_id, int(new_value), int(time.time()))
            )
            return int(new_value)

        row = await self.bot.db.fetchone(
            "SELECT streak FROM rps_streaks WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )
        cur = int(row["streak"]) if row else 0
        cur = cur + 1 if increment else 0
        await self.bot.db.execute(
            "INSERT INTO rps_streaks(guild_id,user_id,streak,updated_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET streak=excluded.streak, updated_ts=excluded.updated_ts",
            (guild_id, user_id, cur, int(time.time()))
        )
        return cur

    # --- /gambling ---
    async def _gambling(self, ctx: discord.ApplicationContext):
        if not self._in_allowed_guild(ctx):
            return await ctx.respond("Wrong server.", ephemeral=True)

        # Anti-spam: hardcoded 10s cooldown per user for /gambling
        now_ts = time.time()
        last_ts = self._gamble_last_ts.get(ctx.user.id, 0.0)
        if now_ts - last_ts < 10.0:
            remaining = int(10 - (now_ts - last_ts) + 0.999)
            return await ctx.respond(f"Slow down... try again in {remaining}s", ephemeral=True)
        self._gamble_last_ts[ctx.user.id] = now_ts
        cfg = self.bot.config
        gcfg = cfg.get("fun", "gambling", default={}) or {}
        emojis = gcfg.get("emojis", ["🍒","🍋","🍇","⭐","💎"])
        interval = float(gcfg.get("spin_interval_seconds", 0.5) or 0.5)
        total = float(gcfg.get("spin_total_seconds", 2.5) or 2.5)
        rare = float(gcfg.get("rare_win_chance", 0.01) or 0.01)
        win_combo = str(gcfg.get("win_combo", "💎💎💎") or "💎💎💎")

        reward_role_id = cfg.get_int("roles", "gambling_reward_role_id") or 0
        role = ctx.guild.get_role(reward_role_id) if reward_role_id else None

        await ctx.respond("Spinning…")
        msg = await ctx.interaction.original_response()

        # animate edits
        steps = max(1, int(total / interval))
        current = ""
        for _ in range(steps):
            current = "".join(random.choice(emojis) for _ in range(3))
            try:
                await msg.edit(content=f"{current}")
            except Exception:
                pass
            await asyncio.sleep(interval)

        # final result
        final = "".join(random.choice(emojis) for _ in range(3))
        won = False
        if random.random() < rare:
            final = win_combo
            won = True

        content = f"🎰 **{final}** 🎰\n"
        if won and role is not None:
            member = ctx.guild.get_member(ctx.user.id)
            if member and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Gambling win")
                    content += f"You hit a rare combo and earned **{role.name}**!"
                except Exception:
                    content += "You hit a rare combo, but I couldn't give the reward role (permissions/role hierarchy)."
            else:
                content += "You hit a rare combo!"
        else:
            content += "No win this time."

        try:
            await msg.edit(content=content)
        except Exception:
            pass

def setup(bot: discord.Bot):
    bot.add_cog(CommandsCog(bot))
