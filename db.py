import os
from datetime import datetime, timezone
from typing import Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)


async def run_schema():
    """Auto-applies schema.sql on startup so first deploy needs zero manual SQL."""
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r") as f:
        sql = f.read()
    async with _pool.acquire() as conn:
        await conn.execute(sql)


async def get_or_create_user(telegram_id: int, username: str | None, first_name: str | None):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)
        if row:
            await conn.execute(
                "UPDATE users SET username=$2, first_name=$3 WHERE telegram_id=$1",
                telegram_id, username, first_name,
            )
            return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)
        return await conn.fetchrow(
            """INSERT INTO users (telegram_id, username, first_name)
               VALUES ($1, $2, $3) RETURNING *""",
            telegram_id, username, first_name,
        )


async def get_user(telegram_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)


async def apply_growth(
    telegram_id: int,
    amount: int,
    log_type: str,
    set_last_grow: bool = False,
    clamp_zero: bool = False,
) -> int:
    """
    Adjusts a user's height and logs the change atomically.
    clamp_zero=True means a negative result is floored at 0 (used for attack losses).
    Loan repayment deliberately does NOT clamp, so it can push a user to -10cm.
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", telegram_id
            )
            current = row["height_cm"]
            new_height = current + amount
            actual_amount = amount

            if clamp_zero and new_height < 0:
                actual_amount = -current
                new_height = 0

            if set_last_grow:
                await conn.execute(
                    "UPDATE users SET height_cm=$1, last_grow=$2 WHERE telegram_id=$3",
                    new_height, datetime.now(timezone.utc), telegram_id,
                )
            else:
                await conn.execute(
                    "UPDATE users SET height_cm=$1 WHERE telegram_id=$2",
                    new_height, telegram_id,
                )

            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, $3)",
                telegram_id, actual_amount, log_type,
            )
            return new_height


async def create_challenge(chat_id: int, challenger_id: int, amount: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO challenges (chat_id, challenger_id, amount)
               VALUES ($1, $2, $3) RETURNING id""",
            chat_id, challenger_id, amount,
        )
        return row["id"]


async def set_challenge_message(challenge_id: int, message_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenges SET message_id=$1 WHERE id=$2", message_id, challenge_id
        )


async def get_challenge(challenge_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM challenges WHERE id=$1", challenge_id)


async def resolve_challenge(challenge_id: int, winner_id: int, loser_id: int) -> bool:
    """
    Atomically claims a challenge. Returns False if it was already resolved by
    someone else (protects against two people tapping the button at once).
    """
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE challenges
               SET status='resolved', winner_id=$1, loser_id=$2, resolved_at=now()
               WHERE id=$3 AND status='open'
               RETURNING id""",
            winner_id, loser_id, challenge_id,
        )
        return row is not None


async def create_loan(telegram_id: int, amount: int):
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE users
                   SET loan_active=true, loan_amount=$1, loan_taken_at=now()
                   WHERE telegram_id=$2""",
                amount, telegram_id,
            )
            await conn.execute(
                "UPDATE users SET height_cm = height_cm + $1 WHERE telegram_id=$2",
                amount, telegram_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'loan')",
                telegram_id, amount,
            )


async def get_due_loans(duration_hours: int):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT telegram_id FROM users
               WHERE loan_active=true
               AND loan_taken_at <= now() - ($1 || ' hours')::interval""",
            str(duration_hours),
        )


async def clear_loan(telegram_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            """UPDATE users SET loan_active=false, loan_amount=0, loan_taken_at=NULL
               WHERE telegram_id=$1""",
            telegram_id,
        )


async def get_leaderboard_today(limit: int = 5):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT u.telegram_id, u.first_name, u.username, SUM(g.amount) AS gained
               FROM growth_log g
               JOIN users u ON u.telegram_id = g.telegram_id
               WHERE g.created_at >= date_trunc('day', now())
               GROUP BY u.telegram_id, u.first_name, u.username
               ORDER BY gained DESC
               LIMIT $1""",
            limit,
        )


async def get_leaderboard_alltime(limit: int = 5):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT telegram_id, first_name, username, height_cm
               FROM users
               ORDER BY height_cm DESC
               LIMIT $1""",
            limit,
        )
