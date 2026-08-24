import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from article_extractor_gui import (
    ExtractionError,
    TOKEN_LIMIT,
    TelQuelAdapter,
    TelQuelPageParser,
    TokenCounter,
    WEBSITE_EXTRACTORS_BY_KEY,
    export_article,
    telquel_html_to_blocks,
)


EXAMPLE_URL = "https://telquel.ma/2026/07/24/le-reflexe-jettou_2001434"
IMAGE_SOURCE = "https://cdn.telquel.ma/media/diagram.jpg"
RELATED_SOURCE = "https://telquel.ma/related-story_2000001"


def article_page() -> str:
    return f"""
    <html><head><link rel="canonical" href="{EXAMPLE_URL}"></head><body>
      <main class="main">
        <main class="main post">
          <header class="single-header"><div class="container">
            <h2 class="article-heading"> Le réflexe Jettou </h2>
            <div class="article-meta">
              <time class="article-publish">Le 24 juillet 2026</time>
            </div>
            <h4 class="article-editor">
              <a class="article-editor-name"><small>Par</small> Yassine Majdi</a>
            </h4>
            <div class="single-pre-content"><p>Public standfirst.</p></div>
          </div></header>
          <section><div class="container"><div class="col-large col-gutter">
            <div class="single-content">
              <div class="col-small col-gutter"><time>4 min</time></div>
              <div class="col-large">
                <p>Opening <strong>paragraph</strong>.</p>
                <h2>Section heading</h2>
                <ol><li>First item.</li><li>Second item.</li></ol>
                <blockquote><p>Quoted passage.</p>
                  <div class="blockquote-author">Yassine Majdi</div></blockquote>
                <figure><noscript><img src="/fallback.jpg"></noscript>
                  <img src="data:image/svg+xml,placeholder" data-src="{IMAGE_SOURCE}">
                  <figcaption>Diagram caption.</figcaption></figure>
                <table><tr><td>Non-linear table.</td></tr></table>
                <div id="inneradsholder"></div>
                <div class="newsletter-desktop"></div>
                <div class="related-in-article"><h4>À lire aussi</h4>
                  <a href="{RELATED_SOURCE}">Related story</a></div>
                <p>Closing paragraph.</p>
              </div>
            </div>
            <div class="single-content"><p>Author footer must not be captured.</p></div>
          </div></div></section>
        </main>
      </main>
    </body></html>
    """


