import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from article_extractor_gui import (
    Comment,
    ExtractedArticle,
    ExtractionError,
    MediaReference,
    ReeseReportAdapter,
    ReeseReportRichTextConverter,
    TOKEN_LIMIT,
    WEBSITE_EXTRACTORS_BY_KEY,
    export_article,
)


EXAMPLE_URL = (
    "https://gregreese.substack.com/p/cymatics-and-the-mysteries-of-the"
)
POST_ENDPOINT = (
    "https://gregreese.substack.com/api/v1/posts/"
    "cymatics-and-the-mysteries-of-the"
)
COMMENTS_ENDPOINT = (
    "https://gregreese.substack.com/api/v1/post/207304793/comments"
    "?all_comments=true&sort=best_first"
)
VIDEO_UPLOAD_ID = "2eb5f425-81d6-4871-b65b-201d7009888b"
VIDEO_SOURCE = (
    "https://api.substack.com/api/v1/video/upload/"
    f"{VIDEO_UPLOAD_ID}/src"
)


class LengthCounter:
    mode = "test characters"

    @staticmethod
    def count(text: str) -> int:
        return len(text)


def example_post_payload() -> dict:
    return {
        "id": 207304793,
        "slug": "cymatics-and-the-mysteries-of-the",
        "title": "Cymatics and the Mysteries of the Cathedrals",
        "subtitle": "",
        "post_date": "2026-07-21T13:03:23.374Z",
        "canonical_url": EXAMPLE_URL,
        "type": "podcast",
        # Live video posts expose a poster and podcast audio too. Neither is a
        # second editorial item: the public video is the primary media.
        "cover_image": "https://cdn.example/video-poster.png",
        "video_upload_id": VIDEO_UPLOAD_ID,
        "podcast_url": "https://api.substack.com/api/v1/audio/upload/example/src",
        "default_comment_sort": None,
        "comment_count": 5,
        "publishedBylines": [{"name": "Greg Reese"}],
        "body_html": """
          <p>Opening paragraph.</p>
          <h2>Resonance</h2>
          <blockquote><p>A quotation.</p></blockquote>
          <ul><li>First item.</li><li>Second item.</li></ul>
          <figure>
            <img src="https://cdn.example/diagram.jpg" alt="Frequency diagram">
            <figcaption>Frequency diagram caption.</figcaption>
          </figure>
          <div class="image-gallery"
               data-attrs="{&quot;url&quot;:&quot;https://cdn.example/gallery&quot;}">
            <img src="https://cdn.example/gallery-item.jpg">
          </div>
          <table><tr><td>Non-linear measurements.</td></tr></table>
          <iframe src="https://player.example/cymatics"></iframe>
          <div data-component-name="EmbeddedPublicationToDOMWithSubscribe"
               data-attrs="{&quot;base_url&quot;:&quot;https://guest.substack.com&quot;}">
            <a href="https://guest.substack.com">Guest publication</a>
          </div>
          <p class="button-wrapper" data-component-name="ButtonCreateButton"
             data-attrs="{&quot;url&quot;:&quot;https://gregreese.substack.com/subscribe&quot;}">
            <a href="https://gregreese.substack.com/subscribe">Subscribe</a>
          </p>
          <p>Closing paragraph.</p>
        """,
    }


def example_comments_payload() -> dict:
    return {
        "comments": [
            {
                "id": 100,
                "name": "Parent Reader",
                "date": "2026-07-21T14:00:00.000Z",
                "body": "Parent fallback.",
                "body_json": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Parent first."}],
                        },
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Parent second."}],
                        },
                    ],
                },
                "ancestor_path": "",
                "deleted": False,
                "children": [
                    {
                        "id": 101,
                        "name": "Child Reader",
                        "date": "2026-07-21T14:05:00.000Z",
                        "body": "Child body.",
                        "body_json": None,
                        "ancestor_path": "100",
                        "deleted": False,
                        "children": [
                            {
                                "id": 102,
                                "name": "Grandchild Reader",
                                "date": "2026-07-21T14:10:00.000Z",
                                "body": "Grandchild body.",
                                "body_json": None,
                                "ancestor_path": "100.101",
                                "deleted": False,
                                "children": [],
                            }
                        ],
                    }
                ],
            },
            {
                "id": 200,
                "name": None,
                "date": "2026-07-21T15:00:00.000Z",
                "body": None,
                "body_json": None,
                "ancestor_path": "",
                "deleted": True,
                "children": [
                    {
                        "id": 201,
                        "name": "Visible Reply",
                        "date": "2026-07-21T15:05:00.000Z",
                        "body": "Reply to a deleted public parent.",
                        "body_json": None,
                        "ancestor_path": "200",
                        "deleted": False,
                        "children": [],
                    }
                ],
            },
        ]
    }


