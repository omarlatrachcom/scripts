import tempfile
import unittest
from pathlib import Path

import book_utils


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


if __name__ == "__main__":
    unittest.main()
