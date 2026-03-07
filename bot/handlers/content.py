from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from html import escape
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot.keyboards import (
    export_format_keyboard,
    main_menu_keyboard,
    model_select_keyboard,
    summary_actions_keyboard_for_plan,
)
from bot.services.database import (
    FREE_ALLOWED_SOURCE_TYPES,
    MODEL_DAILY_LIMITS,
    DatabaseService,
    DatabaseServiceError,
    UsageStatus,
    is_admin_username,
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
EMPTY_SUMMARY_MARKERS = (
    "материал не предоставлен",
    "нет информации для конспекта",
    "требуется текст для анализа",
)

PROVIDER_LABELS = {
    "groq": "Groq",
    "gpt4o": "ChatGPT",
    "claude": "Claude",
}


@dataclass(slots=True)
class ReworkPayload:
    source_type: str
    transcript: str
    meta: str


@dataclass(slots=True)
class ExportPayload:
    summary: str
    processed: ProcessedContent
    remaining: str
    provider: str


@dataclass(slots=True)
class SummaryParts:
    main: str
    brief: str
    bullets: list[str]
    conclusion: str


REWORK_CACHE: dict[str, ReworkPayload] = {}
EXPORT_CACHE: dict[str, ExportPayload] = {}
MAX_CACHE_ITEMS = 500


def _menu_for_username(username: str | None):
    return main_menu_keyboard(username)


def _provider_title(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider)


def _format_remaining(status: UsageStatus) -> str:
    if status.limit is None:
        return "∞"
    return f"{status.remaining}/{status.limit}"


def _is_source_allowed(plan: str, source_type: str, username: str | None = None) -> bool:
    if plan in {"pro", "premium"} or is_admin_username(username):
        return True
    if plan == "free":
        return source_type in FREE_ALLOWED_SOURCE_TYPES
    return True


async def _check_limit(message: Message, db: DatabaseService) -> UsageStatus | None:
    try:
        await db.ensure_user(message.from_user.id, message.from_user.username)
        status = await db.get_usage_status(message.from_user.id, message.from_user.username)
    except DatabaseServiceError as exc:
        await message.answer(str(exc), reply_markup=_menu_for_username(message.from_user.username))
        return None
    if status.is_exceeded:
        limit = status.limit or 5
        await message.answer(
            f"❌ Лимит исчерпан. Осталось: 0/{limit}. Оформи подписку /plans",
            reply_markup=_menu_for_username(message.from_user.username),
        )
        return None
    return status


async def _check_limit_callback(callback: CallbackQuery, db: DatabaseService) -> UsageStatus | None:
    if not callback.from_user:
        await callback.answer()
        return None
    try:
        status = await db.get_usage_status(callback.from_user.id, callback.from_user.username)
    except DatabaseServiceError as exc:
        if callback.message:
            await callback.message.answer(
                str(exc),
                reply_markup=_menu_for_username(callback.from_user.username),
            )
        await callback.answer()
        return None
    if status.is_exceeded:
        limit = status.limit or 5
        await callback.answer(f"Лимит исчерпан: 0/{limit}", show_alert=True)
        return None
    return status


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
    if status.plan not in {"premium", "admin"}:
        return False, 0, MODEL_DAILY_LIMITS[provider]
    used = await db.get_model_usage(user_id, provider)
    limit = MODEL_DAILY_LIMITS[provider]
    return used < limit, used, limit


async def _resolve_provider_for_message(
    message: Message,
    status: UsageStatus,
    db: DatabaseService,
    state: FSMContext,
) -> str | None:
    if status.plan not in {"premium", "admin"}:
        return "groq"

    state_data = await state.get_data()
    provider = state_data.get("selected_provider")
    if provider not in PROVIDER_LABELS:
        await message.answer(
            "Перед отправкой выбери модель:",
            reply_markup=model_select_keyboard(),
        )
        await message.answer(
            "Доступные модели: Groq, Claude, ChatGPT.",
            reply_markup=_menu_for_username(message.from_user.username),
        )
        return None

    allowed, used, limit = await _check_premium_model_quota(
        message.from_user.id,
        provider,
        status,
        message.from_user.username,
        db,
    )
    if not allowed:
        await message.answer(
            f"⚠️ Лимит {_provider_title(provider)} исчерпан: {used}/{limit}.",
            reply_markup=_menu_for_username(message.from_user.username),
        )
        return None
    return provider


async def _resolve_provider_for_callback(
    callback: CallbackQuery,
    status: UsageStatus,
    db: DatabaseService,
    state: FSMContext,
) -> str | None:
    if not callback.from_user:
        await callback.answer()
        return None

    if status.plan not in {"premium", "admin"}:
        return "groq"

    state_data = await state.get_data()
    provider = state_data.get("selected_provider")
    if provider not in PROVIDER_LABELS:
        if callback.message:
            await callback.message.answer(
                "Перед переделыванием выбери модель:",
                reply_markup=model_select_keyboard(),
            )
        await callback.answer("Сначала выбери модель", show_alert=True)
        return None

    allowed, used, limit = await _check_premium_model_quota(
        callback.from_user.id,
        provider,
        status,
        callback.from_user.username,
        db,
    )
    if not allowed:
        await callback.answer(f"Лимит {_provider_title(provider)}: {used}/{limit}", show_alert=True)
        return None
    return provider


def _normalize_line(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"^#+\s*", "", cleaned)
    cleaned = cleaned.lstrip("-•* ").strip()
    return cleaned


def _extract_summary_parts(summary: str) -> SummaryParts:
    main_lines: list[str] = []
    brief_lines: list[str] = []
    bullets: list[str] = []
    conclusion_lines: list[str] = []
    section = "brief"

    for raw_line in summary.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        lower = line.lower().rstrip(":")
        if lower.startswith("самое главное"):
            section = "main"
            tail = line.split(":", 1)
            if len(tail) == 2 and tail[1].strip():
                main_lines.append(tail[1].strip())
            continue
        if lower.startswith(("кратко", "о чем материал", "о чём материал")):
            section = "brief"
            tail = line.split(":", 1)
            if len(tail) == 2 and tail[1].strip():
                brief_lines.append(tail[1].strip())
            continue
        if lower.startswith(("уточнения", "ключевые тезисы", "главное", "основные тезисы")):
            section = "bullets"
            continue
        if lower.startswith(("вывод", "итог", "итоги")):
            section = "conclusion"
            tail = line.split(":", 1)
            if len(tail) == 2 and tail[1].strip():
                conclusion_lines.append(tail[1].strip())
            continue

        if raw_line.strip().startswith(("-", "•", "*")) or section == "bullets":
            bullets.append(line)
            continue
        if section == "main":
            main_lines.append(line)
            continue
        if section == "conclusion":
            conclusion_lines.append(line)
            continue
        brief_lines.append(line)

    brief_text = " ".join(brief_lines).strip()
    if not brief_text:
        brief_text = " ".join(_normalize_line(part) for part in summary.splitlines()).strip()
    main_text = " ".join(main_lines).strip()
    if not main_text:
        main_text = brief_text.split(".")[0].strip() if brief_text else "Ключевая идея не выделена."
    if not bullets and brief_text:
        bullets = [sentence.strip() for sentence in re.split(r"[.!?]", brief_text) if sentence.strip()][:5]
    conclusion = " ".join(conclusion_lines).strip()
    if not conclusion:
        conclusion = "Материал полезен как ориентир по теме."
    return SummaryParts(main=main_text, brief=brief_text, bullets=bullets, conclusion=conclusion)


def _format_summary(summary: str, meta: str, status: UsageStatus, provider: str) -> str:
    parts = _extract_summary_parts(summary)
    bullets = "\n".join(f"• {escape(item)}" for item in parts.bullets) or "• Нет уточнений"
    return (
        "🔥 <b>САМОЕ ГЛАВНОЕ</b>\n"
        f"{escape(parts.main)}\n\n"
        "🧾 <b>КРАТКО</b>\n"
        f"{escape(parts.brief)}\n\n"
        "🔍 <b>УТОЧНЕНИЯ</b>\n"
        f"{bullets}\n\n"
        "✅ <b>ВЫВОД</b>\n"
        f"{escape(parts.conclusion)}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🤖 <b>Модель:</b> {_provider_title(provider)}\n"
        f"⏱ <b>Источник/объем:</b> {escape(meta)}\n"
        f"🆓 <b>Осталось сегодня:</b> {escape(_format_remaining(status))}"
    )


def _build_markdown(summary: str, payload: ExportPayload) -> str:
    parts = _extract_summary_parts(summary)
    bullets = "\n".join(f"- {item}" for item in parts.bullets) or "- Нет уточнений"
    return (
        "# BriefBot — Конспект\n\n"
        "## Самое главное\n"
        f"{parts.main}\n\n"
        "## Кратко\n"
        f"{parts.brief}\n\n"
        "## Уточнения\n"
        f"{bullets}\n\n"
        "## Вывод\n"
        f"{parts.conclusion}\n\n"
        "---\n"
        f"- Модель: {payload.provider}\n"
        f"- Источник: {payload.processed.source_type}\n"
        f"- Объем: {payload.processed.meta}\n"
        f"- Осталось сегодня: {payload.remaining}\n"
    )


def _build_txt(summary: str, payload: ExportPayload) -> str:
    parts = _extract_summary_parts(summary)
    bullets = "\n".join(f"- {item}" for item in parts.bullets) or "- Нет уточнений"
    return (
        "САМОЕ ГЛАВНОЕ\n"
        f"{parts.main}\n\n"
        "КРАТКО\n"
        f"{parts.brief}\n\n"
        "УТОЧНЕНИЯ\n"
        f"{bullets}\n\n"
        "ВЫВОД\n"
        f"{parts.conclusion}\n\n"
        f"Модель: {payload.provider}\n"
        f"Источник: {payload.processed.source_type}\n"
        f"Объем: {payload.processed.meta}\n"
        f"Осталось сегодня: {payload.remaining}\n"
    )


def _find_pdf_font() -> str | None:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def _build_pdf_file(text: str, source_type: str) -> str:
    try:
        from fpdf import FPDF
    except Exception as exc:  # pragma: no cover
        raise ProcessingError("⚠️ PDF-вариант временно недоступен.") from exc

    font_path = _find_pdf_font()
    if not font_path:
        raise ProcessingError("⚠️ PDF-вариант временно недоступен (нет шрифта для русского текста).")

    with tempfile.NamedTemporaryFile(suffix=".pdf", prefix=f"briefbot-{source_type}-", delete=False) as temp_file:
        output_path = temp_file.name

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.add_font("Unicode", "", font_path, uni=True)
    pdf.set_font("Unicode", size=11)
    for line in text.splitlines():
        pdf.multi_cell(0, 7, line)
    pdf.output(output_path)
    return output_path


def _create_export_payload(summary: str, processed: ProcessedContent, status: UsageStatus, provider: str) -> str:
    token = uuid4().hex
    if len(EXPORT_CACHE) >= MAX_CACHE_ITEMS:
        EXPORT_CACHE.pop(next(iter(EXPORT_CACHE)))
    EXPORT_CACHE[token] = ExportPayload(
        summary=summary,
        processed=processed,
        remaining=_format_remaining(status),
        provider=_provider_title(provider),
    )
    return token


def _create_rework_payload(processed: ProcessedContent) -> str:
    token = uuid4().hex
    if len(REWORK_CACHE) >= MAX_CACHE_ITEMS:
        REWORK_CACHE.pop(next(iter(REWORK_CACHE)))
    REWORK_CACHE[token] = ReworkPayload(
        source_type=processed.source_type,
        transcript=processed.text,
        meta=processed.meta,
    )
    return token


async def _update_progress(progress_message: Message, text: str, username: str | None) -> None:
    try:
        await progress_message.edit_text(text, reply_markup=_menu_for_username(username))
    except Exception:
        logger.debug("Failed to update progress message", exc_info=True)


async def _send_export_file(message: Message, export_token: str, fmt: str, username: str | None) -> None:
    payload = EXPORT_CACHE.get(export_token)
    if payload is None:
        await message.answer("⚠️ Файл уже недоступен. Сгенерируй конспект заново.")
        return

    temp_path: str | None = None
    filename = f"briefbot-{payload.processed.source_type}-summary"
    try:
        if fmt == "md":
            content = _build_markdown(payload.summary, payload)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".md",
                prefix=f"{filename}-",
                delete=False,
            ) as temp_file:
                temp_file.write(content)
                temp_path = temp_file.name
            out_name = f"{filename}.md"
        elif fmt == "txt":
            content = _build_txt(payload.summary, payload)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix=f"{filename}-",
                delete=False,
            ) as temp_file:
                temp_file.write(content)
                temp_path = temp_file.name
            out_name = f"{filename}.txt"
        elif fmt == "pdf":
            content = _build_txt(payload.summary, payload)
            temp_path = _build_pdf_file(content, payload.processed.source_type)
            out_name = f"{filename}.pdf"
        else:
            await message.answer("Ок, файл не отправляю.", reply_markup=_menu_for_username(username))
            return

        await message.bot.send_document(
            chat_id=message.chat.id,
            document=FSInputFile(temp_path, filename=out_name),
            caption="Готово, отправил файл ✅",
        )
    except Exception as exc:
        logger.exception("Failed to send export file: %s", exc)
        await message.answer("⚠️ Не удалось сформировать файл в этом формате.")
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