class FakeMediaResponse:
    def __init__(self) -> None:
        self._stream = io.BytesIO(b"jpeg-bytes")
        self.headers = {"Content-Type": "image/jpeg"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def extract_fixture():
    with patch("article_extractor_gui.fetch_telquel_page", return_value=article_page()) as fetch:
        article = TelQuelAdapter().extract(
            EXAMPLE_URL + "?utm_source=test", lambda _message: None
        )
    return article, fetch


class TelQuelSelectionTests(unittest.TestCase):
    def test_profile_is_explicit_article_only_and_host_scoped(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["telquel"]
        self.assertEqual(website.display_name, "Telquel.ma")
        self.assertEqual(website.homepage, "https://telquel.ma/categorie/opinions")
        self.assertEqual(website.example_url, EXAMPLE_URL)
        self.assertFalse(website.extracts_comments)
        self.assertIsInstance(website.make_adapter(), TelQuelAdapter)
        self.assertEqual(website.validate_url(EXAMPLE_URL), EXAMPLE_URL)
        with self.assertRaises(ExtractionError):
            website.validate_url("https://example.com/2026/07/24/story_2001434")


class TelQuelParserTests(unittest.TestCase):
    def test_inline_emphasis_does_not_insert_spaces_around_french_quotes(self):
        blocks, _media = telquel_html_to_blocks(
            "<p>d&rsquo;<em>“entorse</em>”.</p>", EXAMPLE_URL
        )
        self.assertEqual(blocks, ["d’“entorse”."])

    def test_real_page_container_excludes_share_chrome_and_author_footer(self):
        parser = TelQuelPageParser()
        parser.feed(article_page())
        parser.close()

        self.assertEqual(parser.title, "Le réflexe Jettou")
        self.assertEqual(parser.subtitle, "Public standfirst.")
        self.assertEqual(parser.author, "Yassine Majdi")
        self.assertEqual(parser.published, "Le 24 juillet 2026")
        self.assertEqual(parser.canonical_url, EXAMPLE_URL)
        self.assertIn("Opening", parser.body_html)
        self.assertNotIn("4 min", parser.body_html)
        self.assertNotIn("Author footer", parser.body_html)

    def test_linear_structure_and_every_non_linear_component_keep_source_order(self):
        page_parser = TelQuelPageParser()
        page_parser.feed(article_page())
        page_parser.close()
        blocks, media = telquel_html_to_blocks(page_parser.body_html, EXAMPLE_URL)

        self.assertEqual(
            blocks,
            [
                "Opening paragraph.",
                "## Section heading",
                "1. First item.",
                "2. Second item.",
                "> Quoted passage.",
                "> Yassine Majdi",
                "IMAGE-01",
                "Diagram caption.",
                "IMAGE-02",
                "IMAGE-03",
                "IMAGE-04",
                "IMAGE-05",
                "Closing paragraph.",
            ],
        )
        self.assertEqual(
            [(item.placeholder, item.kind, item.source) for item in media],
            [
                ("IMAGE-01", "image", IMAGE_SOURCE),
                ("IMAGE-02", "table", ""),
                ("IMAGE-03", "embedded content", ""),
                ("IMAGE-04", "embedded content", ""),
                ("IMAGE-05", "embedded content", RELATED_SOURCE),
            ],
        )
        self.assertTrue(all(item.render_source_as_link for item in media))


class TelQuelAdapterTests(unittest.TestCase):
    def test_server_rendered_article_is_complete_and_comments_are_never_requested(self):
        article, fetch = extract_fixture()

        fetch.assert_called_once_with(EXAMPLE_URL)
        self.assertEqual(article.title, "Le réflexe Jettou")
        self.assertEqual(article.subtitle, "Public standfirst.")
        self.assertEqual(article.author, "Yassine Majdi")
        self.assertEqual(article.published, "Le 24 juillet 2026")
        self.assertEqual(article.canonical_url, EXAMPLE_URL)
        self.assertEqual(article.slug, "le-reflexe-jettou")
        self.assertEqual(article.comments, [])
        self.assertEqual(article.blocks[-1], "Closing paragraph.")

    def test_non_article_path_is_rejected_before_any_request(self):
        with patch("article_extractor_gui.fetch_telquel_page") as fetch:
            with self.assertRaisesRegex(ExtractionError, "Telquel.ma article URL"):
                TelQuelAdapter().extract(
                    "https://telquel.ma/categorie/opinions", lambda _message: None
                )
        fetch.assert_not_called()


class TelQuelExportTests(unittest.TestCase):
    def test_export_link_layout_media_option_unique_folder_and_token_ceiling(self):
        article, _fetch = extract_fixture()
        counter = TokenCounter()
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            with patch(
                "article_extractor_gui.urllib.request.urlopen",
                return_value=FakeMediaResponse(),
            ):
                first, _written = export_article(
                    article, parent, counter=counter, download_media=True
                )
            second, _second_written = export_article(article, parent, counter=counter)

            self.assertEqual(first.name, "le-reflexe-jettou")
            self.assertEqual(second.name, "le-reflexe-jettou-2")
            self.assertEqual(
                (first / "media" / "IMAGE-01.jpg").read_bytes(), b"jpeg-bytes"
            )
            article_path = first / "le-reflexe-jettou.txt"
            article_text = article_path.read_text(encoding="utf-8")
            self.assertIn(
                f"IMAGE-01\n\nMedia source: [{IMAGE_SOURCE}]({IMAGE_SOURCE})",
                article_text,
            )
            self.assertNotIn("Downloaded file:", article_text)
            self.assertFalse(any(first.glob("*_comments*.txt")))
            for path in first.glob("*.txt"):
                self.assertLess(
                    counter.count(path.read_text(encoding="utf-8")), TOKEN_LIMIT
                )


if __name__ == "__main__":
    unittest.main()
