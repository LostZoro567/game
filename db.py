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


# ---------------------------------------------------------------
# Users
# ---------------------------------------------------------------

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
    """Case-insensitive lookup. Only finds users the bot has already seen."""
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE lower(username)=lower($1)", username.lstrip("@")
        )


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
    clamp_zero=True means a negative result is floored at 0 (used for attack losses,
    snitch losses, curse losses).
    floor, if set, caps how far a negative change can push the user down
    (used for loan repayment, which can push a user to -10cm but no further).
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


async def set_last_grow_date(telegram_id: int, today):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_grow_date=$1 WHERE telegram_id=$2", today, telegram_id
        )


async def clear_ed(telegram_id: int):
    """Consumes the ED (hex) debuff after it's been applied to a grow roll."""
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET ed_expires_at=NULL WHERE telegram_id=$1", telegram_id
        )


def is_protected(user_row) -> bool:
    exp = user_row["condom_expires_at"]
    return exp is not None and exp > datetime.now(timezone.utc)


# ---------------------------------------------------------------
# Chat membership (used so /cursethisgroup only hits people from
# the group it was actually run in)
# ---------------------------------------------------------------

async def track_chat_member(chat_id: int, telegram_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO chat_members (chat_id, telegram_id) VALUES ($1, $2)
               ON CONFLICT DO NOTHING""",
            chat_id, telegram_id,
        )


async def get_chat_member_ids(chat_id: int) -> list[int]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT telegram_id FROM chat_members WHERE chat_id=$1", chat_id
        )
        return [r["telegram_id"] for r in rows]


# ---------------------------------------------------------------
# Attack / challenges (unchanged)
# ---------------------------------------------------------------

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
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE challenges
               SET status='resolved', winner_id=$1, loser_id=$2, resolved_at=now()
               WHERE id=$3 AND status='open'
               RETURNING id""",
            winner_id, loser_id, challenge_id,
        )
        return row is not None


# ---------------------------------------------------------------
# Loans (unchanged)
# ---------------------------------------------------------------

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


# ---------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------

async def get_leaderboard_today(start_of_day, limit: int = 5):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT u.telegram_id, u.first_name, u.username, SUM(g.amount) AS gained
               FROM growth_log g
               JOIN users u ON u.telegram_id = g.telegram_id
               WHERE g.created_at >= $2
               GROUP BY u.telegram_id, u.first_name, u.username
               ORDER BY gained DESC
               LIMIT $1""",
            limit, start_of_day,
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


# ---------------------------------------------------------------
# /pray
# ---------------------------------------------------------------

async def pray(telegram_id: int, win_amount: int):
    """Returns ('used', None) | ('lose', new_height) | ('win', new_height)."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT prayed, height_cm FROM users WHERE telegram_id=$1 FOR UPDATE",
                telegram_id,
            )
            if row["prayed"]:
                return "used", row["height_cm"]

            await conn.execute(
                "UPDATE users SET prayed=true WHERE telegram_id=$1", telegram_id
            )

            import random
            if random.random() < 0.01:
                new_height = row["height_cm"] + win_amount
                await conn.execute(
                    "UPDATE users SET height_cm=$1 WHERE telegram_id=$2",
                    new_height, telegram_id,
                )
                await conn.execute(
                    "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'pray')",
                    telegram_id, win_amount,
                )
                return "win", new_height
            return "lose", row["height_cm"]


# ---------------------------------------------------------------
# /simp
# ---------------------------------------------------------------

