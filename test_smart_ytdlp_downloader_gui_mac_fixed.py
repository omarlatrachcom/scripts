import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


os.environ.setdefault("SMART_YTDLP_RESTARTED_AFTER_UPDATE", "1")

# The conversion tests do not need a working YouTube connection. Provide the
# small import surface used while the GUI module is loaded so tests remain
# offline and do not trigger the app's package installer.
yt_dlp = types.ModuleType("yt_dlp")
yt_dlp.YoutubeDL = object
postprocessor = types.ModuleType("yt_dlp.postprocessor")
postprocessor_common = types.ModuleType("yt_dlp.postprocessor.common")
postprocessor_common.PostProcessor = object
utils = types.ModuleType("yt_dlp.utils")
utils.sanitize_filename = lambda value, **_kwargs: value
version = types.ModuleType("yt_dlp.version")
version.__version__ = "test"
sys.modules.setdefault("yt_dlp", yt_dlp)
sys.modules.setdefault("yt_dlp.postprocessor", postprocessor)
sys.modules.setdefault("yt_dlp.postprocessor.common", postprocessor_common)
sys.modules.setdefault("yt_dlp.utils", utils)
sys.modules.setdefault("yt_dlp.version", version)

import smart_ytdlp_downloader_gui_mac_fixed as downloader


SAMPLE_VTT = """WEBVTT
Kind: captions
Language: es

cue-one
00:00:01.250 --> 00:00:03.500 align:start position:0%
Hola&nbsp;mundo

00:00:04.000 --> 00:00:06.125
Segunda línea&nbsp;&nbsp;
"""


class SubtitleLanguageSelectionTests(unittest.TestCase):
    def test_original_is_available_as_a_subtitle_language_choice(self) -> None:
        self.assertIn("original", downloader.SUPPORTED_SUBTITLE_LANGUAGES)
        self.assertEqual(
            downloader.subtitle_languages_for_choice("original"),
            [downloader.ORIGINAL_SUBTITLE_PATTERN],
        )

    def test_original_choice_downloads_originals_in_predefined_languages_too(self) -> None:
        opts = downloader.build_subtitle_opts(
            langs=downloader.subtitle_languages_for_choice("original"),
            auto=True,
        )

        self.assertEqual(opts["subtitleslangs"], [downloader.ORIGINAL_SUBTITLE_PATTERN])
        for language in ("fr-orig", "en-orig", "es-orig", "ar-orig"):
            with self.subTest(language=language):
                self.assertIsNotNone(re.fullmatch(opts["subtitleslangs"][0], language))

    def test_auto_subtitles_include_originals_outside_predefined_languages(self) -> None:
        opts = downloader.build_subtitle_opts(langs=["en"], auto=True)

        self.assertEqual(opts["subtitleslangs"][0], "en")
        original_pattern = opts["subtitleslangs"][1]
        self.assertIsNotNone(re.fullmatch(original_pattern, "ar-orig"))
        self.assertIsNotNone(re.fullmatch(original_pattern, "de-orig"))
        self.assertIsNotNone(re.fullmatch(original_pattern, "ja-orig"))

    def test_auto_subtitle_original_fallback_excludes_predefined_languages(self) -> None:
        original_pattern = downloader.ORIGINAL_SUBTITLE_FALLBACK_PATTERN

        for language in ("fr-orig", "en-orig", "es-orig", "fr-CA-orig", "es-419-orig"):
            with self.subTest(language=language):
                self.assertIsNone(re.fullmatch(original_pattern, language))
        self.assertIsNone(re.fullmatch(original_pattern, "de"))

    def test_manual_subtitle_selection_remains_exact(self) -> None:
        opts = downloader.build_subtitle_opts(langs=["es"], auto=False)

        self.assertEqual(opts["subtitleslangs"], ["es"])

    def test_player_client_override_preserves_other_extractor_args(self) -> None:
        original = {"youtube": {"player_skip": ["configs"]}, "generic": {"foo": ["bar"]}}

        result = downloader.with_youtube_player_client(original, "web_embedded")

        self.assertEqual(result["youtube"]["player_client"], ["web_embedded"])
        self.assertEqual(result["youtube"]["player_skip"], ["configs"])
        self.assertEqual(result["generic"], {"foo": ["bar"]})
        self.assertNotIn("player_client", original["youtube"])


