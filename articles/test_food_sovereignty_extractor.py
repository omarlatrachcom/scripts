import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from article_extractor_gui import (
    ExtractionError,
    FoodSovereigntyAdapter,
    TOKEN_LIMIT,
    TokenCounter,
    WEBSITE_EXTRACTORS_BY_KEY,
    export_article,
)


EXAMPLE_URL = (
    "https://off-guardian.org/2026/07/24/"
    "manufacturing-inevitability-breaking-the-myth-of-no-alternative/"
)
POST_ENDPOINT = (
    "https://off-guardian.org/wp-json/wp/v2/posts/102189"
    "?_embed=wp%3Afeaturedmedia"
)
COVER_SOURCE = (
    "https://off-guardian.org/wp-content/medialibrary/"
    "adobestock-tina-there-is-no-alternative.jpeg?x95823"
)


def article_page() -> str:
    return f"""
    <html><head>
      <link rel="alternate" type="application/json"
            href="https://off-guardian.org/wp-json/wp/v2/posts/102189">
      <link rel="canonical" href="{EXAMPLE_URL}">
    </head><body><article>
      <h6 class="author-cf"><span>Colin Todhunter</span></h6>
      <div class="title_image_wrap"><div class="wp-caption">
        <img src="{COVER_SOURCE}">
      </div></div>
    </article></body></html>
    """


def post_payload() -> dict:
    return {
        "id": 102189,
        "slug": "manufacturing-inevitability-breaking-the-myth-of-no-alternative",
        "date": "2026-07-24T16:00:03",
        "link": EXAMPLE_URL,
        "title": {
            "rendered": "Manufacturing Inevitability: Breaking the Myth of No Alternative"
        },
        "content": {
            "rendered": """
              <h5>The following is an extract from the new open access book.</h5>
              <p>Opening paragraph.</p>
              <h2>Agrarian alternatives</h2>
              <blockquote><p>A quotation.</p></blockquote>
              <ul><li>Food sovereignty.</li><li>Local knowledge.</li></ul>
              <img src="/wp-content/medialibrary/book-cover.png">
              <div class="grid author-wrap twenty-eighty">
                <div><img src="/wp-content/medialibrary/everyman.jpg"></div>
                <div><p><strong>Colin Todhunter</strong> specialises in food, agriculture and development.</p></div>
              </div>
              <table><tr><td>Non-linear data.</td></tr></table>
              <iframe src="https://player.example/interview"></iframe>
              <p>Closing paragraph.</p>
            """
        },
        "_embedded": {
            "wp:featuredmedia": [
                {"source_url": "https://off-guardian.org/fallback-cover.jpg"}
            ]
        },
    }


def comment_pages() -> tuple[list[dict], list[dict]]:
    return (
        [
            {
                "id": 774752,
                "parent": 0,
                "author_name": "Parent Reader",
                "date": "2026-07-24T17:42:42",
                "content": {"rendered": "<p>Parent first.</p><p>Parent second.</p>"},
            },
            {
                "id": 774754,
                "parent": 774752,
                "author_name": "Reply Reader",
                "date": "2026-07-24T18:11:16",
                "content": {
                    "rendered": (
                        '<p><img src="/wp-content/medialibrary/comment.jpg">'
                        "Reply after the image.</p>"
                    )
                },
            },
        ],
        [
            {
                "id": 774765,
                "parent": 774754,
                "author_name": "Nested Reader",
                "date_gmt": "2026-07-24T21:48:47",
                "content": {"rendered": "<blockquote><p>Nested reply.</p></blockquote>"},
            }
        ],
    )


def extract_fixture():
    first_comments, second_comments = comment_pages()
    with patch.object(FoodSovereigntyAdapter, "COMMENTS_PAGE_SIZE", 2):
        first_comments_url = FoodSovereigntyAdapter._comments_api_url(102189, 0)
        second_comments_url = FoodSovereigntyAdapter._comments_api_url(102189, 2)
        responses = {
            EXAMPLE_URL: article_page(),
            POST_ENDPOINT: json.dumps(post_payload()),
            first_comments_url: json.dumps(first_comments),
            second_comments_url: json.dumps(second_comments),
        }

        def fake_fetch(url: str) -> str:
            return responses[url]

        with patch("article_extractor_gui.fetch_text", side_effect=fake_fetch) as fetch:
            article = FoodSovereigntyAdapter().extract(
                EXAMPLE_URL + "?utm_source=test", lambda _message: None
            )
    return article, fetch, first_comments_url, second_comments_url