async def simp(giver_id: int, receiver_id: int, amount: int, today, daily_limit: int):
    """
    Returns one of:
      ('limit', None)
      ('insufficient', giver_height)
      ('success', giver_new_height, receiver_new_height)
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            giver = await conn.fetchrow(
                "SELECT height_cm, simp_count, simp_date FROM users WHERE telegram_id=$1 FOR UPDATE",
                giver_id,
            )
            count = giver["simp_count"]
            if giver["simp_date"] != today:
                count = 0

            if count >= daily_limit:
                return "limit", None

            if giver["height_cm"] < amount:
                return "insufficient", giver["height_cm"]

            new_giver_height = giver["height_cm"] - amount
            await conn.execute(
                "UPDATE users SET height_cm=$1, simp_count=$2, simp_date=$3 WHERE telegram_id=$4",
                new_giver_height, count + 1, today, giver_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'simp_give')",
                giver_id, -amount,
            )

            receiver = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", receiver_id
            )
            new_receiver_height = receiver["height_cm"] + amount
            await conn.execute(
                "UPDATE users SET height_cm=$1 WHERE telegram_id=$2",
                new_receiver_height, receiver_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'simp_receive')",
                receiver_id, amount,
            )

            return "success", new_giver_height, new_receiver_height


# ---------------------------------------------------------------
# /hex
# ---------------------------------------------------------------

async def hex_target(attacker_id: int, target_id: int, cost: int, duration_hours: int):
    """
    Returns one of:
      ('self', None)
      ('blocked', None)                -- target wearing a condom
      ('insufficient', attacker_height)
      ('success', attacker_new_height)
    """
    if attacker_id == target_id:
        return "self", None

    async with _pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow(
                "SELECT condom_expires_at FROM users WHERE telegram_id=$1 FOR UPDATE", target_id
            )
            if target["condom_expires_at"] and target["condom_expires_at"] > datetime.now(timezone.utc):
                return "blocked", None

            attacker = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", attacker_id
            )
            if attacker["height_cm"] < cost:
                return "insufficient", attacker["height_cm"]

            new_height = attacker["height_cm"] - cost
            await conn.execute(
                "UPDATE users SET height_cm=$1 WHERE telegram_id=$2", new_height, attacker_id
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'hex_cost')",
                attacker_id, -cost,
            )
            await conn.execute(
                """UPDATE users SET ed_expires_at = now() + ($1 || ' hours')::interval
                   WHERE telegram_id=$2""",
                str(duration_hours), target_id,
            )
            return "success", new_height


# ---------------------------------------------------------------
# /snitch
# ---------------------------------------------------------------

async def snitch(attacker_id: int, target_id: int, min_pct: float, max_pct: float, cooldown_hours: int):
    """
    Returns one of:
      ('self', None)
      ('cooldown', seconds_remaining)
      ('blocked', None)
      ('broke', None)                  -- target has nothing worth stealing
      ('success', stolen, attacker_new_height, target_new_height)
    """
    import random

    if attacker_id == target_id:
        return "self", None

    async with _pool.acquire() as conn:
        async with conn.transaction():
            attacker = await conn.fetchrow(
                "SELECT height_cm, snitch_cooldown_until FROM users WHERE telegram_id=$1 FOR UPDATE",
                attacker_id,
            )
            now = datetime.now(timezone.utc)
            if attacker["snitch_cooldown_until"] and attacker["snitch_cooldown_until"] > now:
                remaining = (attacker["snitch_cooldown_until"] - now).total_seconds()
                return "cooldown", remaining

            target = await conn.fetchrow(
                "SELECT height_cm, condom_expires_at FROM users WHERE telegram_id=$1 FOR UPDATE",
                target_id,
            )
            if target["condom_expires_at"] and target["condom_expires_at"] > now:
                return "blocked", None

            if target["height_cm"] <= 0:
                return "broke", None

            pct = random.uniform(min_pct, max_pct)
            stolen = max(1, round(target["height_cm"] * pct))
            stolen = min(stolen, target["height_cm"])

            new_target_height = target["height_cm"] - stolen
            new_attacker_height = attacker["height_cm"] + stolen

            await conn.execute(
                "UPDATE users SET height_cm=$1 WHERE telegram_id=$2", new_target_height, target_id
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'snitch_stolen')",
                target_id, -stolen,
            )
            await conn.execute(
                """UPDATE users SET height_cm=$1,
                   snitch_cooldown_until = now() + ($2 || ' hours')::interval
                   WHERE telegram_id=$3""",
                new_attacker_height, str(cooldown_hours), attacker_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'snitch_steal')",
                attacker_id, stolen,
            )
            return "success", stolen, new_attacker_height, new_target_height


# ---------------------------------------------------------------
# Condom shop
# ---------------------------------------------------------------

def condom_cost_for(height_cm: int, tier1_cost: int, tier2_cost: int, tier3_cost: int) -> int:
    if height_cm < 100:
        return tier1_cost
    if height_cm < 1000:
        return tier2_cost
    return tier3_cost


async def buy_condom(telegram_id: int, duration_hours: int, tier1: int, tier2: int, tier3: int):
    """
    Returns one of:
      ('already_active', expires_at)
      ('insufficient', cost, height)
      ('success', cost, expires_at)
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT height_cm, condom_expires_at FROM users WHERE telegram_id=$1 FOR UPDATE",
                telegram_id,
            )
            now = datetime.now(timezone.utc)
            if row["condom_expires_at"] and row["condom_expires_at"] > now:
                return "already_active", row["condom_expires_at"]

            cost = condom_cost_for(row["height_cm"], tier1, tier2, tier3)
            if row["height_cm"] < cost:
                return "insufficient", cost, row["height_cm"]

            new_height = row["height_cm"] - cost
            expires_at = now.replace(microsecond=0)
            await conn.execute(
                """UPDATE users SET height_cm=$1,
                   condom_expires_at = now() + ($2 || ' hours')::interval
                   WHERE telegram_id=$3""",
                new_height, str(duration_hours), telegram_id,
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'condom_buy')",
                telegram_id, -cost,
            )
            row2 = await conn.fetchrow(
                "SELECT condom_expires_at FROM users WHERE telegram_id=$1", telegram_id
            )
            return "success", cost, row2["condom_expires_at"]


