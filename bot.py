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

LOAN_AMOUNT = 10
LOAN_FLOOR = -10  # a defaulted loan can push a user down to this, but no further
LOAN_DURATION_HOURS = 24
LOAN_CHECK_INTERVAL_SECONDS = 300  # how often the repayment job runs

# ---- Pray ----
PRAY_SUCCESS_CHANCE = 0.01
PRAY_SUCCESS_AMOUNT = 100

# ---- Simp ----
SIMP_MAX_AMOUNT = 50
SIMP_COOLDOWN_HOURS = 24

# ---- Hex ----
HEX_COST = 15
HEX_COOLDOWN_HOURS = 6
HEX_DEBUFF_WINDOW_HOURS = 12  # how long the curse waits around for their next /grow
HEX_MULTIPLIER = 0.5  # halves the next grow roll

# ---- Condom shop ----
CONDOM_DURATION_HOURS = 50
CONDOM_PRICE_TIERS = [  # (height ceiling, price) — first match wins
    (100, 5),
    (1000, 50),
    (float("inf"), 200),
]

# ---- Snitch ----
SNITCH_STEAL_MIN_PCT = 20
SNITCH_STEAL_MAX_PCT = 30
SNITCH_COOLDOWN_HOURS = 24

# ---- Curse this group ----
CURSE_TARGET_COUNT = 5
CURSE_DMG_MIN, CURSE_DMG_MAX = 5, 15
CURSE_WINDOW_HOURS = 1
CURSE_COOLDOWN_HOURS = 12
CURSE_CHECK_INTERVAL_SECONDS = 30  # how often we check for due curse hits

# ---- Gamble / pussy ----
GAMBLE_COOLDOWN_HOURS = 24
GAMBLE_WIN_CHANCE = 0.5
GAMBLE_WIN_AMOUNT = 50
PUSSY_DURATION_HOURS = 2
FUCK_GAIN_HOUR1 = (2, 5)
FUCK_GAIN_HOUR2 = (5, 10)
PUSSY_ACCUM_RANGE = (5, 10)  # same range both hours, per spec
PUSSY_CHECK_INTERVAL_SECONDS = 30


def mention(user) -> str:
    name = user.first_name or user.username or "someone"
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def display_name(row) -> str:
    return row["first_name"] or row["username"] or "Unknown"


def height_price(height_cm: int) -> int:
    for ceiling, price in CONDOM_PRICE_TIERS:
        if height_cm < ceiling:
            return price
    return CONDOM_PRICE_TIERS[-1][1]


def fmt_hours_minutes(remaining_seconds: float) -> str:
    hours, minutes = int(remaining_seconds // 3600), int((remaining_seconds % 3600) // 60)
    return f"{hours}h {minutes}m"


async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, usage: str):
    """
    Resolves a target user either from a reply-to-message or a @username arg.
    Sends a usage message and returns None if it can't figure out who's meant.
    """
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user
        await db.get_or_create_user(target_user.id, target_user.username, target_user.first_name)
        return target_user

    if context.args:
        handle = context.args[0].lstrip("@")
        row = await db.get_user_by_username(handle)
        if row is None:
            await update.message.reply_text(
                f"Can't find @{handle} — they need to have used the bot here first."
            )
            return None

        class _FakeUser:
            id = row["telegram_id"]
            username = row["username"]
            first_name = row["first_name"]

        return _FakeUser()

    await update.message.reply_html(usage)
    return None


async def track_chat(update: Update):
    """Records that this user is active in this chat (used by /cursethisgroup)."""
    chat = update.effective_chat
    if chat and chat.type != "private":
        await db.record_chat_member(chat.id, update.effective_user.id)


async def block_if_pussy(update: Update, u, user_row) -> bool:
    """
    If the user is currently in pussy status, replies with a blocked message
    and returns True (caller should stop). Otherwise returns False.
    """
    if not user_row["pussy_active"]:
        return False
    started = user_row["pussy_started_at"]
    now = datetime.now(timezone.utc)
    ends = started + timedelta(hours=PUSSY_DURATION_HOURS)
    remaining = max(0, (ends - now).total_seconds())
    await update.message.reply_html(
        random.choice(PUSSY_BLOCKED).format(name=mention(u), remaining=fmt_hours_minutes(remaining))
    )
    return True


# ---------------- Flavor text banks ----------------
# Each list has 10-20 variants so the bot doesn't repeat itself. {name}/{target}/
# {amount}/{cost} etc. get filled in by .format() at call sites.

