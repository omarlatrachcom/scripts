import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from article_extractor_gui import (
    Comment,
    ExtractedArticle,
    ExtractionError,
    InfoQArticleAdapter,
    InfoQPodcastAdapter,
    IslamOnlineBooksAdapter,
    MediaReference,
    SubstackCommentsParser,
    SubstackAdapter,
    SubstackRichTextConverter,
    WEBSITE_EXTRACTORS,
    WEBSITE_EXTRACTORS_BY_KEY,
    build_website_extension_prompt,
    infer_website_name,
    export_article,
    html_to_blocks,
    permanently_remove_website_source,
    remove_managed_website_sections,
    split_blocks,
    website_sections_to_remove,
)


class LengthCounter:
    mode = "test characters"

    @staticmethod
    def count(text: str) -> int:
        return len(text)


class StructuredTextTests(unittest.TestCase):
    def test_media_placeholders_follow_source_order_without_svg_duplicates(self):
        fragment = """
        <p>Before.</p>
        <figure><img src="one.png" alt="First"><svg><path></path></svg></figure>
        <p>Middle.</p>
        <table><tr><td>nonlinear</td></tr></table>
        <p>After.</p>
        """
        blocks, media = html_to_blocks(fragment)
        self.assertEqual(
            blocks,
            ["Before.", "IMAGE-01", "Middle.", "IMAGE-02", "After."],
        )
        self.assertEqual([item.kind for item in media], ["image", "table"])
        self.assertEqual(media[0].source, "one.png")

    def test_headings_lists_and_quotes_remain_blocks(self):
        blocks, _ = html_to_blocks(
            "<h2>Heading</h2><p>Paragraph</p><ul><li>One</li><li>Two</li></ul>"
            "<blockquote><p>Quoted</p></blockquote>"
        )
        self.assertEqual(
            blocks, ["## Heading", "Paragraph", "- One", "- Two", "> Quoted"]
        )

    def test_paragraphs_inside_list_items_keep_bullets(self):
        blocks, _ = html_to_blocks(
            "<ol><li><p>First item.</p></li><li><p>Second item.</p></li></ol>"
        )
        self.assertEqual(blocks, ["1. First item.", "2. Second item."])


class ChunkingTests(unittest.TestCase):
    def test_chunks_only_between_blocks_and_heading_moves_with_paragraph(self):
        chunks = split_blocks(
            "H",
            ["A" * 20, "## Section", "B" * 20, "C" * 20],
            LengthCounter(),
            max_tokens=48,
        )
        self.assertEqual(len(chunks), 3)
        self.assertIn("## Section\n\n" + "B" * 20, chunks[1])
        self.assertFalse(chunks[0].rstrip().endswith("## Section"))
        self.assertTrue(all(len(chunk) <= 48 for chunk in chunks))


class CommentParserTests(unittest.TestCase):
    def test_nested_substack_comments_have_parent_ids(self):
        source = """
        <div role="article" aria-label="Comment by Parent">
          <a href="/p/post/comment/101" title="Jan 1">date</a>
          <div class="comment-body expanded"><p>Parent body.</p></div>
          <div role="article" aria-label="Comment by Child">
            <a href="/p/post/comment/102" title="Jan 2">date</a>
            <div class="comment-body"><p>Child body.</p></div>
          </div>
        </div>
        """
        parser = SubstackCommentsParser()
        parser.feed(source)
        parser.close()
        comments = parser.comments()
        self.assertEqual([item.identifier for item in comments], ["101", "102"])
        self.assertEqual(comments[1].parent_identifier, "101")
        self.assertEqual(comments[1].blocks, ["Child body."])

    def test_api_comments_are_recursive_and_keep_parent_ids(self):
        payload = {
            "comments": [
                {
                    "id": 10,
                    "name": "Parent",
                    "date": "now",
                    "body": "Parent",
                    "body_json": None,
                    "children": [
                        {
                            "id": 11,
                            "name": "Child",
                            "date": "later",
                            "body": "Child",
                            "body_json": None,
                            "children": [],
                        }
                    ],
                }
            ]
        }
        comments = SubstackAdapter._comments_from_api(payload)
        self.assertEqual([comment.identifier for comment in comments], ["10", "11"])
        self.assertEqual(comments[1].parent_identifier, "10")

    def test_comment_rich_text_preserves_paragraphs_and_images(self):
        document = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Before"}]},
                {"type": "image", "attrs": {"src": "picture.png"}},
                {"type": "paragraph", "content": [{"type": "text", "text": "After"}]},
            ],
        }
        blocks, media = SubstackRichTextConverter().convert(document)
        self.assertEqual(blocks, ["Before", "IMAGE-01", "After"])
        self.assertEqual(media[0].source, "picture.png")


