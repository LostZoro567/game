import html
import logging
import os
import random
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import db

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---- Tunable game constants ----
GROW_MIN, GROW_MAX = 10, 25
GROW_COOLDOWN_HOURS = 24
ED_DEBUFF_MULTIPLIER = 0.3  # grow roll is shrunk by this much while hexed

LOAN_AMOUNT = 10
LOAN_FLOOR = -10  # a defaulted loan can push a user down to this, but no further
LOAN_DURATION_HOURS = 24
LOAN_CHECK_INTERVAL_SECONDS = 300

PRAY_WIN_AMOUNT = 50  # /pray is a once-per-user 1% shot, chance is fixed in db.pray()

SIMP_DAILY_LIMIT = 3

HEX_COST = 15
HEX_DURATION_HOURS = 3

SNITCH_MIN_PCT, SNITCH_MAX_PCT = 0.05, 0.15
SNITCH_COOLDOWN_HOURS = 12

CONDOM_DURATION_HOURS = 24
CONDOM_TIER1_COST, CONDOM_TIER2_COST, CONDOM_TIER3_COST = 10, 30, 60

CURSE_COOLDOWN_HOURS = 24
CURSE_MIN_AMOUNT, CURSE_MAX_AMOUNT = 5, 20
CURSE_WINDOW_MIN_MINUTES, CURSE_WINDOW_MAX_MINUTES = 5, 180
CURSE_CHECK_INTERVAL_SECONDS = 60


def esc(text) -> str:
    return html.escape(str(text))


def mention(user) -> str:
    name = esc(user.first_name or user.username or "someone")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def display_name(row) -> str:
    return row["first_name"] or row["username"] or "Unknown"


async def register_user(update: Update):
    """get_or_create + keeps track of who's in which group, for /cursethisgroup."""
    u = update.effective_user
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    chat = update.effective_chat
    if chat and chat.type != "private":
        await db.track_chat_member(chat.id, u.id)
    return user


