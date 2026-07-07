import random
from telegram import Update
from telegram.ext import ContextTypes

import db
import game_data as gd
import game_logic as gl


def _display_name(update: Update) -> str:
    u = update.effective_user
    return u.username or u.first_name or f"Player{u.id}"


def _require_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


# ---------------- Basic commands ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_group(update):
        await update.message.reply_text(
            "🐉 This game only works inside a group chat! Add me to a group and use /join to start the raid."
        )
        return
    await update.message.reply_text(
        "🏰 *Welcome to Kingdoms & Dragons!*\n\n"
        "Four villains stand between your kingdom and peace:\n"
        "1️⃣ The Goblin King\n2️⃣ The Sea Monster\n3️⃣ The Demon Lord\n4️⃣ The Dragon\n\n"
        "Use /join to enter the raid, /attack to fight the current boss, "
        "/dungeon to grind solo, and /shop to gear up.",
        parse_mode="Markdown",
    )


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_group(update):
        await update.message.reply_text("Join the game inside a group chat!")
        return
    user = update.effective_user
    db.get_or_create_player(user.id, _display_name(update))
    db.join_group(update.effective_chat.id, user.id)
    await update.message.reply_text(f"⚔️ {_display_name(update)} has joined the raid!")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = db.get_or_create_player(user.id, _display_name(update))
    status = "💀 DOWNED" if p["downed"] else f"❤️ {p['hp']}/{p['max_hp']}"
    await update.message.reply_text(
        f"👤 *{p['username']}*\n"
        f"{status}\n"
        f"💰 {p['gold']} gold   🪙 {p['silver']} silver\n"
        f"{gl.gear_summary(p['sword_level'], p['armor_level'])}",
        parse_mode="Markdown",
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.leaderboard()
    if not rows:
        await update.message.reply_text("No adventurers yet. Be the first with /join!")
        return
    lines = ["🏆 *Leaderboard (by gold)*"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['username']} — 💰{r['gold']}  (⚔️Lv{r['sword_level']} 🛡️Lv{r['armor_level']})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------- Shop / upgrades ----------------

async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = db.get_or_create_player(user.id, _display_name(update))
    sword_cost = gd.upgrade_cost(p["sword_level"])
    armor_cost = gd.upgrade_cost(p["armor_level"])
    lines = ["🏪 *Shop*", f"💰 Your gold: {p['gold']}\n"]
    lines.append(
        f"⚔️ Sword Lv{p['sword_level']} → Lv{p['sword_level']+1}: "
        f"{sword_cost if sword_cost else 'MAX LEVEL'} gold  →  /upgrade_sword"
    )
    lines.append(
        f"🛡️ Armor Lv{p['armor_level']} → Lv{p['armor_level']+1}: "
        f"{armor_cost if armor_cost else 'MAX LEVEL'} gold  →  /upgrade_armor"
    )
    lines.append(f"\n🪙 /convert — turn silver into gold ({gd.SILVER_TO_GOLD_RATE} silver = 1 gold)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def upgrade_sword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _upgrade(update, "sword_level")


async def upgrade_armor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _upgrade(update, "armor_level")


async def _upgrade(update: Update, field: str):
    user = update.effective_user
    p = db.get_or_create_player(user.id, _display_name(update))
    level = p[field]
    cost = gd.upgrade_cost(level)
    if cost is None:
        await update.message.reply_text(f"That gear is already at max level ({gd.MAX_GEAR_LEVEL})! 🏆")
        return
    if p["gold"] < cost:
        await update.message.reply_text(f"Not enough gold. Need {cost}, you have {p['gold']}.")
        return
    db.update_player(user.id, **{field: level + 1, "gold": p["gold"] - cost})
    label = "⚔️ Sword" if field == "sword_level" else "🛡️ Armor"
    await update.message.reply_text(f"{label} upgraded to Lv{level+1}! (-{cost} gold)")


async def convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = db.get_or_create_player(user.id, _display_name(update))
    convertible = (p["silver"] // gd.SILVER_TO_GOLD_RATE) * gd.SILVER_TO_GOLD_RATE
    if convertible <= 0:
        await update.message.reply_text(f"You need at least {gd.SILVER_TO_GOLD_RATE} silver to convert.")
        return
    gold_gained = convertible // gd.SILVER_TO_GOLD_RATE
    db.update_player(user.id, silver=p["silver"] - convertible, gold=p["gold"] + gold_gained)
    await update.message.reply_text(f"🪙 Converted {convertible} silver → 💰 {gold_gained} gold!")


# ---------------- Main boss (group raid) ----------------

async def boss_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_group(update):
        return
    chat_id = update.effective_chat.id
    state = db.get_boss_state(chat_id)
    boss = gd.MAIN_BOSSES[state["boss_index"]]
    pct = max(0, int(state["current_hp"] / boss["hp"] * 100))
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    await update.message.reply_text(
        f"{boss['name']}\n❤️ [{bar}] {state['current_hp']}/{boss['hp']} ({pct}%)"
    )


async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_group(update):
        await update.message.reply_text("Boss fights only happen in group chats. Use /dungeon to play solo!")
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    p = db.get_or_create_player(user.id, _display_name(update))

    if user.id not in db.get_group_members(chat_id):
        await update.message.reply_text("Use /join first to enter the raid!")
        return
    if p["downed"] or p["hp"] <= 0:
        await update.message.reply_text("💀 You're downed! Wait for HP to regen before attacking.")
        return

    cooldown = db.check_and_set_cooldown(user.id)
    if cooldown > 0:
        await update.message.reply_text(f"⏳ Attack on cooldown — wait {cooldown}s.")
        return

    state = db.get_boss_state(chat_id)
    boss = gd.MAIN_BOSSES[state["boss_index"]]

    if state["current_hp"] <= 0:
        await update.message.reply_text(f"{boss['name']} is already defeated! Use /boss to check status.")
        return

    dmg, crit = gl.player_attack_damage(p["sword_level"])
    new_boss_hp = max(0, state["current_hp"] - dmg)
    db.set_boss_hp(chat_id, new_boss_hp)

    lines = [f"⚔️ {p['username']} hits {boss['name']} for {dmg}{' 💥 CRIT!' if crit else ''}"]

    if new_boss_hp <= 0:
        gold = boss["gold_reward"]
        silver = boss["silver_reward"]
        db.add_currency(user.id, gold=gold, silver=silver)
        lines.append(f"\n🎉 *{boss['name']} HAS BEEN DEFEATED!* 🎉")
        lines.append(f"+{gold} gold, +{silver} silver for the final blow!")
        next_boss = db.advance_to_next_boss(chat_id)
        if next_boss:
            lines.append(f"\n{next_boss['intro']}\n{next_boss['name']} awaits — {next_boss['hp']} HP.")
        else:
            lines.append("\nThe kingdom has no greater foe left to summon... for now. 🐉")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # Boss counterattacks — pick random active member(s)
    targets = db.get_active_members(chat_id)
    num_targets = 1
    if boss.get("tidal_wave_chance") and random.random() < boss["tidal_wave_chance"] and len(targets) > 1:
        num_targets = 2
        lines.append(f"\n🌊 {boss['name']} unleashes a tidal wave!")
    if targets:
        chosen = random.sample(targets, min(num_targets, len(targets)))
        for t in chosen:
            bdmg = gl.boss_attack_damage(boss, t["armor_level"], boss_current_hp=new_boss_hp)
            new_hp = db.damage_player(t["user_id"], bdmg)
            if new_hp <= 0:
                lines.append(f"💥 {boss['name']} strikes {t['username']} for {bdmg} — 💀 DOWNED!")
            else:
                lines.append(f"💥 {boss['name']} strikes {t['username']} for {bdmg} (❤️{new_hp}/{t['max_hp']})")

    lines.append(f"\n{boss['name']} HP: {new_boss_hp}/{boss['hp']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------- Dungeon (solo) ----------------

async def dungeon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = db.get_or_create_player(user.id, _display_name(update))

    if p["downed"] or p["hp"] <= 0:
        await update.message.reply_text("💀 You're downed! Heal up before entering a dungeon.")
        return

    left = db.get_dungeon_attempts_left(user.id)
    if left <= 0:
        await update.message.reply_text("You've used all 5 dungeon attempts today. Come back tomorrow!")
        return

    db.use_dungeon_attempt(user.id)
    gear_level = max(p["sword_level"], p["armor_level"])
    boss = db.random_dungeon_boss(gear_level)

    boss_hp = boss["hp"]
    player_hp = p["hp"]
    log = [f"🗝️ You enter the dungeon and encounter *{boss['name']}* (Tier {boss['tier']}, {boss_hp} HP)!\n"]

    turn = 0
    while boss_hp > 0 and player_hp > 0 and turn < 30:
        turn += 1
        dmg, crit = gl.player_attack_damage(p["sword_level"])
        boss_hp = max(0, boss_hp - dmg)
        if boss_hp <= 0:
            break
        bdmg = gl.boss_attack_damage(boss, p["armor_level"])
        player_hp = max(0, player_hp - bdmg)

    won = boss_hp <= 0
    db.damage_player(user.id, p["hp"] - player_hp)  # sync final HP

    if won:
        silver = boss["silver_reward"]
        db.add_currency(user.id, silver=silver)
        log.append(f"🏆 Victory! {boss['name']} defeated. You have {player_hp} HP left.")
        log.append(f"+{silver} silver")
    else:
        consolation = max(2, boss["silver_reward"] // 4)
        db.add_currency(user.id, silver=consolation)
        log.append(f"☠️ You were overwhelmed by {boss['name']} and retreated.")
        log.append(f"+{consolation} silver for the effort.")

    log.append(f"\nDungeon attempts left today: {db.get_dungeon_attempts_left(user.id)}")
    await update.message.reply_text("\n".join(log), parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands*\n"
        "/join — enter the group raid\n"
        "/attack — attack the current main boss (group only)\n"
        "/boss — see current boss HP\n"
        "/dungeon — solo fight (5/day)\n"
        "/shop — view upgrade costs\n"
        "/upgrade_sword, /upgrade_armor — spend gold\n"
        "/convert — silver → gold\n"
        "/profile — your stats\n"
        "/leaderboard — top players",
        parse_mode="Markdown",
    )