async def _deliver_summary(
    message: Message,
    summary: str,
    processed: ProcessedContent,
    db: DatabaseService,
    status_before: UsageStatus,
    provider: str,
    user_id: int,
    username: str | None,
    thinking_message: Message | None = None,
) -> None:
    await db.save_transcription(
        user_id=user_id,
        source_type=processed.source_type,
        transcript=processed.text,
        summary=summary,
        plan=status_before.plan,
    )
    status = await db.increment_usage(user_id, username)
    if provider in MODEL_DAILY_LIMITS:
        await db.increment_model_usage(user_id, provider)

    if thinking_message is not None:
        try:
            await thinking_message.delete()
        except Exception:
            logger.debug("Failed to delete progress message", exc_info=True)

    await message.answer(
        _format_summary(summary, processed.meta, status, provider),
        parse_mode="HTML",
        reply_markup=_menu_for_username(username),
    )

    rework_token = _create_rework_payload(processed)
    await message.answer(
        "♻️ Нужна переделка? Выбери формат:",
        reply_markup=summary_actions_keyboard_for_plan(rework_token, include_premium_models=False),
    )

    export_token = _create_export_payload(summary, processed, status, provider)
    await message.answer(
        "📎 В каком формате желаете получить конспект?",
        reply_markup=export_format_keyboard(export_token),
    )