class ExportTests(unittest.TestCase):
    def test_export_inlines_media_and_combines_comments(self):
        article = ExtractedArticle(
            title="Example",
            subtitle="",
            author="Author",
            published="2026-01-01",
            canonical_url="https://example.com/post",
            slug="example",
            blocks=["Text"],
            comments=[Comment("1", "Reader", "Today", ["Comment text"])],
            media=[
                MediaReference(
                    "IMAGE-01", "image", "https://example.com/picture.png"
                )
            ],
            adapter_name="Test",
        )
        article.blocks.append("IMAGE-01")
        with tempfile.TemporaryDirectory() as directory:
            output, written = export_article(
                article, Path(directory), counter=LengthCounter()
            )
            self.assertTrue((output / "example.txt").is_file())
            article_text = (output / "example.txt").read_text()
            self.assertIn(
                "IMAGE-01\n\nMedia source: https://example.com/picture.png",
                article_text,
            )
            self.assertNotIn("Media type:", article_text)
            self.assertNotIn("Media location:", article_text)
            self.assertNotIn("Downloaded file:", article_text)
            self.assertFalse((output / "example_media.txt").exists())
            comments_path = output / "example_comments.txt"
            self.assertTrue(comments_path.is_file())
            comments_text = comments_path.read_text()
            self.assertIn("COMMENT 01", comments_text)
            self.assertIn("Comment text", comments_text)
            self.assertFalse((output / "comments").exists())
            self.assertTrue((output / "extraction_summary.json").is_file())
            self.assertEqual(len(written), 3)


class WebsiteSelectionTests(unittest.TestCase):
    def test_selected_website_rejects_another_substack_publication(self):
        website = WEBSITE_EXTRACTORS[0]
        self.assertEqual(
            website.validate_url(
                "https://anatoledo.substack.com/p/freemasons-in-france-convicted"
            ),
            "https://anatoledo.substack.com/p/freemasons-in-france-convicted",
        )
        with self.assertRaises(ExtractionError):
            website.validate_url("https://unrelated.substack.com/p/example")