async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finds a target user via reply-to-message or an @mention. Returns (telegram_id, display_name) or (None, None)."""
    message = update.message

    if message.reply_to_message and message.reply_to_message.from_user:
        ru = message.reply_to_message.from_user
        if not ru.is_bot:
            return ru.id, (ru.first_name or ru.username or "someone")

    for ent in message.entities or []:
        if ent.type == "text_mention" and ent.user:
            u = ent.user
            return u.id, (u.first_name or u.username or "someone")
        if ent.type == "mention":
            username = message.text[ent.offset: ent.offset + ent.length]
            row = await db.get_user_by_username(username)
            if row:
                return row["telegram_id"], (row["first_name"] or row["username"] or "someone")

    return None, None


# ---------------- Commands ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    await update.message.reply_html(
        "📏 Welcome to <b>DickGrow Bot</b>!\n\n"
        "/grow — grow 10-25cm, once a day\n"
        "/attack &lt;amount&gt; — challenge the group for any amount up to your height\n"
        "/me — check your stats\n"
        "/loan — borrow 10cm when you're at 0\n"
        "/leaderboard — today's and all-time rankings\n"
        "/pray — one lifetime 1% shot at +50cm\n"
        "/simp &lt;amount&gt; — reply to or @mention someone to give them cm\n"
        "/hex — reply to or @mention someone to curse their next /grow\n"
        "/snitch — reply to or @mention someone to steal a % of their height\n"
        "/condom — buy temporary protection from hexes and snitches\n"
        "/cursethisgroup — unleash random shrinkage on the whole group\n"
    )


async def grow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await register_user(update)

    last_grow = user["last_grow"]
    now = datetime.now(timezone.utc)
    if last_grow is not None:
        elapsed = (now - last_grow).total_seconds()
        if elapsed < GROW_COOLDOWN_HOURS * 3600:
            remaining = GROW_COOLDOWN_HOURS * 3600 - elapsed
            hours, minutes = int(remaining // 3600), int((remaining % 3600) // 60)
            await update.message.reply_text(
                f"⏳ You already grew today. Come back in {hours}h {minutes}m."
            )
            return

    amount = random.randint(GROW_MIN, GROW_MAX)
    debuffed = False
    ed_expires = user["ed_expires_at"]
    if ed_expires is not None and ed_expires > now:
        amount = max(1, round(amount * ED_DEBUFF_MULTIPLIER))
        debuffed = True
        await db.clear_ed(u.id)

    new_height = await db.apply_growth(u.id, amount, "grow", set_last_grow=True)

    text = (
        f"🌱 {mention(u)} grew <b>+{amount}cm</b> today!\n"
        f"📏 New height: <b>{new_height}cm</b>"
    )
    if debuffed:
        text += "\n💀 Ouch — you were hexed with ED, growth was weaker than usual."

    await update.message.reply_html(text)


async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await register_user(update)

    if not context.args:
        await update.message.reply_html(
            "⚔️ Usage: <code>/attack &lt;amount&gt;</code>\n"
            f"Pick anywhere from 1cm up to your full height ({user['height_cm']}cm)."
        )
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That's not a valid number. Try e.g. /attack 20")
        return

    if amount < 1:
        await update.message.reply_text("Minimum challenge amount is 1cm.")
        return

    if amount > user["height_cm"]:
        await update.message.reply_text(
            f"You've only got {user['height_cm']}cm — you can't challenge more than that."
        )
        return

    challenge_id = await db.create_challenge(update.effective_chat.id, u.id, amount)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"⚔️ Attack ({amount}cm)", callback_data=f"atk:{challenge_id}")]]
    )

    msg = await update.message.reply_html(
        f"🔥 {mention(u)} has challenged the group for <b>{amount}cm</b>!\n"
        f"Tap below to accept — you need at least {amount}cm to fight.",
        reply_markup=keyboard,
    )
    await db.set_challenge_message(challenge_id, msg.message_id)


async def attack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    clicker = query.from_user

    challenge_id = int(query.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)

    if challenge is None:
        await query.answer("This challenge no longer exists.", show_alert=True)
        return

    if challenge["status"] != "open":
        await query.answer("This challenge has already been settled!", show_alert=True)
        return

    if challenge["challenger_id"] == clicker.id:
        await query.answer("You can't attack your own challenge 😅", show_alert=True)
        return

    clicker_row = await db.get_or_create_user(clicker.id, clicker.username, clicker.first_name)
    amount = challenge["amount"]

    if clicker_row["height_cm"] < amount:
        await query.answer(
            f"You need at least {amount}cm to accept this challenge!", show_alert=True
        )
        return

    challenger_id = challenge["challenger_id"]
    winner_id, loser_id = (
        (challenger_id, clicker.id) if random.random() < 0.5 else (clicker.id, challenger_id)
    )

    claimed = await db.resolve_challenge(challenge_id, winner_id, loser_id)
    if not claimed:
        await query.answer("Someone beat you to it!", show_alert=True)
        return

    await db.apply_growth(winner_id, amount, "attack_win")
    await db.apply_growth(loser_id, -amount, "attack_loss", clamp_zero=True)

    winner_row = await db.get_user(winner_id)
    loser_row = await db.get_user(loser_id)
    challenger_row = await db.get_user(challenger_id)

    challenger_name = display_name(challenger_row)
    clicker_name = clicker.first_name or clicker.username or "Someone"
    winner_name = challenger_name if winner_id == challenger_id else clicker_name
    loser_name = clicker_name if winner_id == challenger_id else challenger_name

    result_text = (
        f"⚔️ <b>{esc(challenger_name)}</b> vs <b>{esc(clicker_name)}</b> — {amount}cm on the line!\n\n"
        f"🏆 <b>{esc(winner_name)}</b> wins!\n"
        f"💀 {esc(loser_name)} loses {amount}cm.\n\n"
        f"📏 {esc(winner_name)}: {winner_row['height_cm']}cm\n"
        f"📏 {esc(loser_name)}: {loser_row['height_cm']}cm"
    )

    await query.edit_message_text(text=result_text, parse_mode=ParseMode.HTML)
    await query.answer("You won! 🏆" if winner_id == clicker.id else "You lost this one 💀")


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await register_user(update)
    loan_text = ""
    if user["loan_active"]:
        loan_text = f"\n💰 Active loan: {user['loan_amount']}cm (auto-repays in {LOAN_DURATION_HOURS}h)"

    protection_text = ""
    if db.is_protected(user):
        protection_text = f"\n🛡️ Protected until {user['condom_expires_at'].strftime('%H:%M UTC')}"

    ed_text = ""
    now = datetime.now(timezone.utc)
    if user["ed_expires_at"] and user["ed_expires_at"] > now:
        ed_text = f"\n💀 Hexed with ED until {user['ed_expires_at'].strftime('%H:%M UTC')}"

    await update.message.reply_html(
        f"📏 <b>{esc(u.first_name)}</b>\nHeight: <b>{user['height_cm']}cm</b>"
        f"{loan_text}{protection_text}{ed_text}"
    )


async def loan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await register_user(update)

    if user["loan_active"]:
        await update.message.reply_text("You already have an active loan.")
        return

    if user["height_cm"] != 0:
        await update.message.reply_text("Loans are only available when you're at exactly 0cm.")
        return

    await db.create_loan(u.id, LOAN_AMOUNT)
    await update.message.reply_html(
        f"💸 Loan approved! You received <b>+{LOAN_AMOUNT}cm</b>.\n"
        f"⚠️ It will be automatically deducted in {LOAN_DURATION_HOURS} hours. "
        f"If you're still at 0cm by then, you'll go to <b>{LOAN_FLOOR}cm</b>."
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_top = await db.get_leaderboard_today(start_of_day, limit=5)
    alltime_top = await db.get_leaderboard_alltime(limit=5)

    lines = ["🏆 <b>Today's Top Gainers</b>"]
    if today_top:
        for i, row in enumerate(today_top, 1):
            lines.append(f"{i}. {esc(display_name(row))} — {row['gained']:+d}cm today")
    else:
        lines.append("No activity yet today.")

    lines.append("\n👑 <b>All-Time Rankings</b>")
    for i, row in enumerate(alltime_top, 1):
        lines.append(f"{i}. {esc(display_name(row))} — {row['height_cm']}cm")

    await update.message.reply_html("\n".join(lines))


async def pray_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await register_user(update)

    status, value = await db.pray(u.id, PRAY_WIN_AMOUNT)
    if status == "used":
        await update.message.reply_text("🙏 You've already used your one shot at prayer.")
        return
    if status == "win":
        await update.message.reply_html(
            f"✨ A miracle! {mention(u)} gained <b>+{PRAY_WIN_AMOUNT}cm</b>!\n📏 New height: {value}cm"
        )
        return
    await update.message.reply_text("🙏 You prayed... nothing happened. (1% chance — that's the whole point)")


async def simp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await register_user(update)

    if not context.args:
        await update.message.reply_text(
            "Usage: /simp <amount> — reply to someone's message or @mention them."
        )
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That's not a valid number.")
        return

    if amount < 1:
        await update.message.reply_text("Minimum is 1cm.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        await update.message.reply_text("Reply to someone's message or @mention them to simp on them.")
        return
    if target_id == u.id:
        await update.message.reply_text("You can't simp on yourself 😅")
        return

    today = datetime.now(timezone.utc).date()
    result = await db.simp(u.id, target_id, amount, today, SIMP_DAILY_LIMIT)

    if result[0] == "limit":
        await update.message.reply_text(f"You've hit your daily simp limit ({SIMP_DAILY_LIMIT}).")
        return
    if result[0] == "insufficient":
        await update.message.reply_text(f"You only have {result[1]}cm — can't give away {amount}cm.")
        return

    _, giver_new, _receiver_new = result
    await update.message.reply_html(
        f"💸 {mention(u)} simped <b>{amount}cm</b> to {esc(target_name)}!\n"
        f"📏 {mention(u)}: {giver_new}cm"
    )


async def hex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await register_user(update)

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        await update.message.reply_text("Reply to someone's message or @mention them to hex.")
        return

    result = await db.hex_target(u.id, target_id, HEX_COST, HEX_DURATION_HOURS)
    status = result[0]

    if status == "self":
        await update.message.reply_text("You can't hex yourself.")
        return
    if status == "blocked":
        await update.message.reply_text(f"{esc(target_name)} is wearing a condom 🚫 — hex blocked.")
        return
    if status == "insufficient":
        await update.message.reply_text(f"You need {HEX_COST}cm to cast a hex — you have {result[1]}cm.")
        return

    _, new_height = result
    await update.message.reply_html(
        f"🔮 {mention(u)} hexed {esc(target_name)} with ED for {HEX_DURATION_HOURS}h!\n"
        f"Their next /grow will come out weaker.\n"
        f"📏 {mention(u)}: {new_height}cm (-{HEX_COST}cm)"
    )


async def snitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await register_user(update)

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        await update.message.reply_text("Reply to someone's message or @mention them to snitch on them.")
        return

    result = await db.snitch(u.id, target_id, SNITCH_MIN_PCT, SNITCH_MAX_PCT, SNITCH_COOLDOWN_HOURS)
    status = result[0]

    if status == "self":
        await update.message.reply_text("You can't snitch on yourself.")
        return
    if status == "cooldown":
        secs = result[1]
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        await update.message.reply_text(f"⏳ Snitch is on cooldown. Try again in {h}h {m}m.")
        return
    if status == "blocked":
        await update.message.reply_text(f"{esc(target_name)} is wearing a condom 🚫 — can't snitch on them.")
        return
    if status == "broke":
        await update.message.reply_text(f"{esc(target_name)} has nothing worth stealing.")
        return

    _, stolen, attacker_new, target_new = result
    await update.message.reply_html(
        f"🕵️ {mention(u)} snitched on {esc(target_name)} and stole <b>{stolen}cm</b>!\n"
        f"📏 {mention(u)}: {attacker_new}cm\n"
        f"📏 {esc(target_name)}: {target_new}cm"
    )


async def condom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await register_user(update)

    result = await db.buy_condom(
        u.id, CONDOM_DURATION_HOURS, CONDOM_TIER1_COST, CONDOM_TIER2_COST, CONDOM_TIER3_COST
    )
    status = result[0]

    if status == "already_active":
        await update.message.reply_text(
            f"🛡️ You're already protected until {result[1].strftime('%H:%M UTC')}."
        )
        return
    if status == "insufficient":
        _, cost, height = result
        await update.message.reply_text(
            f"You need {cost}cm for a condom at your size — you have {height}cm."
        )
        return

    _, cost, expires_at = result
    await update.message.reply_html(
        f"🛡️ Protection bought for <b>{cost}cm</b>! You're safe from hexes and snitches until "
        f"{expires_at.strftime('%H:%M UTC')}."
    )


async def cursethisgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    u = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("This only works in groups.")
        return

    await register_user(update)

    now = datetime.now(timezone.utc)
    cooldown_row = await db.get_group_curse_cooldown(chat.id)
    if cooldown_row and cooldown_row["last_cursed_at"]:
        elapsed = (now - cooldown_row["last_cursed_at"]).total_seconds()
        if elapsed < CURSE_COOLDOWN_HOURS * 3600:
            remaining = CURSE_COOLDOWN_HOURS * 3600 - elapsed
            h, m = int(remaining // 3600), int((remaining % 3600) // 60)
            await update.message.reply_text(
                f"This group was already cursed recently. Try again in {h}h {m}m."
            )
            return

    member_ids = await db.get_chat_member_ids(chat.id)
    if not member_ids:
        await update.message.reply_text(
            "No tracked members yet in this group — have people use a command first."
        )
        return

    await db.set_group_curse_time(chat.id)

    events = []
    for member_id in member_ids:
        member_row = await db.get_user(member_id)
        if member_row is None:
            continue
        protected = db.is_protected(member_row)
        amount = random.randint(CURSE_MIN_AMOUNT, CURSE_MAX_AMOUNT)
        delay_minutes = random.randint(CURSE_WINDOW_MIN_MINUTES, CURSE_WINDOW_MAX_MINUTES)
        scheduled_at = now + timedelta(minutes=delay_minutes)
        events.append((member_id, amount, protected, scheduled_at))

    await db.create_curse_events(chat.id, events)

    await update.message.reply_html(
        f"🌙 {mention(u)} has cursed the entire group!\n"
        f"Random shrinkage will strike over the next {CURSE_WINDOW_MAX_MINUTES // 60}h... "
        f"unless you're wearing a condom 🛡️"
    )


# ---------------- Background jobs ----------------

async def loan_repayment_job(context: ContextTypes.DEFAULT_TYPE):
    due_loans = await db.get_due_loans(LOAN_DURATION_HOURS)
    for loan_row in due_loans:
        telegram_id = loan_row["telegram_id"]
        await db.apply_growth(telegram_id, -LOAN_AMOUNT, "loan_repay", floor=LOAN_FLOOR)
        await db.clear_loan(telegram_id)
        logger.info(f"Repaid loan for user {telegram_id}")


async def curse_events_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    pending = await db.get_pending_curse_events()
    for event in pending:
        if event["scheduled_at"] > now:
            continue

        result = await db.execute_curse_event(event["id"])
        if result is None:
            continue

        chat_id = result["chat_id"]
        telegram_id = result["telegram_id"]
        row = await db.get_user(telegram_id)
        name = esc(display_name(row)) if row else "Someone"

        if result["protected"]:
            text = f"🛡️ The curse tried to strike {name} but their condom blocked it!"
        else:
            text = f"🌙 The curse struck {name} for <b>-{result['amount']}cm</b>! (now {result['new_height']}cm)"

        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception(f"Failed to send curse strike message for event {event['id']}")


# ---------------- Startup ----------------

async def post_init(application: Application):
    await db.init_pool()
    await db.run_schema()
    logger.info("Database ready.")


def main():
    token = os.environ["BOT_TOKEN"]
    application = Application.builder().token(token).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("grow", grow))
    application.add_handler(CommandHandler("attack", attack))
    application.add_handler(CommandHandler("me", me))
    application.add_handler(CommandHandler("loan", loan))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("pray", pray_cmd))
    application.add_handler(CommandHandler("simp", simp_cmd))
    application.add_handler(CommandHandler("hex", hex_cmd))
    application.add_handler(CommandHandler("snitch", snitch_cmd))
    application.add_handler(CommandHandler("condom", condom_cmd))
    application.add_handler(CommandHandler("cursethisgroup", cursethisgroup))
    application.add_handler(CallbackQueryHandler(attack_callback, pattern=r"^atk:\d+$"))

    application.job_queue.run_repeating(
        loan_repayment_job, interval=LOAN_CHECK_INTERVAL_SECONDS, first=10
    )
    application.job_queue.run_repeating(
        curse_events_job, interval=CURSE_CHECK_INTERVAL_SECONDS, first=15
    )

    logger.info("Bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
