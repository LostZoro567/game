import os
import random
from datetime import datetime, timedelta, timezone
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


async def get_user_by_username(username: str):
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE lower(username)=lower($1)", username
        )


async def record_chat_member(chat_id: int, telegram_id: int):
    """Called on every command so we know who's actually active in a given group."""
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO chat_members (chat_id, telegram_id)
               VALUES ($1, $2) ON CONFLICT DO NOTHING""",
            chat_id, telegram_id,
        )


async def get_chat_member_ids(chat_id: int, exclude: int | None = None):
    async with _pool.acquire() as conn:
        if exclude is not None:
            rows = await conn.fetch(
                "SELECT telegram_id FROM chat_members WHERE chat_id=$1 AND telegram_id != $2",
                chat_id, exclude,
            )
        else:
            rows = await conn.fetch(
                "SELECT telegram_id FROM chat_members WHERE chat_id=$1", chat_id
            )
        return [r["telegram_id"] for r in rows]


async def apply_growth(
    telegram_id: int,
    amount: int,
    log_type: str,
    set_last_grow: bool = False,
    clamp_zero: bool = False,
    floor: int | None = None,
) -> int:
    """
    Adjusts a user's height and logs the change atomically.
    clamp_zero=True means a negative result is floored at 0 (used for attack losses).
    floor, if set, caps how far a negative change can push the user down
    (used for loan repayment, which can push a user to -10cm but no further,
    regardless of what they did between taking the loan and it coming due).
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
            elif floor is not None and new_height < floor:
                actual_amount = floor - current
                new_height = floor

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


async def set_last_pray(telegram_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_pray=now() WHERE telegram_id=$1", telegram_id
        )


async def transfer_cm(from_id: int, to_id: int, amount: int, type_out: str, type_in: str) -> bool:
    """
    Atomically moves cm from one user to another. Returns False if the sender
    doesn't actually have enough (caller should already have checked, but this
    is the last line of defense against races).
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            sender = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", from_id
            )
            if sender["height_cm"] < amount:
                return False

            await conn.execute(
                "UPDATE users SET height_cm = height_cm - $1 WHERE telegram_id=$2",
                amount, from_id,
            )
            await conn.execute(
                "UPDATE users SET height_cm = height_cm + $1 WHERE telegram_id=$2",
                amount, to_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, $3)",
                from_id, -amount, type_out,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, $3)",
                to_id, amount, type_in,
            )
            return True


async def set_last_simp(telegram_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_simp=now() WHERE telegram_id=$1", telegram_id
        )


async def cast_hex(caster_id: int, target_id: int, cost: int, duration_hours: int) -> bool:
    """
    Charges the caster and arms a debuff on the target's next /grow.
    Returns False if the caster can't afford it.
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            caster = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", caster_id
            )
            if caster["height_cm"] < cost:
                return False

            await conn.execute(
                "UPDATE users SET height_cm = height_cm - $1 WHERE telegram_id=$2",
                cost, caster_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'hex_cast')",
                caster_id, -cost,
            )
            expires = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
            await conn.execute(
                """UPDATE users SET hex_active=true, hex_expires_at=$1
                   WHERE telegram_id=$2""",
                expires, target_id,
            )
            return True


async def set_last_hex_cast(telegram_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_hex_cast=now() WHERE telegram_id=$1", telegram_id
        )


async def consume_hex_if_active(telegram_id: int) -> bool:
    """
    Checks + clears a user's hex right before a /grow roll. Returns True if a
    live (non-expired) hex was consumed, meaning the caller should halve the roll.
    Always clears the flag either way (expired hexes just fizzle silently).
    """
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hex_active, hex_expires_at FROM users WHERE telegram_id=$1", telegram_id
        )
        if not row["hex_active"]:
            return False
        await conn.execute(
            "UPDATE users SET hex_active=false, hex_expires_at=NULL WHERE telegram_id=$1",
            telegram_id,
        )
        if row["hex_expires_at"] and row["hex_expires_at"] > datetime.now(timezone.utc):
            return True
        return False


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


async def buy_condom(telegram_id: int, cost: int, duration_hours: int) -> bool:
    """Charges the user and extends their condom protection. False if broke."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", telegram_id
            )
            if row["height_cm"] < cost:
                return False
            await conn.execute(
                "UPDATE users SET height_cm = height_cm - $1 WHERE telegram_id=$2",
                cost, telegram_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'condom')",
                telegram_id, -cost,
            )
            until = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
            await conn.execute(
                "UPDATE users SET condom_until=$1 WHERE telegram_id=$2", until, telegram_id
            )
            return True


async def set_last_snitch(telegram_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_snitch=now() WHERE telegram_id=$1", telegram_id
        )


async def snitch_steal(actor_id: int, target_id: int, min_pct: int, max_pct: int):
    """
    Steals a random min_pct-max_pct slice of the target's current height.
    Returns the stolen amount (0 if target had nothing to take).
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", target_id
            )
            if target["height_cm"] <= 0:
                return 0
            pct = random.randint(min_pct, max_pct)
            amount = max(1, (target["height_cm"] * pct) // 100)
            amount = min(amount, target["height_cm"])

            await conn.execute(
                "UPDATE users SET height_cm = height_cm - $1 WHERE telegram_id=$2",
                amount, target_id,
            )
            await conn.execute(
                "UPDATE users SET height_cm = height_cm + $1 WHERE telegram_id=$2",
                amount, actor_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'snitched_from')",
                target_id, -amount,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'snitch_gain')",
                actor_id, amount,
            )
            return amount


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


