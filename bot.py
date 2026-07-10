import logging
import os
import random
from datetime import datetime, timezone

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

LOAN_AMOUNT = 10
LOAN_FLOOR = -10  # a defaulted loan can push a user down to this, but no further
LOAN_DURATION_HOURS = 24
LOAN_CHECK_INTERVAL_SECONDS = 300  # how often the repayment job runs


def mention(user) -> str:
    name = user.first_name or user.username or "someone"
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def display_name(row) -> str:
    return row["first_name"] or row["username"] or "Unknown"


# ---------------- Commands ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.get_or_create_user(u.id, u.username, u.first_name)
    await update.message.reply_html(
        "📏 Welcome to <b>DickGrow Bot</b>!\n\n"
        "/grow — grow 10-25cm, once a day\n"
        "/attack &lt;amount&gt; — challenge the group for any amount from 1cm up to your full height\n"
        "/me — check your stats\n"
        "/loan — borrow 10cm when you're at 0\n"
        "/leaderboard — today's and all-time rankings\n"
    )


async def grow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await db.get_or_create_user(u.id, u.username, u.first_name)

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
    new_height = await db.apply_growth(u.id, amount, "grow", set_last_grow=True)

    await update.message.reply_html(
        f"🌱 {mention(u)} grew <b>+{amount}cm</b> today!\n"
        f"📏 New height: <b>{new_height}cm</b>"
    )


async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await db.get_or_create_user(u.id, u.username, u.first_name)

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
        f"⚔️ <b>{challenger_name}</b> vs <b>{clicker_name}</b> — {amount}cm on the line!\n\n"
        f"🏆 <b>{winner_name}</b> wins!\n"
        f"💀 {loser_name} loses {amount}cm.\n\n"
        f"📏 {winner_name}: {winner_row['height_cm']}cm\n"
        f"📏 {loser_name}: {loser_row['height_cm']}cm"
    )

    await query.edit_message_text(text=result_text, parse_mode=ParseMode.HTML)
    await query.answer("You won! 🏆" if winner_id == clicker.id else "You lost this one 💀")


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    loan_text = ""
    if user["loan_active"]:
        loan_text = f"\n💰 Active loan: {user['loan_amount']}cm (auto-repays in {LOAN_DURATION_HOURS}h)"
    await update.message.reply_html(
        f"📏 <b>{u.first_name}</b>\nHeight: <b>{user['height_cm']}cm</b>{loan_text}"
    )


async def loan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await db.get_or_create_user(u.id, u.username, u.first_name)

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
        f"If you're still at 0cm by then, you'll go to <b>-10cm</b>."
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_top = await db.get_leaderboard_today(limit=5)
    alltime_top = await db.get_leaderboard_alltime(limit=5)

    lines = ["🏆 <b>Today's Top Gainers</b>"]
    if today_top:
        for i, row in enumerate(today_top, 1):
            lines.append(f"{i}. {display_name(row)} — {row['gained']:+d}cm today")
    else:
        lines.append("No activity yet today.")

    lines.append("\n👑 <b>All-Time Rankings</b>")
    for i, row in enumerate(alltime_top, 1):
        lines.append(f"{i}. {display_name(row)} — {row['height_cm']}cm")

    await update.message.reply_html("\n".join(lines))


# ---------------- Background job ----------------

async def loan_repayment_job(context: ContextTypes.DEFAULT_TYPE):
    due_loans = await db.get_due_loans(LOAN_DURATION_HOURS)
    for loan_row in due_loans:
        telegram_id = loan_row["telegram_id"]
        await db.apply_growth(telegram_id, -LOAN_AMOUNT, "loan_repay", floor=LOAN_FLOOR)
        await db.clear_loan(telegram_id)
        logger.info(f"Repaid loan for user {telegram_id}")


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
    application.add_handler(CallbackQueryHandler(attack_callback, pattern=r"^atk:\d+$"))

    application.job_queue.run_repeating(
        loan_repayment_job, interval=LOAN_CHECK_INTERVAL_SECONDS, first=10
    )

    logger.info("Bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
