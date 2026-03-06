from __future__ import annotations

import asyncio
import os
import re
from typing import Iterable

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig

YOUTUBE_ID_PATTERN = re.compile(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})")
PREFERRED_LANGUAGES = ["ru", "en"]


class YouTubeProcessingError(Exception):
    pass


def extract_video_id(url: str) -> str:
    match = YOUTUBE_ID_PATTERN.search(url)
    if not match:
        raise ValueError("Не удалось извлечь ID видео из ссылки")
    return match.group(1)


def _join_transcript_lines(fetched: Iterable[object]) -> str:
    chunks: list[str] = []
    for snippet in fetched:
        text = getattr(snippet, "text", None)
        if text is None and isinstance(snippet, dict):
            text = snippet.get("text", "")
        if text:
            chunks.append(str(text).strip())
    return " ".join(chunk for chunk in chunks if chunk).strip()


async def _fetch_transcript_text(ytt: YouTubeTranscriptApi, video_id: str) -> str:
    fetched = await asyncio.to_thread(ytt.fetch, video_id, languages=PREFERRED_LANGUAGES)
    text = _join_transcript_lines(fetched)
    if not text:
        raise Exception("Субтитры недоступны для этого видео")
    return text


async def get_youtube_transcript(url: str) -> str:
    video_id = extract_video_id(url)
    # Сначала пробуем без прокси
    try:
        ytt = YouTubeTranscriptApi()
        return await _fetch_transcript_text(ytt, video_id)
    except Exception:
        pass

    # Если заблокировано — пробуем через Webshare прокси
    try:
        proxy_config = WebshareProxyConfig(
            proxy_username=os.getenv("WEBSHARE_PROXY_USERNAME"),
            proxy_password=os.getenv("WEBSHARE_PROXY_PASSWORD"),
        )
        ytt = YouTubeTranscriptApi(proxy_config=proxy_config)
        return await _fetch_transcript_text(ytt, video_id)
    except Exception as exc:
        raise Exception("YouTube недоступен. Попробуй скачать видео и отправить файлом.") from exc


async def fetch_youtube_transcript(url: str) -> str:
    return await get_youtube_transcript(url)