class SubtitleFallbackTests(unittest.TestCase):
    def test_single_srt_job_fails_when_no_valid_subtitle_was_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gui = object.__new__(downloader.DownloaderGUI)
            gui.queue = mock.Mock()
            gui.queue_log = lambda _message: None
            gui.queue_progress = lambda *_args, **_kwargs: None
            gui.make_progress_hook = lambda: (lambda _data: None)
            config = {
                "mode": "single",
                "media_type": "srt",
                "url": "https://www.youtube.com/watch?v=5WDBhdPprqw",
                "output_dir": tmp,
                "wrap_in_folder": False,
                "use_cookies": False,
                "browser": "chrome",
                "want_subs": True,
                "subs_lang": "original",
            }

            with (
                mock.patch.object(downloader, "run_download", return_value=0),
                mock.patch.object(
                    downloader,
                    "run_optional_auto_sub_fallback",
                    return_value=downloader.SubtitleCleanupStats(),
                ),
            ):
                success, summary = gui.download_one(config)

            self.assertFalse(success)
            self.assertIn("Subtitle download failed", summary)

    def test_playlist_completion_ignores_invalid_srt_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "01 - broken.en.srt").write_text("not subtitles", encoding="utf-8")
            (output_dir / "02 - valid.en.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nCaption\n",
                encoding="utf-8",
            )

            completed = downloader.completed_playlist_indices(
                output_dir,
                extensions={"srt"},
            )

            self.assertEqual(completed, {2})

    def test_retries_with_embedded_client_when_defaults_create_no_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            calls: list[dict] = []

            def fake_run_download(_urls, opts, **_kwargs) -> int:
                calls.append(opts)
                if len(calls) == 2:
                    (output_dir / "video.ja-orig.srt").write_text(
                        "1\n00:00:01,000 --> 00:00:02,000\nCaption\n",
                        encoding="utf-8",
                    )
                return 0

            logs: list[str] = []
            base_opts = {
                "paths": {"home": str(output_dir)},
                "subtitleslangs": [downloader.ORIGINAL_SUBTITLE_PATTERN],
                "extractor_args": {},
            }
            with mock.patch.object(downloader, "run_download", side_effect=fake_run_download):
                stats = downloader.run_optional_auto_sub_fallback(
                    ["https://example.invalid/video"],
                    base_opts,
                    output_dir=output_dir,
                    retry_without_cookies=False,
                    logger=logs.append,
                )

            self.assertEqual(len(calls), 2)
            self.assertEqual(
                calls[1]["extractor_args"]["youtube"]["player_client"],
                ["web_embedded"],
            )
            self.assertTrue(calls[1]["ignore_no_formats_error"])
            self.assertEqual(stats.scanned_files, 1)
            self.assertTrue(any("embedded client" in message for message in logs))

    def test_does_not_use_embedded_client_when_default_fallback_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            def fake_run_download(_urls, _opts, **_kwargs) -> int:
                (output_dir / "video.en.srt").write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nCaption\n",
                    encoding="utf-8",
                )
                return 0

            with mock.patch.object(downloader, "run_download", side_effect=fake_run_download) as run_mock:
                downloader.run_optional_auto_sub_fallback(
                    ["https://example.invalid/video"],
                    {
                        "paths": {"home": str(output_dir)},
                        "subtitleslangs": ["en"],
                    },
                    output_dir=output_dir,
                    retry_without_cookies=False,
                    logger=lambda _message: None,
                )

            self.assertEqual(run_mock.call_count, 1)

    def test_gui_logger_confirms_only_parseable_srt_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            valid_path = output_dir / "valid.en.srt"
            invalid_path = output_dir / "invalid.en.srt"
            valid_path.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nCaption\n",
                encoding="utf-8",
            )
            invalid_path.write_text("not subtitles", encoding="utf-8")
            logger = downloader.GuiLogger(lambda _message: None)

            logger.info(f"[info] Writing video subtitles to: {valid_path}")
            logger.info(f"[info] Writing video subtitles to: {invalid_path}")

            self.assertEqual(logger.valid_srt_destinations(), {valid_path.resolve()})


