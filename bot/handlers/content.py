from __future__ import annotations

import logging
import os
import tempfile
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot.keyboards import main_menu_keyboard, summary_actions_keyboard_for_plan
from bot.services.database import (
    FREE_ALLOWED_SOURCE_TYPES,
    MODEL_DAILY_LIMITS,
    DatabaseService,
    DatabaseServiceError,
    UsageStatus,
    is_admin_username,
    should_save_history,
)
from bot.services.summarize import SUMMARY_STYLES, SummaryService, SummaryServiceError
from bot.services.transcribe import (
    ProcessingError,
    ProcessedContent,
    TranscriptionService,
    YouTubeProcessingError,
    extract_url_from_message,
)
from bot.services.youtube import get_youtube_transcript

router = Router()
logger = logging.getLogger(__name__)

ERROR_TEXT = "⚠️ Не удалось обработать файл. Попробуй ещё раз."
LIMIT_TEXT = "❌ Лимит исчерпан. Осталось: 0/5. Оформи подписку /plans"
EMPTY_SUMMARY_MARKERS = (
    "материал не предоставлен",
    "нет информации для конспекта",
    "требуется текст для анализа",
)


async def _check_limit(message: Message, db: DatabaseService) -> UsageStatus | None:
    try:
        await db.ensure_user(message.from_user.id, message.from_user.username)
        status = await db.get_usage_status(message.from_user.id, message.from_user.username)
    except DatabaseServiceError as exc:
        await message.answer(str(exc), reply_markup=main_menu_keyboard())
        return None
    if status.is_exceeded:
        limit = status.limit or 5
        await message.answer(
            f"❌ Лимит исчерпан. Осталось: 0/{limit}. Оформи подписку /plans",
            reply_markup=main_menu_keyboard(),
        )
        return None
    return status


def _format_remaining(status: UsageStatus) -> str:
    if status.limit is None:
        return "∞"
    remaining = status.remaining
    return f"{remaining}/{status.limit}"


def _is_source_allowed(plan: str, source_type: str, username: str | None = None) -> bool:
    if plan in {"pro", "premium"} or is_admin_username(username):
        return True
    if plan == "free":
        return source_type in FREE_ALLOWED_SOURCE_TYPES
    return True


def _is_premium_or_admin(status: UsageStatus, username: str | None) -> bool:
    return status.plan == "premium" or is_admin_username(username)


def _provider_title(provider: str) -> str:
    return {
        "groq": "Groq",
        "gpt4o": "GPT-4o",
        "claude": "Claude",
    }.get(provider, provider)


async def _check_premium_model_quota(
    user_id: int,
    provider: str,
    status: UsageStatus,
    username: str | None,
    db: DatabaseService,
) -> tuple[bool, int, int]:
    if provider not in MODEL_DAILY_LIMITS:
        return True, 0, 0
    if is_admin_username(username):
        return True, 0, 0
    if not _is_premium_or_admin(status, username):
        return False, 0, MODEL_DAILY_LIMITS[provider]
    used = await db.get_model_usage(user_id, provider)
    limit = MODEL_DAILY_LIMITS[provider]
    return used < limit, used, limit


def _split_summary(summary: str) -> tuple[str, list[str]]:
    intro_lines: list[str] = []
    bullet_lines: list[str] = []
    conclusion_lines: list[str] = []
    bullets_started = False
    conclusion_started = False

    for raw_line in summary.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower().rstrip(":")
        if lowered in {"вывод", "итог", "итоги"}:
            conclusion_started = True
            bullets_started = False
            continue
        if lowered in {"ключевые тезисы", "главное", "основные тезисы"}:
            bullets_started = True
            conclusion_started = False
            continue
        if lowered in {"о чем материал", "о чём материал", "кратко", "суть"}:
            continue
        if line.startswith(("-", "•", "*")):
            bullets_started = True
            conclusion_started = False
            bullet_lines.append(line.lstrip("-•* ").strip())
        elif conclusion_started:
            conclusion_lines.append(line)
        elif bullets_started:
            bullet_lines.append(line)
        else:
            intro_lines.append(line)

    intro = " ".join(intro_lines).strip() or summary.strip()
    if conclusion_lines:
        intro = intro.strip()
    return intro, bullet_lines, conclusion_lines