async def _finalize(
    message: Message,
    processed: ProcessedContent,
    db: DatabaseService,
    summarizer: SummaryService,
    status_before: UsageStatus,
    provider: str,
    user_id: int,
    username: str | None,
    thinking_message: Message | None = None,
    style_key: str = "detailed",
) -> None:
    if thinking_message is not None:
        await _update_progress(
            thinking_message,
            f"🧠 Думаю... собираю конспект через {_provider_title(provider)}",
            username,
        )

    summary = await summarizer.summarize_text(processed.text, style_key=style_key, provider=provider)
    lowered = summary.lower()
    if any(marker in lowered for marker in EMPTY_SUMMARY_MARKERS):
        preview = processed.text[:700].strip()
        summary = (
            f"{preview}\n\n"
            "- Материал распознан, но для полноценной структуры текста пока мало.\n"
            "- Попробуй отправить более длинный или более четкий материал."
        )

    await _deliver_summary(
        message=message,
        summary=summary,
        processed=processed,
        db=db,
        status_before=status_before,
        provider=provider,
        user_id=user_id,
        username=username,
        thinking_message=thinking_message,
    )


async def _handle_processing_error(
    message: Message,
    exc: Exception,
    username: str | None,
    thinking_message: Message | None = None,
) -> None:
    logger.exception("Processing failed: %s", exc)
    if thinking_message is not None:
        try:
            await thinking_message.delete()
        except Exception:
            logger.debug("Failed to delete thinking message", exc_info=True)
    if isinstance(exc, (ProcessingError, YouTubeProcessingError, SummaryServiceError, DatabaseServiceError)):
        await message.answer(str(exc), reply_markup=_menu_for_username(username))
        return
    await message.answer(ERROR_TEXT, reply_markup=_menu_for_username(username))


