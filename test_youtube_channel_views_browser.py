import unittest
from pathlib import Path
from unittest.mock import patch

import youtube_channel_views_browser as browser


class FakeYoutubeDL:
    entries = [
        {
            "id": "aaaaaaaaaaa",
            "title": "First",
            "view_count": 300_000,
            "webpage_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        },
        {
            "id": "bbbbbbbbbbb",
            "title": "Second",
            "view_count": 100_000,
            "webpage_url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
        },
        {
            "id": "ccccccccccc",
            "title": "Below threshold",
            "view_count": 49_999,
            "webpage_url": "https://www.youtube.com/watch?v=ccccccccccc",
        },
        {
            "id": "ddddddddddd",
            "title": "Must not be scanned",
            "view_count": 10_000,
            "webpage_url": "https://www.youtube.com/watch?v=ddddddddddd",
        },
    ]

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def extract_info(self, url, download=False):
        start = self.options["playliststart"] - 1
        end = self.options["playlistend"]
        return {
            "title": "Example channel",
            "entries": self.entries[start:end],
        }


def test_config() -> browser.Config:
    return browser.Config(
        min_views=50_000,
        recent_min_views=50_000,
        channels=[browser.ChannelConfig("https://www.youtube.com/@example/videos")],
        cookies_from_browser=None,
        config_dir=Path("."),
    )


class PopularChannelTests(unittest.TestCase):
    def test_popular_url_replaces_existing_tab_and_query(self):
        actual = browser.popular_channel_videos_url(
            "https://www.youtube.com/@example/shorts?feature=shared"
        )
        self.assertEqual(
            actual,
            "https://www.youtube.com/@example/videos?view=0&sort=p&flow=grid",
        )

    def test_fetch_stops_at_first_video_below_minimum(self):
        messages = []
        with patch.object(browser, "POPULAR_FETCH_BATCH_SIZE", 4):
            videos, stats = browser.fetch_channel_videos(
                FakeYoutubeDL,
                "https://www.youtube.com/@example/videos",
                test_config(),
                messages.append,
            )

        self.assertEqual([video.video_id for video in videos], ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertEqual(stats.scanned, 3)
        self.assertEqual(stats.included, 2)
        self.assertTrue(any("Stopping at popular item #3" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