def extract_example():
    with patch(
        "article_extractor_gui.fetch_text",
        side_effect=[
            json.dumps(example_post_payload()),
            json.dumps(example_comments_payload()),
        ],
    ) as fetch:
        article = ReeseReportAdapter().extract(EXAMPLE_URL, lambda _message: None)
    return article, fetch


class ReeseReportSelectionTests(unittest.TestCase):
    def test_profile_is_explicit_comments_enabled_and_host_scoped(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["reese-report"]
        self.assertEqual(website.display_name, "The Reese Report")
        self.assertEqual(website.homepage, "https://gregreese.substack.com/archive")
        self.assertTrue(website.extracts_comments)
        self.assertIsInstance(website.make_adapter(), ReeseReportAdapter)
        self.assertEqual(website.validate_url(EXAMPLE_URL), EXAMPLE_URL)
        with self.assertRaises(ExtractionError):
            website.validate_url("https://another-publication.substack.com/p/example")


class ReeseReportAdapterTests(unittest.TestCase):
    def test_public_endpoints_preserve_structure_media_and_nested_threads(self):
        article, fetch = extract_example()

        self.assertEqual(
            fetch.call_args_list,
            [call(POST_ENDPOINT), call(COMMENTS_ENDPOINT)],
        )
        self.assertEqual(article.title, "Cymatics and the Mysteries of the Cathedrals")
        self.assertEqual(article.author, "Greg Reese")
        self.assertEqual(article.published, "2026-07-21T13:03:23.374Z")
        self.assertEqual(article.canonical_url, EXAMPLE_URL)
        self.assertEqual(
            article.blocks,
            [
                "IMAGE-01",
                "Opening paragraph.",
                "## Resonance",
                "> A quotation.",
                "- First item.",
                "- Second item.",
                "IMAGE-02",
                "Frequency diagram caption.",
                "IMAGE-03",
                "IMAGE-04",
                "IMAGE-05",
                "IMAGE-06",
                "IMAGE-07",
                "Closing paragraph.",
            ],
        )
        self.assertEqual(
            [(item.placeholder, item.kind, item.source) for item in article.media],
            [
                ("IMAGE-01", "video", VIDEO_SOURCE),
                ("IMAGE-02", "image", "https://cdn.example/diagram.jpg"),
                ("IMAGE-03", "interactive content", "https://cdn.example/gallery"),
                ("IMAGE-04", "table", ""),
                ("IMAGE-05", "iframe", "https://player.example/cymatics"),
                (
                    "IMAGE-06",
                    "interactive content",
                    "https://guest.substack.com",
                ),
                (
                    "IMAGE-07",
                    "interactive content",
                    "https://gregreese.substack.com/subscribe",
                ),
            ],
        )
        self.assertNotIn("video-poster.png", "\n".join(item.source for item in article.media))
        self.assertNotIn("audio/upload", "\n".join(item.source for item in article.media))

        self.assertEqual(
            [comment.identifier for comment in article.comments],
            ["100", "101", "102", "200", "201"],
        )
        self.assertEqual(
            [comment.parent_identifier for comment in article.comments],
            ["", "100", "101", "", "200"],
        )
        self.assertEqual(article.comments[0].blocks, ["Parent first.", "Parent second."])
        self.assertEqual(article.comments[2].blocks, ["Grandchild body."])
        self.assertEqual(article.comments[3].blocks, ["[Deleted comment]"])
        self.assertEqual(article.comments[4].blocks, ["Reply to a deleted public parent."])

    def test_non_article_path_is_rejected_before_any_request(self):
        with patch("article_extractor_gui.fetch_text") as fetch:
            with self.assertRaisesRegex(ExtractionError, "Reese Report article URL"):
                ReeseReportAdapter().extract(
                    "https://gregreese.substack.com/archive",
                    lambda _message: None,
                )
        fetch.assert_not_called()

    def test_comment_inline_media_stays_an_exact_source_bearing_block(self):
        document = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "image", "attrs": {"src": "https://cdn.example/comment.jpg"}},
                        {"type": "text", "text": "Text after the image."},
                    ],
                }
            ],
        }
        blocks, media = ReeseReportRichTextConverter().convert(document)
        self.assertEqual(blocks, ["IMAGE-01", "Text after the image."])
        self.assertEqual(media[0].source, "https://cdn.example/comment.jpg")