@router.message(F.voice)
async def handle_voice(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
    state: FSMContext,
) -> None:
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "voice", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=_menu_for_username(message.from_user.username))
        return
    provider = await _resolve_provider_for_message(message, status, db, state)
    if provider is None:
        return

    thinking_message = await message.answer("🎙 Принял голосовое. Думаю...", reply_markup=_menu_for_username(message.from_user.username))
    try:
        processed = await transcriber.process_voice(message.bot, message.voice)
        await _finalize(
            message,
            processed,
            db,
            summarizer,
            status,
            provider,
            message.from_user.id,
            message.from_user.username,
            thinking_message,
        )
    except Exception as exc:
        await _handle_processing_error(message, exc, message.from_user.username, thinking_message)


@router.message(F.video_note)
async def handle_video_note(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
    state: FSMContext,
) -> None:
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "video_note", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=_menu_for_username(message.from_user.username))
        return
    provider = await _resolve_provider_for_message(message, status, db, state)
    if provider is None:
        return

    thinking_message = await message.answer("🎥 Получил кружок. Думаю...", reply_markup=_menu_for_username(message.from_user.username))
    try:
        processed = await transcriber.process_video_note(message.bot, message.video_note)
        await _finalize(
            message,
            processed,
            db,
            summarizer,
            status,
            provider,
            message.from_user.id,
            message.from_user.username,
            thinking_message,
        )
    except Exception as exc:
        await _handle_processing_error(message, exc, message.from_user.username, thinking_message)