PRAY_SUCCESS = [
    "🙏 {name} prayed and God said 'bet.' +{amount}cm, certified miracle.",
    "🙏 A literal miracle. {name} just pulled +{amount}cm out of thin air.",
    "🙏 The heavens opened up for {name}. +{amount}cm, no cap.",
    "🙏 {name} rolled a 1-in-100 and God personally showed up. +{amount}cm.",
    "🙏 Somewhere a priest just felt a disturbance. {name} +{amount}cm.",
    "🙏 {name}'s prayers got answered fr fr. +{amount}cm blessing incoming.",
    "🙏 Jesus Christ himself (the real one) blessed {name} with +{amount}cm.",
    "🙏 {name} hit the holy jackpot. +{amount}cm, go to church or something.",
    "🙏 The RNG gods looked down on {name} with mercy. +{amount}cm.",
    "🙏 {name} manifested it and the universe delivered. +{amount}cm.",
]

PRAY_FAIL = [
    "🙏 {name} prayed and God left them on read. No cm for you.",
    "🙏 God saw {name}'s search history and said no.",
    "🙏 {name} prayed. The response was radio silence and a little judgment.",
    "🙏 Nothing happened. Even the universe doesn't believe in {name}.",
    "🙏 {name} rolled the dice with the Almighty and got dust.",
    "🙏 God's out to lunch, {name}. Try again tomorrow, hoe.",
    "🙏 {name} asked the heavens for growth and got a 404 not found.",
    "🙏 Divine intervention: denied. {name} stays exactly this mid.",
    "🙏 {name} prayed so hard nothing changed whatsoever. Iconic, actually.",
    "🙏 God read the message and left {name} on delivered.",
    "🙏 The lord giveth, and today he gaveth {name} absolutely nothing.",
    "🙏 {name} lit a candle for +cm and got a fire hazard instead.",
    "🙏 Prayer status: ignored. God's got bigger fish, {name}.",
    "🙏 {name} really thought a text message could fix their size. Cute.",
]

PRAY_ALREADY = [
    "🙏 {name}, you already used your one shot with God today. Come back tomorrow.",
    "🙏 God's not taking a second call from {name} today. Try tomorrow, hoe.",
    "🙏 {name} already prayed once. Spamming the Lord isn't gonna work twice.",
    "🙏 The heavens are closed for {name} until tomorrow. One prayer a day, max.",
    "🙏 {name}, you begged already. God's got a do-not-disturb setting for a reason.",
    "🙏 One miracle request per day, {name}. The line's busy, try again tomorrow.",
    "🙏 God already answered {name} today (or didn't). Either way, come back tomorrow.",
    "🙏 {name} really tried to double-dip on divine favors. Denied, permanently, for today.",
]

SIMP_SUCCESS = [
    "💸 {name} just paid rent to {target}. Simp tax: {amount}cm.",
    "💸 {name} tossed {amount}cm at {target} like it's raining. Certified simp behavior.",
    "💸 {name} handed {target} {amount}cm, no questions asked. Absolute hoe hours.",
    "💸 {name} caught feelings and paid {target} {amount}cm for it.",
    "💸 {target} just got blessed with {amount}cm by simp-in-chief {name}.",
    "💸 {name} gave {target} {amount}cm expecting nothing back. Lmao sure.",
    "💸 Simp alert: {name} sent {amount}cm to {target}, no cap.",
    "💸 {name} really said 'take my cm' to {target}. {amount}cm gone.",
    "💸 {target} secured the bag ({amount}cm) off {name}'s simp energy.",
    "💸 {name} donated {amount}cm to {target}'s cause. Sponsored by desperation.",
    "💸 {name} just Venmo'd {target} {amount}cm in dick-inches. Weird flex.",
    "💸 {target} eating good tonight thanks to {name}'s {amount}cm tribute.",
]

SIMP_LIMIT = "💸 Max simp tribute is {max}cm, {name}. Even simps have budgets."
SIMP_COOLDOWN_MSG = "💸 {name}, you already simped today. One tribute per day, save some for tomorrow."
SIMP_BROKE = "💸 {name}, you don't even have {amount}cm to give away. Get your own bag first."

HEX_SUCCESS = [
    "😈 {name} cursed {target} for {cost}cm. Their next /grow is about to be sad.",
    "😈 {target} just got hexed by {name}. Enjoy the shrinkage, buddy.",
    "😈 {name} spent {cost}cm to put a jinx on {target}'s next grow. Petty and effective.",
    "😈 A dark ritual was performed by {name} on {target}. -50% next grow, no refunds.",
    "😈 {target}'s about to grow like it's a bad hair day, courtesy of {name}.",
    "😈 {name} paid {cost}cm for pure spite. {target} is now cursed.",
    "😈 The hex lands. {target}, your next grow just got nerfed by {name}.",
    "😈 {name} said 'not today' to {target}'s growth. {cost}cm well spent.",
    "😈 {target} has been jinxed by {name}. Karma's coming for you eventually though.",
    "😈 {name} activated villain arc, cursed {target} for {cost}cm.",
]

