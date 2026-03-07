from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

from bot.handlers import content, start
from bot.services import DatabaseService, SummaryService, TranscriptionService

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

missing = [
    name
    for name, value in {
        "BOT_TOKEN": BOT_TOKEN,
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


def _bot_mode() -> str:
    mode = (os.getenv("BOT_MODE") or "polling").strip().lower()
    if mode not in {"auto", "webhook", "polling"}:
        return "polling"
    return mode


def _resolve_webhook_url() -> tuple[str, str] | None:
    raw = (
        os.getenv("WEBHOOK_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("RAILWAY_STATIC_URL")
        or os.getenv("KOYEB_PUBLIC_DOMAIN")
    )
    if not raw:
        return None

    url = raw.strip()
    if "$" in url:
        raise RuntimeError("WEBHOOK_URL contains unresolved variables.")
    if url.startswith("http://"):
        raise RuntimeError("WEBHOOK_URL must use HTTPS.")
    if not url.startswith("https://"):
        url = f"https://{url}"

    parsed = urlparse(url)
    if not parsed.netloc:
        raise RuntimeError("Invalid WEBHOOK_URL host.")
    if "localhost" in parsed.netloc or parsed.netloc.startswith("127."):
        raise RuntimeError("WEBHOOK_URL cannot point to localhost/127.0.0.1.")

    path = parsed.path or ""
    if not path or path == "/":
        url = url.rstrip("/") + "/webhook"
        path = "/webhook"
    return url, path


async def setup_commands() -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="help", description="Что умеет бот"),
            BotCommand(command="stats", description="Моя статистика"),
            BotCommand(command="plans", description="Планы подписки"),
        ]
    )


async def run_polling() -> None:
    logger.info("Starting in polling mode")
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_commands()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


def run_webhook(webhook_url: str, webhook_path: str) -> None:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def on_startup(_: web.Application) -> None:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await bot.set_webhook(url=webhook_url)
            await setup_commands()
            logger.info("Webhook configured: %s", webhook_url)
        except TelegramBadRequest as exc:
            await bot.session.close()
            raise RuntimeError(
                "Failed to set Telegram webhook. Check WEBHOOK_URL DNS/SSL and path."
            ) from exc

    async def on_shutdown(_: web.Application) -> None:
        await bot.delete_webhook()
        await bot.session.close()

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    ).register(app, path=webhook_path)

    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    port = int(os.getenv("PORT", 8080))
    logger.info(
        "Starting in webhook mode on 0.0.0.0:%s, webhook path=%s",
        port,
        webhook_path,
    )
    web.run_app(app, host="0.0.0.0", port=port)


def main() -> None:
    mode = _bot_mode()
    webhook = _resolve_webhook_url()

    if mode == "polling":
        asyncio.run(run_polling())
        return

    if mode == "webhook":
        if webhook is None:
            raise RuntimeError("BOT_MODE=webhook requires WEBHOOK_URL.")
        run_webhook(*webhook)
        return

    if webhook is None:
        asyncio.run(run_polling())
    else:
        run_webhook(*webhook)


if __name__ == "__main__":
    main()