class FoodSovereigntySelectionTests(unittest.TestCase):
    def test_profile_is_a_new_explicit_comments_enabled_choice(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["food-sovereignty"]
        self.assertEqual(
            website.display_name,
            "Food Sovereignty | Agrarian Systems | Development",
        )
        self.assertEqual(
            website.homepage,
            "https://off-guardian.org/category/colin-todhunter/",
        )
        self.assertEqual(website.example_url, EXAMPLE_URL)
        self.assertTrue(website.extracts_comments)
        self.assertIsInstance(website.make_adapter(), FoodSovereigntyAdapter)
        self.assertEqual(website.validate_url(EXAMPLE_URL), EXAMPLE_URL)
        with self.assertRaises(ExtractionError):
            website.validate_url("https://example.com/2026/07/24/article/")


class FoodSovereigntyAdapterTests(unittest.TestCase):
    def test_real_public_endpoint_shapes_preserve_media_and_reply_parents(self):
        article, fetch, first_comments_url, second_comments_url = extract_fixture()

        self.assertEqual(
            fetch.call_args_list,
            [
                call(EXAMPLE_URL),
                call(POST_ENDPOINT),
                call(first_comments_url),
                call(second_comments_url),
            ],
        )
        self.assertIn("status=approve", first_comments_url)
        self.assertIn("orderby=date_gmt", first_comments_url)
        self.assertEqual(
            article.title,
            "Manufacturing Inevitability: Breaking the Myth of No Alternative",
        )
        self.assertEqual(article.author, "Colin Todhunter")
        self.assertEqual(article.published, "2026-07-24T16:00:03")
        self.assertEqual(article.canonical_url, EXAMPLE_URL)
        self.assertEqual(
            article.blocks,
            [
                "IMAGE-01",
                "##### The following is an extract from the new open access book.",
                "Opening paragraph.",
                "## Agrarian alternatives",
                "> A quotation.",
                "- Food sovereignty.",
                "- Local knowledge.",
                "IMAGE-02",
                "IMAGE-03",
                "Colin Todhunter specialises in food, agriculture and development.",
                "IMAGE-04",
                "IMAGE-05",
                "Closing paragraph.",
            ],
        )
        self.assertEqual(
            [(item.placeholder, item.kind, item.source) for item in article.media],
            [
                ("IMAGE-01", "cover image", COVER_SOURCE),
                (
                    "IMAGE-02",
                    "image",
                    "https://off-guardian.org/wp-content/medialibrary/book-cover.png",
                ),
                (
                    "IMAGE-03",
                    "image",
                    "https://off-guardian.org/wp-content/medialibrary/everyman.jpg",
                ),
                ("IMAGE-04", "table", ""),
                ("IMAGE-05", "iframe", "https://player.example/interview"),
            ],
        )
        self.assertTrue(all(item.render_source_as_link for item in article.media))
        self.assertEqual(
            [comment.identifier for comment in article.comments],
            ["774752", "774754", "774765"],
        )
        self.assertEqual(
            [comment.parent_identifier for comment in article.comments],
            ["", "774752", "774754"],
        )
        self.assertEqual(article.comments[0].blocks, ["Parent first.", "Parent second."])
        self.assertEqual(
            article.comments[1].blocks,
            ["IMAGE-01", "Reply after the image."],
        )
        self.assertEqual(article.comments[2].blocks, ["> Nested reply."])

    def test_non_article_path_is_rejected_before_any_request(self):
        with patch("article_extractor_gui.fetch_text") as fetch:
            with self.assertRaisesRegex(ExtractionError, "OffGuardian article URL"):
                FoodSovereigntyAdapter().extract(
                    "https://off-guardian.org/category/colin-todhunter/",
                    lambda _message: None,
                )
        fetch.assert_not_called()


class FoodSovereigntyExportTests(unittest.TestCase):
    def test_exact_media_layout_single_comments_file_and_unique_directories(self):
        article, _fetch, _first_comments_url, _second_comments_url = extract_fixture()
        counter = TokenCounter()
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            first, _first_written = export_article(article, parent, counter=counter)
            second, _second_written = export_article(article, parent, counter=counter)

            self.assertEqual(
                first.name,
                "manufacturing-inevitability-breaking-the-myth-of-no-alternative",
            )
            self.assertEqual(second.name, first.name + "-2")
            article_path = first / f"{first.name}.txt"
            article_text = article_path.read_text(encoding="utf-8")
            self.assertIn(
                f"IMAGE-01\n\nMedia source: [{COVER_SOURCE}]({COVER_SOURCE})",
                article_text,
            )
            self.assertNotIn("Media type:", article_text)
            self.assertNotIn("Media location:", article_text)
            self.assertNotIn("Downloaded file:", article_text)
            self.assertFalse((first / "media").exists())

            comments_files = list(first.glob("*_comments.txt"))
            self.assertEqual(len(comments_files), 1)
            comments_text = comments_files[0].read_text(encoding="utf-8")
            self.assertIn("Reply to comment ID: 774752", comments_text)
            self.assertIn("Reply to comment ID: 774754", comments_text)
            self.assertIn(
                "IMAGE-01\n\nMedia source: "
                "[https://off-guardian.org/wp-content/medialibrary/comment.jpg]"
                "(https://off-guardian.org/wp-content/medialibrary/comment.jpg)",
                comments_text,
            )
            for path in first.glob("*.txt"):
                self.assertLess(
                    counter.count(path.read_text(encoding="utf-8")),
                    TOKEN_LIMIT,
                )


if __name__ == "__main__":
    unittest.main()