async def get_last_group_curse(chat_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            """SELECT * FROM group_curses WHERE chat_id=$1
               ORDER BY started_at DESC LIMIT 1""",
            chat_id,
        )


async def create_group_curse(
    chat_id: int,
    initiator_id: int,
    victim_ids: list[int],
    window_hours: int,
    dmg_min: int,
    dmg_max: int,
) -> int:
    """
    Creates the curse row plus one curse_hit per victim, each fired at a random
    moment inside the curse window. The initiator is always included and flagged.
    """
    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(hours=window_hours)
    async with _pool.acquire() as conn:
        async with conn.transaction():
            curse_row = await conn.fetchrow(
                """INSERT INTO group_curses (chat_id, initiator_id, ends_at)
                   VALUES ($1, $2, $3) RETURNING id""",
                chat_id, initiator_id, ends_at,
            )
            curse_id = curse_row["id"]

            for victim_id in victim_ids:
                amount = random.randint(dmg_min, dmg_max)
                offset_seconds = random.randint(0, window_hours * 3600)
                scheduled_at = now + timedelta(seconds=offset_seconds)
                await conn.execute(
                    """INSERT INTO curse_hits
                       (curse_id, telegram_id, amount, is_initiator, scheduled_at)
                       VALUES ($1, $2, $3, $4, $5)""",
                    curse_id, victim_id, amount, victim_id == initiator_id, scheduled_at,
                )
            return curse_id


async def get_due_curse_hits():
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT ch.id, ch.telegram_id, ch.amount, ch.is_initiator, gc.chat_id
               FROM curse_hits ch
               JOIN group_curses gc ON gc.id = ch.curse_id
               WHERE ch.applied=false AND ch.scheduled_at <= now()"""
        )


async def apply_curse_hit(hit_id: int, telegram_id: int, amount: int) -> int:
    # Mark applied first so a crash mid-flight can't double-fire this hit.
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE curse_hits SET applied=true WHERE id=$1", hit_id)
    return await apply_growth(telegram_id, -amount, "cursed", clamp_zero=True)


async def get_leaderboard_alltime(limit: int = 5):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT telegram_id, first_name, username, height_cm
               FROM users
               ORDER BY height_cm DESC
               LIMIT $1""",
            limit,
        )


async def set_last_gamble(telegram_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_gamble=now() WHERE telegram_id=$1", telegram_id
        )


async def gamble_win(telegram_id: int, amount: int) -> int:
    return await apply_growth(telegram_id, amount, "gamble_win")


async def gamble_lose_become_pussy(telegram_id: int, chat_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            """UPDATE users
               SET pussy_active=true, pussy_started_at=now(), pussy_accum=0,
                   pussy_chat_id=$1, pussy_hour2_announced=false
               WHERE telegram_id=$2""",
            chat_id, telegram_id,
        )


async def get_pussy_status(telegram_id: int):
    """Returns the row if the user is currently an active (non-expired) pussy, else None."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id=$1 AND pussy_active=true", telegram_id
        )
        return row


async def record_fuck(target_id: int, actor_id: int, hour_window: int) -> bool:
    """Returns False if this actor already fucked this target in this hour window."""
    async with _pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO fuck_log (target_id, actor_id, hour_window)
                   VALUES ($1, $2, $3)""",
                target_id, actor_id, hour_window,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False


async def apply_fuck_gain(actor_id: int, target_id: int, actor_gain: int, pussy_gain: int):
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET height_cm = height_cm + $1 WHERE telegram_id=$2",
                actor_gain, actor_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'fuck_gain')",
                actor_id, actor_gain,
            )
            await conn.execute(
                "UPDATE users SET pussy_accum = pussy_accum + $1 WHERE telegram_id=$2",
                pussy_gain, target_id,
            )


async def get_pussies_needing_hour2_announcement():
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT telegram_id, pussy_chat_id FROM users
               WHERE pussy_active=true AND pussy_hour2_announced=false
               AND pussy_started_at <= now() - interval '1 hour'"""
        )


async def mark_hour2_announced(telegram_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET pussy_hour2_announced=true WHERE telegram_id=$1", telegram_id
        )


async def get_expired_pussies(duration_hours: int):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT telegram_id, pussy_chat_id, pussy_accum FROM users
               WHERE pussy_active=true
               AND pussy_started_at <= now() - ($1 || ' hours')::interval""",
            str(duration_hours),
        )


async def finalize_pussy(telegram_id: int, bonus: int) -> int:
    """Adds the accumulated bonus onto the real height and clears pussy status."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE users
                   SET pussy_active=false, pussy_started_at=NULL, pussy_accum=0,
                       pussy_chat_id=NULL, pussy_hour2_announced=false
                   WHERE telegram_id=$1""",
                telegram_id,
            )
    if bonus > 0:
        return await apply_growth(telegram_id, bonus, "pussy_bonus")
    return (await get_user(telegram_id))["height_cm"]
