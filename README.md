# DickGrow Bot 📏

A fun Telegram group bot where users grow their "height" daily, attack each other
with public challenges, and can take risky loans when they hit rock bottom.

## Features

- `/grow` — random +10–25cm, once per 24h
- `/attack` — posts a public challenge with a random amount (15–40cm) and an
  inline **⚔️ Attack** button. Anyone in the group can tap it (except the
  attacker) if they have enough cm to match the stake. Winner is a 50/50 coin
  flip and takes the full amount from the loser. No cooldown — spam away.
- `/loan` — only usable at exactly 0cm. Grants +10cm instantly; auto-deducted
  10cm after 24h. If you're still sitting at 0 when it's repaid, you drop to
  **-10cm**.
- `/leaderboard` — today's top gainers + all-time rankings
- `/me` — your current stats and loan status

## Stack

- Python 3.11+, [python-telegram-bot](https://docs.python-telegram-bot.org/) v21 (async)
- Supabase Postgres via `asyncpg`
- Deployed as a `systemd` service on your VPS (long-polling, no webhook/server needed)

## 1. Set up Supabase

1. Create a project at [supabase.com](https://supabase.com).
2. Go to **Project Settings → Database → Connection string → URI**, and copy
   the "Session pooler" connection string (port `5432`).
3. You don't need to run `schema.sql` manually — the bot applies it
   automatically on startup. You can also run it yourself in the Supabase SQL
   editor if you'd like to inspect it first.

## 2. Create your bot

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram.
2. `/newbot`, follow the prompts, copy the token.
3. Add the bot to your group as a normal member (admin not required).

## 3. Local setup / VPS setup

```bash
git clone <your-repo-url> dickgrowbot
cd dickgrowbot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in BOT_TOKEN and DATABASE_URL

python3 bot.py   # test run
```

If it starts without errors and `/start` works in your group, you're good.
Stop it with `Ctrl+C` before setting up the service below.

## 4. Run permanently on your VPS (systemd)

```bash
sudo cp dickgrowbot.service /etc/systemd/system/
sudo nano /etc/systemd/system/dickgrowbot.service   # replace YOUR_VPS_USERNAME
sudo systemctl daemon-reload
sudo systemctl enable dickgrowbot
sudo systemctl start dickgrowbot

# check it's alive
sudo systemctl status dickgrowbot
journalctl -u dickgrowbot -f
```

It'll now survive reboots and auto-restart on crash.

## Game balance notes / things you can tune

All the numbers live at the top of `bot.py`:

```python
GROW_MIN, GROW_MAX = 10, 25
GROW_COOLDOWN_HOURS = 24
ATTACK_MIN, ATTACK_MAX = 15, 40
LOAN_AMOUNT = 10
LOAN_DURATION_HOURS = 24
```

- Attack losses are floored at 0cm (can't go negative from a fight — only a
  defaulted loan can push you negative).
- Attack winner/loser is a coin flip, not skill-weighted — this was a
  deliberate design choice per your spec so any group member is a threat
  regardless of size.
- The loan repayment job runs every 5 minutes checking for loans older than
  24h — adjust `LOAN_CHECK_INTERVAL_SECONDS` if you want tighter precision.

## Ideas for next features

- `/gift <amount> @user` — let people donate cm to each other
- Rank titles shown next to leaderboard names (e.g. "Twig 🌱" → "Legend 🍆")
- Weekly reset leaderboard alongside the permanent all-time one
- Random daily group-wide events ("🌍 Global shrinkage! Everyone -5% today")
- Shield/steroid items purchasable with cm to block or boost attacks
