import tempfile
import unittest
from pathlib import Path

from srt_translator_gui_mac import (
    arabic_srt_output_path,
    find_source_srt_files,
    source_srt_name_parts,
)


class SrtFileDiscoveryTests(unittest.TestCase):
    def test_finds_every_regular_srt_file_regardless_of_name_or_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in (
                "movie.srt",
                "documentary.de.srt",
                "captions.ja-orig.srt",
                "UPPER.SRT",
            ):
                (directory / name).write_text("", encoding="utf-8")
            (directory / "notes.txt").write_text("", encoding="utf-8")
            (directory / "fake.srt.backup").write_text("", encoding="utf-8")
            (directory / "folder.srt").mkdir()

            self.assertEqual(
                find_source_srt_files(tmp),
                ["captions.ja-orig.srt", "documentary.de.srt", "movie.srt", "UPPER.SRT"],
            )

    def test_missing_directory_has_no_results(self) -> None:
        self.assertEqual(find_source_srt_files("/path/that/does/not/exist"), [])


class SrtSourceNameTests(unittest.TestCase):
    def test_plain_srt_uses_the_whole_stem_as_base(self) -> None:
        self.assertEqual(source_srt_name_parts("movie.srt"), ("movie", None))

    def test_any_final_label_is_accepted_as_language_metadata(self) -> None:
        self.assertEqual(
            source_srt_name_parts("long.movie.title.pt-BR.SRT"),
            ("long.movie.title", "pt-BR"),
        )

    def test_non_srt_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            source_srt_name_parts("movie.vtt")

    def test_arabic_source_is_not_overwritten(self) -> None:
        self.assertEqual(
            arabic_srt_output_path("/subs", "movie", "/subs/movie.AR.SRT"),
            "/subs/movie.translated.ar.srt",
        )


if __name__ == "__main__":
    unittest.main()