HEX_BROKE = "😈 You need {cost}cm to cast a hex, {name}. You're too broke to be this evil."
HEX_COOLDOWN_MSG = "😈 {name}, your hex energy's on cooldown. Even curses need a breather."
HEX_SELF = "😈 You can't hex yourself, {name}. That's just called low self-esteem."

HEX_TRIGGERED = [
    "💀 {name} tried to grow but the hex kicked in. Grow slashed to +{amount}cm. Someone cursed you, lol.",
    "💀 Cursed! {name}'s grow got cut down to +{amount}cm thanks to a hex.",
    "💀 {name} felt a dark presence mid-grow. Only +{amount}cm today. Skill issue, someone hexed you.",
    "💀 The hex strikes. {name} only grows +{amount}cm this time. Find out who did this to you.",
    "💀 {name}'s growth got nerfed by a curse. +{amount}cm instead of the usual. Ouch.",
    "💀 Someone out there is laughing. {name} only got +{amount}cm from the hexed roll.",
]

CONDOM_SUCCESS = [
    "🛡️ {name} suited up. Condom purchased for {cost}cm, protected for {hours}h.",
    "🛡️ Safety first. {name} bought protection for {cost}cm, good for {hours}h.",
    "🛡️ {name} is now wrapped up for {hours}h. Cost: {cost}cm. Stay safe out there.",
    "🛡️ {name} copped a condom for {cost}cm. Snitches can't touch you for {hours}h.",
    "🛡️ Protection secured. {name} is safe from thieves for {hours}h ({cost}cm spent).",
    "🛡️ {name} really said 'not on my watch' and bought {hours}h of protection for {cost}cm.",
    "🛡️ {name} is now condom'd up. {hours}h of immunity from snitches, {cost}cm well spent.",
]

CONDOM_BROKE = "🛡️ You need {cost}cm for protection at your size, {name}. Can't even afford safe sex smh."
CONDOM_ACTIVE = "🛡️ {name}, you're already wrapped up until {until}. No need to double up."

SNITCH_SUCCESS = [
    "🕵️ {name} snitched on {target} and stole {amount}cm. Rat behavior.",
    "🕵️ {target} got robbed blind by {name}. {amount}cm gone, no trace.",
    "🕵️ {name} pulled a heist on {target}, walked away with {amount}cm.",
    "🕵️ Snitches get riches. {name} took {amount}cm off {target} in broad daylight.",
    "🕵️ {target} just got jumped by {name} for {amount}cm. Watch your back next time.",
    "🕵️ {name} really said 'that's mine now' and yanked {amount}cm off {target}.",
    "🕵️ {target} down {amount}cm courtesy of {name}'s sneaky ass.",
    "🕵️ {name} snitched {target} for {amount}cm. No condom, no mercy.",
    "🕵️ Robbery in progress: {name} made off with {amount}cm from {target}.",
    "🕵️ {target}'s pockets got picked by {name}. {amount}cm lighter now.",
]

SNITCH_BLOCKED = [
    "🛡️ {target} is wrapped up and protected. {name}'s snitch attempt bounced right off.",
    "🛡️ Nice try, {name}. {target}'s got a condom on, nothing to steal here.",
    "🛡️ {name} tried to rob {target} but got denied — protection's still active.",
    "🛡️ {target} saw {name} coming from a mile away. Condom held strong.",
    "🛡️ {name}'s heist failed. {target} is safe until their protection runs out.",
    "🛡️ Blocked. {target}'s condom absorbed {name}'s whole snitch attempt.",
]

SNITCH_COOLDOWN_MSG = "🕵️ {name}, you already snitched today. Lay low for a bit."
SNITCH_NOTHING = "🕵️ {name} tried to snitch {target} but they've got nothing worth stealing. Sad."
SNITCH_SELF = "🕵️ You can't snitch yourself, {name}. That's called being broke, not a crime."

CURSE_START = [
    "🌪️ {name} just cursed the whole damn group. 5 unlucky souls about to lose cm over the next hour.",
    "🌪️ {name} unleashed chaos on the group. Somewhere, 5 people are about to have a bad hour.",
    "🌪️ A dark cloud rolls over the chat, summoned by {name}. 5 victims incoming.",
    "🌪️ {name} said 'let's ruin everyone's day' and the group is now cursed for 1 hour.",
    "🌪️ Group curse activated by {name}. 5 random members about to eat losses.",
    "🌪️ {name} pressed the self-destruct button on the whole chat. 5 will suffer.",
]

