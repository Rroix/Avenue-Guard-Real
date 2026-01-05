from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence


class Database:
    """Small SQLite wrapper safe to use from an async bot.

    - Uses a single connection opened with check_same_thread=False
    - Serializes all operations with an asyncio.Lock
    - Executes each query fully inside one to_thread call to avoid cursor/thread mismatches
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    async def connect(self) -> None:
        async with self._lock:
            if self._conn is not None:
                return

            def _open():
                conn = sqlite3.connect(str(self.path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA foreign_keys=ON;")
                conn.commit()
                return conn

            self._conn = await asyncio.to_thread(_open)

        await self._migrate()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is None:
                return

            def _close():
                assert self._conn is not None
                self._conn.close()

            await asyncio.to_thread(_close)
            self._conn = None

    async def _migrate(self) -> None:
        # Create base tables first
        stmts = [
            """CREATE TABLE IF NOT EXISTS activity_counts(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, week_start)
            );""",
            """CREATE TABLE IF NOT EXISTS activity_last_counted(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_counted_ts INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );""",
            """CREATE TABLE IF NOT EXISTS weekly_claims(
                guild_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                status TEXT NOT NULL,
                contacted_ts INTEGER NOT NULL,
                PRIMARY KEY (guild_id, week_start, user_id)
            );""",
            """CREATE TABLE IF NOT EXISTS weekly_sessions(
                guild_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                expires_ts INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, week_start, user_id)
            );""",
            """CREATE TABLE IF NOT EXISTS weekly_dm_log(
    guild_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    ts INTEGER NOT NULL
);""",
"""CREATE TABLE IF NOT EXISTS weekly_reminders(
    guild_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    reminded_ts INTEGER NOT NULL,
    PRIMARY KEY (guild_id, week_start, user_id)
);""",
"""CREATE TABLE IF NOT EXISTS weekly_runs(
    guild_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    ran_ts INTEGER NOT NULL,
    PRIMARY KEY (guild_id, week_start)
);""",
"""CREATE TABLE IF NOT EXISTS tickets(
                guild_id INTEGER NOT NULL,
                channel_id INTEGER PRIMARY KEY,
                creator_id INTEGER NOT NULL,
                created_ts INTEGER NOT NULL,
                last_user_activity_ts INTEGER NOT NULL,
                status TEXT NOT NULL,
                ticket_id INTEGER
            );""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_ticket_id
                ON tickets(guild_id, ticket_id) WHERE ticket_id IS NOT NULL;""",
            """CREATE TABLE IF NOT EXISTS ticket_sequences(
                guild_id INTEGER PRIMARY KEY,
                next_ticket_id INTEGER NOT NULL
            );""",
            """CREATE TABLE IF NOT EXISTS ticket_transcripts(
                guild_id INTEGER NOT NULL,
                ticket_id INTEGER NOT NULL,
                log_channel_id INTEGER NOT NULL,
                log_message_id INTEGER NOT NULL,
                created_ts INTEGER NOT NULL,
                PRIMARY KEY (guild_id, ticket_id)
            );""",
            """CREATE TABLE IF NOT EXISTS ticket_cooldowns(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_created_ts INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );""",
            """CREATE TABLE IF NOT EXISTS sticky_state(
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                last_sticky_message_id INTEGER,
                PRIMARY KEY (guild_id, channel_id)
            );""",
            """CREATE TABLE IF NOT EXISTS help_sessions(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                created_ts INTEGER NOT NULL,
                data_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (guild_id, user_id)
            );""",
            """CREATE TABLE IF NOT EXISTS help_cooldowns(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                last_used_ts INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, action)
            );""",
            """CREATE TABLE IF NOT EXISTS transcript_requests(
                guild_id INTEGER NOT NULL,
                request_message_id INTEGER PRIMARY KEY,
                ticket_channel_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_ts INTEGER NOT NULL,
                ticket_id INTEGER
            );""",
            """CREATE TABLE IF NOT EXISTS rps_streaks(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                streak INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );""",
        ]
        for s in stmts:
            await self.execute(s)

        # Ensure columns exist on older DBs
        await self._ensure_column("tickets", "ticket_id", "INTEGER")
        await self._ensure_column("transcript_requests", "ticket_id", "INTEGER")

        # Ensure sequence exists (set next_ticket_id based on max ticket_id)
        async with self._lock:
            assert self._conn is not None

            def _init_seq():
                cur = self._conn.execute("SELECT MAX(ticket_id) AS m FROM tickets")
                row = cur.fetchone()
                max_id = int(row["m"]) if row and row["m"] is not None else 0
                # if sequence row missing, create it
                for gid_row in self._conn.execute("SELECT DISTINCT guild_id FROM tickets"):
                    gid = int(gid_row["guild_id"])
                    cur2 = self._conn.execute("SELECT next_ticket_id FROM ticket_sequences WHERE guild_id=?", (gid,))
                    if cur2.fetchone() is None:
                        self._conn.execute(
                            "INSERT INTO ticket_sequences(guild_id, next_ticket_id) VALUES(?,?)",
                            (gid, max_id + 1 if max_id > 0 else 1)
                        )
                self._conn.commit()

            await asyncio.to_thread(_init_seq)

    async def _ensure_column(self, table: str, column: str, coltype: str) -> None:
        await self.connect()
        async with self._lock:
            assert self._conn is not None

            def _run():
                assert self._conn is not None
                info = list(self._conn.execute(f"PRAGMA table_info({table})"))
                cols = {r["name"] for r in info}
                if column in cols:
                    return
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                    self._conn.commit()
                except Exception:
                    # ignore if cannot alter
                    pass

            await asyncio.to_thread(_run)

    async def next_ticket_id(self, guild_id: int) -> int:
        await self.connect()
        async with self._lock:
            assert self._conn is not None

            def _run():
                assert self._conn is not None
                cur = self._conn.execute("SELECT next_ticket_id FROM ticket_sequences WHERE guild_id=?", (guild_id,))
                row = cur.fetchone()
                if row is None:
                    next_id = 1
                    self._conn.execute("INSERT INTO ticket_sequences(guild_id, next_ticket_id) VALUES(?,?)", (guild_id, 2))
                    self._conn.commit()
                    return next_id
                next_id = int(row["next_ticket_id"])
                self._conn.execute("UPDATE ticket_sequences SET next_ticket_id=? WHERE guild_id=?", (next_id + 1, guild_id))
                self._conn.commit()
                return next_id

            return await asyncio.to_thread(_run)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self.connect()
        async with self._lock:
            assert self._conn is not None

            def _run():
                assert self._conn is not None
                self._conn.execute(sql, params)
                self._conn.commit()

            await asyncio.to_thread(_run)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        await self.connect()
        items = list(seq)
        async with self._lock:
            assert self._conn is not None

            def _run():
                assert self._conn is not None
                self._conn.executemany(sql, items)
                self._conn.commit()

            await asyncio.to_thread(_run)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        await self.connect()
        async with self._lock:
            assert self._conn is not None

            def _run():
                assert self._conn is not None
                cur = self._conn.execute(sql, params)
                return cur.fetchone()

            return await asyncio.to_thread(_run)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        await self.connect()
        async with self._lock:
            assert self._conn is not None

            def _run():
                assert self._conn is not None
                cur = self._conn.execute(sql, params)
                return list(cur.fetchall())

            return await asyncio.to_thread(_run)