class VttSubtitleRecoveryTests(unittest.TestCase):
    def test_converts_vtt_to_normalized_srt_and_removes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vtt_path = Path(tmp) / "video.es.vtt"
            vtt_path.write_text(SAMPLE_VTT, encoding="utf-8")

            srt_path = downloader.convert_vtt_to_srt_file(vtt_path)

            self.assertFalse(vtt_path.exists())
            self.assertTrue(srt_path.exists())
            content = srt_path.read_text(encoding="utf-8")
            self.assertIn("00:00:01,250 --> 00:00:03,500", content)
            self.assertIn("Hola mundo", content)
            self.assertIn("Segunda línea", content)
            self.assertNotIn("\\h", content)
            self.assertNotIn("&nbsp;", content)
            self.assertFalse(any(line.endswith(" ") for line in content.splitlines()))
            self.assertEqual(len(downloader.parse_srt_content(content)), 2)

    def test_run_download_converts_only_vtt_created_by_that_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            existing_vtt = output_dir / "unrelated.vtt"
            existing_vtt.write_text(SAMPLE_VTT, encoding="utf-8")

            def fake_download(*_args, **_kwargs) -> int:
                (output_dir / "downloaded.es.vtt").write_text(SAMPLE_VTT, encoding="utf-8")
                return 0

            logs: list[str] = []
            with mock.patch.object(downloader, "_run_download", side_effect=fake_download):
                result = downloader.run_download(
                    ["https://example.invalid/video"],
                    {"paths": {"home": str(output_dir)}},
                    retry_without_cookies=False,
                    logger=logs.append,
                )

            self.assertEqual(result, 0)
            self.assertTrue(existing_vtt.exists())
            self.assertFalse((output_dir / "unrelated.srt").exists())
            self.assertFalse((output_dir / "downloaded.es.vtt").exists())
            self.assertTrue((output_dir / "downloaded.es.srt").exists())
            self.assertTrue(any("Converted subtitle" in message for message in logs))

    def test_failed_conversion_keeps_original_vtt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vtt_path = Path(tmp) / "video.es.vtt"
            vtt_path.write_text(SAMPLE_VTT, encoding="utf-8")
            failed = subprocess.CompletedProcess(
                args=["ffmpeg"],
                returncode=1,
                stdout="",
                stderr="conversion failed",
            )

            with mock.patch.object(downloader.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "conversion failed"):
                    downloader.convert_vtt_to_srt_file(vtt_path)

            self.assertTrue(vtt_path.exists())
            self.assertFalse(vtt_path.with_suffix(".srt").exists())

    def test_new_redundant_vtt_is_removed_when_valid_srt_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            srt_path = output_dir / "downloaded.es.srt"
            srt_path.write_text(
                "1\n00:00:01,250 --> 00:00:03,500\nManual subtitle\n",
                encoding="utf-8",
            )

            def fake_download(*_args, **_kwargs) -> int:
                (output_dir / "downloaded.es.vtt").write_text(SAMPLE_VTT, encoding="utf-8")
                return 0

            logs: list[str] = []
            with mock.patch.object(downloader, "_run_download", side_effect=fake_download):
                result = downloader.run_download(
                    ["https://example.invalid/video"],
                    {"paths": {"home": str(output_dir)}},
                    retry_without_cookies=False,
                    logger=logs.append,
                )

            self.assertEqual(result, 0)
            self.assertFalse((output_dir / "downloaded.es.vtt").exists())
            self.assertIn("Manual subtitle", srt_path.read_text(encoding="utf-8"))
            self.assertTrue(any("Removed redundant VTT" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
