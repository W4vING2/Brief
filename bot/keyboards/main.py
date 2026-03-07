from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

PROFILE_BUTTON = "Личный кабинет"
HISTORY_BUTTON = "История"
ABOUT_BUTTON = "О нас"
SEND_BUTTON = "Отправить материал"
ADMIN_PANEL_BUTTON = "Admin Panel"


def main_menu_keyboard(username: str | None = None) -> ReplyKeyboardMarkup:
    is_admin = bool(username and username.lstrip("@").lower() == "w9v33")
    rows = [
        [KeyboardButton(text=PROFILE_BUTTON), KeyboardButton(text=HISTORY_BUTTON)],
        [KeyboardButton(text=ABOUT_BUTTON)],
        [KeyboardButton(text=SEND_BUTTON)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN_PANEL_BUTTON)])

    return ReplyKeyboardMarkup(
        keyboard=rows,
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
            InlineKeyboardButton(text="⚡ Более кратко", callback_data=f"summary:short:{transcription_id}"),
            InlineKeyboardButton(text="🧠 Более развернуто", callback_data=f"summary:detailed:{transcription_id}"),
        ],
        [
            InlineKeyboardButton(text="📣 Для поста", callback_data=f"summary:post:{transcription_id}"),
        ]
    ]

    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 Оплатить Pro", callback_data="buy:pro"),
                InlineKeyboardButton(text="👑 Оплатить Premium", callback_data="buy:premium"),
            ]
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Все пользователи и тарифы", callback_data="admin:list")],
            [
                InlineKeyboardButton(text="🚀 Выдать Pro", callback_data="admin:grant:pro"),
                InlineKeyboardButton(text="👑 Выдать Premium", callback_data="admin:grant:premium"),
            ],
            [InlineKeyboardButton(text="🆓 Выдать Free", callback_data="admin:grant:free")],
        ]
    )


def model_select_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ Groq", callback_data="model:set:groq"),
                InlineKeyboardButton(text="🧩 Claude", callback_data="model:set:claude"),
                InlineKeyboardButton(text="🤖 ChatGPT", callback_data="model:set:gpt4o"),
            ]
        ]
    )


def export_format_keyboard(export_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Markdown", callback_data=f"export:md:{export_id}"),
                InlineKeyboardButton(text="TXT", callback_data=f"export:txt:{export_id}"),
            ],
            [
                InlineKeyboardButton(text="PDF", callback_data=f"export:pdf:{export_id}"),
                InlineKeyboardButton(text="Ни в каком", callback_data=f"export:none:{export_id}"),
            ],
        ]
    )
