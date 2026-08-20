import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from article_extractor_gui import (
    ExtractionError,
    GlobalResearchAdapter,
    TOKEN_LIMIT,
    TokenCounter,
    WEBSITE_EXTRACTORS_BY_KEY,
    export_article,
    globalresearch_html_to_blocks,
)


EXAMPLE_URL = (
    "https://www.globalresearch.ca/"
    "living-most-corrupt-democracy-imagined/5934366"
)
POST_ENDPOINT = (
    "https://www.globalresearch.ca/wp-json/wp/v2/posts/5934366?_embed=1"
)
READER_ENDPOINT = "https://r.jina.ai/" + POST_ENDPOINT
READER_HEADERS = {
    "User-Agent": "ArticleExtractor/1.0",
    "Accept": "text/plain,application/json;q=0.9,*/*;q=0.8",
}
COVER_SOURCE = (
    "https://www.globalresearch.ca/wp-content/uploads/2014/08/"
    "mkultra-400x391.jpg"
)


class FakeMediaResponse:
    def __init__(self, payload: bytes = b"jpeg-bytes") -> None:
        self._stream = io.BytesIO(payload)
        self.headers = {"Content-Type": "image/jpeg"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def post_payload() -> dict:
    return {
        "id": 5934366,
        "date": "2026-07-23T18:09:41",
        "slug": "living-most-corrupt-democracy-imagined",
        "status": "publish",
        "link": EXAMPLE_URL,
        "title": {
            "rendered": "We Are Living in the Most Corrupt Democracy That Could Be Imagined"
        },
        "content": {
            "rendered": """
              <p>Opening paragraph.</p>
              <p style="padding-left: 30px; text-align: justify;">A styled quotation.</p>
              <h2>Main section</h2>
              <ul><li>First item.</li><li>Second item.</li></ul>
              <figure><img data-lazy-src="/media/diagram.jpg">
                <figcaption>Diagram caption.</figcaption></figure>
              <div class="wp-block-gallery" data-url="https://gallery.example/set">
                <img src="/media/gallery-one.jpg"><img src="/media/gallery-two.jpg">
              </div>
              <table><tr><td>Non-linear table.</td></tr></table>
              <iframe src="https://player.example/video"></iframe>
              <button data-url="https://interactive.example/tool">Open tool</button>
              <p>Closing paragraph.</p>
            """
        },
        "_embedded": {
            "author": [{"id": 410916, "name": "Mojmir Babacek"}],
            "wp:featuredmedia": [
                {
                    "source_url": (
                        "https://www.globalresearch.ca/wp-content/uploads/2014/08/"
                        "mkultra.jpg"
                    ),
                    "media_details": {
                        "sizes": {
                            "single-post-thumbnail": {"source_url": COVER_SOURCE}
                        }
                    },
                }
            ],
        },
    }


def extract_fixture(*, reader_fallback: bool = False):
    payload = json.dumps(post_payload())
    responses = {POST_ENDPOINT: payload}
    if reader_fallback:
        responses = {
            READER_ENDPOINT: (
                "Title: \n\nURL Source: "
                + POST_ENDPOINT
                + "\n\nMarkdown Content:\n"
                + payload
            )
        }

    def fake_fetch(url: str, **_kwargs) -> str:
        if reader_fallback and url == POST_ENDPOINT:
            raise ExtractionError("The website returned HTTP 403")
        return responses[url]

    with patch("article_extractor_gui.fetch_text", side_effect=fake_fetch) as fetch:
        article = GlobalResearchAdapter().extract(
            EXAMPLE_URL + "?utm_source=test", lambda _message: None
        )
    return article, fetch


class GlobalResearchSelectionTests(unittest.TestCase):
    def test_profile_is_explicit_article_only_and_host_scoped(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["globalresearch"]
        self.assertEqual(website.display_name, "Globalresearch")
        self.assertEqual(
            website.homepage,
            "https://www.globalresearch.ca/latest-news-and-top-stories",
        )
        self.assertEqual(website.example_url, EXAMPLE_URL)
        self.assertFalse(website.extracts_comments)
        self.assertIsInstance(website.make_adapter(), GlobalResearchAdapter)
        self.assertEqual(website.validate_url(EXAMPLE_URL), EXAMPLE_URL)
        with self.assertRaises(ExtractionError):
            website.validate_url("https://example.com/article/5934366")


class GlobalResearchAdapterTests(unittest.TestCase):
    def test_site_specific_inline_spans_and_indented_quotes_are_preserved(self):
        blocks, _media = globalresearch_html_to_blocks(
            "<p>Since the 1990<span>‘</span><span>s</span>.</p>"
            '<p style="padding-left: 0px">Not a quote.</p>'
            '<p style="padding-left: 30px">Quoted.</p>',
            EXAMPLE_URL,
        )
        self.assertEqual(blocks, ["Since the 1990‘s.", "Not a quote.", "> Quoted."])

    def test_first_party_post_endpoint_preserves_article_structure_and_media_order(self):
        article, fetch = extract_fixture()

        self.assertEqual(fetch.call_args_list, [call(POST_ENDPOINT)])
        self.assertEqual(
            article.title,
            "We Are Living in the Most Corrupt Democracy That Could Be Imagined",
        )
        self.assertEqual(article.author, "Mojmir Babacek")
        self.assertEqual(article.published, "2026-07-23T18:09:41")
        self.assertEqual(article.canonical_url, EXAMPLE_URL)
        self.assertEqual(article.comments, [])
        self.assertEqual(
            article.blocks,
            [
                "IMAGE-01",
                "Opening paragraph.",
                "> A styled quotation.",
                "## Main section",
                "- First item.",
                "- Second item.",
                "IMAGE-02",
                "Diagram caption.",
                "IMAGE-03",
                "IMAGE-04",
                "IMAGE-05",
                "IMAGE-06",
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
                    "https://www.globalresearch.ca/media/diagram.jpg",
                ),
                ("IMAGE-03", "embedded content", "https://gallery.example/set"),
                ("IMAGE-04", "table", ""),
                ("IMAGE-05", "iframe", "https://player.example/video"),
                (
                    "IMAGE-06",
                    "interactive content",
                    "https://interactive.example/tool",
                ),
            ],
        )
        self.assertTrue(all(item.render_source_as_link for item in article.media))

    def test_cloudflare_block_uses_reader_transport_for_same_public_endpoint(self):
        article, fetch = extract_fixture(reader_fallback=True)
        self.assertEqual(
            fetch.call_args_list,
            [
                call(POST_ENDPOINT),
                call(READER_ENDPOINT, request_headers=READER_HEADERS),
            ],
        )
        self.assertEqual(article.slug, "living-most-corrupt-democracy-imagined")
        self.assertEqual(article.comments, [])

    def test_non_article_path_is_rejected_before_any_request(self):
        with patch("article_extractor_gui.fetch_text") as fetch:
            with self.assertRaisesRegex(ExtractionError, "Globalresearch article URL"):
                GlobalResearchAdapter().extract(
                    "https://www.globalresearch.ca/latest-news-and-top-stories",
                    lambda _message: None,
                )
        fetch.assert_not_called()


class GlobalResearchExportTests(unittest.TestCase):
    def test_export_uses_exact_media_link_layout_never_overwrites_and_stays_below_limit(self):
        article, _fetch = extract_fixture()
        counter = TokenCounter()
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            first, _first_written = export_article(article, parent, counter=counter)
            second, _second_written = export_article(article, parent, counter=counter)

            self.assertEqual(first.name, "living-most-corrupt-democracy-imagined")
            self.assertEqual(second.name, "living-most-corrupt-democracy-imagined-2")
            article_path = first / "living-most-corrupt-democracy-imagined.txt"
            article_text = article_path.read_text(encoding="utf-8")
            self.assertIn(
                f"IMAGE-01\n\nMedia source: [{COVER_SOURCE}]({COVER_SOURCE})",
                article_text,
            )
            self.assertNotIn("Media type:", article_text)
            self.assertNotIn("Media location:", article_text)
            self.assertNotIn("Downloaded file:", article_text)
            self.assertFalse((first / "media").exists())
            self.assertFalse(any(first.glob("*_comments*.txt")))
            for path in first.glob("*.txt"):
                self.assertLess(
                    counter.count(path.read_text(encoding="utf-8")), TOKEN_LIMIT
                )

    def test_selected_media_download_retries_raw_archive_without_changing_text(self):
        article, _fetch = extract_fixture()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "article_extractor_gui.urllib.request.urlopen",
                side_effect=[
                    OSError("Cloudflare blocked the original"),
                    FakeMediaResponse(b"cover"),
                    OSError("Cloudflare blocked the original"),
                    FakeMediaResponse(b"diagram"),
                ],
            ) as urlopen:
                output, _written = export_article(
                    article,
                    Path(directory),
                    counter=TokenCounter(),
                    download_media=True,
                )

            self.assertEqual(urlopen.call_count, 4)
            self.assertEqual((output / "media" / "IMAGE-01.jpg").read_bytes(), b"cover")
            self.assertEqual((output / "media" / "IMAGE-02.jpg").read_bytes(), b"diagram")
            article_text = (
                output / "living-most-corrupt-democracy-imagined.txt"
            ).read_text(encoding="utf-8")
            self.assertIn(
                f"IMAGE-01\n\nMedia source: [{COVER_SOURCE}]({COVER_SOURCE})",
                article_text,
            )
            self.assertNotIn("Downloaded file:", article_text)


if __name__ == "__main__":
    unittest.main()