CURSE_HIT = [
    "⚡ The curse strikes {name} — -{amount}cm. Bad luck really said 'you specifically.'",
    "⚡ {name} just got hit by the curse. -{amount}cm, no warning, no mercy.",
    "⚡ Unlucky! {name} loses {amount}cm to the group curse.",
    "⚡ The chaos found {name}. -{amount}cm, courtesy of the curse.",
    "⚡ {name} got smited out of nowhere. -{amount}cm.",
    "⚡ Curse damage: {name} down {amount}cm. Wrong place, wrong time.",
    "⚡ {name} just paid the group curse toll. -{amount}cm.",
]

CURSE_HIT_INITIATOR = [
    "⚡ Karma's instant today — {name} started this curse and got hit first. -{amount}cm. Should've thought that through.",
    "⚡ {name} summoned the curse and it turned around and bit them too. -{amount}cm. Poetic.",
    "⚡ Plot twist: {name}, the one who cursed everyone, also takes -{amount}cm. Own goal.",
    "⚡ {name} played themselves. -{amount}cm from the very curse they started.",
    "⚡ Chaos doesn't discriminate — {name} eats -{amount}cm from their own curse.",
]

CURSE_COOLDOWN_MSG = "🌪️ This group's still recovering from the last curse, {name}. Try again later."

GAMBLE_WIN = [
    "🎲 {name} rolled the dice and won big. +{amount}cm, absolute unit.",
    "🎲 {name} beat the odds. +{amount}cm, house always loses sometimes.",
    "🎲 Jackpot for {name}! +{amount}cm, no strings attached.",
    "🎲 {name} gambled and it actually paid off. +{amount}cm.",
    "🎲 The dice loved {name} today. +{amount}cm.",
    "🎲 {name} risked it for the biscuit and got +{amount}cm.",
    "🎲 Lucky roll, {name}! +{amount}cm straight to the bank.",
]

GAMBLE_LOSE = [
    "🎲 {name} gambled and lost hard. Dick's now a pussy for {hours}h. Everyone run it up.",
    "🎲 Unlucky roll. {name}'s dick just became a pussy for {hours}h. Free real estate, boys.",
    "🎲 {name} bet it all and lost everything. Pussy status active for {hours}h — get in line.",
    "🎲 The house wins. {name} is now walking around pussy-mode for {hours}h.",
    "🎲 {name} rolled the wrong number. Transformation complete, pussy for {hours}h. Chat, you're welcome.",
    "🎲 Coin flip said no. {name}'s officially pussy for the next {hours}h. Open season.",
    "🎲 {name} really gambled their whole identity and lost. Pussy for {hours}h.",
]

GAMBLE_COOLDOWN_MSG = "🎲 {name}, the casino's closed for you today. One gamble per day."

FUCK_SUCCESS_H1 = [
    "😩 {actor} ran up on {target} and got +{amount}cm. First hour rates, could be worse.",
    "😩 {actor} hit it and quit it. +{amount}cm off {target}.",
    "😩 {actor} took their shot at {target}. +{amount}cm secured.",
    "😩 {target} got run through by {actor}. +{amount}cm gained.",
    "😩 {actor} said 'my turn' and walked away with +{amount}cm from {target}.",
]

FUCK_SUCCESS_H2 = [
    "🔥 {actor} came back for round two on {target} — rates went up, +{amount}cm.",
    "🔥 Second hour, bigger numbers. {actor} got +{amount}cm off {target}.",
    "🔥 {actor} ran it back on {target} for +{amount}cm. Efficient.",
    "🔥 {target} took another hit from {actor}. +{amount}cm, hour 2 premium.",
    "🔥 {actor} doubled down on {target} and cashed out +{amount}cm.",
]

FUCK_ALREADY = "😤 {actor}, you already got yours with {target} this hour. Patience."
FUCK_NOT_PUSSY = "😤 {target} isn't in pussy mode right now, {actor}. Nothing to see here."
FUCK_SELF = "😤 You can't fuck yourself for cm gains, {actor}. Well, you can, but it doesn't count here."

PUSSY_HOUR2_ANNOUNCE = [
    "🔔 Round two is OPEN. {name}'s still pussy for another hour — everyone gets one more shot.",
    "🔔 {name} survived hour one. Second wave starts now, rates just went up.",
    "🔔 Hour 2 unlocked on {name}. Get your second /fuck in before time's up.",
    "🔔 {name}'s pussy status renewed for another hour. Line forms here again.",
]

PUSSY_BLOCKED = [
    "🙅 {name}, you're pussy right now — no dick commands until it wears off in {remaining}.",
    "🙅 Can't do that mid-transformation, {name}. {remaining} left on your pussy status.",
    "🙅 {name}, you got no dick to work with right now. {remaining} to go.",
    "🙅 That command needs a dick, {name}, and you're fresh out for {remaining}.",
    "🙅 {name}, sit this one out — pussy status active for {remaining} more.",
]

