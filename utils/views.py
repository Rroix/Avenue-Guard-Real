from __future__ import annotations

import discord

# Persistent custom_ids (stable across restarts)
CID_TRACK_DECLINE_YES = "tracking_decline_yes"
CID_TRACK_DECLINE_NO = "tracking_decline_no"

CID_TICKET_CLOSE_YES = "ticket_close_yes"
CID_TICKET_CLOSE_NO = "ticket_close_no"

CID_HELP_MENU = "help_menu_select"
CID_HELP_MODCONF_YES = "help_modconfirm_yes"
CID_HELP_MODCONF_NO = "help_modconfirm_no"

CID_TRANSCRIPT_APPROVE = "transcript_approve"
CID_TRANSCRIPT_DENY = "transcript_deny"


class TranscriptRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id=CID_TRANSCRIPT_APPROVE)
    async def approve(self, button: discord.ui.Button, interaction: discord.Interaction):
        cog = interaction.client.get_cog("HelpCog")
        if cog:
            await cog.handle_transcript_request_decision(interaction, approved=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id=CID_TRANSCRIPT_DENY)
    async def deny(self, button: discord.ui.Button, interaction: discord.Interaction):
        cog = interaction.client.get_cog("HelpCog")
        if cog:
            await cog.handle_transcript_request_decision(interaction, approved=False)


class TicketClosePromptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger, custom_id=CID_TICKET_CLOSE_YES)
    async def yes(self, button: discord.ui.Button, interaction: discord.Interaction):
        cog = interaction.client.get_cog("HelpCog")
        if cog:
            await cog.handle_ticket_close_prompt(interaction, confirmed=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, custom_id=CID_TICKET_CLOSE_NO)
    async def no(self, button: discord.ui.Button, interaction: discord.Interaction):
        cog = interaction.client.get_cog("HelpCog")
        if cog:
            await cog.handle_ticket_close_prompt(interaction, confirmed=False)


class HelpModConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, custom_id=CID_HELP_MODCONF_YES)
    async def yes(self, button: discord.ui.Button, interaction: discord.Interaction):
        cog = interaction.client.get_cog("HelpCog")
        if cog:
            await cog.handle_mod_confirm(interaction, confirmed=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, custom_id=CID_HELP_MODCONF_NO)
    async def no(self, button: discord.ui.Button, interaction: discord.Interaction):
        cog = interaction.client.get_cog("HelpCog")
        if cog:
            await cog.handle_mod_confirm(interaction, confirmed=False)


class _HelpMenuSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Contact staff",
                value="mod_contact",
                description="Creates a private channel for you and staff if you have any problems",
            ),
            discord.SelectOption(
                label="FAQ",
                value="faq",
                description="Common questions and answers",
            ),
            discord.SelectOption(
                label="Appeal punishment",
                value="appeal",
                description="Ask staff to lift a punishment such as a ban",
            ),
            discord.SelectOption(
                label="Report a user",
                value="report",
                description="Report in-server harassment, scams, NSFW...",
            ),
            discord.SelectOption(
                label="Report a bot issue",
                value="bot_issue",
                description="Report a bug or broken command the bot has",
            ),
            discord.SelectOption(
                label="Check my weekly status",
                value="weekly_status",
                description="See your current placement and message count so far this week",
            ),
            discord.SelectOption(
                label="Request transcript",
                value="transcript",
                description="Request a transcript of a conversation you had with staff",
            ),
        ]
        super().__init__(
            placeholder="Select what you need help with…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=CID_HELP_MENU,
        )

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("HelpCog")
        if cog:
            await cog.handle_help_selection(interaction, self.values[0])
        else:
            await interaction.response.send_message("Help system is unavailable... Please contact <@1102884420207255653>")


class HelpMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(_HelpMenuSelect())


class TrackingDeclineConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger, custom_id=CID_TRACK_DECLINE_YES)
    async def yes(self, button: discord.ui.Button, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TrackingCog")
        if cog:
            await cog.handle_decline_confirm(interaction, confirmed=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, custom_id=CID_TRACK_DECLINE_NO)
    async def no(self, button: discord.ui.Button, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TrackingCog")
        if cog:
            await cog.handle_decline_confirm(interaction, confirmed=False)
