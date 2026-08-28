import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from srt_translator_gui_mac import (
    PROMPT_TEXT,
    SRTTranslatorGUI,
    arabic_srt_output_path,
    find_source_srt_files,
    find_video_for_base,
    infer_media_context,
    open_video_in_vlc,
    source_srt_name_parts,
    translation_prompt_for_srt,
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


class TranslationPromptTests(unittest.TestCase):
    def test_prompt_explicitly_requires_us_to_moroccan_metric_conversion(self) -> None:
        self.assertIn("MANDATORY: Convert every US measurement", PROMPT_TEXT)
        self.assertIn("NEVER retain the original US unit", PROMPT_TEXT)
        self.assertIn("mi/mph → km/km/h", PROMPT_TEXT)
        self.assertIn("°F → °C", PROMPT_TEXT)

    def test_infers_simpsons_episode_context_from_library_filename(self) -> None:
        filename = (
            "The Simpsons - S09E21 - Girly Edition "
            "[1080p] [x265] [pseudo].en.srt"
        )

        self.assertEqual(
            infer_media_context(filename),
            "The Simpsons — S09E21 — Girly Edition",
        )

    def test_infers_underscore_separated_episode_context(self) -> None:
        filename = "Mad_Men_2007_-_S07E08_-_Severance_1080p_BluRay_x265_LION.srt"

        self.assertEqual(
            infer_media_context(filename),
            "Mad Men 2007 — S07E08 — Severance",
        )

    def test_adds_inferred_context_to_prompt_without_inviting_added_content(self) -> None:
        prompt = translation_prompt_for_srt(
            "The Simpsons - S09E21 - Girly Edition [1080p].srt"
        )

        self.assertIn(
            "MEDIA CONTEXT (metadata only): The Simpsons — S09E21 — Girly Edition.",
            prompt,
        )
        self.assertIn("never add content absent from the supplied lines", prompt)

    def test_non_episode_filename_keeps_base_prompt_unchanged(self) -> None:
        self.assertEqual(
            translation_prompt_for_srt("A standalone documentary.en.srt"),
            PROMPT_TEXT,
        )


class VlcTests(unittest.TestCase):
    def test_finds_matching_video_with_uppercase_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp, "Movie.MP4")
            video_path.write_bytes(b"")

            self.assertEqual(
                find_video_for_base("movie", tmp),
                str(video_path),
            )

    @patch("srt_translator_gui_mac.sys.platform", "darwin")
    @patch("srt_translator_gui_mac.subprocess.run")
    def test_opens_video_through_macos_launch_services(self, run: Mock) -> None:
        video_path = "/videos/movie.mp4"

        self.assertTrue(open_video_in_vlc(video_path))

        run.assert_called_once_with(
            ["/usr/bin/open", "-a", "VLC", video_path],
            check=True,
            capture_output=True,
            text=True,
        )


class SrtTranslatorGuiTests(unittest.TestCase):
    def test_scroll_to_bottom_moves_text_view_to_end(self) -> None:
        text_widget = Mock()

        SRTTranslatorGUI.scroll_to_bottom(text_widget)

        text_widget.yview_moveto.assert_called_once_with(1.0)

    def test_failed_validation_restores_previous_editor_content(self) -> None:
        gui = SRTTranslatorGUI.__new__(SRTTranslatorGUI)
        gui.status_var = Mock()
        gui.pre_replace_contents = {}
        text_widget = Mock()
        text_widget.get.return_value = "L000001|Translated line\n"

        with patch("srt_translator_gui_mac.messagebox.showwarning") as warning:
            gui.validate_tab(
                text_widget,
                ["L000001", "L000002"],
                "Chunk 1",
            )

        warning.assert_called_once()
        text_widget.edit_undo.assert_called_once_with()
        gui.status_var.set.assert_called_once_with(
            "Validation warnings in Chunk 1. Previous content restored."
        )

    def test_failed_validation_restores_content_from_before_erase_and_paste(self) -> None:
        gui = SRTTranslatorGUI.__new__(SRTTranslatorGUI)
        gui.root = Mock()
        gui.root.clipboard_get.return_value = "L000001|Incomplete translation"
        gui.status_var = Mock()
        gui.pre_replace_contents = {}
        text_widget = Mock()
        text_widget.get.side_effect = [
            "original prompt and subtitle lines",
            "L000001|Incomplete translation\n",
        ]

        gui.erase_text(text_widget)
        gui.paste_text(text_widget)
        with patch("srt_translator_gui_mac.messagebox.showwarning"):
            gui.validate_tab(
                text_widget,
                ["L000001", "L000002"],
                "Chunk 1",
            )

        self.assertEqual(
            text_widget.insert.call_args_list[-1].args,
            ("1.0", "original prompt and subtitle lines"),
        )
        text_widget.edit_undo.assert_not_called()
        self.assertNotIn(text_widget, gui.pre_replace_contents)

    def test_successful_validation_does_not_undo_editor_content(self) -> None:
        gui = SRTTranslatorGUI.__new__(SRTTranslatorGUI)
        gui.status_var = Mock()
        gui.pre_replace_contents = {}
        text_widget = Mock()
        text_widget.get.return_value = (
            "L000001|First translated line\n"
            "L000002|Second translated line\n"
        )

        with patch("srt_translator_gui_mac.messagebox.showinfo") as info:
            gui.validate_tab(
                text_widget,
                ["L000001", "L000002"],
                "Chunk 1",
            )

        info.assert_called_once()
        text_widget.edit_undo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
