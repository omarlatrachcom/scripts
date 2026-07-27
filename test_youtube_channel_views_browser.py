import unittest
from pathlib import Path
from unittest.mock import patch

import youtube_channel_views_browser as browser


def test_config() -> browser.Config:
    return browser.Config(
        min_views=50_000,
        recent_min_views=50_000,
        channels=[browser.ChannelConfig("https://www.youtube.com/@example/videos")],
        youtube_api_key="test-key",
        cookies_from_browser=None,
        config_dir=Path("."),
    )


class YouTubeApiTests(unittest.TestCase):
    def test_channel_lookup_supports_handles_and_ids(self):
        self.assertEqual(
            browser.channel_lookup_parameter("https://www.youtube.com/@example/videos"),
            ("forHandle", "@example"),
        )
        self.assertEqual(
            browser.channel_lookup_parameter("https://www.youtube.com/channel/UC123/videos"),
            ("id", "UC123"),
        )

    def test_fetch_filters_complete_upload_page_by_minimum(self):
        responses = [
            {
                "items": [{
                    "id": "UC123",
                    "snippet": {"title": "Example"},
                    "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}},
                }]
            },
            {
                "items": [
                    {"contentDetails": {"videoId": "aaaaaaaaaaa"}},
                    {"contentDetails": {"videoId": "bbbbbbbbbbb"}},
                    {"contentDetails": {"videoId": "ccccccccccc"}},
                ]
            },
            {
                "items": [
                    {
                        "id": "aaaaaaaaaaa",
                        "snippet": {"title": "First", "channelTitle": "Example", "publishedAt": "2020-01-02T00:00:00Z"},
                        "statistics": {"viewCount": "300000"},
                        "contentDetails": {"duration": "PT1H2M3S"},
                    },
                    {
                        "id": "bbbbbbbbbbb",
                        "snippet": {"title": "Second", "channelTitle": "Example", "publishedAt": "2020-01-01T00:00:00Z"},
                        "statistics": {"viewCount": "100000"},
                        "contentDetails": {"duration": "PT5M"},
                    },
                    {
                        "id": "ccccccccccc",
                        "snippet": {"title": "Below", "channelTitle": "Example"},
                        "statistics": {"viewCount": "49999"},
                        "contentDetails": {"duration": "PT1M"},
                    },
                ]
            },
        ]
        messages = []
        with patch.object(browser, "youtube_api_get", side_effect=responses):
            videos, stats = browser.fetch_channel_videos(
                "https://www.youtube.com/@example/videos",
                test_config(),
                messages.append,
            )

        self.assertEqual([video.video_id for video in videos], ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertEqual(videos[0].duration, "1:02:03")
        self.assertEqual(stats.scanned, 3)
        self.assertEqual(stats.included, 2)


if __name__ == "__main__":
    unittest.main()
