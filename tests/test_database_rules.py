from __future__ import annotations

import unittest

from bot.services.database import (
    FREE_ALLOWED_SOURCE_TYPES,
    MODEL_DAILY_LIMITS,
    PLAN_LIMITS,
    PLAN_PRICES,
    UsageStatus,
    is_admin_username,
    should_save_history,
)


class DatabaseRulesTests(unittest.TestCase):
    def test_plan_limits_match_expected(self) -> None:
        self.assertEqual(PLAN_LIMITS["free"], 5)
        self.assertIsNone(PLAN_LIMITS["pro"])
        self.assertIsNone(PLAN_LIMITS["premium"])
        self.assertEqual(PLAN_PRICES["pro"], 149)
        self.assertEqual(PLAN_PRICES["premium"], 449)
        self.assertEqual(MODEL_DAILY_LIMITS["gpt4o"], 15)
        self.assertEqual(MODEL_DAILY_LIMITS["claude"], 15)
        self.assertSetEqual(FREE_ALLOWED_SOURCE_TYPES, {"voice", "video_note", "audio"})

    def test_admin_username_bypass(self) -> None:
        self.assertTrue(is_admin_username("@w9v33"))
        self.assertTrue(is_admin_username("w9v33"))
        self.assertFalse(is_admin_username("@someone_else"))

    def test_usage_remaining(self) -> None:
        status = UsageStatus(plan="free", used=3, limit=5)
        self.assertEqual(status.remaining, 2)
        self.assertFalse(status.is_exceeded)

    def test_history_policy(self) -> None:
        self.assertFalse(should_save_history("free"))
        self.assertTrue(should_save_history("pro"))
        self.assertTrue(should_save_history("premium"))


if __name__ == "__main__":
    unittest.main()
