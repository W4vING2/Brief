from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv
import os

from bot.handlers import content, start
from bot.services import DatabaseService, SummaryService, TranscriptionService

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

missing = [
    name
    for name, value in {
        "BOT_TOKEN": BOT_TOKEN,
        "WEBHOOK_URL": WEBHOOK_URL,
        "GROQ_API_KEY": GROQ_API_KEY,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
    }.items()
    if not value
]
if missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

dp.include_router(start.router)
dp.include_router(content.router)

dp["db"] = DatabaseService(SUPABASE_URL, SUPABASE_KEY)
dp["transcriber"] = TranscriptionService(GROQ_API_KEY)
dp["summarizer"] = SummaryService(
    GROQ_API_KEY,
    openai_api_key=OPENAI_API_KEY,
    anthropic_api_key=ANTHROPIC_API_KEY,
)


async def setup_commands() -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="help", description="Что умеет бот"),
            BotCommand(command="stats", description="Моя статистика"),
            BotCommand(command="plans", description="Планы подписки"),
        ]
    )


async def on_startup(bot: Bot):
    await bot.set_webhook(url=WEBHOOK_URL)
    await setup_commands()


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    await bot.session.close()


def main():
    app = web.Application()

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    ).register(app, path="/webhook")

    setup_application(app, dp, bot=bot)

    app.on_startup.append(lambda app: on_startup(bot))
    app.on_shutdown.append(lambda app: on_shutdown(bot))

    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))


if __name__ == "__main__":
    main()
