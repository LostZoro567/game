# Kingdoms & Dragons — Telegram Group RPG Bot

## 1. Create the bot
Talk to [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token.
Also send BotFather `/setprivacy` → **Disable** (so the bot can see all group messages, not just commands directed at it — commands still work either way, but this is recommended for future features like passive events).

## 2. Set up Supabase
1. Create a project at https://supabase.com
2. Open the SQL editor → paste the contents of `schema.sql` → run it.
3. Grab your Project URL and `anon`/`service_role` key from Settings → API.

## 3. Configure environment
```bash
cp .env.example .env
# then edit .env with your real values:
# TELEGRAM_BOT_TOKEN=...
# SUPABASE_URL=...
# SUPABASE_KEY=...
```

## 4. Install & run
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

## 5. Keep it running on your VPS
Use `tmux`/`screen`, or better, a systemd service:

```ini
# /etc/systemd/system/dragonbot.service
[Unit]
Description=Kingdoms & Dragons Telegram Bot
After=network.target

[Service]
WorkingDirectory=/path/to/dragonbot
ExecStart=/path/to/dragonbot/venv/bin/python3 main.py
Restart=always
EnvironmentFile=/path/to/dragonbot/.env

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now dragonbot
```

## Commands
| Command | Where | Description |
|---|---|---|
| `/join` | group | Enter the raid team |
| `/attack` | group | Attack the current main boss (20s cooldown/user) |
| `/boss` | group | Show current boss HP bar |
| `/dungeon` | anywhere | Solo fight, 5 free attempts/day |
| `/shop` | anywhere | View upgrade costs |
| `/upgrade_sword` `/upgrade_armor` | anywhere | Spend gold (max level 10 each) |
| `/convert` | anywhere | Silver → gold (10:1) |
| `/profile` | anywhere | Your HP/gold/silver/gear |
| `/leaderboard` | anywhere | Top players by gold |

## What's implemented (MVP)
- Sequential main bosses (Goblin King → Sea Monster → Demon Lord → Dragon), shared HP per group
- Boss counter-attacks a random *active (non-downed)* joined member each `/attack`
- Sea Monster's tidal wave (hits 2 players) and Dragon's enrage-at-low-HP are live
- Dragon HP resets weekly even if not killed, so it stays "content" instead of a wall
- 20-boss dungeon (4 tiers of 5), weighted by your gear level, 5 attempts/day
- Hard gear cap at level 10, silver→gold conversion, passive slow HP regen

## Natural next additions (not yet built, easy to layer on)
- Demon Lord's curse debuff (reduce a random member's next attack)
- `/revive` — teammates spending gold/an item to revive a downed player
- Daily quest command for bonus silver
- MVP callout + top-3 damage on boss kill (currently only tracks the killing blow's rewards — could log per-fight damage for a proper "top damage dealer" shoutout)

Want me to build any of these next, or wire up a proper per-fight damage log for MVP/leaderboard tracking?