# ---------------------------------------------------------------
# /cursethisgroup
# ---------------------------------------------------------------

async def get_group_curse_cooldown(chat_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT last_cursed_at FROM group_curses WHERE chat_id=$1", chat_id
        )


async def set_group_curse_time(chat_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO group_curses (chat_id, last_cursed_at) VALUES ($1, now())
               ON CONFLICT (chat_id) DO UPDATE SET last_cursed_at = now()""",
            chat_id,
        )


async def create_curse_events(chat_id: int, events: list[tuple[int, int, bool, datetime]]):
    """events: list of (telegram_id, amount, protected, scheduled_at). Returns list of ids."""
    async with _pool.acquire() as conn:
        ids = []
        for telegram_id, amount, protected, scheduled_at in events:
            row = await conn.fetchrow(
                """INSERT INTO curse_events (chat_id, telegram_id, amount, protected, scheduled_at)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                chat_id, telegram_id, amount, protected, scheduled_at,
            )
            ids.append(row["id"])
        return ids


async def get_pending_curse_events():
    async with _pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM curse_events WHERE executed=false ORDER BY scheduled_at"
        )


async def execute_curse_event(event_id: int):
    """
    Atomically claims + executes a curse event. Returns None if already executed,
    otherwise a dict with telegram_id, chat_id, amount, protected, new_height.
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            event = await conn.fetchrow(
                "SELECT * FROM curse_events WHERE id=$1 AND executed=false FOR UPDATE", event_id
            )
            if event is None:
                return None

            await conn.execute(
                "UPDATE curse_events SET executed=true WHERE id=$1", event_id
            )

            if event["protected"]:
                return {
                    "telegram_id": event["telegram_id"],
                    "chat_id": event["chat_id"],
                    "amount": event["amount"],
                    "protected": True,
                    "new_height": None,
                }

            user = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", event["telegram_id"]
            )
            actual = event["amount"]
            new_height = user["height_cm"] - actual
            if new_height < 0:
                actual = user["height_cm"]
                new_height = 0

            await conn.execute(
                "UPDATE users SET height_cm=$1 WHERE telegram_id=$2", new_height, event["telegram_id"]
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'curse')",
                event["telegram_id"], -actual,
            )
            return {
                "telegram_id": event["telegram_id"],
                "chat_id": event["chat_id"],
                "amount": actual,
                "protected": False,
                "new_height": new_height,
            }


# ---------------------------------------------------------------
# /gamble + pussy mode
# ---------------------------------------------------------------

async def gamble(telegram_id: int, chat_id: int, win_chance: float, win_amount: int):
    """Returns ('win', new_height) or ('lose', pussy_started_at)."""
    import random
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", telegram_id
            )
            if random.random() < win_chance:
                new_height = row["height_cm"] + win_amount
                await conn.execute(
                    "UPDATE users SET height_cm=$1 WHERE telegram_id=$2", new_height, telegram_id
                )
                await conn.execute(
                    "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'gamble_win')",
                    telegram_id, win_amount,
                )
                return "win", new_height
            else:
                now = datetime.now(timezone.utc)
                await conn.execute(
                    """UPDATE users SET pussy_active=true, pussy_started_at=$1,
                       pussy_banked=0, pussy_chat_id=$2 WHERE telegram_id=$3""",
                    now, chat_id, telegram_id,
                )
                return "lose", now


async def get_pussy_status(telegram_id: int):
    return await get_user(telegram_id)


async def get_active_pussies():
    async with _pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM users WHERE pussy_active=true")


async def has_fucked(pussy_id: int, fucker_id: int, stage: int, since: datetime) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT 1 FROM fuck_log
               WHERE pussy_id=$1 AND fucker_id=$2 AND hour_stage=$3 AND created_at >= $4""",
            pussy_id, fucker_id, stage, since,
        )
        return row is not None


