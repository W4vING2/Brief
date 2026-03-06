from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

PROFILE_BUTTON = "Личный кабинет"
ABOUT_BUTTON = "О нас"
SEND_BUTTON = "Отправить материал"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PROFILE_BUTTON), KeyboardButton(text=ABOUT_BUTTON)],
            [KeyboardButton(text=SEND_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие или отправь материал",
    )


def summary_actions_keyboard(transcription_id: str) -> InlineKeyboardMarkup:
    return summary_actions_keyboard_for_plan(transcription_id, include_premium_models=False)


def summary_actions_keyboard_for_plan(
    transcription_id: str,
    *,
    include_premium_models: bool,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="⚡ Коротко", callback_data=f"summary:groq:short:{transcription_id}"),
            InlineKeyboardButton(text="🧠 Подробнее", callback_data=f"summary:groq:detailed:{transcription_id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Чеклист", callback_data=f"summary:groq:checklist:{transcription_id}"),
            InlineKeyboardButton(text="📣 Для поста", callback_data=f"summary:groq:post:{transcription_id}"),
        ],
    ]
    if include_premium_models:
        rows.append(
            [
                InlineKeyboardButton(text="🤖 GPT-4o", callback_data=f"summary:gpt4o:detailed:{transcription_id}"),
                InlineKeyboardButton(text="🧩 Claude", callback_data=f"summary:claude:detailed:{transcription_id}"),
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)
