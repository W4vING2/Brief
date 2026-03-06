from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from aiogram import Bot
from aiogram.types import Audio, Document, Message, Video, VideoNote, Voice
from bs4 import BeautifulSoup
from groq import Groq
from pypdf import PdfReader

from bot.services.youtube import YouTubeProcessingError, get_youtube_transcript

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = (
    "youtube.com",
    "youtu.be",
    "www.youtube.com",
    "m.youtube.com",
)


@dataclass(slots=True)
class ProcessedContent:
    source_type: str
    text: str
    meta: str


class ProcessingError(Exception):
    pass


async def transcribe(file_path: str, client: Groq) -> str:
    try:
        with Path(file_path).open("rb") as audio_file:
            result = await asyncio.to_thread(
                client.audio.transcriptions.create,
                model="whisper-large-v3",
                file=audio_file,
                language="ru",
            )
    except Exception as exc:
        raise ProcessingError("⚠️ Не удалось распознать аудио. Попробуй файл с более чистым звуком.") from exc
    text = result.text.strip()
    normalized = re.sub(r"\s+", " ", text).strip()
    logger.info("Transcription preview: %r", normalized[:200])
    if not normalized or len(re.sub(r"[^A-Za-zА-Яа-я0-9]", "", normalized)) < 2:
        raise ProcessingError("⚠️ В аудио не удалось уверенно распознать речь.")
    return normalized


class TranscriptionService:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set")
        self.client = Groq(api_key=api_key)

    async def process_voice(self, bot: Bot, voice: Voice) -> ProcessedContent:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "voice.ogg"
            await bot.download(voice, destination=input_path)
            text = await self._transcribe_audio(input_path)
        return ProcessedContent("voice", text, f"{voice.duration} сек")

    async def process_video_note(self, bot: Bot, video_note: VideoNote) -> ProcessedContent:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "video_note.mp4"
            audio_path = Path(tmpdir) / "video_note.mp3"
            await bot.download(video_note, destination=input_path)
            await self._extract_audio(input_path, audio_path)
            text = await self._transcribe_audio(audio_path)
        return ProcessedContent("video_note", text, f"{video_note.duration} сек")

    async def process_audio(self, bot: Bot, audio: Audio) -> ProcessedContent:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / (audio.file_name or "audio.mp3")
            await bot.download(audio, destination=input_path)
            text = await self._transcribe_audio(input_path)
        return ProcessedContent("audio", text, f"{audio.duration} сек")

    async def process_video(self, bot: Bot, video: Video) -> ProcessedContent:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / (video.file_name or "video.mp4")
            audio_path = Path(tmpdir) / "video.mp3"
            await bot.download(video, destination=input_path)
            await self._extract_audio(input_path, audio_path)
            text = await self._transcribe_audio(audio_path)
        return ProcessedContent("video", text, f"{video.duration} сек")

    async def process_pdf(self, bot: Bot, document: Document) -> ProcessedContent:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / (document.file_name or "document.pdf")
            await bot.download(document, destination=input_path)
            reader = await asyncio.to_thread(PdfReader, str(input_path))
            pages: list[str] = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
        text = "\n".join(filter(None, pages)).strip()
        if not text:
            raise ProcessingError("⚠️ В PDF не удалось найти текст для обработки.")
        return ProcessedContent("pdf", text, f"{len(pages)} стр")

    async def process_url(self, url: str) -> ProcessedContent:
        if self._is_youtube_url(url):
            return await self.process_youtube(url)

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else ""
        text = " ".join(chunk.strip() for chunk in soup.stripped_strings)
        text = re.sub(r"\s+", " ", text).strip()
        if title and not text.startswith(title):
            text = f"{title}\n\n{text}"
        if not text:
            raise ProcessingError("⚠️ Не удалось извлечь текст со страницы по этой ссылке.")
        return ProcessedContent("url", text[:20000], "web")

    async def process_youtube(self, url: str) -> ProcessedContent:
        transcript = await get_youtube_transcript(url)
        meta = "YouTube"
        return ProcessedContent("youtube", transcript, meta)

    async def _transcribe_audio(self, file_path: Path) -> str:
        return await transcribe(str(file_path), self.client)

    async def _extract_audio(self, input_path: Path, output_path: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise ProcessingError("⚠️ Не удалось извлечь аудио из видеофайла.") from RuntimeError(
                stderr.decode().strip() or "ffmpeg failed"
            )

    def _is_youtube_url(self, url: str) -> bool:
        return any(host in url for host in YOUTUBE_HOSTS)


def extract_url_from_message(message: Message) -> str | None:
    candidates = [message.text, message.caption]
    for candidate in candidates:
        if not candidate:
            continue
        match = re.search(r"https?://\S+", candidate)
        if match:
            return match.group(0)
    return None
