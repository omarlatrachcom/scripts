import tempfile
import unittest
from pathlib import Path

from video_splitter_gui_mac import (
    matching_subtitle_files,
    split_ass_cut_file,
    split_ass_window_file,
    subtitle_sidecar_output_path,
)


ASS_SAMPLE = """[Script Info]\r
Title: Preserve me\r
\r
[V4+ Styles]\r
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r
Style: Fancy,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,2,20,20,30,1\r
\r
[Events]\r
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r
Dialogue: 0,0:00:08.00,0:00:12.00,Fancy,,0,0,0,,{\\pos(100,200)}hello, world\r
Dialogue: 0,0:00:15.00,0:00:25.00,Fancy,,0,0,0,,second\r
Comment: 0,0:00:30.00,0:00:31.00,Fancy,,0,0,0,,outside\r
"""


class AssSubtitleSplitTests(unittest.TestCase):
    def test_window_split_preserves_formatting_and_clips_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.ass"
            output = Path(tmp) / "output.ass"
            source.write_bytes(b"\xef\xbb\xbf" + ASS_SAMPLE.encode("utf-8"))

            split_ass_window_file(source, start_seconds=10, length_seconds=10, output_path=output)

            data = output.read_bytes()
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", data)
            text = data[3:].decode("utf-8")
            self.assertIn("Style: Fancy,Arial,48", text)
            self.assertIn(r"{\pos(100,200)}hello, world", text)
            self.assertIn("Dialogue: 0,0:00:00.00,0:00:02.00", text)
            self.assertIn("Dialogue: 0,0:00:05.00,0:00:10.00", text)
            self.assertNotIn("outside", text)

    def test_cut_split_duplicates_and_rebases_boundary_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.ass"
            part1 = Path(tmp) / "part1.ass"
            part2 = Path(tmp) / "part2.ass"
            source.write_text(ASS_SAMPLE, encoding="utf-8", newline="")

            split_ass_cut_file(source, cut_seconds=10, output_path_1=part1, output_path_2=part2)

            first = part1.read_text(encoding="utf-8")
            second = part2.read_text(encoding="utf-8")
            self.assertIn("Dialogue: 0,0:00:08.00,0:00:10.00", first)
            self.assertIn("Dialogue: 0,0:00:00.00,0:00:02.00", second)
            self.assertIn(r"{\pos(100,200)}hello, world", first)
            self.assertIn(r"{\pos(100,200)}hello, world", second)

    def test_discovery_includes_srt_and_ass_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            video = "movie.mp4"
            for name in (video, "movie.srt", "movie.bilingual.ass", "notes.vtt"):
                (directory / name).touch()

            self.assertEqual(
                matching_subtitle_files(directory, video),
                ["movie.srt", "movie.bilingual.ass"],
            )

    def test_sidecar_name_places_label_after_part_number(self) -> None:
        input_video = Path("movie.mp4")
        input_subtitle = Path("movie.bilingual.ass")

        self.assertEqual(
            subtitle_sidecar_output_path(
                Path("movie_part001.mp4"),
                input_video,
                input_subtitle,
                subtitle_index=1,
            ).name,
            "movie_part001.bilingual.ass",
        )
        self.assertEqual(
            subtitle_sidecar_output_path(
                Path("movie.part1.mp4"),
                input_video,
                input_subtitle,
                subtitle_index=1,
            ).name,
            "movie.part1.bilingual.ass",
        )


if __name__ == "__main__":
    unittest.main()