def _safe_preview(text: str, limit: int = 80) -> str:
    cleaned = " ".join(text.split()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _format_summary(summary: str, meta: str, status: UsageStatus) -> str:
    intro, bullet_lines, conclusion_lines = _split_summary(summary)
    bullets = "\n".join(f"• {escape(line)}" for line in bullet_lines) or f"• {escape(intro)}"
    conclusion = ""
    if conclusion_lines:
        conclusion = "\n\n💡 <b>Вывод:</b>\n" + escape(" ".join(conclusion_lines))
    return (
        "✨ <b>Готово! Вот твой конспект</b>\n\n"
        "📝 <b>О чем материал:</b>\n"
        f"{escape(intro)}\n\n"
        "📌 <b>Ключевые тезисы:</b>\n"
        f"{bullets}"
        f"{conclusion}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"⏱ <b>Длительность / объем:</b> {escape(meta)}\n"
        f"🆓 <b>Осталось сегодня:</b> {escape(_format_remaining(status))}\n"
        "📎 <i>Ниже приложил красиво оформленный .md файл</i>"
    )


def _build_markdown(summary: str, processed: ProcessedContent, status: UsageStatus) -> str:
    intro, bullet_lines, conclusion_lines = _split_summary(summary)
    bullets = "\n".join(f"- {line}" for line in bullet_lines) or f"- {intro}"
    conclusion = "\n".join(conclusion_lines).strip() or "Материал передан без явно выделенного вывода."
    return (
        "# ✨ BriefBot Summary\n\n"
        "> Краткий и красиво оформленный конспект материала\n\n"
        "## 📝 О чем материал\n\n"
        f"{intro}\n\n"
        "## 📌 Ключевые тезисы\n\n"
        f"{bullets}\n\n"
        "## 💡 Вывод\n\n"
        f"{conclusion}\n\n"
        "---\n\n"
        "## 📎 Метаданные\n\n"
        f"- **Источник:** `{processed.source_type}`\n"
        f"- **Длительность / объем:** {processed.meta}\n"
        f"- **Осталось сегодня:** {_format_remaining(status)}\n\n"
        "---\n\n"
        "_Создано в BriefBot_"
    )


async def _send_markdown_file(
    message: Message,
    summary: str,
    processed: ProcessedContent,
    status: UsageStatus,
) -> None:
    markdown = _build_markdown(summary, processed, status)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".md",
            prefix=f"briefbot-{processed.source_type}-",
            delete=False,
        ) as temp_file:
            temp_file.write(markdown)
            temp_path = temp_file.name

        await message.bot.send_document(
            chat_id=message.chat.id,
            document=FSInputFile(temp_path, filename=f"briefbot-{processed.source_type}-summary.md"),
            caption="Markdown-версия конспекта",
        )
    except Exception as exc:
        logger.exception("Failed to send markdown file: %s", exc)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


async def _update_progress(progress_message: Message, text: str) -> None:
    try:
        await progress_message.edit_text(text, reply_markup=main_menu_keyboard())
    except Exception:
        logger.debug("Failed to update progress message", exc_info=True)


