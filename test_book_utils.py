import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import book_utils


class TokenizerOfflineTests(unittest.TestCase):
    def test_bundled_o200k_asset_matches_expected_checksum(self) -> None:
        asset_data = book_utils.TOKENIZER_ASSET_PATH.read_bytes()

        self.assertEqual(
            hashlib.sha256(asset_data).hexdigest(),
            book_utils.TOKENIZER_ASSET_SHA256,
        )

    def test_token_counter_works_with_empty_cache_and_network_blocked(self) -> None:
        module_dir = Path(book_utils.__file__).resolve().parent

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            empty_cache = temp_path / "empty_tiktoken_cache"
            empty_cache.mkdir()
            env = os.environ.copy()
            env["TIKTOKEN_CACHE_DIR"] = str(empty_cache)
            env["DATA_GYM_CACHE_DIR"] = str(empty_cache)
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = os.pathsep.join(
                part
                for part in (str(module_dir), existing_pythonpath)
                if part
            )
            test_script = textwrap.dedent(
                """
                import socket
                import urllib.request

                def network_disabled(*args, **kwargs):
                    raise AssertionError("network access attempted")

                socket.create_connection = network_disabled
                socket.getaddrinfo = network_disabled
                urllib.request.urlopen = network_disabled

                import book_utils

                count_tokens = book_utils.token_counter()
                assert count_tokens("This is a test.") == 5
                assert count_tokens("Hello, 世界") == 3
                assert count_tokens("<|endoftext|>") == 7
                assert count_tokens("") == 0
                """
            )

            result = subprocess.run(
                [sys.executable, "-c", test_script],
                cwd=temp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertEqual(list(empty_cache.iterdir()), [])


class JPGPDFConversionTests(unittest.TestCase):
    def test_find_jpg_images_uses_natural_order_and_ignores_other_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            for name in ("page10.jpg", "page2.JPEG", "page1.jpg", "page3.png"):
                (folder / name).write_bytes(b"placeholder")

            image_names = [path.name for path in book_utils.find_jpg_images(folder)]

            self.assertEqual(
                image_names,
                ["page1.jpg", "page2.JPEG", "page10.jpg"],
            )

    @unittest.skipUnless(
        importlib.util.find_spec("fitz") is not None,
        "PyMuPDF is required for JPG-to-PDF integration testing",
    )
    def test_construct_pdf_from_jpgs_creates_pages_in_natural_order(self) -> None:
        import fitz
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "scanned_book"
            folder.mkdir()
            colors_by_name = {
                "page10.jpg": (255, 0, 0),
                "page2.jpeg": (0, 255, 0),
                "page1.jpg": (0, 0, 255),
            }
            for name, color in colors_by_name.items():
                Image.new("RGB", (80, 60), color).save(folder / name, quality=100)
            Image.new("RGB", (80, 60), "white").save(folder / "ignored.png")

            result = book_utils.construct_pdf_from_jpgs(folder)

            self.assertEqual(result.output_pdf, folder / "scanned_book.pdf")
            self.assertEqual(
                [path.name for path in result.image_files],
                ["page1.jpg", "page2.jpeg", "page10.jpg"],
            )
            with fitz.open(result.output_pdf) as document:
                self.assertEqual(document.page_count, 3)
                center_colors = []
                for page in document:
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(0.1, 0.1))
                    center_index = (
                        (pixmap.height // 2) * pixmap.width + (pixmap.width // 2)
                    ) * pixmap.n
                    center_colors.append(tuple(pixmap.samples[center_index:center_index + 3]))

            self.assertGreater(center_colors[0][2], 240)
            self.assertGreater(center_colors[1][1], 240)
            self.assertGreater(center_colors[2][0], 240)

    def test_construct_pdf_from_jpgs_rejects_empty_folder_before_importing_fitz(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                book_utils.JPGPDFConversionError,
                "does not contain any JPG",
            ):
                book_utils.construct_pdf_from_jpgs(Path(temp_dir))

    def test_unique_jpg_pdf_output_path_does_not_overwrite_existing_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "My Scans"
            folder.mkdir()
            (folder / "My Scans.pdf").write_bytes(b"existing")

            output_path = book_utils.unique_jpg_pdf_output_path(folder)

            self.assertEqual(output_path, folder / "My Scans_2.pdf")


class TextCleanerPageNumberTests(unittest.TestCase):
    def clean_folder_text(self, text: str) -> str:
        with tempfile.TemporaryDirectory() as temp_dir:
            txt_path = Path(temp_dir) / "sample.txt"
            txt_path.write_text(text, encoding="utf-8")

            book_utils.clean_txt_files_in_folder(Path(temp_dir))

            return txt_path.read_text(encoding="utf-8")

    def test_preserves_i_after_numeric_page_number(self) -> None:
        cleaned = self.clean_folder_text(
            "### Source PDF: sample.pdf\n\n"
            "### Page 1\n\n"
            "149 Chapter 1 Louise sapped.\n\n"
            "I went to work to get into Louise,\n\n"
            "### Page 2\n\n"
            "150 I thought still I was to be fooled.\n\n"
            "### Page 3\n\n"
            "155 I bought her a bonnet.\n"
        )

        self.assertIn("I went to work to get into Louise,", cleaned)
        self.assertIn("I thought still I was to be fooled.", cleaned)
        self.assertIn("I bought her a bonnet.", cleaned)
        self.assertNotIn("\nthought still I was", cleaned)
        self.assertNotIn("\nbought her a bonnet.", cleaned)

    def test_does_not_learn_common_page_start_words_as_headers(self) -> None:
        cleaned = self.clean_folder_text(
            "### Page 1\n\n"
            "100 My first paragraph continues here.\n\n"
            "### Page 2\n\n"
            "101 My second paragraph continues here.\n\n"
            "### Page 3\n\n"
            "102 All hopes were still intact.\n\n"
            "### Page 4\n\n"
            "103 All right, we can continue.\n"
        )

        self.assertIn("My first paragraph continues here.", cleaned)
        self.assertIn("My second paragraph continues here.", cleaned)
        self.assertIn("All hopes were still intact.", cleaned)
        self.assertIn("All right, we can continue.", cleaned)
        self.assertNotIn("\nfirst paragraph continues", cleaned)
        self.assertNotIn("\nhopes were still intact", cleaned)

    def test_does_not_treat_single_i_as_page_number(self) -> None:
        cleaned, _, _ = book_utils.clean_txt_content(
            "The plan,\n\n"
            "### Page 2\n\n"
            "I went to work the next morning.\n"
        )

        self.assertIn("The plan, I went to work the next morning.", cleaned)
        self.assertNotIn("The plan, went to work", cleaned)

    def test_still_removes_actual_numeric_page_number(self) -> None:
        cleaned, _, _ = book_utils.clean_txt_content(
            "### Page 7\n\n"
            "155 I bought her a bonnet.\n"
        )

        self.assertIn("I bought her a bonnet.", cleaned)
        self.assertNotIn("155 I bought", cleaned)

    def test_removes_numeric_page_number_inside_open_quote(self) -> None:
        cleaned, _, _ = book_utils.clean_txt_content(
            '"Oh!\n\n'
            "### Page 2\n\n"
            '218 for shame!" she said.\n'
        )

        self.assertIn('"Oh! for shame!" she said.', cleaned)
        self.assertNotIn("218 for shame", cleaned)

    def test_still_removes_repeated_punctuated_ocr_header(self) -> None:
        cleaned, _, _ = book_utils.clean_txt_content(
            "### Page 1\n\n"
            "g. On Advanced Lovemaking\n\n"
            "First body line.\n\n"
            "### Page 2\n\n"
            "10. On Advanced Lovemaking\n\n"
            "Second body line.\n"
        )

        self.assertNotIn("On Advanced Lovemaking", cleaned)
        self.assertIn("First body line.", cleaned)
        self.assertIn("Second body line.", cleaned)


class PDFColumnExtractionTests(unittest.TestCase):
    def test_two_column_text_reads_left_column_before_right(self) -> None:
        lines = [
            book_utils.PDFTextLine("Report header", 24, 20, 180, 32),
            book_utils.PDFTextLine("Document title", 220, 50, 392, 64),
            book_utils.PDFTextLine("January 2013", 520, 72, 590, 84),
            book_utils.PDFTextLine("Introduction:", 24, 110, 110, 122),
            book_utils.PDFTextLine("Left line one.", 24, 126, 280, 138),
            book_utils.PDFTextLine("Left line two continues", 24, 142, 280, 154),
            book_utils.PDFTextLine("rolled into a", 24, 158, 210, 170),
            book_utils.PDFTextLine("cigarette and smoked.", 310, 110, 570, 122),
            book_utils.PDFTextLine("User Population:", 310, 142, 430, 154),
            book_utils.PDFTextLine("Right body one", 310, 158, 570, 170),
            book_utils.PDFTextLine("Right body two.", 310, 174, 570, 186),
        ]

        text = book_utils.extract_two_column_text_from_pdf_lines(lines, page_width=612)

        self.assertIn("rolled into a cigarette and smoked.", text)
        self.assertLess(text.index("Left line one."), text.index("cigarette and smoked."))
        self.assertLess(text.index("cigarette and smoked."), text.index("User Population:"))

    def test_single_column_text_does_not_trigger_two_column_path(self) -> None:
        lines = [
            book_utils.PDFTextLine("Heading", 72, 40, 180, 52),
            book_utils.PDFTextLine("First body line", 72, 70, 500, 82),
            book_utils.PDFTextLine("Second body line", 72, 86, 500, 98),
            book_utils.PDFTextLine("Third body line.", 72, 102, 500, 114),
        ]

        text = book_utils.extract_two_column_text_from_pdf_lines(lines, page_width=612)

        self.assertEqual(text, "")


class EpubPDFConversionTests(unittest.TestCase):
    def test_unique_epub_pdf_output_path_adds_counter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            epub_path = folder / "book.epub"
            existing_pdf = folder / "book.pdf"
            epub_path.write_text("placeholder", encoding="utf-8")
            existing_pdf.write_text("placeholder", encoding="utf-8")

            output_path = book_utils.unique_epub_pdf_output_path(epub_path)

            self.assertEqual(output_path, folder / "book_2.pdf")

    def test_convert_epub_to_pdf_rejects_non_epub_before_converter_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            txt_path = Path(temp_dir) / "book.txt"
            txt_path.write_text("placeholder", encoding="utf-8")

            with self.assertRaises(book_utils.EpubPDFConversionError):
                book_utils.convert_epub_to_pdf(txt_path)


if __name__ == "__main__":
    unittest.main()
