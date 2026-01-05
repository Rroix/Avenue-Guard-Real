from __future__ import annotations

from typing import Iterable, Optional, List
import discord

def member_has_any_role(member: discord.Member, role_ids: Iterable[int]) -> bool:
    ids = set(role_ids)
    return any(r.id in ids for r in getattr(member, "roles", []))

def is_admin_or_owner(member: discord.Member, admin_role_ids: List[int]) -> bool:
    return member_has_any_role(member, admin_role_ids) or member.guild_permissions.administrator

def is_mod(member: discord.Member, mod_role_id: int) -> bool:
    return any(r.id == mod_role_id for r in getattr(member, "roles", [])) or member.guild_permissions.manage_guild

def ensure_allowed_guild_id(guild: Optional[discord.Guild], allowed_guild_id: int) -> bool:
    return guild is not None and guild.id == allowed_guild_id

def basic_color(name: str) -> discord.Color:
    n = (name or "").strip().lower()
    mapping = {
        "blue": discord.Color.blue(),
        "red": discord.Color.red(),
        "green": discord.Color.green(),
        "purple": discord.Color.purple(),
        "gold": discord.Color.gold(),
        "orange": discord.Color.orange(),
        "teal": discord.Color.teal(),
        "blurple": discord.Color.blurple(),
        "dark": discord.Color.dark_grey(),
        "light": discord.Color.light_grey(),
    }
    if n in mapping:
        return mapping[n]
    if n.startswith("#") and len(n) in (7, 9):
        try:
            return discord.Color(int(n.lstrip("#"), 16))
        except Exception:
            return discord.Color.default()
    return discord.Color.default()
