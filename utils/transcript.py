from __future__ import annotations

import io
from datetime import datetime
from typing import Optional
import discord

async def build_text_transcript(channel: discord.TextChannel, limit: int = 2000) -> io.BytesIO:
    lines = []
    async for msg in channel.history(limit=limit, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{msg.author} ({msg.author.id})"
        content = msg.content or ""
        if msg.attachments:
            att = " ".join(a.url for a in msg.attachments)
            content = (content + " " + att).strip()
        lines.append(f"[{ts}] {author}: {content}")
    data = "\n".join(lines).encode("utf-8", errors="replace")
    bio = io.BytesIO(data)
    bio.seek(0)
    return bio