@router.message(F.video)
async def handle_video(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
    state: FSMContext,
) -> None:
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "video", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=_menu_for_username(message.from_user.username))
        return
    provider = await _resolve_provider_for_message(message, status, db, state)
    if provider is None:
        return

    thinking_message = await message.answer("🎬 Видео получено. Думаю...", reply_markup=_menu_for_username(message.from_user.username))
    try:
        processed = await transcriber.process_video(message.bot, message.video)
        await _finalize(
            message,
            processed,
            db,
            summarizer,
            status,
            provider,
            message.from_user.id,
            message.from_user.username,
            thinking_message,
        )
    except Exception as exc:
        await _handle_processing_error(message, exc, message.from_user.username, thinking_message)


@router.message(F.document)
async def handle_document(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
    state: FSMContext,
) -> None:
    document = message.document
    if document.mime_type != "application/pdf":
        return
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "pdf", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=_menu_for_username(message.from_user.username))
        return
    provider = await _resolve_provider_for_message(message, status, db, state)
    if provider is None:
        return

    thinking_message = await message.answer("📄 PDF принят. Думаю...", reply_markup=_menu_for_username(message.from_user.username))
    try:
        processed = await transcriber.process_pdf(message.bot, document)
        await _finalize(
            message,
            processed,
            db,
            summarizer,
            status,
            provider,
            message.from_user.id,
            message.from_user.username,
            thinking_message,
        )
    except Exception as exc:
        await _handle_processing_error(message, exc, message.from_user.username, thinking_message)


