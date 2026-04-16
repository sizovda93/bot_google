"""Entry point for the receipt processing Telegram bot."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

from bot.config import Config
from bot.handlers import router, init_services


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("gspread").setLevel(logging.WARNING)


async def main() -> None:
    load_dotenv()
    setup_logging()

    logger = logging.getLogger(__name__)
    logger.info("Starting receipt bot...")

    config = Config.from_env()
    init_services(config)

    bot = Bot(token=config.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Bot is running. Listening for receipts in chat %s", config.telegram_chat_id)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
