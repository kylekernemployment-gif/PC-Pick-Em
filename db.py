"""Storage for PC Pick Em'.

Uses Postgres when DATABASE_URL is set (recommended on Render, since the free
web service disk is wiped on every restart/deploy), otherwise a local SQLite
file. All queries are written with `?` placeholders and work on both.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from typing import Any, Callable

GAME_COLS = [
    "game_id", "tip_utc", "away_tri", "away_name", "away_record",
    "home_tri", "home_name", "home_record", "postponed", "time_tbd",
    "status", "channel_id", "message_id", "away_score", "home_score", "winner",
]

# Game status lifecycle: scheduled -> open (poll posted) -> locked (tip-off)
# -> final (points awarded).  "void" = postponed/cancelled, no points.
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS games (
        game_id      TEXT PRIMARY KEY,
        tip_utc      BIGINT NOT NULL,
        away_tri     TEXT NOT NULL,
        away_name    TEXT NOT NULL,
        away_record  TEXT,
        home_tri     TEXT NOT NULL,
        home_name    TEXT NOT NULL,
        home_record  TEXT,
        postponed    INTEGER NOT NULL DEFAULT 0,
        time_tbd     INTEGER NOT NULL DEFAULT 0,
        status       TEXT NOT NULL DEFAULT 'scheduled',
        channel_id   BIGINT,
        message_id   BIGINT,
        away_score   INTEGER,
        home_score   INTEGER,
        winner       INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS games_status_idx ON games (status, tip_utc)",
    "CREATE INDEX IF NOT EXISTS games_message_idx ON games (message_id)",
    """CREATE TABLE IF NOT EXISTS picks (
        game_id  TEXT NOT NULL,
        user_id  BIGINT NOT NULL,
        choice   INTEGER NOT NULL,
        correct  INTEGER,
        PRIMARY KEY (game_id, user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS picks_user_idx ON picks (user_id)",
    """CREATE TABLE IF NOT EXISTS users (
        user_id  BIGINT PRIMARY KEY,
        points   BIGINT NOT NULL DEFAULT 0
    )""",
]


class Database:
    def __init__(self, url: str | None = None, path: str = "pickem.db"):
        self.url = url
        self.path = path
        self.pg = bool(url)
        self._lock = threading.Lock()

    # -- plumbing ---------------------------------------------------------
    def _connect(self):
        if self.pg:
            import psycopg

            return psycopg.connect(self.url, connect_timeout=15)
        return sqlite3.connect(self.path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def _run(self, fn: Callable[[Callable], Any]) -> Any:
        with self._lock:
            conn = self._connect()
            try:
                def ex(sql: str, params: tuple = ()):
                    return conn.execute(self._sql(sql), params)

                result = fn(ex)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def run(self, fn: Callable[[Callable], Any]) -> Any:
        return await asyncio.to_thread(self._run, fn)

    @staticmethod
    def _game(row) -> dict | None:
        return dict(zip(GAME_COLS, row)) if row else None

    def _games(self, ex, where: str, params: tuple = ()) -> list[dict]:
        rows = ex(f"SELECT {', '.join(GAME_COLS)} FROM games WHERE {where} ORDER BY tip_utc", params).fetchall()
        return [self._game(r) for r in rows]

    async def init(self) -> None:
        def fn(ex):
            for stmt in SCHEMA:
                ex(stmt)
        await self.run(fn)

    # -- games ------------------------------------------------------------
    async def upsert_games(self, games: list) -> None:
        """Insert new games; refresh details of games whose poll hasn't closed."""
        def fn(ex):
            for g in games:
                ex(
                    """INSERT INTO games (game_id, tip_utc, away_tri, away_name, away_record,
                                          home_tri, home_name, home_record, postponed, time_tbd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (game_id) DO UPDATE SET
                           tip_utc = excluded.tip_utc,
                           away_tri = excluded.away_tri, away_name = excluded.away_name,
                           away_record = excluded.away_record,
                           home_tri = excluded.home_tri, home_name = excluded.home_name,
                           home_record = excluded.home_record,
                           postponed = excluded.postponed, time_tbd = excluded.time_tbd
                       WHERE games.status IN ('scheduled', 'open')""",
                    (g.game_id, g.tip_utc, g.away_tri, g.away_name, g.away_record,
                     g.home_tri, g.home_name, g.home_record, int(g.postponed), int(g.time_tbd)),
                )
                # Postponed/suspended after tip-off: remember it so the game can be voided.
                if g.postponed:
                    ex("UPDATE games SET postponed = 1 WHERE game_id = ? AND status = 'locked'", (g.game_id,))
        await self.run(fn)

    async def get_game(self, game_id: str) -> dict | None:
        return await self.run(lambda ex: self._game(ex(
            f"SELECT {', '.join(GAME_COLS)} FROM games WHERE game_id = ?", (game_id,)).fetchone()))

    async def game_by_message(self, message_id: int) -> dict | None:
        return await self.run(lambda ex: self._game(ex(
            f"SELECT {', '.join(GAME_COLS)} FROM games WHERE message_id = ?", (message_id,)).fetchone()))

    async def games_to_post(self, now: int, window: int) -> list[dict]:
        return await self.run(lambda ex: self._games(
            ex, "status = 'scheduled' AND postponed = 0 AND time_tbd = 0 AND tip_utc - ? <= ? AND tip_utc > ?",
            (window, now, now + 60)))

    async def games_with_status(self, status: str) -> list[dict]:
        return await self.run(lambda ex: self._games(ex, "status = ?", (status,)))

    async def mark_posted(self, game_id: str, channel_id: int, message_id: int) -> None:
        await self.run(lambda ex: ex(
            "UPDATE games SET status = 'open', channel_id = ?, message_id = ? WHERE game_id = ?",
            (channel_id, message_id, game_id)))

    async def reopen_later(self, game_id: str) -> None:
        """Cancel an open poll (game postponed) so it gets reposted once rescheduled."""
        def fn(ex):
            ex("DELETE FROM picks WHERE game_id = ?", (game_id,))
            ex("UPDATE games SET status = 'scheduled', channel_id = NULL, message_id = NULL "
               "WHERE game_id = ? AND status = 'open'", (game_id,))
        await self.run(fn)

    async def lock_game(self, game_id: str, picks: dict[int, int] | None) -> None:
        """Close the poll. If `picks` is given it replaces the stored picks."""
        def fn(ex):
            if picks is not None:
                ex("DELETE FROM picks WHERE game_id = ?", (game_id,))
                for user_id, choice in picks.items():
                    ex("INSERT INTO picks (game_id, user_id, choice) VALUES (?, ?, ?)",
                       (game_id, user_id, choice))
            ex("UPDATE games SET status = 'locked' WHERE game_id = ? AND status = 'open'", (game_id,))
        await self.run(fn)

    async def pick_count(self, game_id: str) -> int:
        return await self.run(lambda ex: ex(
            "SELECT COUNT(*) FROM picks WHERE game_id = ?", (game_id,)).fetchone()[0])

    async def resolve_game(self, game_id: str, away_score: int, home_score: int,
                           winner: int, points: int) -> tuple[list[int], int] | None:
        """Grade picks and award points. Returns (winning user ids, total picks),
        or None if the game was already resolved (so points are never paid twice)."""
        def fn(ex):
            cur = ex("UPDATE games SET status = 'final', away_score = ?, home_score = ?, winner = ? "
                     "WHERE game_id = ? AND status = 'locked'",
                     (away_score, home_score, winner, game_id))
            if cur.rowcount != 1:
                return None
            ex("UPDATE picks SET correct = CASE WHEN choice = ? THEN 1 ELSE 0 END WHERE game_id = ?",
               (winner, game_id))
            winners = [r[0] for r in ex(
                "SELECT user_id FROM picks WHERE game_id = ? AND correct = 1", (game_id,)).fetchall()]
            total = ex("SELECT COUNT(*) FROM picks WHERE game_id = ?", (game_id,)).fetchone()[0]
            for uid in winners:
                ex("INSERT INTO users (user_id, points) VALUES (?, ?) "
                   "ON CONFLICT (user_id) DO UPDATE SET points = users.points + excluded.points",
                   (uid, points))
            return winners, total
        return await self.run(fn)

    async def void_game(self, game_id: str) -> bool:
        def fn(ex):
            cur = ex("UPDATE games SET status = 'void' WHERE game_id = ? AND status IN ('open', 'locked')",
                     (game_id,))
            if cur.rowcount == 1:
                ex("DELETE FROM picks WHERE game_id = ?", (game_id,))
                return True
            return False
        return await self.run(fn)

    # -- picks ------------------------------------------------------------
    async def get_pick(self, game_id: str, user_id: int) -> int | None:
        row = await self.run(lambda ex: ex(
            "SELECT choice FROM picks WHERE game_id = ? AND user_id = ?", (game_id, user_id)).fetchone())
        return row[0] if row else None

    async def set_pick(self, game_id: str, user_id: int, choice: int) -> None:
        await self.run(lambda ex: ex(
            "INSERT INTO picks (game_id, user_id, choice) VALUES (?, ?, ?) "
            "ON CONFLICT (game_id, user_id) DO UPDATE SET choice = excluded.choice",
            (game_id, user_id, choice)))

    async def delete_pick(self, game_id: str, user_id: int, choice: int) -> None:
        await self.run(lambda ex: ex(
            "DELETE FROM picks WHERE game_id = ? AND user_id = ? AND choice = ?", (game_id, user_id, choice)))

    # -- points & stats ---------------------------------------------------
    async def add_points(self, user_id: int, amount: int) -> int:
        def fn(ex):
            ex("INSERT INTO users (user_id, points) VALUES (?, ?) "
               "ON CONFLICT (user_id) DO UPDATE SET points = users.points + excluded.points",
               (user_id, amount))
            return ex("SELECT points FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
        return int(await self.run(fn))

    async def get_points(self, user_id: int) -> tuple[int, int | None]:
        """Returns (points, rank). Rank is None if the user has no points."""
        def fn(ex):
            row = ex("SELECT points FROM users WHERE user_id = ?", (user_id,)).fetchone()
            points = int(row[0]) if row else 0
            if points <= 0:
                return points, None
            higher = ex("SELECT COUNT(*) FROM users WHERE points > ?", (points,)).fetchone()[0]
            return points, int(higher) + 1
        return await self.run(fn)

    async def leaderboard(self, limit: int = 10) -> list[tuple[int, int]]:
        rows = await self.run(lambda ex: ex(
            "SELECT user_id, points FROM users WHERE points > 0 ORDER BY points DESC, user_id LIMIT ?",
            (limit,)).fetchall())
        return [(int(u), int(p)) for u, p in rows]

    async def record(self, user_id: int) -> tuple[int, int]:
        row = await self.run(lambda ex: ex(
            "SELECT COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0), "
            "       COALESCE(SUM(CASE WHEN correct = 0 THEN 1 ELSE 0 END), 0) "
            "FROM picks WHERE user_id = ? AND correct IS NOT NULL", (user_id,)).fetchone())
        return int(row[0]), int(row[1])
