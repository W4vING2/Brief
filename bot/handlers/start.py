from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.keyboards import (
    ABOUT_BUTTON,
    ADMIN_PANEL_BUTTON,
    PROFILE_BUTTON,
    SEND_BUTTON,
    admin_panel_keyboard,
    main_menu_keyboard,
    plans_keyboard,
)
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
T_BANK_CARD = "2200 7005 9320 9017"


class AdminGrantState(StatesGroup):
    waiting_user_identifier = State()


def _menu(message: Message) -> object:
    return main_menu_keyboard(message.from_user.username if message.from_user else None)


def _is_admin_message(message: Message) -> bool:
    return is_admin_username(message.from_user.username if message.from_user else None)


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return is_admin_username(callback.from_user.username if callback.from_user else None)


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
    if _is_admin_message(message):
        text += "\n\n🛠 Для тебя доступна кнопка Admin Panel."
    await message.answer(text, reply_markup=_menu(message))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Поддерживаемые форматы:\n"
        "🆓 Free: только голосовые, кружки и аудио\n"
        "🚀 Pro/Premium: голосовые, кружки, аудио, видео, PDF, YouTube и ссылки на статьи"
    )
    await message.answer(text, reply_markup=_menu(message))


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: DatabaseService) -> None:
    try:
        await db.ensure_user(message.from_user.id, message.from_user.username)
        status = await db.get_usage_status(message.from_user.id, message.from_user.username)
    except DatabaseServiceError as exc:
        await message.answer(str(exc), reply_markup=_menu(message))
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
    await message.answer(text, reply_markup=_menu(message))


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
    await message.answer(text, reply_markup=_menu(message))
    await message.answer("Выбери тариф для оплаты:", reply_markup=plans_keyboard())


@router.callback_query(F.data.startswith("buy:"))
async def handle_buy_plan(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return
    plan = callback.data.split(":", 1)[1]
    if plan not in PLAN_PRICES:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    amount = PLAN_PRICES[plan]
    title = "Pro" if plan == "pro" else "Premium"
    text = (
        f"💳 Оплата тарифа {title}\n\n"
        f"Сумма: {amount}₽\n"
        f"Карта Т-Банк: `{T_BANK_CARD}`\n\n"
        "После оплаты отправь чек администратору @w9v33 с твоим Telegram ID."
    )
    if callback.message:
        await callback.message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(callback.from_user.username if callback.from_user else None),
        )
    await callback.answer("Реквизиты отправлены")


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
        await message.answer(str(exc), reply_markup=_menu(message))
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
    await message.answer(text, reply_markup=_menu(message))


@router.message(F.text == ABOUT_BUTTON)
async def show_about(message: Message) -> None:
    text = (
        "О нас:\n"
        "BriefBot помогает быстро получать конспекты из голосовых сообщений, видео, PDF, "
        "YouTube-ссылок и статей.\n"
        "Бот извлекает текст, выделяет главное и возвращает краткое резюме на русском языке."
    )
    await message.answer(text, reply_markup=_menu(message))


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
    await message.answer(text, reply_markup=_menu(message))


@router.message(F.text == ADMIN_PANEL_BUTTON)
async def show_admin_panel(message: Message) -> None:
    if not _is_admin_message(message):
        await message.answer("⛔ Доступ запрещён.", reply_markup=_menu(message))
        return

    text = (
        "🛠 Admin Panel\n\n"
        "Выбери действие:\n"
        "• посмотреть всех пользователей и их тарифы\n"
        "• выдать тариф пользователю"
    )
    await message.answer(text, reply_markup=_menu(message))
    await message.answer("Админ-действия:", reply_markup=admin_panel_keyboard())


@router.callback_query(F.data == "admin:list")
async def admin_list_users(callback: CallbackQuery, db: DatabaseService) -> None:
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        users = await db.list_users_with_plans(limit=200)
    except DatabaseServiceError as exc:
        if callback.message:
            await callback.message.answer(
                str(exc),
                reply_markup=main_menu_keyboard(callback.from_user.username if callback.from_user else None),
            )
        await callback.answer()
        return

    if not users:
        if callback.message:
            await callback.message.answer(
                "Пользователи пока отсутствуют.",
                reply_markup=main_menu_keyboard(callback.from_user.username if callback.from_user else None),
            )
        await callback.answer()
        return

    lines = []
    for user in users:
        uname = f"@{user.username}" if user.username else "без username"
        lines.append(f"• {user.id} | {uname} | {user.plan}")
    text = "👥 Пользователи и тарифы:\n\n" + "\n".join(lines)
    if len(text) > 3800:
        text = text[:3790].rstrip() + "\n…"
    if callback.message:
        await callback.message.answer(
            text,
            reply_markup=main_menu_keyboard(callback.from_user.username if callback.from_user else None),
        )
    await callback.answer("Список отправлен")


@router.callback_query(F.data.startswith("admin:grant:"))
async def admin_grant_plan_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if not callback.data:
        await callback.answer()
        return
    target_plan = callback.data.rsplit(":", 1)[-1]
    if target_plan not in PLAN_LIMITS:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    await state.set_state(AdminGrantState.waiting_user_identifier)
    await state.update_data(target_plan=target_plan)
    if callback.message:
        await callback.message.answer(
            f"Введи ID пользователя или @username для выдачи тарифа `{target_plan}`.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(callback.from_user.username if callback.from_user else None),
        )
    await callback.answer("Ожидаю пользователя")


@router.message(AdminGrantState.waiting_user_identifier)
async def admin_grant_plan_finish(
    message: Message,
    state: FSMContext,
    db: DatabaseService,
) -> None:
    if not _is_admin_message(message):
        await state.clear()
        await message.answer("⛔ Доступ запрещён.", reply_markup=_menu(message))
        return

    data = await state.get_data()
    target_plan = data.get("target_plan")
    identifier = (message.text or "").strip()
    if target_plan not in PLAN_LIMITS:
        await state.clear()
        await message.answer("⚠️ Сессия истекла. Открой Admin Panel заново.", reply_markup=_menu(message))
        return
    if not identifier:
        await message.answer("Введи корректный ID или @username.", reply_markup=_menu(message))
        return

    try:
        user = await db.find_user(identifier)
        if user is None:
            await message.answer(
                "Пользователь не найден. Он должен хотя бы один раз нажать /start.",
                reply_markup=_menu(message),
            )
            return
        updated = await db.set_user_plan(user.id, target_plan)
    except DatabaseServiceError as exc:
        await message.answer(str(exc), reply_markup=_menu(message))
        return
    finally:
        await state.clear()

    uname = f"@{updated.username}" if updated.username else "без username"
    await message.answer(
        f"✅ Тариф обновлён: {updated.id} ({uname}) → {updated.plan}",
        reply_markup=_menu(message),
    )


def _shorten(text: str, limit: int = 56) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
