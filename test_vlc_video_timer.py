import struct

import vlc_video_timer as app


def test_format_remaining():
    assert app.format_remaining(0) == "0 min 00 sec"
    assert app.format_remaining(61.1) == "1 min 02 sec"
    assert app.format_remaining(3661) == "1 hr 01 min 01 sec"


def test_mp4_duration(tmp_path):
    mvhd_payload = b"\0\0\0\0" + (b"\0" * 8) + struct.pack(">II", 1_000, 12_345)
    mvhd = struct.pack(">I4s", len(mvhd_payload) + 8, b"mvhd") + mvhd_payload
    moov = struct.pack(">I4s", len(mvhd) + 8, b"moov") + mvhd
    video = tmp_path / "video.mp4"
    video.write_bytes(moov)
    assert app.mp4_duration(video) == 12.345