async def _finalize(
    message: Message,
    processed: ProcessedContent,
    db: DatabaseService,
    summarizer: SummaryService,
    status_before: UsageStatus,
    thinking_message: Message | None = None,
    style_key: str = "detailed",
    provider: str = "groq",
) -> None:
    if thinking_message is not None:
        await _update_progress(
            thinking_message,
            f"🧠 Собираю конспект через {_provider_title(provider)} и выделяю главное...",
        )

    summary = await summarizer.summarize_text(processed.text, style_key=style_key, provider=provider)
    lowered = summary.lower()
    if any(marker in lowered for marker in EMPTY_SUMMARY_MARKERS):
        preview = processed.text[:700].strip()
        summary = (
            f"{preview}\n\n"
            "- Распознанный текст слишком короткий или фрагментарный для полноценного конспекта.\n"
            "- Попробуй отправить более длинное или более чёткое аудио."
        )
    record = await db.save_transcription(
        user_id=message.from_user.id,
        source_type=processed.source_type,
        transcript=processed.text,
        summary=summary,
        plan=status_before.plan,
    )

    if provider == "groq":
        status = await db.increment_usage(message.from_user.id, message.from_user.username)
    else:
        await db.increment_model_usage(message.from_user.id, provider)
        status = await db.get_usage_status(message.from_user.id, message.from_user.username)
    if thinking_message is not None:
        try:
            await thinking_message.delete()
        except Exception:
            logger.debug("Failed to delete thinking message", exc_info=True)
    await message.answer(
        _format_summary(summary, processed.meta, status),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )
    if record is not None:
        await message.answer(
            "🎛 <b>Хочешь другой формат?</b> Нажми одну из кнопок ниже.",
            parse_mode="HTML",
            reply_markup=summary_actions_keyboard_for_plan(
                record.id,
                include_premium_models=_is_premium_or_admin(status_before, message.from_user.username),
            ),
        )
    else:
        await message.answer(
            "🗂 На тарифе Free история не сохраняется. Для сохранения конспектов перейди на /plans.",
            reply_markup=main_menu_keyboard(),
        )
    await _send_markdown_file(message, summary, processed, status)


async def _handle_processing_error(
    message: Message,
    exc: Exception,
    thinking_message: Message | None = None,
) -> None:
    logger.exception("Processing failed: %s", exc)
    if thinking_message is not None:
        try:
            await thinking_message.delete()
        except Exception:
            logger.debug("Failed to delete thinking message", exc_info=True)
    error_text = str(exc) if isinstance(exc, (ProcessingError, YouTubeProcessingError, SummaryServiceError, DatabaseServiceError)) else ERROR_TEXT
    await message.answer(error_text, reply_markup=main_menu_keyboard())


@router.message(F.voice)
async def handle_voice(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
) -> None:
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "voice", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=main_menu_keyboard())
        return
    thinking_message = await message.answer("🎙 Принял голосовое. Извлекаю текст...", reply_markup=main_menu_keyboard())
    try:
        processed = await transcriber.process_voice(message.bot, message.voice)
        await _finalize(message, processed, db, summarizer, status, thinking_message)
    except Exception as exc:
        await _handle_processing_error(message, exc, thinking_message)


@router.message(F.video_note)
async def handle_video_note(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
) -> None:
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "video_note", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=main_menu_keyboard())
        return
    thinking_message = await message.answer("🎥 Получил кружок. Достаю аудио и текст...", reply_markup=main_menu_keyboard())
    try:
        processed = await transcriber.process_video_note(message.bot, message.video_note)
        await _finalize(message, processed, db, summarizer, status, thinking_message)
    except Exception as exc:
        await _handle_processing_error(message, exc, thinking_message)


@router.message(F.video)
async def handle_video(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
) -> None:
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "video", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=main_menu_keyboard())
        return
    thinking_message = await message.answer("🎬 Видео получено. Извлекаю дорожку и текст...", reply_markup=main_menu_keyboard())
    try:
        processed = await transcriber.process_video(message.bot, message.video)
        await _finalize(message, processed, db, summarizer, status, thinking_message)
    except Exception as exc:
        await _handle_processing_error(message, exc, thinking_message)


@router.message(F.document)
async def handle_document(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
) -> None:
    document = message.document
    if document.mime_type != "application/pdf":
        return
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "pdf", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=main_menu_keyboard())
        return
    thinking_message = await message.answer("📄 PDF принят. Извлекаю текст со страниц...", reply_markup=main_menu_keyboard())
    try:
        processed = await transcriber.process_pdf(message.bot, document)
        await _finalize(message, processed, db, summarizer, status, thinking_message)
    except Exception as exc:
        await _handle_processing_error(message, exc, thinking_message)


