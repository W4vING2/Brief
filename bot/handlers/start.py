from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.keyboards import ABOUT_BUTTON, PROFILE_BUTTON, SEND_BUTTON, main_menu_keyboard
from bot.services.database import (
    MODEL_DAILY_LIMITS,
    PLAN_LIMITS,
    PLAN_PRICES,
    DatabaseService,
    DatabaseServiceError,
    should_save_history,
    is_admin_username,
)

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, db: DatabaseService) -> None:
    await db.ensure_user(message.from_user.id, message.from_user.username)
    text = (
        "Привет! Я BriefBot.\n\n"
        "Я извлекаю текст из материалов и делаю конспекты на русском.\n\n"
        "🆓 Free: только ГС/кружки/аудио\n"
        "🚀 Pro/Premium: все форматы, включая видео, PDF и ссылки\n\n"
        "Внизу есть меню: личный кабинет, информация о боте и кнопка для отправки материала."
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Поддерживаемые форматы:\n"
        "🆓 Free: только голосовые, кружки и аудио\n"
        "🚀 Pro/Premium: голосовые, кружки, аудио, видео, PDF, YouTube и ссылки на статьи"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: DatabaseService) -> None:
    try:
        await db.ensure_user(message.from_user.id, message.from_user.username)
        status = await db.get_usage_status(message.from_user.id, message.from_user.username)
    except DatabaseServiceError as exc:
        await message.answer(str(exc), reply_markup=main_menu_keyboard())
        return

    extra = ""
    if status.plan in {"premium", "admin"}:
        if is_admin_username(message.from_user.username):
            extra = "\n🤖 GPT-4o: безлимит\n🧩 Claude: безлимит"
        else:
            gpt_used = await db.get_model_usage(message.from_user.id, "gpt4o")
            claude_used = await db.get_model_usage(message.from_user.id, "claude")
            extra = (
                f"\n🤖 GPT-4o: {gpt_used}/{MODEL_DAILY_LIMITS['gpt4o']} сегодня"
                f"\n🧩 Claude: {claude_used}/{MODEL_DAILY_LIMITS['claude']} сегодня"
            )

    if status.limit is None:
        text = f"📊 Groq: {status.used} сегодня\n🏷 План: {status.plan} (безлимит){extra}"
    else:
        text = f"📊 Groq: {status.used}/{status.limit} сегодня{extra}"
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("plans"))
async def cmd_plans(message: Message) -> None:
    free_limit = PLAN_LIMITS["free"]
    pro_price = PLAN_PRICES["pro"]
    premium_price = PLAN_PRICES["premium"]
    text = (
        "💼 Планы подписки:\n\n"
        f"🆓 Free\n"
        f"• Только ГС/кружки/аудио\n"
        f"• {free_limit} запросов к Groq в день\n"
        "• История не сохраняется\n\n"
        f"🚀 Pro — {pro_price}₽/месяц\n"
        "• Все форматы\n"
        "• Безлимит к Groq\n"
        "• История хранится 30 дней\n\n"
        f"👑 Premium — {premium_price}₽/месяц\n"
        "• Все форматы\n"
        "• Безлимит к Groq\n"
        f"• GPT-4o: {MODEL_DAILY_LIMITS['gpt4o']}/день\n"
        f"• Claude: {MODEL_DAILY_LIMITS['claude']}/день\n"
        "• История хранится без ограничений"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(F.text == PROFILE_BUTTON)
async def show_profile(message: Message, db: DatabaseService) -> None:
    try:
        await db.ensure_user(message.from_user.id, message.from_user.username)
        status = await db.get_usage_status(message.from_user.id, message.from_user.username)
        if should_save_history(status.plan):
            recent = await db.get_recent_transcriptions(message.from_user.id, limit=5)
        else:
            recent = []
    except DatabaseServiceError as exc:
        await message.answer(str(exc), reply_markup=main_menu_keyboard())
        return

    if status.limit is None:
        usage_text = f"{status.used} сегодня"
        remaining_text = "без ограничений"
    else:
        usage_text = f"{status.used}/{status.limit} сегодня"
        remaining_text = f"{status.remaining}/{status.limit}"

    username = f"@{message.from_user.username}" if message.from_user.username else "не указан"
    if should_save_history(status.plan):
        history_lines = "\n".join(
            f"• {item.source_type}: {item.created_at[:16].replace('T', ' ')} — {_shorten(item.summary)}"
            for item in recent
        ) or "• Пока нет сохраненных конспектов"
    else:
        history_lines = "• На тарифе Free история не сохраняется"

    limits_line = ""
    if status.plan in {"premium", "admin"}:
        if is_admin_username(message.from_user.username):
            limits_line = "\n• GPT-4o: безлимит\n• Claude: безлимит"
        else:
            gpt_used = await db.get_model_usage(message.from_user.id, "gpt4o")
            claude_used = await db.get_model_usage(message.from_user.id, "claude")
            limits_line = (
                f"\n• GPT-4o: {gpt_used}/{MODEL_DAILY_LIMITS['gpt4o']} сегодня"
                f"\n• Claude: {claude_used}/{MODEL_DAILY_LIMITS['claude']} сегодня"
            )
    text = (
        "👤 Личный кабинет:\n"
        f"• ID: {message.from_user.id}\n"
        f"• Username: {username}\n"
        f"• План: {status.plan}\n"
        f"• Использовано: {usage_text}\n"
        f"• Осталось: {remaining_text}"
        f"{limits_line}\n\n"
        "🕘 Последние конспекты:\n"
        f"{history_lines}"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(F.text == ABOUT_BUTTON)
async def show_about(message: Message) -> None:
    text = (
        "О нас:\n"
        "BriefBot помогает быстро получать конспекты из голосовых сообщений, видео, PDF, "
        "YouTube-ссылок и статей.\n"
        "Бот извлекает текст, выделяет главное и возвращает краткое резюме на русском языке."
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(F.text == SEND_BUTTON)
async def show_send_material(message: Message) -> None:
    text = (
        "Отправь материал одним сообщением:\n"
        "- голосовое\n"
        "- кружок\n"
        "- видео\n"
        "- PDF\n"
        "- YouTube-ссылку\n"
        "- ссылку на статью"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


def _shorten(text: str, limit: int = 56) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