PUSSY_FINALIZE = [
    "✨ {name}'s pussy phase is over. All that action added up: +{amount}cm back to their real height. Win win, as promised.",
    "✨ Transformation reversed. {name} banks +{amount}cm from everyone who ran through. Not bad for doing nothing.",
    "✨ {name}'s back to normal, {amount}cm richer for it. Sometimes losing is winning.",
    "✨ Pussy mode: complete. {name} collects +{amount}cm in accumulated gains.",
    "✨ {name} is whole again, and +{amount}cm heavier for the experience.",
]


# ---------------- Commands ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.get_or_create_user(u.id, u.username, u.first_name)
    await update.message.reply_html(
        "📏 Welcome to <b>DickGrow Bot</b>!\n\n"
        "<b>Core</b>\n"
        "/grow — grow 10-25cm, once a day\n"
        "/attack &lt;amount&gt; — challenge the group for any amount from 1cm up to your full height\n"
        "/me — check your stats\n"
        "/loan — borrow 10cm when you're at 0\n"
        "/leaderboard — today's and all-time rankings\n\n"
        "<b>Chaos</b>\n"
        "/pray — 1% shot at +100cm, once a day\n"
        "/simp &lt;amount&gt; @user — tribute cm to someone, max 50/day\n"
        "/hex @user — curse their next /grow for 15cm\n"
        "/shop, /buycondom — protection from /snitch\n"
        "/snitch @user — steal 20-30% of their height\n"
        "/cursethisgroup — curse 5 random members for an hour\n"
        "/gamble — 50/50 for +50cm or 2h as a pussy\n"
        "/fuck @user — if someone's a pussy, get yours\n"
    )


async def grow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

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
    hexed = await db.consume_hex_if_active(u.id)
    if hexed:
        amount = max(1, int(amount * HEX_MULTIPLIER))

    new_height = await db.apply_growth(u.id, amount, "grow", set_last_grow=True)

    if hexed:
        await update.message.reply_html(
            random.choice(HEX_TRIGGERED).format(name=mention(u), amount=amount)
            + f"\n📏 New height: <b>{new_height}cm</b>"
        )
    else:
        await update.message.reply_html(
            f"🌱 {mention(u)} grew <b>+{amount}cm</b> today!\n"
            f"📏 New height: <b>{new_height}cm</b>"
        )


async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

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

    if clicker_row["pussy_active"]:
        await query.answer("You've got no dick to fight with right now 🙅", show_alert=True)
        return

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
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    loan_text = ""
    if user["loan_active"]:
        loan_text = f"\n💰 Active loan: {user['loan_amount']}cm (auto-repays in {LOAN_DURATION_HOURS}h)"

    condom_text = ""
    now = datetime.now(timezone.utc)
    if user["condom_until"] and user["condom_until"] > now:
        condom_text = f"\n🛡️ Protected until {user['condom_until'].strftime('%H:%M UTC')}"

    pussy_text = ""
    if user["pussy_active"]:
        ends = user["pussy_started_at"] + timedelta(hours=PUSSY_DURATION_HOURS)
        remaining = max(0, (ends - now).total_seconds())
        pussy_text = f"\n😳 Pussy status: {fmt_hours_minutes(remaining)} remaining"

    await update.message.reply_html(
        f"📏 <b>{u.first_name}</b>\nHeight: <b>{user['height_cm']}cm</b>{loan_text}{condom_text}{pussy_text}"
    )


async def loan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

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


