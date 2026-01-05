from __future__ import annotations

import os
import discord

from utils.config import Config
from utils.db import Database
from utils.keepalive import start_keepalive
from utils.errors import setup_global_error_handlers, log_error
from utils.views import TrackingDeclineConfirmView, TicketClosePromptView, HelpMenuView, HelpModConfirmView, TranscriptRequestView
from utils.checks import ensure_allowed_guild_id

DB_PATH = "data/bot.db"

def create_bot() -> discord.Bot:
    intents = discord.Intents.all()
    bot = discord.Bot(intents=intents)

    bot.config = Config("config.json")
    bot.db = Database(DB_PATH)

    setup_global_error_handlers(bot)

    async def _load_cogs():
        bot.load_extension("cogs.Mod")
        bot.load_extension("cogs.Tracking")
        bot.load_extension("cogs.Help")
        bot.load_extension("cogs.MessageResponses")
        bot.load_extension("cogs.Sticky")
        bot.load_extension("cogs.Commands")
        bot.load_extension("cogs.Background")

    @bot.event
    async def on_ready():
        try:
            await bot.db.connect()
        except Exception:
            pass

        # Ensure only in allowed guild
        allowed = bot.config.get_int("guild", "allowed_guild_id")
        if allowed:
            g = bot.get_guild(allowed)
            if g is None:
                await log_error(bot, f"Bot is not in allowed guild_id={allowed}. Shutting down.")
                await bot.close()
                return

        # Start keepalive server
        try:
            # start once
            if not getattr(bot, "_keepalive_started", False):
                bot._keepalive_started = True
                bot.loop.create_task(start_keepalive())
        except Exception:
            pass

        # Start background tasks in cogs
        tracking = bot.get_cog("TrackingCog")
        if tracking:
            await tracking.start_background()

        helpcog = bot.get_cog("HelpCog")
        if helpcog:
            await helpcog.start_background()
        
        bgcog = bot.get_cog("BackgroundCog")
        if bgcog:
            await bgcog.start_background()

        # Register persistent views (for interactions to survive restarts)
        await bot.register_persistent_views()

        print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    async def register_persistent_views():
        # It's okay to add multiple times; discord.py ignores duplicates by custom_id mapping.
        bot.add_view(TrackingDeclineConfirmView())
        bot.add_view(TicketClosePromptView())
        bot.add_view(HelpMenuView())
        bot.add_view(HelpModConfirmView())
        bot.add_view(TranscriptRequestView())

    bot.register_persistent_views = register_persistent_views

    bot.loop.create_task(_load_cogs())
    return bot

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable is missing.")
    bot = create_bot()
    bot.run(token)