class WebsiteRemovalTests(unittest.TestCase):
    def test_exact_managed_sections_are_removed_without_touching_shared_text(self):
        source = """before
# BEGIN WEBSITE CODE: example-adapter
website only
# END WEBSITE CODE: example-adapter
after
"""
        self.assertEqual(
            remove_managed_website_sections(source, {"example-adapter"}),
            "before\nafter\n",
        )
        with self.assertRaisesRegex(ExtractionError, "missing or ambiguous"):
            remove_managed_website_sections(source, {"unknown"})
        with self.assertRaisesRegex(ExtractionError, "missing or ambiguous"):
            remove_managed_website_sections(source + source, {"example-adapter"})

    def test_shared_islamonline_parser_is_removed_with_its_final_consumer(self):
        books = WEBSITE_EXTRACTORS_BY_KEY["islamonline-books"]
        final_sections = website_sections_to_remove(books, [books])
        self.assertIn("islamonline-shared-parser", final_sections)

    def test_every_profile_can_be_removed_sequentially_and_source_still_compiles(self):
        source_path = Path(__file__).with_name("article_extractor_gui.py")
        source = source_path.read_text(encoding="utf-8")
        installed = list(WEBSITE_EXTRACTORS)
        for website in tuple(installed):
            sections = website_sections_to_remove(website, installed)
            source = remove_managed_website_sections(source, sections)
            installed = [item for item in installed if item.key != website.key]
            module_name = "removal_test_" + website.key.replace("-", "_")
            module = types.ModuleType(module_name)
            module.__file__ = str(source_path)
            sys.modules[module_name] = module
            try:
                exec(compile(source, str(source_path), "exec"), module.__dict__)
            finally:
                sys.modules.pop(module_name, None)
            self.assertEqual(
                sorted(module.WEBSITE_EXTRACTORS_BY_KEY),
                sorted(item.key for item in installed),
            )
        self.assertNotIn("# BEGIN WEBSITE CODE: profile-", source)

    def test_permanent_removal_replaces_an_editable_source_atomically(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["infoq-podcasts"]
        source = """value = 1
# BEGIN WEBSITE CODE: infoq-podcasts-parser
parser_value = 2
# END WEBSITE CODE: infoq-podcasts-parser
# BEGIN WEBSITE CODE: infoq-podcasts-adapter
adapter_value = 3
# END WEBSITE CODE: infoq-podcasts-adapter
# BEGIN WEBSITE CODE: profile-infoq-podcasts
profile_value = 4
# END WEBSITE CODE: profile-infoq-podcasts
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editable.py"
            path.write_text(source, encoding="utf-8")
            path.chmod(0o755)
            removed = permanently_remove_website_source(website, [website], path)
            updated = path.read_text(encoding="utf-8")
            self.assertEqual(
                removed,
                {
                    "infoq-podcasts-parser",
                    "infoq-podcasts-adapter",
                    "profile-infoq-podcasts",
                },
            )
            self.assertEqual(updated, "value = 1\n")
            self.assertTrue(path.stat().st_mode & 0o100)
            self.assertFalse(path.with_name(".editable.py.website-removal.tmp").exists())

    def test_islamonline_books_is_an_explicit_article_only_profile(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["islamonline-books"]
        self.assertEqual(website.display_name, "islamonline.net/category/books")
        self.assertEqual(
            website.validate_url("https://islamonline.net/example/"),
            "https://islamonline.net/example/",
        )
        with self.assertRaises(ExtractionError):
            website.validate_url("https://example.com/post")

    def test_infoq_podcasts_is_a_separate_explicit_profile(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["infoq-podcasts"]
        self.assertEqual(website.display_name, "InfoQ")
        self.assertFalse(website.extracts_comments)
        self.assertIsInstance(website.make_adapter(), InfoQPodcastAdapter)
        with self.assertRaises(ExtractionError):
            website.validate_url("https://example.com/podcasts/episode/")

    def test_infoq_articles_is_a_separate_explicit_article_only_profile(self):
        website = WEBSITE_EXTRACTORS_BY_KEY["infoq-articles"]
        self.assertEqual(website.display_name, "InfoQ Articles")
        self.assertFalse(website.extracts_comments)
        self.assertIsInstance(website.make_adapter(), InfoQArticleAdapter)
        self.assertEqual(
            website.validate_url("https://www.infoq.com/articles/example/"),
            "https://www.infoq.com/articles/example/",
        )
        with self.assertRaises(ExtractionError):
            website.validate_url("https://example.com/articles/example/")


class IslamOnlineBooksTests(unittest.TestCase):
    def test_dedicated_adapter_preserves_structure_and_nonlinear_order(self):
        page = """
        <html><head>
          <link rel="canonical" href="https://islamonline.net/example/" />
          <meta property="og:title" content="عنوان احتياطي" />
          <meta property="og:image" content="https://cdn.example/cover.jpg" />
          <meta property="og:image:alt" content="غلاف الكتاب" />
          <meta property="article:published_time" content="2026-08-03T11:12:59+03:00" />
        </head><body>
          <nav id="breadcrumb"><ol><li>
            <a href="https://islamonline.net/category/books/">كتب</a>
          </li></ol></nav>
          <h1 itemprop="headline name">عنوان المقال</h1>
          <span itemprop="author"><a href="/author/editor">إسلام أون لاين</a></span>
          <picture id="postImg"><img src="https://cdn.example/cover.jpg"></picture>
          <article id="article" itemprop="articleBody">
            <p>مقدمة المقال.</p>
            <h2>عنوان فرعي</h2>
            <ul><li>البند الأول</li><li>البند الثاني</li></ul>
            <div class="content-in-middle cards">
              <img src="https://cdn.example/related.jpg">
              <article><a href="/related">اقرأ أيضا</a></article>
            </div>
            <p>النص بعد البطاقة.</p>
            <table><tr><td>بيانات</td></tr></table>
            <blockquote><p>اقتباس.</p></blockquote>
          </article>
        </body></html>
        """
        with patch("article_extractor_gui.fetch_text", return_value=page):
            article = IslamOnlineBooksAdapter().extract(
                "https://islamonline.net/example/", lambda _message: None
            )

        self.assertEqual(article.title, "عنوان المقال")
        self.assertEqual(article.author, "إسلام أون لاين")
        self.assertEqual(article.published, "2026-08-03T11:12:59+03:00")
        self.assertEqual(article.canonical_url, "https://islamonline.net/example/")
        self.assertEqual(article.comments, [])
        self.assertEqual(
            article.blocks,
            [
                "IMAGE-01",
                "مقدمة المقال.",
                "## عنوان فرعي",
                "- البند الأول",
                "- البند الثاني",
                "IMAGE-02",
                "النص بعد البطاقة.",
                "IMAGE-03",
                "> اقتباس.",
            ],
        )
        self.assertEqual(
            [(item.placeholder, item.kind, item.source) for item in article.media],
            [
                ("IMAGE-01", "cover image", "https://cdn.example/cover.jpg"),
                ("IMAGE-02", "embedded content", ""),
                ("IMAGE-03", "table", ""),
            ],
        )

    def test_adapter_rejects_non_books_article(self):
        page = """
        <html><body>
        <nav id="main-menu"><a href="https://islamonline.net/category/books/">Books</a></nav>
        <h1 itemprop="headline name">Other</h1>
        <article id="article" itemprop="articleBody"><p>Text.</p></article>
        </body></html>
        """
        with patch("article_extractor_gui.fetch_text", return_value=page):
            with self.assertRaisesRegex(ExtractionError, "Books category"):
                IslamOnlineBooksAdapter().extract(
                    "https://islamonline.net/other/", lambda _message: None
                )


class InfoQPodcastTests(unittest.TestCase):
    def test_adapter_preserves_editorial_sections_and_resolves_direct_audio(self):
        page = """
        <html><head>
          <link rel="canonical" href="https://www.infoq.com/podcasts/example/">
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": "Example Podcast",
            "datePublished": "2026-07-20T11:00:00+0000",
            "author": [{"@type": "Person", "name": "Host Person"}],
            "contributor": [{"@type": "Person", "name": "Guest Person"}]
          }
          </script>
        </head><body>
          <article data-type="podcast" class="article">
            <div class="intro article__data"><p>Episode introduction.</p></div>
            <div class="article__data"><div class="takeaways">
              <h3>Key Takeaways</h3><ul><li>First takeaway.</li></ul>
            </div></div>
            <div class="podcast__subscribe__list"><button>Subscribe</button></div>
            <iframe id="player" src="https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/12345"></iframe>
            <div class="article__data">
              <h2>Transcript</h2><p>Host: Welcome.</p>
              <table><tr><td>Nonlinear</td></tr></table>
              <p>Guest: Thank you.</p>
              <div class="author-section-full"><h2>About the Author</h2>
                <button>Show more</button></div>
            </div>
          </article>
        </body></html>
        """
        feed = """
        <rss><channel><item>
          <guid>tag:soundcloud,2010:tracks/12345</guid>
          <enclosure type="audio/mpeg"
            url="http://dts.podtrac.com/redirect.mp3/feeds.soundcloud.com/stream/12345-example.mp3" />
        </item></channel></rss>
        """
        with patch(
            "article_extractor_gui.fetch_text", side_effect=[page, feed]
        ):
            article = InfoQPodcastAdapter().extract(
                "https://www.infoq.com/podcasts/example/", lambda _message: None
            )

        self.assertEqual(article.title, "Example Podcast")
        self.assertEqual(article.subtitle, "Podcast with Guest Person")
        self.assertEqual(article.author, "Host Person")
        self.assertEqual(article.comments, [])
        self.assertEqual(
            article.blocks,
            [
                "Episode introduction.",
                "### Key Takeaways",
                "- First takeaway.",
                "IMAGE-01",
                "## Transcript",
                "Host: Welcome.",
                "IMAGE-02",
                "Guest: Thank you.",
            ],
        )
        self.assertEqual(
            [(item.placeholder, item.kind, item.source) for item in article.media],
            [
                (
                    "IMAGE-01",
                    "audio",
                    "https://dts.podtrac.com/redirect.mp3/feeds.soundcloud.com/stream/12345-example.mp3",
                ),
                ("IMAGE-02", "table", ""),
            ],
        )
        self.assertNotIn("Subscribe", "\n".join(article.blocks))
        self.assertNotIn("About the Author", "\n".join(article.blocks))

    def test_adapter_rejects_non_podcast_infoq_pages(self):
        page = """
        <html><head><script type="application/ld+json">
        {"@type":"NewsArticle","headline":"News"}
        </script></head><body><article data-type="news"><p>News.</p></article></body></html>
        """
        with patch("article_extractor_gui.fetch_text", return_value=page):
            with self.assertRaisesRegex(ExtractionError, "no recognizable public podcast"):
                InfoQPodcastAdapter().extract(
                    "https://www.infoq.com/news/example/", lambda _message: None
                )


class InfoQArticleTests(unittest.TestCase):
    def test_adapter_preserves_live_article_structure_and_nonlinear_order(self):
        page = """
        <html><head>
          <link rel="canonical" href="https://www.infoq.com/articles/example/">
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": "Example InfoQ Article",
            "datePublished": "2026-08-10T11:00:00+0000",
            "author": [
              {"@type": "Person", "name": "First Author"},
              {"@type": "Person", "name": "Second Author"}
            ]
          }
          </script>
        </head><body>
          <article data-type="article" class="article">
            <div class="article__content">
              <div class="article__actions">
                <audio id="audio-player"><source src=""></audio>
              </div>
              <div class="article__data">
                <div class="takeaways">
                  <h3>Key Takeaways</h3>
                  <ul><li>First takeaway.</li><li>Second takeaway.</li></ul>
                </div>
                <p>Opening paragraph.</p>
                <h2>Main Section</h2>
                <blockquote><p>A quotation.</p></blockquote>
                <p><img data-src="/media/diagram.png" alt="Architecture diagram"></p>
                <p>Diagram caption.</p>
                <table><tr><td>Nonlinear table data</td></tr></table>
                <iframe src="/embeds/demo"></iframe>
                <div class="related__group related__vc"><h4>Related Sponsors</h4></div>
                <p>Closing paragraph.</p>
                <div class="author-section-full">
                  <h2>About the Authors</h2><p>Frontend biography UI.</p>
                </div>
              </div>
            </div>
          </article>
        </body></html>
        """
        with patch("article_extractor_gui.fetch_text", return_value=page):
            article = InfoQArticleAdapter().extract(
                "https://www.infoq.com/articles/example/", lambda _message: None
            )

        self.assertEqual(article.title, "Example InfoQ Article")
        self.assertEqual(article.author, "First Author, Second Author")
        self.assertEqual(article.published, "2026-08-10T11:00:00+0000")
        self.assertEqual(article.canonical_url, "https://www.infoq.com/articles/example/")
        self.assertEqual(article.comments, [])
        self.assertEqual(
            article.blocks,
            [
                "IMAGE-01",
                "### Key Takeaways",
                "- First takeaway.",
                "- Second takeaway.",
                "Opening paragraph.",
                "## Main Section",
                "> A quotation.",
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
                ("IMAGE-01", "audio player", ""),
                ("IMAGE-02", "image", "https://www.infoq.com/media/diagram.png"),
                ("IMAGE-03", "table", ""),
                ("IMAGE-04", "iframe", "https://www.infoq.com/embeds/demo"),
            ],
        )
        joined = "\n".join(article.blocks)
        self.assertNotIn("Related Sponsors", joined)
        self.assertNotIn("About the Authors", joined)

    def test_adapter_rejects_non_article_infoq_pages(self):
        page = """
        <html><head><script type="application/ld+json">
        {"@type":"NewsArticle","headline":"Podcast"}
        </script></head><body>
          <article data-type="podcast"><div class="article__data"><p>Audio.</p></div></article>
        </body></html>
        """
        with patch("article_extractor_gui.fetch_text", return_value=page):
            with self.assertRaisesRegex(ExtractionError, "no recognizable public article"):
                InfoQArticleAdapter().extract(
                    "https://www.infoq.com/podcasts/example/", lambda _message: None
                )


class PromptGeneratorTests(unittest.TestCase):
    def test_website_name_is_inferred_from_metadata(self):
        name = infer_website_name(
            "https://example.com/article",
            fetcher=lambda _url: '<meta property="og:site_name" content="Example Gazette">',
        )
        self.assertEqual(name, "Example Gazette")

    def test_website_name_falls_back_to_host(self):
        name = infer_website_name(
            "https://ana-toledo.substack.com/p/example",
            fetcher=lambda _url: (_ for _ in ()).throw(OSError("offline")),
        )
        self.assertEqual(name, "Ana Toledo")

    def test_prompt_uses_script_name_and_required_media_layout(self):
        prompt = build_website_extension_prompt(
            "Example News",
            "https://example.com/archive",
            "https://example.com/articles/one",
            "No comments",
        )
        self.assertIn("Extend article_extractor_gui.py in the current directory", prompt)
        self.assertNotIn("/Users/omar/Documents/scripts", prompt)
        self.assertIn("IMAGE-01\n\nMedia source: https://example.com/media", prompt)
        self.assertIn("Do not implement comment extraction", prompt)

    def test_comments_prompt_changes_with_selected_option(self):
        top_level = build_website_extension_prompt(
            "Site", "", "https://example.com/post", "Comments only"
        )
        nested = build_website_extension_prompt(
            "Site", "", "https://example.com/post", "Comments and nested replies"
        )
        self.assertIn("top-level comments", top_level)
        self.assertIn("do not include nested replies", top_level)
        self.assertIn("comments and nested replies", nested)
        self.assertIn("parent comment ID", nested)


if __name__ == "__main__":
    unittest.main()