async def pray(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

    last_pray = user["last_pray"]
    if last_pray is not None:
        elapsed = (datetime.now(timezone.utc) - last_pray).total_seconds()
        if elapsed < 24 * 3600:
            await update.message.reply_html(random.choice(PRAY_ALREADY).format(name=mention(u)))
            return

    await db.set_last_pray(u.id)
    if random.random() < PRAY_SUCCESS_CHANCE:
        await db.apply_growth(u.id, PRAY_SUCCESS_AMOUNT, "pray_win")
        await update.message.reply_html(
            random.choice(PRAY_SUCCESS).format(name=mention(u), amount=PRAY_SUCCESS_AMOUNT)
        )
    else:
        await update.message.reply_html(random.choice(PRAY_FAIL).format(name=mention(u)))


async def simp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

    usage = (
        f"💸 Usage: <code>/simp &lt;amount&gt; @user</code> (max {SIMP_MAX_AMOUNT}cm/day), "
        f"or reply to their message with <code>/simp &lt;amount&gt;</code>"
    )

    reply_msg = update.message.reply_to_message
    if reply_msg and reply_msg.from_user:
        if not context.args:
            await update.message.reply_html(usage)
            return
        amount_str = context.args[0]
        target_user = reply_msg.from_user
        await db.get_or_create_user(target_user.id, target_user.username, target_user.first_name)
    else:
        if len(context.args) < 2:
            await update.message.reply_html(usage)
            return
        amount_str = context.args[0]
        handle = context.args[1].lstrip("@")
        row = await db.get_user_by_username(handle)
        if row is None:
            await update.message.reply_text(
                f"Can't find @{handle} — they need to have used the bot here first."
            )
            return

        class _FakeUser:
            id = row["telegram_id"]
            username = row["username"]
            first_name = row["first_name"]

        target_user = _FakeUser()

    try:
        amount = int(amount_str)
    except ValueError:
        await update.message.reply_text("That's not a valid number. Try e.g. /simp 20 @someone")
        return

    if amount < 1:
        await update.message.reply_text("Gotta simp for at least 1cm.")
        return

    if amount > SIMP_MAX_AMOUNT:
        await update.message.reply_html(SIMP_LIMIT.format(max=SIMP_MAX_AMOUNT, name=mention(u)))
        return

    if target_user.id == u.id:
        await update.message.reply_text("You can't simp for yourself, that's just called savings.")
        return

    last_simp = user["last_simp"]
    if last_simp is not None:
        elapsed = (datetime.now(timezone.utc) - last_simp).total_seconds()
        if elapsed < SIMP_COOLDOWN_HOURS * 3600:
            await update.message.reply_html(SIMP_COOLDOWN_MSG.format(name=mention(u)))
            return

    if user["height_cm"] < amount:
        await update.message.reply_html(SIMP_BROKE.format(name=mention(u), amount=amount))
        return

    ok = await db.transfer_cm(u.id, target_user.id, amount, "simp_out", "simp_in")
    if not ok:
        await update.message.reply_html(SIMP_BROKE.format(name=mention(u), amount=amount))
        return

    await db.set_last_simp(u.id)
    await update.message.reply_html(
        random.choice(SIMP_SUCCESS).format(name=mention(u), target=mention(target_user), amount=amount)
    )


async def hex_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

    usage = "😈 Usage: <code>/hex @user</code> or reply to their message with <code>/hex</code>"
    target_user = await resolve_target(update, context, usage)
    if target_user is None:
        return

    if target_user.id == u.id:
        await update.message.reply_html(HEX_SELF.format(name=mention(u)))
        return

    last_hex = user["last_hex_cast"]
    if last_hex is not None:
        elapsed = (datetime.now(timezone.utc) - last_hex).total_seconds()
        if elapsed < HEX_COOLDOWN_HOURS * 3600:
            await update.message.reply_html(HEX_COOLDOWN_MSG.format(name=mention(u)))
            return

    if user["height_cm"] < HEX_COST:
        await update.message.reply_html(HEX_BROKE.format(cost=HEX_COST, name=mention(u)))
        return

    ok = await db.cast_hex(u.id, target_user.id, HEX_COST, HEX_DEBUFF_WINDOW_HOURS)
    if not ok:
        await update.message.reply_html(HEX_BROKE.format(cost=HEX_COST, name=mention(u)))
        return

    await db.set_last_hex_cast(u.id)
    await update.message.reply_html(
        random.choice(HEX_SUCCESS).format(name=mention(u), target=mention(target_user), cost=HEX_COST)
    )


async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    price = height_price(user["height_cm"])
    await update.message.reply_html(
        "🛒 <b>Shop</b>\n\n"
        f"🛡️ Condom — {price}cm — blocks /snitch for {CONDOM_DURATION_HOURS}h\n"
        f"(price scales with your size — bigger you are, more it costs)\n\n"
        "Buy with <code>/buycondom</code>"
    )


async def buycondom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

    now = datetime.now(timezone.utc)
    if user["condom_until"] and user["condom_until"] > now:
        await update.message.reply_html(
            CONDOM_ACTIVE.format(name=mention(u), until=user["condom_until"].strftime("%H:%M UTC"))
        )
        return

    cost = height_price(user["height_cm"])
    ok = await db.buy_condom(u.id, cost, CONDOM_DURATION_HOURS)
    if not ok:
        await update.message.reply_html(CONDOM_BROKE.format(cost=cost, name=mention(u)))
        return

    await update.message.reply_html(
        random.choice(CONDOM_SUCCESS).format(name=mention(u), cost=cost, hours=CONDOM_DURATION_HOURS)
    )


async def snitch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

    usage = "🕵️ Usage: <code>/snitch @user</code> or reply to their message with <code>/snitch</code>"
    target_user = await resolve_target(update, context, usage)
    if target_user is None:
        return

    if target_user.id == u.id:
        await update.message.reply_html(SNITCH_SELF.format(name=mention(u)))
        return

    last_snitch = user["last_snitch"]
    if last_snitch is not None:
        elapsed = (datetime.now(timezone.utc) - last_snitch).total_seconds()
        if elapsed < SNITCH_COOLDOWN_HOURS * 3600:
            await update.message.reply_html(SNITCH_COOLDOWN_MSG.format(name=mention(u)))
            return

    target_row = await db.get_user(target_user.id)
    now = datetime.now(timezone.utc)
    if target_row["condom_until"] and target_row["condom_until"] > now:
        await db.set_last_snitch(u.id)
        await update.message.reply_html(
            random.choice(SNITCH_BLOCKED).format(name=mention(u), target=mention(target_user))
        )
        return

    amount = await db.snitch_steal(u.id, target_user.id, SNITCH_STEAL_MIN_PCT, SNITCH_STEAL_MAX_PCT)
    await db.set_last_snitch(u.id)

    if amount <= 0:
        await update.message.reply_html(
            SNITCH_NOTHING.format(name=mention(u), target=mention(target_user))
        )
        return

    await update.message.reply_html(
        random.choice(SNITCH_SUCCESS).format(name=mention(u), target=mention(target_user), amount=amount)
    )


async def cursethisgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("This only works in a group, genius.")
        return

    await track_chat(update)
    await db.get_or_create_user(u.id, u.username, u.first_name)

    last_curse = await db.get_last_group_curse(chat.id)
    if last_curse is not None:
        elapsed = (datetime.now(timezone.utc) - last_curse["started_at"]).total_seconds()
        if elapsed < CURSE_COOLDOWN_HOURS * 3600:
            await update.message.reply_html(CURSE_COOLDOWN_MSG.format(name=mention(u)))
            return

    member_ids = await db.get_chat_member_ids(chat.id)
    if u.id not in member_ids:
        member_ids.append(u.id)

    if len(member_ids) <= CURSE_TARGET_COUNT:
        victims = member_ids
    else:
        others = [m for m in member_ids if m != u.id]
        random.shuffle(others)
        victims = others[: CURSE_TARGET_COUNT - 1] + [u.id]

    if u.id not in victims:
        victims.append(u.id)

    await db.create_group_curse(
        chat.id, u.id, victims, CURSE_WINDOW_HOURS, CURSE_DMG_MIN, CURSE_DMG_MAX
    )

    await update.message.reply_html(random.choice(CURSE_START).format(name=mention(u)))


async def gamble(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    user = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, user):
        return

    last_gamble = user["last_gamble"]
    if last_gamble is not None:
        elapsed = (datetime.now(timezone.utc) - last_gamble).total_seconds()
        if elapsed < GAMBLE_COOLDOWN_HOURS * 3600:
            await update.message.reply_html(GAMBLE_COOLDOWN_MSG.format(name=mention(u)))
            return

    await db.set_last_gamble(u.id)

    if random.random() < GAMBLE_WIN_CHANCE:
        new_height = await db.gamble_win(u.id, GAMBLE_WIN_AMOUNT)
        await update.message.reply_html(
            random.choice(GAMBLE_WIN).format(name=mention(u), amount=GAMBLE_WIN_AMOUNT)
            + f"\n📏 New height: <b>{new_height}cm</b>"
        )
    else:
        await db.gamble_lose_become_pussy(u.id, update.effective_chat.id)
        await update.message.reply_html(
            random.choice(GAMBLE_LOSE).format(name=mention(u), hours=PUSSY_DURATION_HOURS)
        )


async def fuck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await track_chat(update)
    actor = await db.get_or_create_user(u.id, u.username, u.first_name)
    if await block_if_pussy(update, u, actor):
        return

    usage = "😩 Usage: <code>/fuck @user</code> or reply to their message with <code>/fuck</code>"
    target_user = await resolve_target(update, context, usage)
    if target_user is None:
        return

    if target_user.id == u.id:
        await update.message.reply_html(FUCK_SELF.format(actor=mention(u)))
        return

    target_row = await db.get_pussy_status(target_user.id)
    if target_row is None:
        await update.message.reply_html(FUCK_NOT_PUSSY.format(actor=mention(u), target=mention(target_user)))
        return

    now = datetime.now(timezone.utc)
    elapsed = (now - target_row["pussy_started_at"]).total_seconds()
    if elapsed >= PUSSY_DURATION_HOURS * 3600:
        # Expired but the periodic job hasn't cleaned it up yet — treat as not-pussy.
        await update.message.reply_html(FUCK_NOT_PUSSY.format(actor=mention(u), target=mention(target_user)))
        return

    hour_window = 1 if elapsed < 3600 else 2
    logged = await db.record_fuck(target_user.id, u.id, hour_window)
    if not logged:
        await update.message.reply_html(FUCK_ALREADY.format(actor=mention(u), target=mention(target_user)))
        return

    if hour_window == 1:
        actor_gain = random.randint(*FUCK_GAIN_HOUR1)
        variants = FUCK_SUCCESS_H1
    else:
        actor_gain = random.randint(*FUCK_GAIN_HOUR2)
        variants = FUCK_SUCCESS_H2

    pussy_gain = random.randint(*PUSSY_ACCUM_RANGE)
    await db.apply_fuck_gain(u.id, target_user.id, actor_gain, pussy_gain)

    await update.message.reply_html(
        random.choice(variants).format(actor=mention(u), target=mention(target_user), amount=actor_gain)
    )


# ---------------- Background job ----------------

async def loan_repayment_job(context: ContextTypes.DEFAULT_TYPE):
    due_loans = await db.get_due_loans(LOAN_DURATION_HOURS)
    for loan_row in due_loans:
        telegram_id = loan_row["telegram_id"]
        await db.apply_growth(telegram_id, -LOAN_AMOUNT, "loan_repay", floor=LOAN_FLOOR)
        await db.clear_loan(telegram_id)
        logger.info(f"Repaid loan for user {telegram_id}")


async def curse_hits_job(context: ContextTypes.DEFAULT_TYPE):
    due_hits = await db.get_due_curse_hits()
    for hit in due_hits:
        new_height = await db.apply_curse_hit(hit["id"], hit["telegram_id"], hit["amount"])
        user_row = await db.get_user(hit["telegram_id"])
        name_html = f'<a href="tg://user?id={hit["telegram_id"]}">{display_name(user_row)}</a>'
        variants = CURSE_HIT_INITIATOR if hit["is_initiator"] else CURSE_HIT
        text = random.choice(variants).format(name=name_html, amount=hit["amount"])
        try:
            await context.bot.send_message(
                chat_id=hit["chat_id"], text=f"{text}\n📏 New height: <b>{new_height}cm</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"Failed to send curse hit message to chat {hit['chat_id']}: {e}")


async def pussy_status_job(context: ContextTypes.DEFAULT_TYPE):
    # Hour-2 "everyone can fuck again" announcements
    for row in await db.get_pussies_needing_hour2_announcement():
        await db.mark_hour2_announced(row["telegram_id"])
        if row["pussy_chat_id"] is None:
            continue
        user_row = await db.get_user(row["telegram_id"])
        name_html = f'<a href="tg://user?id={row["telegram_id"]}">{display_name(user_row)}</a>'
        text = random.choice(PUSSY_HOUR2_ANNOUNCE).format(name=name_html)
        try:
            await context.bot.send_message(
                chat_id=row["pussy_chat_id"], text=text, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Failed to send hour-2 announcement to chat {row['pussy_chat_id']}: {e}")

    # Finalize expired pussy statuses, banking the accumulated bonus
    for row in await db.get_expired_pussies(PUSSY_DURATION_HOURS):
        telegram_id = row["telegram_id"]
        bonus = row["pussy_accum"]
        new_height = await db.finalize_pussy(telegram_id, bonus)
        if row["pussy_chat_id"] is None:
            continue
        user_row = await db.get_user(telegram_id)
        name_html = f'<a href="tg://user?id={telegram_id}">{display_name(user_row)}</a>'
        text = random.choice(PUSSY_FINALIZE).format(name=name_html, amount=bonus)
        try:
            await context.bot.send_message(
                chat_id=row["pussy_chat_id"],
                text=f"{text}\n📏 New height: <b>{new_height}cm</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"Failed to send pussy finalize message to chat {row['pussy_chat_id']}: {e}")


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

    application.add_handler(CommandHandler("pray", pray))
    application.add_handler(CommandHandler("simp", simp))
    application.add_handler(CommandHandler("hex", hex_command))
    application.add_handler(CommandHandler("shop", shop))
    application.add_handler(CommandHandler("buycondom", buycondom))
    application.add_handler(CommandHandler("snitch", snitch))
    application.add_handler(CommandHandler("cursethisgroup", cursethisgroup))
    application.add_handler(CommandHandler("gamble", gamble))
    application.add_handler(CommandHandler("fuck", fuck))

    application.job_queue.run_repeating(
        loan_repayment_job, interval=LOAN_CHECK_INTERVAL_SECONDS, first=10
    )
    application.job_queue.run_repeating(
        curse_hits_job, interval=CURSE_CHECK_INTERVAL_SECONDS, first=15
    )
    application.job_queue.run_repeating(
        pussy_status_job, interval=PUSSY_CHECK_INTERVAL_SECONDS, first=15
    )

    logger.info("Bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
