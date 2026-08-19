import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from article_extractor_gui import (
    ExtractionError,
    OffGuardianAdapter,
    TOKEN_LIMIT,
    TokenCounter,
    WEBSITE_EXTRACTORS_BY_KEY,
    export_article,
)


EXAMPLE_URL = "https://off-guardian.org/2026/07/25/i-no-longer-trust-anyone/"
POST_ENDPOINT = (
    "https://off-guardian.org/wp-json/wp/v2/posts/102202"
    "?_embed=wp%3Afeaturedmedia"
)
COVER_SOURCE = "https://off-guardian.org/media/title-image.jpg?cache=1"


def article_page() -> str:
    return f"""
    <html><head>
      <link rel="alternate" type="application/json"
            href="https://off-guardian.org/wp-json/wp/v2/posts/102202">
      <link rel="canonical" href="{EXAMPLE_URL}">
    </head><body><article>
      <h6 class="author-cf"><span>Todd Hayen</span></h6>
      <div class="title_image_wrap"><div class="wp-caption">
        <img src="{COVER_SOURCE}">
      </div></div>
    </article></body></html>
    """


def post_payload() -> dict:
    return {
        "id": 102202,
        "slug": "i-no-longer-trust-anyone",
        "date": "2026-07-25T14:00:25",
        "link": EXAMPLE_URL,
        "title": {"rendered": "I No Longer Trust Anyone"},
        "content": {
            "rendered": """
              <p>Opening paragraph.</p>
              <h2>Main section</h2>
              <blockquote><p>A quotation.</p></blockquote>
              <ol><li>First item.</li><li>Second item.</li></ol>
              <figure><img data-lazy-src="/media/diagram.jpg">
                <figcaption>Diagram caption.</figcaption></figure>
              <table><tr><td>Non-linear table.</td></tr></table>
              <iframe src="https://player.example/video"></iframe>
              <p>Closing paragraph.</p>
            """
        },
        "_embedded": {
            "wp:featuredmedia": [
                {"source_url": "https://off-guardian.org/media/fallback-cover.jpg"}
            ]
        },
    }


def comment_payloads() -> tuple[list[dict], list[dict]]:
    return (
        [
            {
                "id": 500,
                "parent": 0,
                "author_name": "Parent Reader",
                "date": "2026-07-25T15:00:00",
                "content": {"rendered": "<p>Parent first.</p><p>Parent second.</p>"},
            },
            {
                "id": 501,
                "parent": 500,
                "author_name": "Reply Reader",
                "date": "2026-07-25T15:05:00",
                "content": {
                    "rendered": (
                        '<p><img src="/media/comment.jpg">Text after the image.</p>'
                    )
                },
            },
        ],
        [
            {
                "id": 502,
                "parent": 501,
                "author_name": "Nested Reader",
                "date_gmt": "2026-07-25T15:10:00",
                "content": {"rendered": "<blockquote><p>Nested reply.</p></blockquote>"},
            }
        ],
    )


def extract_fixture():
    first_comments, second_comments = comment_payloads()
    with patch.object(OffGuardianAdapter, "COMMENTS_PAGE_SIZE", 2):
        first_comments_url = OffGuardianAdapter._comments_api_url(102202, 0)
        second_comments_url = OffGuardianAdapter._comments_api_url(102202, 2)
        responses = {
            EXAMPLE_URL: article_page(),
            POST_ENDPOINT: json.dumps(post_payload()),
            first_comments_url: json.dumps(first_comments),
            second_comments_url: json.dumps(second_comments),
        }

        def fake_fetch(url: str) -> str:
            return responses[url]

        with patch("article_extractor_gui.fetch_text", side_effect=fake_fetch) as fetch:
            article = OffGuardianAdapter().extract(
                EXAMPLE_URL + "?utm_source=test", lambda _message: None
            )
    return article, fetch, first_comments_url, second_comments_url