@router.message(F.text | F.caption)
async def handle_links(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
    state: FSMContext,
) -> None:
    url = extract_url_from_message(message)
    if not url:
        return
    status = await _check_limit(message, db)
    if status is None:
        return
    source_type = "youtube" if "youtu" in url else "url"
    if not _is_source_allowed(status.plan, source_type, message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=_menu_for_username(message.from_user.username))
        return
    provider = await _resolve_provider_for_message(message, status, db, state)
    if provider is None:
        return

    thinking_message = await message.answer("🔎 Смотрю ссылку и думаю...", reply_markup=_menu_for_username(message.from_user.username))
    if source_type == "youtube":
        try:
            text = await get_youtube_transcript(url)
            processed = ProcessedContent("youtube", text, "YouTube")
            await _finalize(
                message,
                processed,
                db,
                summarizer,
                status,
                provider,
                message.from_user.id,
                message.from_user.username,
                thinking_message,
            )
        except Exception as exc:
            await _handle_processing_error(message, exc, message.from_user.username, thinking_message)
        return

    try:
        processed = await transcriber.process_url(url)
        await _finalize(
            message,
            processed,
            db,
            summarizer,
            status,
            provider,
            message.from_user.id,
            message.from_user.username,
            thinking_message,
        )
    except Exception as exc:
        await _handle_processing_error(message, exc, message.from_user.username, thinking_message)


@router.message(F.audio)
async def handle_audio(
    message: Message,
    db: DatabaseService,
    transcriber: TranscriptionService,
    summarizer: SummaryService,
    state: FSMContext,
) -> None:
    status = await _check_limit(message, db)
    if status is None:
        return
    if not _is_source_allowed(status.plan, "audio", message.from_user.username):
        await message.answer("⚠️ На тарифе Free доступны только ГС, кружки и аудио.", reply_markup=_menu_for_username(message.from_user.username))
        return
    provider = await _resolve_provider_for_message(message, status, db, state)
    if provider is None:
        return

    thinking_message = await message.answer("🎧 Аудио получено. Думаю...", reply_markup=_menu_for_username(message.from_user.username))
    try:
        processed = await transcriber.process_audio(message.bot, message.audio)
        await _finalize(
            message,
            processed,
            db,
            summarizer,
            status,
            provider,
            message.from_user.id,
            message.from_user.username,
            thinking_message,
        )
    except Exception as exc:
        await _handle_processing_error(message, exc, message.from_user.username, thinking_message)


@router.callback_query(F.data.startswith("summary:"))
async def handle_summary_actions(
    callback: CallbackQuery,
    db: DatabaseService,
    summarizer: SummaryService,
    state: FSMContext,
) -> None:
    if not callback.from_user or not callback.message or not callback.data:
        await callback.answer()
        return

    try:
        _, style_key, token = callback.data.split(":", 2)
    except ValueError:
        await callback.answer("Некорректное действие", show_alert=True)
        return

    if style_key not in {"short", "detailed", "post"}:
        await callback.answer("Неизвестный формат", show_alert=True)
        return

    status = await _check_limit_callback(callback, db)
    if status is None:
        return
    provider = await _resolve_provider_for_callback(callback, status, db, state)
    if provider is None:
        return

    payload = REWORK_CACHE.get(token)
    if payload is None:
        await callback.answer("Материал недоступен, отправь заново.", show_alert=True)
        return

    style_title = SUMMARY_STYLES.get(style_key, SUMMARY_STYLES["detailed"]).title.lower()
    await callback.answer(f"Переделываю: {style_title}")
    progress = await callback.message.answer("🪄 Думаю... переделываю конспект.")
    try:
        processed = ProcessedContent(payload.source_type, payload.transcript, payload.meta)
        summary = await summarizer.summarize_text(processed.text, style_key=style_key, provider=provider)
        await _deliver_summary(
            message=callback.message,
            summary=summary,
            processed=processed,
            db=db,
            status_before=status,
            provider=provider,
            user_id=callback.from_user.id,
            username=callback.from_user.username,
            thinking_message=progress,
        )
    except Exception as exc:
        await _handle_processing_error(callback.message, exc, callback.from_user.username, progress)


@router.callback_query(F.data.startswith("export:"))
async def handle_export_format(callback: CallbackQuery) -> None:
    if not callback.message or not callback.data:
        await callback.answer()
        return
    try:
        _, fmt, token = callback.data.split(":", 2)
    except ValueError:
        await callback.answer("Некорректное действие", show_alert=True)
        return

    if fmt == "none":
        await callback.answer("Ок, без файла")
        return

    await callback.answer("Готовлю файл...")
    await _send_export_file(callback.message, token, fmt, callback.from_user.username if callback.from_user else None)
