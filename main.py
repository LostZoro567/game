import os
import logging
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler

load_dotenv()

import handlers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_command))
    app.add_handler(CommandHandler("join", handlers.join))
    app.add_handler(CommandHandler("profile", handlers.profile))
    app.add_handler(CommandHandler("leaderboard", handlers.leaderboard))
    app.add_handler(CommandHandler("shop", handlers.shop))
    app.add_handler(CommandHandler("upgrade_sword", handlers.upgrade_sword))
    app.add_handler(CommandHandler("upgrade_armor", handlers.upgrade_armor))
    app.add_handler(CommandHandler("convert", handlers.convert))
    app.add_handler(CommandHandler("attack", handlers.attack))
    app.add_handler(CommandHandler("boss", handlers.boss_status))
    app.add_handler(CommandHandler("dungeon", handlers.dungeon))

    logging.info("Bot starting (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