class OffGuardianSelectionTests(unittest.TestCase):
    def test_profile_is_explicit_comments_enabled_and_host_scoped(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["offguardian"]
        self.assertEqual(website.display_name, "OffGuardian")
        self.assertEqual(
            website.homepage, "https://off-guardian.org/category/todd-hayen/"
        )
        self.assertTrue(website.extracts_comments)
        self.assertIsInstance(website.make_adapter(), OffGuardianAdapter)
        self.assertEqual(website.validate_url(EXAMPLE_URL), EXAMPLE_URL)
        with self.assertRaises(ExtractionError):
            website.validate_url("https://example.com/2026/07/25/article/")


class OffGuardianAdapterTests(unittest.TestCase):
    def test_public_endpoints_preserve_article_media_and_nested_comment_parents(self):
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
        self.assertEqual(article.title, "I No Longer Trust Anyone")
        self.assertEqual(article.author, "Todd Hayen")
        self.assertEqual(article.published, "2026-07-25T14:00:25")
        self.assertEqual(article.canonical_url, EXAMPLE_URL)
        self.assertEqual(
            article.blocks,
            [
                "IMAGE-01",
                "Opening paragraph.",
                "## Main section",
                "> A quotation.",
                "1. First item.",
                "2. Second item.",
                "IMAGE-02",
                "Diagram caption.",
                "IMAGE-03",
                "IMAGE-04",
                "Closing paragraph.",
            ],
        )
        self.assertEqual(
            [(item.placeholder, item.kind, item.source) for item in article.media],
            [
                ("IMAGE-01", "cover image", COVER_SOURCE),
                ("IMAGE-02", "image", "https://off-guardian.org/media/diagram.jpg"),
                ("IMAGE-03", "table", ""),
                ("IMAGE-04", "iframe", "https://player.example/video"),
            ],
        )
        self.assertTrue(all(item.render_source_as_link for item in article.media))
        self.assertEqual(
            [comment.identifier for comment in article.comments], ["500", "501", "502"]
        )
        self.assertEqual(
            [comment.parent_identifier for comment in article.comments], ["", "500", "501"]
        )
        self.assertEqual(article.comments[0].blocks, ["Parent first.", "Parent second."])
        self.assertEqual(
            article.comments[1].blocks, ["IMAGE-01", "Text after the image."]
        )
        self.assertEqual(article.comments[2].blocks, ["> Nested reply."])
        self.assertEqual(
            article.comments[1].media[0].source,
            "https://off-guardian.org/media/comment.jpg",
        )

    def test_non_article_path_is_rejected_before_any_request(self):
        with patch("article_extractor_gui.fetch_text") as fetch:
            with self.assertRaisesRegex(ExtractionError, "OffGuardian article URL"):
                OffGuardianAdapter().extract(
                    "https://off-guardian.org/category/todd-hayen/",
                    lambda _message: None,
                )
        fetch.assert_not_called()


class OffGuardianExportTests(unittest.TestCase):
    def test_export_uses_exact_link_layout_combines_comments_and_never_overwrites(self):
        article, _fetch, _first_comments_url, _second_comments_url = extract_fixture()
        counter = TokenCounter()
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            first, _first_written = export_article(article, parent, counter=counter)
            second, _second_written = export_article(article, parent, counter=counter)

            self.assertEqual(first.name, "i-no-longer-trust-anyone")
            self.assertEqual(second.name, "i-no-longer-trust-anyone-2")
            article_text = (first / "i-no-longer-trust-anyone.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                f"IMAGE-01\n\nMedia source: [{COVER_SOURCE}]({COVER_SOURCE})",
                article_text,
            )
            self.assertNotIn("Media type:", article_text)
            self.assertNotIn("Media location:", article_text)
            self.assertNotIn("Downloaded file:", article_text)
            self.assertFalse((first / "media").exists())

            comments_path = first / "i-no-longer-trust-anyone_comments.txt"
            comments_text = comments_path.read_text(encoding="utf-8")
            self.assertIn("COMMENT 01", comments_text)
            self.assertIn("Reply to comment ID: 500", comments_text)
            self.assertIn("Reply to comment ID: 501", comments_text)
            self.assertIn(
                "IMAGE-01\n\nMedia source: "
                "[https://off-guardian.org/media/comment.jpg]"
                "(https://off-guardian.org/media/comment.jpg)",
                comments_text,
            )
            for path in first.glob("*.txt"):
                self.assertLess(counter.count(path.read_text(encoding="utf-8")), TOKEN_LIMIT)


if __name__ == "__main__":
    unittest.main()
