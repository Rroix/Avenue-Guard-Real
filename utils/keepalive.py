from __future__ import annotations

import os
import asyncio
from aiohttp import web

async def _handle(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_keepalive() -> None:
    app = web.Application()
    app.router.add_get("/", _handle)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