async def record_fuck(pussy_id: int, fucker_id: int, stage: int, gain: int) -> int:
    """Applies the gain to the fucker, banks it for the pussy user. Returns fucker's new height."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO fuck_log (pussy_id, fucker_id, hour_stage) VALUES ($1, $2, $3)",
                pussy_id, fucker_id, stage,
            )
            fucker = await conn.fetchrow(
                "SELECT height_cm FROM users WHERE telegram_id=$1 FOR UPDATE", fucker_id
            )
            new_height = fucker["height_cm"] + gain
            await conn.execute(
                "UPDATE users SET height_cm=$1 WHERE telegram_id=$2", new_height, fucker_id
            )
            await conn.execute(
                "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'fuck_gain')",
                fucker_id, gain,
            )
            await conn.execute(
                "UPDATE users SET pussy_banked = pussy_banked + $1 WHERE telegram_id=$2",
                gain, pussy_id,
            )
            return new_height


async def end_pussy(telegram_id: int):
    """Pays out the banked gain to the (former) pussy user's real height and clears the status."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT height_cm, pussy_banked, pussy_chat_id FROM users WHERE telegram_id=$1 FOR UPDATE",
                telegram_id,
            )
            if row is None or row["pussy_banked"] is None:
                return None
            banked = row["pussy_banked"]
            new_height = row["height_cm"] + banked
            await conn.execute(
                """UPDATE users SET height_cm=$1, pussy_active=false, pussy_started_at=NULL,
                   pussy_banked=0, pussy_chat_id=NULL WHERE telegram_id=$2""",
                new_height, telegram_id,
            )
            if banked:
                await conn.execute(
                    "INSERT INTO growth_log (telegram_id, amount, type) VALUES ($1, $2, 'pussy_payout')",
                    telegram_id, banked,
                )
            return {"new_height": new_height, "banked": banked, "chat_id": row["pussy_chat_id"]}
