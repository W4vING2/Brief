from __future__ import annotations

import unittest

from bot.handlers.content import _build_markdown, _split_summary
from bot.services.database import UsageStatus
from bot.services.transcribe import ProcessedContent


class ContentFormattingTests(unittest.TestCase):
    def test_split_summary_extracts_sections(self) -> None:
        summary = (
            "О чем материал\n"
            "Это обзор новой функции.\n"
            "Ключевые тезисы\n"
            "- Первый тезис\n"
            "- Второй тезис\n"
            "Вывод\n"
            "Функция экономит время."
        )

        intro, bullets, conclusion = _split_summary(summary)

        self.assertIn("Это обзор новой функции.", intro)
        self.assertEqual(bullets, ["Первый тезис", "Второй тезис"])
        self.assertEqual(conclusion, ["Функция экономит время."])

    def test_build_markdown_contains_sections(self) -> None:
        summary = (
            "О чем материал\n"
            "Материал о тестировании.\n"
            "Ключевые тезисы\n"
            "- Тесты сокращают регрессии\n"
            "Вывод\n"
            "Тесты окупаются."
        )
        processed = ProcessedContent(source_type="pdf", text="source", meta="10 стр")
        status = UsageStatus(plan="free", used=2, limit=5)

        markdown = _build_markdown(summary, processed, status)

        self.assertIn("# ✨ BriefBot Summary", markdown)
        self.assertIn("## 📌 Ключевые тезисы", markdown)
        self.assertIn("**Источник:** `pdf`", markdown)


if __name__ == "__main__":
    unittest.main()
