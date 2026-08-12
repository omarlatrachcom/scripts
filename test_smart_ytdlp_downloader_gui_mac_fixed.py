import os
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
