from __future__ import annotations

import unittest

from bot.services.youtube import YouTubeProcessingError, extract_video_id


class YouTubeServiceTests(unittest.TestCase):
    def test_extract_video_id_from_watch_url(self) -> None:
        self.assertEqual(extract_video_id("https://www.youtube.com/watch?v=wU8diwt99-s"), "wU8diwt99-s")

    def test_extract_video_id_from_short_url(self) -> None:
        self.assertEqual(extract_video_id("https://youtu.be/wU8diwt99-s"), "wU8diwt99-s")

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(Exception):
            extract_video_id("https://example.com/not-youtube")


if __name__ == "__main__":
    unittest.main()
