from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

try:
    from anthropic import Anthropic
except Exception:  # pragma: no cover - optional dependency at runtime
    Anthropic = None

from groq import Groq
try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency at runtime
    OpenAI = None

CHUNK_SIZE = 12000
CHUNK_OVERLAP = 800


@dataclass(frozen=True, slots=True)
class SummaryStyle:
    key: str
    title: str
    prompt_suffix: str


SUMMARY_STYLES = {
    "detailed": SummaryStyle(
        key="detailed",
        title="Подробнее",
        prompt_suffix=(
            "Сделай развернутый, но собранный конспект.\n"
            "Структура ответа:\n"
            "1. Заголовок: 'О чем материал' и 2-4 предложения с сутью материала.\n"
            "2. Заголовок: 'Ключевые тезисы' и 5-10 маркированных пунктов.\n"
            "3. Заголовок: 'Вывод' и 2-3 предложения с итогом или практическим смыслом."
        ),
    ),
    "short": SummaryStyle(
        key="short",
        title="Коротко",
        prompt_suffix=(
            "Сделай максимально короткий и плотный конспект.\n"
            "Структура ответа:\n"
            "1. Заголовок: 'О чем материал' и 1-2 предложения.\n"
            "2. Заголовок: 'Ключевые тезисы' и 3-5 пунктов.\n"
            "3. Заголовок: 'Вывод' и 1-2 предложения."
        ),
    ),
    "checklist": SummaryStyle(
        key="checklist",
        title="Чеклист",
        prompt_suffix=(
            "Преобразуй материал в практический чеклист.\n"
            "Структура ответа:\n"
            "1. Заголовок: 'О чем материал' и 2-3 предложения.\n"
            "2. Заголовок: 'Ключевые тезисы' и 5-10 пунктов в форме действий или проверок.\n"
            "3. Заголовок: 'Вывод' и короткий practical takeaway."
        ),
    ),
    "post": SummaryStyle(
        key="post",
        title="Для поста",
        prompt_suffix=(
            "Сделай конспект в формате основы для Telegram-поста.\n"
            "Структура ответа:\n"
            "1. Заголовок: 'О чем материал' и 2-3 цепляющих предложения.\n"
            "2. Заголовок: 'Ключевые тезисы' и 4-7 ярких пунктов.\n"
            "3. Заголовок: 'Вывод' и короткое заключение/призыв."
        ),
    ),
}

BASE_PROMPT = (
    "Ты помощник, который создает понятный конспект на русском языке. "
    "Нужно выделить тему материала, основные мысли, факты, аргументы и выводы. "
    "Пиши содержательно, без воды и без выдуманных деталей. "
    "Если текст короткий, перескажи его максимально полно. "
    "Никогда не пиши, что материал не предоставлен, если в сообщении есть текст.\n\n"
)


class SummaryServiceError(Exception):
    pass


def _build_prompt(style: SummaryStyle) -> str:
    return BASE_PROMPT + style.prompt_suffix


def _chunk_text(text: str) -> list[str]:
    normalized = text.strip()
    if len(normalized) <= CHUNK_SIZE:
        return [normalized]

    chunks: list[str] = []
    start = 0
    text_length = len(normalized)
    while start < text_length:
        end = min(start + CHUNK_SIZE, text_length)
        chunk = normalized[start:end]
        if end < text_length:
            split = max(chunk.rfind("\n\n"), chunk.rfind(". "), chunk.rfind("! "), chunk.rfind("? "))
            if split > CHUNK_SIZE // 2:
                end = start + split + 1
                chunk = normalized[start:end]
        chunks.append(chunk.strip())
        if end >= text_length:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [chunk for chunk in chunks if chunk]


async def summarize(text: str, client: Groq, style: SummaryStyle) -> str:
    result = await asyncio.to_thread(
        client.chat.completions.create,
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _build_prompt(style)},
            {"role": "user", "content": text[:15000]},
        ],
    )
    return result.choices[0].message.content.strip()


class SummaryService:
    def __init__(
        self,
        api_key: str,
        *,
        openai_api_key: str | None = None,
        anthropic_api_key: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set")
        self.client = Groq(api_key=api_key)
        self.openai_client = OpenAI(api_key=openai_api_key) if openai_api_key and OpenAI else None
        self.anthropic_client = Anthropic(api_key=anthropic_api_key) if anthropic_api_key and Anthropic else None

    async def summarize_text(
        self,
        text: str,
        style_key: str = "detailed",
        provider: str = "groq",
    ) -> str:
        style = SUMMARY_STYLES.get(style_key, SUMMARY_STYLES["detailed"])
        chunks = _chunk_text(text)
        if not chunks:
            raise SummaryServiceError("⚠️ Не удалось подготовить текст для конспекта.")

        try:
            if len(chunks) == 1:
                return await self._summarize_once(chunks[0], style, provider)

            partials = []
            for index, chunk in enumerate(chunks, start=1):
                chunk_prompt = (
                    f"Это часть {index} из {len(chunks)} большого материала.\n\n{chunk}"
                )
                partials.append(await self._summarize_once(chunk_prompt, style, provider))

            combined = "\n\n".join(
                f"Часть {index}:\n{part}" for index, part in enumerate(partials, start=1)
            )
            synthesis_input = (
                "Ниже находятся конспекты частей одного большого материала. "
                "Собери из них единый, цельный итоговый конспект без повторов.\n\n"
                f"{combined}"
            )
            return await self._summarize_once(synthesis_input, style, provider)
        except Exception as exc:
            raise SummaryServiceError("⚠️ Не удалось получить конспект от модели.") from exc

    async def _summarize_once(self, text: str, style: SummaryStyle, provider: str) -> str:
        if provider == "groq":
            return await summarize(text, self.client, style)
        if provider == "gpt4o":
            return await self._summarize_openai(text, style)
        if provider == "claude":
            return await self._summarize_claude(text, style)
        raise SummaryServiceError("⚠️ Неизвестный AI-провайдер.")

    async def _summarize_openai(self, text: str, style: SummaryStyle) -> str:
        if not self.openai_client:
            raise SummaryServiceError("⚠️ GPT-4o недоступен: не настроен OPENAI_API_KEY или пакет openai.")
        result = await asyncio.to_thread(
            self.openai_client.chat.completions.create,
            model="gpt-4o",
            messages=[
                {"role": "system", "content": _build_prompt(style)},
                {"role": "user", "content": text[:15000]},
            ],
        )
        content = result.choices[0].message.content or ""
        if not content.strip():
            raise SummaryServiceError("⚠️ GPT-4o вернул пустой ответ.")
        return content.strip()

    async def _summarize_claude(self, text: str, style: SummaryStyle) -> str:
        if not self.anthropic_client:
            raise SummaryServiceError("⚠️ Claude недоступен: не настроен ANTHROPIC_API_KEY или пакет anthropic.")
        result = await asyncio.to_thread(
            self.anthropic_client.messages.create,
            model="claude-3-5-sonnet-latest",
            max_tokens=2048,
            system=_build_prompt(style),
            messages=[{"role": "user", "content": text[:15000]}],
        )
        parts: list[str] = []
        for block in result.content:
            block_text = getattr(block, "text", "")
            if block_text:
                parts.append(block_text)
        content = "\n".join(parts).strip()
        if not content:
            raise SummaryServiceError("⚠️ Claude вернул пустой ответ.")
        return content
