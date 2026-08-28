import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from srt_translator_gui_mac import (
    PROMPT_TEXT,
    SRTTranslatorGUI,
    arabic_srt_output_path,
    archive_srt_files,
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

    def test_archives_srt_files_and_creates_archive_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "movie.en.srt")
            arabic = Path(tmp, "movie.ar.srt")
            source.write_text("source", encoding="utf-8")
            arabic.write_text("arabic", encoding="utf-8")

            moved = archive_srt_files([str(source), str(arabic)], tmp)

            self.assertEqual(
                moved,
                [str(Path(tmp, "srt", source.name)), str(Path(tmp, "srt", arabic.name))],
            )
            self.assertFalse(source.exists())
            self.assertFalse(arabic.exists())
            self.assertEqual(Path(moved[0]).read_text(encoding="utf-8"), "source")
            self.assertEqual(Path(moved[1]).read_text(encoding="utf-8"), "arabic")

    def test_archiving_never_overwrites_an_existing_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "movie.srt")
            source.write_text("new", encoding="utf-8")
            archive_dir = Path(tmp, "srt")
            archive_dir.mkdir()
            existing = archive_dir / "movie.srt"
            existing.write_text("old", encoding="utf-8")

            moved = archive_srt_files([str(source)], tmp)

            self.assertEqual(moved, [str(archive_dir / "movie.1.srt")])
            self.assertEqual(existing.read_text(encoding="utf-8"), "old")
            self.assertEqual(Path(moved[0]).read_text(encoding="utf-8"), "new")


class SrtSourceNameTests(unittest.TestCase):
    def test_plain_srt_uses_the_whole_stem_as_base(self) -> None:
        self.assertEqual(source_srt_name_parts("movie.srt"), ("movie", None))

    def test_language_shaped_final_label_is_accepted_as_metadata(self) -> None:
        self.assertEqual(
            source_srt_name_parts("long.movie.title.pt-BR.SRT"),
            ("long.movie.title", "pt-BR"),
        )

    def test_technical_suffix_remains_part_of_unique_media_name(self) -> None:
        filename = (
            "UFC.330.Makhachev.vs.Machado.Garry.Main.Card.1080p."
            "WEB-DL.H264-nVa_part007.srt"
        )

        self.assertEqual(
            source_srt_name_parts(filename),
            (filename[:-4], None),
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
        gui.tab_text_widgets = []
        gui.tab_frames = []
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

        info.assert_not_called()
        text_widget.edit_undo.assert_not_called()

    def test_untouched_source_lines_fail_validation(self) -> None:
        gui = SRTTranslatorGUI.__new__(SRTTranslatorGUI)
        gui.status_var = Mock()
        gui.pre_replace_contents = {}
        gui.tab_text_widgets = []
        gui.tab_frames = []
        text_widget = Mock()
        text_widget.get.return_value = (
            "L000001|Hello there\n"
            "L000002|How are you?\n"
        )
        source = {
            "L000001": "Hello there",
            "L000002": "How are you?",
        }

        with patch("srt_translator_gui_mac.messagebox.showwarning") as warning:
            gui.validate_tab(
                text_widget,
                ["L000001", "L000002"],
                "Chunk 1",
                source,
            )

        self.assertIn("No translation detected", warning.call_args.args[1])

    def test_successful_validation_silently_closes_the_chunk_tab(self) -> None:
        gui = SRTTranslatorGUI.__new__(SRTTranslatorGUI)
        gui.status_var = Mock()
        gui.pre_replace_contents = {}
        text_widget = Mock()
        text_widget.get.return_value = (
            "L000001|مرحبًا\n"
            "L000002|كيف حالك؟\n"
        )
        tab_frame = Mock()
        gui.tab_text_widgets = [text_widget]
        gui.tab_frames = [tab_frame]
        gui.close_tab = Mock()

        with patch("srt_translator_gui_mac.messagebox.showinfo") as info:
            gui.validate_tab(
                text_widget,
                ["L000001", "L000002"],
                "Chunk 1",
                {"L000001": "Hello", "L000002": "How are you?"},
            )

        info.assert_not_called()
        gui.close_tab.assert_called_once_with(tab_frame)

    def test_arabic_only_ass_rebuilds_arabic_srt_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = str(Path(tmp, "episode.en.srt"))
            arabic_path = str(Path(tmp, "episode.ar.srt"))
            gui = SRTTranslatorGUI.__new__(SRTTranslatorGUI)
            gui.current_srt_path = source_path
            gui.current_dir = tmp
            gui.original_base = "episode"
            gui.rebuild_srt_only = Mock(return_value=arabic_path)
            gui.status_var = Mock()
            gui.root = Mock()

            with (
                patch("srt_translator_gui_mac.simpledialog.askstring", return_value="model"),
                patch("srt_translator_gui_mac.create_arabic_only_ass") as create_ass,
                patch("srt_translator_gui_mac.archive_srt_files") as archive,
            ):
                gui.create_arabic_only_ass_file()

            gui.rebuild_srt_only.assert_called_once_with()
            create_ass.assert_called_once_with(
                arabic_srt_path=arabic_path,
                output_ass_path=str(Path(tmp, "episode.arabic-only.ass")),
                model_text="model",
            )
            archive.assert_called_once_with([source_path, arabic_path], tmp)

    def test_bilingual_ass_stops_when_automatic_rebuild_fails(self) -> None:
        gui = SRTTranslatorGUI.__new__(SRTTranslatorGUI)
        gui.current_srt_path = "/subs/episode.en.srt"
        gui.current_dir = "/subs"
        gui.original_base = "episode"
        gui.rebuild_srt_only = Mock(return_value=None)
        gui.status_var = Mock()
        gui.root = Mock()

        with patch("srt_translator_gui_mac.create_bilingual_ass") as create_ass:
            gui.create_bilingual_ass_file()

        gui.rebuild_srt_only.assert_called_once_with()
        create_ass.assert_not_called()

if __name__ == "__main__":
    unittest.main()