class ReeseReportExportTests(unittest.TestCase):
    def test_export_uses_exact_media_layout_combines_comments_and_never_overwrites(self):
        article, _fetch = extract_example()
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            first, _first_written = export_article(
                article, parent, counter=LengthCounter()
            )
            first_article_path = first / "cymatics-and-the-mysteries-of-the.txt"
            original_text = first_article_path.read_text(encoding="utf-8")

            second, _second_written = export_article(
                article, parent, counter=LengthCounter()
            )

            self.assertEqual(first.name, "cymatics-and-the-mysteries-of-the")
            self.assertEqual(second.name, "cymatics-and-the-mysteries-of-the-2")
            self.assertEqual(first_article_path.read_text(encoding="utf-8"), original_text)
            self.assertIn(
                f"IMAGE-01\n\nMedia source: {VIDEO_SOURCE}",
                original_text,
            )
            self.assertNotIn("Media type:", original_text)
            self.assertNotIn("Media location:", original_text)
            self.assertNotIn("Downloaded file:", original_text)
            self.assertFalse((first / "media").exists())

            comments_files = list(first.glob("*_comments.txt"))
            self.assertEqual(len(comments_files), 1)
            comments_text = comments_files[0].read_text(encoding="utf-8")
            self.assertIn("COMMENT 01", comments_text)
            self.assertIn("Reply to comment ID: 100", comments_text)
            self.assertIn("Reply to comment ID: 101", comments_text)
            self.assertIn("Reply to comment ID: 200", comments_text)
            for path in first.glob("*.txt"):
                self.assertLess(LengthCounter.count(path.read_text(encoding="utf-8")), TOKEN_LIMIT)

    def test_comment_chunks_split_only_between_complete_comments(self):
        first_body = ("First complete sentence. " * 380).strip()
        second_body = ("Second complete sentence. " * 360).strip()
        article = ExtractedArticle(
            title="Long comments",
            subtitle="",
            author="Greg Reese",
            published="2026-07-21T13:03:23.374Z",
            canonical_url=EXAMPLE_URL,
            slug="long-comments",
            blocks=["Short article."],
            comments=[
                Comment("1", "First Reader", "Today", [first_body]),
                Comment("2", "Second Reader", "Today", [second_body], "1"),
            ],
            media=[MediaReference("IMAGE-01", "video", VIDEO_SOURCE)],
            adapter_name="The Reese Report",
        )
        with tempfile.TemporaryDirectory() as directory:
            output, _written = export_article(
                article, Path(directory), counter=LengthCounter()
            )
            parts = sorted(output.glob("long-comments_comments_part_*.txt"))
            self.assertEqual(len(parts), 2)
            texts = [path.read_text(encoding="utf-8") for path in parts]
            self.assertTrue(all(LengthCounter.count(text) < TOKEN_LIMIT for text in texts))
            self.assertEqual(sum("COMMENT 01" in text for text in texts), 1)
            self.assertEqual(sum("COMMENT 02" in text for text in texts), 1)
            self.assertEqual(sum(first_body in text for text in texts), 1)
            self.assertEqual(sum(second_body in text for text in texts), 1)
            self.assertTrue(
                all(
                    not ("COMMENT 01" in text and "COMMENT 02" in text)
                    for text in texts
                )
            )

    def test_selected_media_download_uses_shared_media_folder_without_extra_lines(self):
        article, _fetch = extract_example()

        class FakeVideoResponse(io.BytesIO):
            headers = {"Content-Type": "video/mp4"}

        with tempfile.TemporaryDirectory() as directory, patch(
            "article_extractor_gui.urllib.request.urlopen",
            return_value=FakeVideoResponse(b"public video bytes"),
        ):
            output, _written = export_article(
                article,
                Path(directory),
                counter=LengthCounter(),
                download_media=True,
            )
            self.assertEqual(
                (output / "media" / "IMAGE-01.mp4").read_bytes(),
                b"public video bytes",
            )
            article_text = (
                output / "cymatics-and-the-mysteries-of-the.txt"
            ).read_text(encoding="utf-8")
            self.assertNotIn("Downloaded file:", article_text)
            self.assertNotIn("Media type:", article_text)
            self.assertNotIn("Media location:", article_text)


if __name__ == "__main__":
    unittest.main()
