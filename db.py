"""
Thin data-access layer over Supabase. Keeps handlers.py free of raw queries.
"""
import os
import random
import datetime as dt
from supabase import create_client, Client

import game_data as gd

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------- Players ----------------

def get_or_create_player(user_id: int, username: str) -> dict:
    res = supabase.table("players").select("*").eq("user_id", user_id).execute()
    if res.data:
        player = res.data[0]
        _apply_hp_regen(player)
        return player
    new_player = {
        "user_id": user_id,
        "username": username,
        "hp": gd.BASE_MAX_HP,
        "max_hp": gd.BASE_MAX_HP,
    }
    supabase.table("players").insert(new_player).execute()
    return get_or_create_player(user_id, username)


def _apply_hp_regen(player: dict):
    """Lazily regen HP based on time elapsed since last_hp_regen."""
    if player["hp"] >= player["max_hp"]:
        return
    last = dt.datetime.fromisoformat(player["last_hp_regen"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.timezone.utc)
    minutes = (now - last).total_seconds() / 60
    regen = int(minutes * gd.HP_REGEN_PER_MINUTE)
    if regen <= 0:
        return
    new_hp = min(player["max_hp"], player["hp"] + regen)
    downed = False if new_hp > 0 else player["downed"]
    supabase.table("players").update({
        "hp": new_hp, "last_hp_regen": now.isoformat(), "downed": (new_hp <= 0)
    }).eq("user_id", player["user_id"]).execute()
    player["hp"] = new_hp
    player["last_hp_regen"] = now.isoformat()


def update_player(user_id: int, **fields):
    supabase.table("players").update(fields).eq("user_id", user_id).execute()


def add_currency(user_id: int, gold: int = 0, silver: int = 0):
    p = supabase.table("players").select("gold,silver").eq("user_id", user_id).execute().data[0]
    supabase.table("players").update({
        "gold": p["gold"] + gold, "silver": p["silver"] + silver
    }).eq("user_id", user_id).execute()


def damage_player(user_id: int, amount: int):
    p = supabase.table("players").select("hp,max_hp").eq("user_id", user_id).execute().data[0]
    new_hp = max(0, p["hp"] - amount)
    supabase.table("players").update({
        "hp": new_hp, "downed": new_hp <= 0, "last_hp_regen": dt.datetime.now(dt.timezone.utc).isoformat()
    }).eq("user_id", user_id).execute()
    return new_hp


def heal_player_full(user_id: int):
    p = supabase.table("players").select("max_hp").eq("user_id", user_id).execute().data[0]
    supabase.table("players").update({
        "hp": p["max_hp"], "downed": False, "last_hp_regen": dt.datetime.now(dt.timezone.utc).isoformat()
    }).eq("user_id", user_id).execute()


def leaderboard(limit=10):
    res = supabase.table("players").select("username,gold,sword_level,armor_level") \
        .order("gold", desc=True).limit(limit).execute()
    return res.data


# ---------------- Group raid membership ----------------

def join_group(chat_id: int, user_id: int):
    supabase.table("group_members").upsert({
        "chat_id": chat_id, "user_id": user_id
    }).execute()


def get_group_members(chat_id: int) -> list:
    res = supabase.table("group_members").select("user_id").eq("chat_id", chat_id).execute()
    return [row["user_id"] for row in res.data]


def get_active_members(chat_id: int) -> list:
    """Group members who are joined AND not downed."""
    member_ids = get_group_members(chat_id)
    if not member_ids:
        return []
    res = supabase.table("players").select("*").in_("user_id", member_ids).eq("downed", False).execute()
    for p in res.data:
        _apply_hp_regen(p)
    return [p for p in res.data if p["hp"] > 0]


# ---------------- Main boss state ----------------

def get_boss_state(chat_id: int) -> dict:
    res = supabase.table("group_boss_state").select("*").eq("chat_id", chat_id).execute()
    if res.data:
        state = res.data[0]
    else:
        boss = gd.MAIN_BOSSES[0]
        state = {
            "chat_id": chat_id, "boss_index": 0, "current_hp": boss["hp"],
            "last_reset": dt.datetime.now(dt.timezone.utc).isoformat()
        }
        supabase.table("group_boss_state").insert(state).execute()

    boss_def = gd.MAIN_BOSSES[state["boss_index"]]
    # weekly reset for the Dragon regardless of kill status
    if boss_def["key"] == "dragon" and "reset_days" in boss_def:
        last = dt.datetime.fromisoformat(state["last_reset"].replace("Z", "+00:00"))
        if (dt.datetime.now(dt.timezone.utc) - last).days >= boss_def["reset_days"]:
            state["current_hp"] = boss_def["hp"]
            state["last_reset"] = dt.datetime.now(dt.timezone.utc).isoformat()
            supabase.table("group_boss_state").update({
                "current_hp": state["current_hp"], "last_reset": state["last_reset"]
            }).eq("chat_id", chat_id).execute()
    return state


def set_boss_hp(chat_id: int, hp: int):
    supabase.table("group_boss_state").update({"current_hp": max(0, hp)}).eq("chat_id", chat_id).execute()


def advance_to_next_boss(chat_id: int):
    state = get_boss_state(chat_id)
    next_index = state["boss_index"] + 1
    if next_index >= len(gd.MAIN_BOSSES):
        return None  # already on final boss, no further advance
    next_boss = gd.MAIN_BOSSES[next_index]
    supabase.table("group_boss_state").update({
        "boss_index": next_index, "current_hp": next_boss["hp"],
        "last_reset": dt.datetime.now(dt.timezone.utc).isoformat()
    }).eq("chat_id", chat_id).execute()
    return next_boss


# ---------------- Attack cooldowns (main boss) ----------------

def check_and_set_cooldown(user_id: int) -> int:
    """Returns seconds remaining if on cooldown, else 0 and refreshes cooldown."""
    res = supabase.table("attack_cooldowns").select("*").eq("user_id", user_id).execute()
    now = dt.datetime.now(dt.timezone.utc)
    if res.data:
        last = dt.datetime.fromisoformat(res.data[0]["last_attack"].replace("Z", "+00:00"))
        elapsed = (now - last).total_seconds()
        if elapsed < gd.MAIN_BOSS_ATTACK_COOLDOWN_SECONDS:
            return int(gd.MAIN_BOSS_ATTACK_COOLDOWN_SECONDS - elapsed)
        supabase.table("attack_cooldowns").update({"last_attack": now.isoformat()}).eq("user_id", user_id).execute()
        return 0
    supabase.table("attack_cooldowns").insert({"user_id": user_id, "last_attack": now.isoformat()}).execute()
    return 0


# ---------------- Dungeon attempts ----------------

def get_dungeon_attempts_left(user_id: int) -> int:
    today = dt.date.today().isoformat()
    res = supabase.table("dungeon_attempts").select("*").eq("user_id", user_id).eq("play_date", today).execute()
    used = res.data[0]["attempts_used"] if res.data else 0
    return max(0, gd.DAILY_DUNGEON_ATTEMPTS - used)


def use_dungeon_attempt(user_id: int):
    today = dt.date.today().isoformat()
    res = supabase.table("dungeon_attempts").select("*").eq("user_id", user_id).eq("play_date", today).execute()
    if res.data:
        supabase.table("dungeon_attempts").update({
            "attempts_used": res.data[0]["attempts_used"] + 1
        }).eq("user_id", user_id).eq("play_date", today).execute()
    else:
        supabase.table("dungeon_attempts").insert({
            "user_id": user_id, "play_date": today, "attempts_used": 1
        }).execute()


def random_dungeon_boss(player_gear_level: int) -> dict:
    """Weighted toward the player's rough gear level, occasional harder pull."""
    if random.random() < 0.15:
        tier = random.randint(1, 4)
    else:
        tier = min(4, max(1, (player_gear_level // 3) + 1))
    pool = [b for b in gd.DUNGEON_BOSSES if b["tier"] == tier]
    return random.choice(pool)