@router.message(F.text | F.caption)
async def handle_links(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
) -> None:
    url = extract_url_from_message(message)
    if not url:
        return
    status = await _check_limit(message, db)
    if status is None:
        return
    source_type = "youtube" if "youtu" in url else "url"
    if not _is_source_allowed(status.plan, source_type, message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=main_menu_keyboard())
        return
    thinking_message = await message.answer("🔎 Смотрю ссылку и достаю исходный текст...", reply_markup=main_menu_keyboard())
    if source_type == "youtube":
        try:
            text = await get_youtube_transcript(url)
        except Exception:
            try:
                await thinking_message.delete()
            except Exception:
                logger.debug("Failed to delete thinking message", exc_info=True)
            await message.answer(
                "⚠️ Не удалось получить субтитры.\n"
                "Попробуй скачать видео и отправить файлом прямо в чат.",
                reply_markup=main_menu_keyboard(),
            )
            return

        processed = ProcessedContent("youtube", text, "YouTube")
        try:
            await _finalize(message, processed, db, summarizer, status, thinking_message)
        except Exception as exc:
            await _handle_processing_error(message, exc, thinking_message)
        return

    try:
        processed = await transcriber.process_url(url)
        await _finalize(message, processed, db, summarizer, status, thinking_message)
    except Exception as exc:
        await _handle_processing_error(message, exc, thinking_message)


@router.message(F.audio)
async def handle_audio(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
) -> None:
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "audio", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=main_menu_keyboard())
        return
    thinking_message = await message.answer("🎧 Аудио получено. Распознаю речь...", reply_markup=main_menu_keyboard())
    try:
        processed = await transcriber.process_audio(message.bot, message.audio)
        await _finalize(message, processed, db, summarizer, status, thinking_message)
    except Exception as exc:
        await _handle_processing_error(message, exc, thinking_message)


@router.callback_query(F.data.startswith("summary:"))
async def handle_summary_actions(
    callback: CallbackQuery,
    db: DatabaseService,
    summarizer: SummaryService,
) -> None:
    if not callback.from_user:
        await callback.answer()
        return
    progress: Message | None = None
    try:
        parts = callback.data.split(":", 3)
        if len(parts) == 4:
            _, provider, style_key, transcription_id = parts
        elif len(parts) == 3:
            _, style_key, transcription_id = parts
            provider = "groq"
        else:
            raise ValueError("bad callback format")
    except ValueError:
        await callback.answer("Некорректное действие", show_alert=True)
        return

    try:
        status = await db.get_usage_status(callback.from_user.id, callback.from_user.username)
        allowed, used, limit = await _check_premium_model_quota(
            callback.from_user.id,
            provider,
            status,
            callback.from_user.username,
            db,
        )
        if not allowed:
            if provider in MODEL_DAILY_LIMITS:
                if not _is_premium_or_admin(status, callback.from_user.username):
                    await callback.answer("Доступно только на Premium", show_alert=True)
                else:
                    await callback.answer(f"Лимит {provider.upper()} исчерпан: {used}/{limit}", show_alert=True)
            else:
                await callback.answer("Недоступно для текущего тарифа", show_alert=True)
            return

        record = await db.get_transcription(transcription_id, callback.from_user.id)
        if record is None:
            await callback.answer("Исходный материал не найден", show_alert=True)
            return

        style_title = SUMMARY_STYLES.get(style_key, SUMMARY_STYLES["detailed"]).title.lower()
        await callback.answer(f"Пересобираю: {style_title} через {_provider_title(provider)}")
        progress = await callback.message.answer(
            "🪄 Пересобираю конспект в новом формате...",
            reply_markup=main_menu_keyboard(),
        )

        summary = await summarizer.summarize_text(record.transcript, style_key=style_key, provider=provider)
        await db.update_summary(record.id, summary, callback.from_user.id)
        if provider in MODEL_DAILY_LIMITS:
            await db.increment_model_usage(callback.from_user.id, provider)
        status = await db.get_usage_status(callback.from_user.id, callback.from_user.username)
        processed = ProcessedContent(record.source_type, record.transcript, "пересборка")
        try:
            await progress.delete()
        except Exception:
            logger.debug("Failed to delete progress message", exc_info=True)
        await callback.message.answer(
            _format_summary(summary, processed.meta, status),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        await callback.message.answer(
            "🎛 <b>Можно пересобрать еще раз в другом стиле.</b>",
            parse_mode="HTML",
            reply_markup=summary_actions_keyboard_for_plan(
                record.id,
                include_premium_models=_is_premium_or_admin(status, callback.from_user.username),
            ),
        )
        await _send_markdown_file(callback.message, summary, processed, status)
    except Exception as exc:
        await _handle_processing_error(callback.message, exc, progress)
