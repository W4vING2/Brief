from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from supabase import Client, create_client

PLAN_LIMITS = {
    "free": 5,
    "pro": None,
    "premium": None,
}
PLAN_PRICES = {
    "pro": 149,
    "premium": 449,
}
PLAN_HISTORY_RETENTION_DAYS = {
    "free": 0,
    "pro": 30,
    "premium": None,
    "admin": None,
}
MODEL_DAILY_LIMITS = {
    "gpt4o": 15,
    "claude": 15,
}
FREE_ALLOWED_SOURCE_TYPES = {"voice", "video_note", "audio"}

ADMIN_USERNAMES = {"w9v33"}
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UsageStatus:
    plan: str
    used: int
    limit: int | None

    @property
    def is_exceeded(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(self.limit - self.used, 0)


@dataclass(slots=True)
class TranscriptionRecord:
    id: str
    source_type: str
    transcript: str
    summary: str
    created_at: str


class DatabaseServiceError(Exception):
    pass


def is_admin_username(username: str | None) -> bool:
    return bool(username and username.lstrip("@").lower() in ADMIN_USERNAMES)


def should_save_history(plan: str) -> bool:
    return PLAN_HISTORY_RETENTION_DAYS.get(plan) != 0


class DatabaseService:
    def __init__(self, url: str, key: str) -> None:
        self.client: Client = create_client(url, key)
        self._model_usage_fallback: dict[tuple[int, str, str], int] = {}

    async def ensure_user(self, user_id: int, username: str | None) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._ensure_user_sync, user_id, username)
        except Exception as exc:
            raise DatabaseServiceError("⚠️ База временно недоступна. Попробуй еще раз чуть позже.") from exc

    def _ensure_user_sync(self, user_id: int, username: str | None) -> dict[str, Any]:
        existing = (
            self.client.table("users")
            .select("*")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            user = existing.data[0]
            updates: dict[str, Any] = {}
            if username and user.get("username") != username:
                updates["username"] = username
            if updates:
                user = (
                    self.client.table("users")
                    .update(updates)
                    .eq("id", user_id)
                    .execute()
                    .data[0]
                )
            return user

        payload = {
            "id": user_id,
            "username": username,
            "plan": "free",
            "created_at": datetime.now(UTC).isoformat(),
        }
        return self.client.table("users").insert(payload).execute().data[0]

    async def get_usage_status(self, user_id: int, username: str | None = None) -> UsageStatus:
        try:
            return await asyncio.to_thread(self._get_usage_status_sync, user_id, username)
        except Exception as exc:
            raise DatabaseServiceError("⚠️ Не удалось получить данные по лимитам.") from exc

    def _get_usage_status_sync(self, user_id: int, username: str | None = None) -> UsageStatus:
        today = date.today().isoformat()
        user_resp = (
            self.client.table("users")
            .select("plan")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        plan = user_resp.data[0]["plan"] if user_resp.data else "free"
        if is_admin_username(username):
            plan = "admin"
        usage_resp = (
            self.client.table("daily_usage")
            .select("count")
            .eq("user_id", user_id)
            .eq("date", today)
            .limit(1)
            .execute()
        )
        used = usage_resp.data[0]["count"] if usage_resp.data else 0
        return UsageStatus(plan=plan, used=used, limit=PLAN_LIMITS.get(plan))

    async def increment_usage(self, user_id: int, username: str | None = None) -> UsageStatus:
        try:
            return await asyncio.to_thread(self._increment_usage_sync, user_id, username)
        except Exception as exc:
            raise DatabaseServiceError("⚠️ Не удалось обновить лимиты пользователя.") from exc

    def _increment_usage_sync(self, user_id: int, username: str | None = None) -> UsageStatus:
        status = self._get_usage_status_sync(user_id, username)
        today = date.today().isoformat()
        new_count = status.used + 1
        payload = {
            "user_id": user_id,
            "date": today,
            "count": new_count,
        }
        self.client.table("daily_usage").upsert(payload).execute()
        return UsageStatus(plan=status.plan, used=new_count, limit=status.limit)

    async def save_transcription(
        self,
        *,
        user_id: int,
        source_type: str,
        transcript: str,
        summary: str,
        plan: str,
    ) -> TranscriptionRecord | None:
        try:
            return await asyncio.to_thread(
                self._save_transcription_sync,
                user_id,
                source_type,
                transcript,
                summary,
                plan,
            )
        except Exception as exc:
            raise DatabaseServiceError("⚠️ Не удалось сохранить результат в базе.") from exc

    async def get_recent_transcriptions(
        self,
        user_id: int,
        limit: int = 5,
    ) -> list[TranscriptionRecord]:
        try:
            return await asyncio.to_thread(self._get_recent_transcriptions_sync, user_id, limit)
        except Exception as exc:
            raise DatabaseServiceError("⚠️ Не удалось загрузить историю запросов.") from exc

    def _get_recent_transcriptions_sync(self, user_id: int, limit: int) -> list[TranscriptionRecord]:
        response = (
            self.client.table("transcriptions")
            .select("id, source_type, transcript, summary, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [
            TranscriptionRecord(
                id=item["id"],
                source_type=item["source_type"],
                transcript=item.get("transcript", ""),
                summary=item.get("summary", ""),
                created_at=item.get("created_at", ""),
            )
            for item in response.data
        ]

    async def get_transcription(self, transcription_id: str, user_id: int) -> TranscriptionRecord | None:
        try:
            return await asyncio.to_thread(self._get_transcription_sync, transcription_id, user_id)
        except Exception as exc:
            raise DatabaseServiceError("⚠️ Не удалось загрузить исходный материал для переработки.") from exc

    def _get_transcription_sync(self, transcription_id: str, user_id: int) -> TranscriptionRecord | None:
        response = (
            self.client.table("transcriptions")
            .select("id, source_type, transcript, summary, created_at")
            .eq("id", transcription_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        item = response.data[0]
        return TranscriptionRecord(
            id=item["id"],
            source_type=item["source_type"],
            transcript=item.get("transcript", ""),
            summary=item.get("summary", ""),
            created_at=item.get("created_at", ""),
        )

    async def update_summary(self, transcription_id: str, summary: str, user_id: int) -> None:
        try:
            await asyncio.to_thread(self._update_summary_sync, transcription_id, summary, user_id)
        except Exception as exc:
            raise DatabaseServiceError("⚠️ Не удалось обновить сохраненный конспект.") from exc

    async def get_model_usage(self, user_id: int, provider: str) -> int:
        return await asyncio.to_thread(self._get_model_usage_sync, user_id, provider)

    async def increment_model_usage(self, user_id: int, provider: str) -> int:
        return await asyncio.to_thread(self._increment_model_usage_sync, user_id, provider)

    def _fallback_model_key(self, user_id: int, provider: str) -> tuple[int, str, str]:
        return user_id, date.today().isoformat(), provider

    def _get_model_usage_sync(self, user_id: int, provider: str) -> int:
        today = date.today().isoformat()
        try:
            response = (
                self.client.table("daily_model_usage")
                .select("count")
                .eq("user_id", user_id)
                .eq("date", today)
                .eq("provider", provider)
                .limit(1)
                .execute()
            )
            if response.data:
                return int(response.data[0].get("count", 0))
        except Exception:
            logger.debug("daily_model_usage table unavailable, using fallback store", exc_info=True)
        return self._model_usage_fallback.get(self._fallback_model_key(user_id, provider), 0)

    def _increment_model_usage_sync(self, user_id: int, provider: str) -> int:
        today = date.today().isoformat()
        current = self._get_model_usage_sync(user_id, provider)
        new_count = current + 1
        payload = {
            "user_id": user_id,
            "date": today,
            "provider": provider,
            "count": new_count,
        }
        try:
            self.client.table("daily_model_usage").upsert(payload).execute()
        except Exception:
            logger.debug("Failed to persist daily_model_usage, using fallback store", exc_info=True)
            self._model_usage_fallback[self._fallback_model_key(user_id, provider)] = new_count
        return new_count

    def _update_summary_sync(self, transcription_id: str, summary: str, user_id: int) -> None:
        (
            self.client.table("transcriptions")
            .update({"summary": summary})
            .eq("id", transcription_id)
            .eq("user_id", user_id)
            .execute()
        )

    def _save_transcription_sync(
        self,
        user_id: int,
        source_type: str,
        transcript: str,
        summary: str,
        plan: str,
    ) -> TranscriptionRecord | None:
        if not should_save_history(plan):
            return None

        payload = {
            "user_id": user_id,
            "source_type": source_type,
            "transcript": transcript,
            "summary": summary,
            "created_at": datetime.now(UTC).isoformat(),
        }
        record = self.client.table("transcriptions").insert(payload).execute().data[0]

        retention_days = PLAN_HISTORY_RETENTION_DAYS.get(plan)
        if retention_days:
            threshold = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
            (
                self.client.table("transcriptions")
                .delete()
                .eq("user_id", user_id)
                .lt("created_at", threshold)
                .execute()
            )

        return TranscriptionRecord(
            id=record["id"],
            source_type=record["source_type"],
            transcript=record.get("transcript", ""),
            summary=record.get("summary", ""),
            created_at=record.get("created_at", ""),
        )
