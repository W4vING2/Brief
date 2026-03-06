from __future__ import annotations

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from dotenv import load_dotenv

from bot.handlers import content_router, start_router
from bot.services import DatabaseService, SummaryService, TranscriptionService


def create_dispatcher(
    *,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
) -> Dispatcher:
    dispatcher = Dispatcher(
        db=db,
        transcriber=transcriber,
        summarizer=summarizer,
    )
    dispatcher.include_router(start_router)
    dispatcher.include_router(content_router)
    return dispatcher


async def on_shutdown(bot: Bot) -> None:
    await bot.session.close()


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="help", description="Что умеет бот"),
            BotCommand(command="stats", description="Моя статистика"),
            BotCommand(command="plans", description="Планы подписки"),
        ]
    )


def configure_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "briefbot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    stream_handler = logging.StreamHandler()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[stream_handler, file_handler],
    )


async def main() -> None:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN")
    groq_api_key = os.getenv("GROQ_API_KEY")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    missing = [
        name
        for name, value in {
            "BOT_TOKEN": bot_token,
            "GROQ_API_KEY": groq_api_key,
            "SUPABASE_URL": supabase_url,
            "SUPABASE_KEY": supabase_key,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    bot = Bot(
        token=bot_token,
        default=DefaultBotProperties(),
    )
    db = DatabaseService(supabase_url, supabase_key)
    transcriber = TranscriptionService(groq_api_key)
    summarizer = SummaryService(
        groq_api_key,
        openai_api_key=openai_api_key,
        anthropic_api_key=anthropic_api_key,
    )
    dispatcher = create_dispatcher(
        db=db,
        transcriber=transcriber,
        summarizer=summarizer,
    )
    dispatcher.shutdown.register(on_shutdown)
    configure_logging()
    await setup_commands(bot)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
