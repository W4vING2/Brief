from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv
import logging
import os
from urllib.parse import urlparse

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


def _resolve_webhook_url() -> tuple[str, str]:
    raw = (
        os.getenv("WEBHOOK_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("RAILWAY_STATIC_URL")
        or os.getenv("KOYEB_PUBLIC_DOMAIN")
    )
    if not raw:
        raise RuntimeError(
            "Missing WEBHOOK_URL. Set WEBHOOK_URL to a public HTTPS URL "
            "ending with /webhook."
        )

    url = raw.strip()
    if "$" in url:
        raise RuntimeError(
            "WEBHOOK_URL contains an unresolved variable. Use a real public URL, "
            "for example: https://your-app-domain.com/webhook"
        )
    if url.startswith("http://"):
        raise RuntimeError("WEBHOOK_URL must use HTTPS, not HTTP.")
    if not url.startswith("https://"):
        url = f"https://{url}"

    parsed = urlparse(url)
    if not parsed.netloc:
        raise RuntimeError(
            "Invalid WEBHOOK_URL host. Use a resolvable public domain, "
            "for example: https://your-app-domain.com/webhook"
        )
    if "localhost" in parsed.netloc or parsed.netloc.startswith("127."):
        raise RuntimeError("WEBHOOK_URL cannot point to localhost or 127.0.0.1.")

    path = parsed.path or ""
    if not path or path == "/":
        url = url.rstrip("/") + "/webhook"
        path = "/webhook"
    return url, path


WEBHOOK_URL, WEBHOOK_PATH = _resolve_webhook_url()

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
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_webhook(url=WEBHOOK_URL)
        await setup_commands()
        logger.info("Webhook configured: %s", WEBHOOK_URL)
    except TelegramBadRequest as exc:
        await bot.session.close()
        raise RuntimeError(
            "Failed to set Telegram webhook. Check WEBHOOK_URL DNS and HTTPS certificate. "
            "Expected public URL format: https://your-app-domain.com/webhook"
        ) from exc


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    await bot.session.close()


def main():
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    app.on_startup.append(lambda app: on_startup(bot))
    app.on_shutdown.append(lambda app: on_shutdown(bot))

    port = int(os.getenv("PORT", 8080))
    logger.info("Starting aiohttp server on 0.0.0.0:%s with webhook path %s", port, WEBHOOK_PATH)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
