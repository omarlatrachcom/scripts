import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from article_extractor_gui import (
    ExtractedArticle,
    ExtractionError,
    MediaReference,
    TomatoBibleAdapter,
    WEBSITE_EXTRACTORS_BY_KEY,
    export_article,
)


class TomatoBibleSelectionTests(unittest.TestCase):
    def test_profile_is_explicit_article_only_and_rejects_other_substacks(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["tomatobible"]
        self.assertEqual(website.display_name, "TomatoBible(トマトバイブル)")
        self.assertFalse(website.extracts_comments)
        self.assertIsInstance(website.make_adapter(), TomatoBibleAdapter)
        self.assertEqual(
            website.validate_url("https://tomatobible.substack.com/p/example"),
            "https://tomatobible.substack.com/p/example",
        )
        with self.assertRaises(ExtractionError):
            website.validate_url("https://another-publication.substack.com/p/example")


class TomatoBibleAdapterTests(unittest.TestCase):
    def test_public_post_endpoint_preserves_linear_and_nonlinear_order(self):
        body_html = """
        <div class="captioned-image-container"><figure>
          <img src="https://cdn.example/cover.jpg" alt="Cover">
          <figcaption>Cover caption.</figcaption>
        </figure></div>
        <p>Opening paragraph.</p>
        <h2>Main section</h2>
        <blockquote><p>A quotation.</p></blockquote>
        <ul><li>First item.</li><li>Second item.</li></ul>
        <div class="highlighted_code_block"><pre><code>first line\nsecond line</code></pre></div>
        <table><tr><td>Non-linear table.</td></tr></table>
        <div class="youtube-wrap"><div class="youtube-inner">
          <iframe src="https://www.youtube-nocookie.com/embed/example?rel=0"></iframe>
        </div></div>
        <p class="button-wrapper"
           data-attrs="{&quot;url&quot;:&quot;https://tomatobible.substack.com/subscribe?&quot;}"
           data-component-name="ButtonCreateButton">
          <a href="https://tomatobible.substack.com/subscribe?">Subscribe now</a>
        </p>
        <p>Closing paragraph.</p>
        """
        payload = {
            "id": 123,
            "slug": "example",
            "title": "Example TomatoBible Article",
            "subtitle": "Example subtitle",
            "post_date": "2026-07-12T06:15:33.091Z",
            "canonical_url": "https://tomatobible.substack.com/p/example",
            "cover_image": "https://cdn.example/cover.jpg",
            "body_html": body_html,
            "publishedBylines": [{"name": "TomatoBible(トマトバイブル)"}],
            "comment_count": 99,
        }

        with patch(
            "article_extractor_gui.fetch_text", return_value=json.dumps(payload)
        ) as fetch:
            article = TomatoBibleAdapter().extract(
                "https://tomatobible.substack.com/p/example?utm_source=test",
                lambda _message: None,
            )

        fetch.assert_called_once_with(
            "https://tomatobible.substack.com/api/v1/posts/example"
        )
        self.assertEqual(article.comments, [])
        self.assertEqual(article.author, "TomatoBible(トマトバイブル)")
        self.assertEqual(
            article.blocks,
            [
                "IMAGE-01",
                "Cover caption.",
                "Opening paragraph.",
                "## Main section",
                "> A quotation.",
                "- First item.",
                "- Second item.",
                "first line\nsecond line",
                "IMAGE-02",
                "IMAGE-03",
                "IMAGE-04",
                "Closing paragraph.",
            ],
        )
        self.assertEqual(
            [(item.placeholder, item.kind, item.source) for item in article.media],
            [
                ("IMAGE-01", "image", "https://cdn.example/cover.jpg"),
                ("IMAGE-02", "table", ""),
                (
                    "IMAGE-03",
                    "iframe",
                    "https://www.youtube-nocookie.com/embed/example?rel=0",
                ),
                (
                    "IMAGE-04",
                    "interactive content",
                    "https://tomatobible.substack.com/subscribe?",
                ),
            ],
        )

    def test_non_article_path_is_rejected_before_any_request(self):
        with patch("article_extractor_gui.fetch_text") as fetch:
            with self.assertRaisesRegex(ExtractionError, "TomatoBible article URL"):
                TomatoBibleAdapter().extract(
                    "https://tomatobible.substack.com/archive", lambda _message: None
                )
        fetch.assert_not_called()

    def test_article_only_export_uses_exact_media_layout_and_no_comments_file(self):
        article = ExtractedArticle(
            title="Example",
            subtitle="",
            author="TomatoBible(トマトバイブル)",
            published="2026-07-12T06:15:33.091Z",
            canonical_url="https://tomatobible.substack.com/p/example",
            slug="example",
            blocks=["Before.", "IMAGE-01", "After."],
            media=[
                MediaReference(
                    "IMAGE-01", "image", "https://cdn.example/image.jpg"
                )
            ],
            adapter_name="TomatoBible(トマトバイブル)",
        )
        with tempfile.TemporaryDirectory() as directory:
            output, _written = export_article(article, Path(directory))
            article_text = (output / "example.txt").read_text(encoding="utf-8")
            self.assertIn(
                "Before.\n\nIMAGE-01\n\n"
                "Media source: https://cdn.example/image.jpg\n\nAfter.",
                article_text,
            )
            self.assertEqual(list(output.glob("*comments*.txt")), [])


if __name__ == "__main__":
    unittest.main()
