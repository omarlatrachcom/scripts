#!/usr/bin/env python3
"""Play an MP4 in VLC at a chosen speed with a synchronized countdown."""

from __future__ import annotations

import math
import os
import shutil
import signal
import struct
import subprocess
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox


USER_HOME = Path(os.environ.get("HOME") or Path.home())
VLC_LOCATIONS = (
    Path("/Applications/VLC.app/Contents/MacOS/VLC"),
    USER_HOME / "Applications/VLC.app/Contents/MacOS/VLC",
)


def find_vlc() -> str | None:
    for location in VLC_LOCATIONS:
        if location.is_file() and os.access(location, os.X_OK):
            return str(location)
    return shutil.which("vlc")


def mp4_duration(path: Path) -> float:
    """Read the movie-header duration without requiring ffprobe."""
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        position = 0
        while position + 8 <= file_size:
            stream.seek(position)
            header = stream.read(16)
            if len(header) < 8:
                break
            size, box_type = struct.unpack(">I4s", header[:8])
            header_size = 8
            if size == 1:
                if len(header) < 16:
                    break
                size = struct.unpack(">Q", header[8:16])[0]
                header_size = 16
            elif size == 0:
                size = file_size - position
            if size < header_size or position + size > file_size:
                break
            if box_type == b"moov":
                duration = _duration_from_moov(stream, position + header_size, position + size)
                if duration > 0:
                    return duration
            position += size
    raise ValueError("Could not read the MP4 duration.")


def _duration_from_moov(stream, start: int, end: int) -> float:
    position = start
    while position + 8 <= end:
        stream.seek(position)
        header = stream.read(16)
        if len(header) < 8:
            break
        size, box_type = struct.unpack(">I4s", header[:8])
        header_size = 8
        if size == 1:
            if len(header) < 16:
                break
            size = struct.unpack(">Q", header[8:16])[0]
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            break
        if box_type == b"mvhd":
            stream.seek(position + header_size)
            version_flags = stream.read(4)
            if len(version_flags) != 4:
                break
            if version_flags[0] == 0:
                data = stream.read(16)
                if len(data) != 16:
                    break
                timescale, duration = struct.unpack(">II", data[8:16])
            elif version_flags[0] == 1:
                data = stream.read(28)
                if len(data) != 28:
                    break
                timescale, duration = struct.unpack(">IQ", data[16:28])
            else:
                break
            if timescale and duration not in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
                return duration / timescale
            break
        position += size
    return 0.0


def format_remaining(seconds: float) -> str:
    total = max(0, math.ceil(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} hr {minutes:02d} min {secs:02d} sec"
    return f"{minutes} min {secs:02d} sec"


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        command,
        color: str,
        hover_color: str,
        width: int = 500,
        height: int = 88,
    ) -> None:
        background = parent.cget("background")
        super().__init__(
            parent,
            width=width,
            height=height,
            background=background,
            highlightthickness=0,
            cursor="pointinghand",
        )
        self.command = command
        self.color = color
        self.hover_color = hover_color
        self.shape = self._rounded_rectangle(2, 2, width - 2, height - 2, 24, fill=color)
        self.label = self.create_text(
            width / 2,
            height / 2,
            text=text,
            fill="white",
            font=("Helvetica", 25, "bold"),
        )
        self.bind("<Enter>", lambda _event: self.itemconfigure(self.shape, fill=self.hover_color))
        self.bind("<Leave>", lambda _event: self.itemconfigure(self.shape, fill=self.color))
        self.bind("<ButtonRelease-1>", lambda _event: self.command())

    def _rounded_rectangle(self, x1, y1, x2, y2, radius, **kwargs):
        points = (
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        )
        return self.create_polygon(points, smooth=True, splinesteps=36, **kwargs)

    def set_text(self, text: str) -> None:
        self.itemconfigure(self.label, text=text)


class VideoTimerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.process: subprocess.Popen | None = None
        self.timer_window: tk.Toplevel | None = None
        self.duration = 0.0
        self.speed = 2.0
        self.elapsed = 0.0
        self.last_tick = 0.0
        self.paused = False
        self.closing = False

        root.title("VLC Video Timer")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.quit_all)
        root.configure(background="#F5F5F7")
        root.bind("<Return>", lambda _event: self.play())

        frame = tk.Frame(root, background="#F5F5F7", padx=52, pady=48)
        frame.grid(sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        tk.Label(
            frame,
            text="MP4 video",
            font=("Helvetica", 25, "bold"),
            background="#F5F5F7",
            foreground="#242424",
        ).grid(row=0, column=0, sticky="w")
        self.video_var = tk.StringVar()
        tk.Entry(
            frame,
            textvariable=self.video_var,
            width=1,
            font=("Helvetica", 18),
            relief="flat",
            highlightthickness=2,
            highlightbackground="#C8C8CC",
            highlightcolor="#1473E6",
        ).grid(
            row=1, column=0, sticky="ew", ipady=5, padx=(0, 22), pady=(12, 38)
        )
        RoundedButton(
            frame,
            text="Choose…",
            command=self.choose_video,
            color="#1473E6",
            hover_color="#0D5FC3",
            width=220,
            height=70,
        ).grid(
            row=1, column=1, pady=(12, 38)
        )

        tk.Label(
            frame,
            text="Playback speed",
            font=("Helvetica", 25, "bold"),
            background="#F5F5F7",
            foreground="#242424",
        ).grid(row=2, column=0, sticky="w")
        self.speed_var = tk.StringVar(value="2")
        tk.Entry(
            frame,
            textvariable=self.speed_var,
            width=8,
            font=("Helvetica", 22),
            relief="flat",
            highlightthickness=2,
            highlightbackground="#C8C8CC",
            highlightcolor="#1473E6",
        ).grid(
            row=3, column=0, sticky="w", ipady=13, pady=(12, 42)
        )
        RoundedButton(
            frame,
            text="▶  Play",
            command=self.play,
            color="#238636",
            hover_color="#1A6B2B",
            width=1090,
            height=88,
        ).grid(
            row=4, column=0, columnspan=2
        )

    def choose_video(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose an MP4 video",
            initialdir=str(USER_HOME / "Downloads"),
            filetypes=(("MP4 videos", "*.mp4"),),
        )
        if selected:
            self.video_var.set(selected)

    def play(self) -> None:
        video = Path(self.video_var.get()).expanduser()
        if not video.is_file() or video.suffix.lower() != ".mp4":
            messagebox.showerror("Invalid video", "Choose an existing MP4 video.")
            return
        try:
            speed = float(self.speed_var.get())
            if not math.isfinite(speed) or speed <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid speed", "Enter a playback speed greater than zero.")
            return
        vlc = find_vlc()
        if not vlc:
            messagebox.showerror("VLC not found", "Install VLC in /Applications and try again.")
            return
        try:
            duration = mp4_duration(video)
        except (OSError, ValueError) as error:
            messagebox.showerror("Duration unavailable", str(error))
            return

        try:
            self.process = subprocess.Popen(
                [
                    vlc,
                    "--play-and-exit",
                    f"--rate={speed:g}",
                    str(video),
                ],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            messagebox.showerror("Could not start VLC", str(error))
            return

        self.duration = duration / speed
        self.speed = speed
        self.elapsed = 0.0
        self.last_tick = time.monotonic()
        self.paused = False
        self.root.withdraw()
        self.open_timer_window(video.name)
        self.update_timer()

    def open_timer_window(self, video_name: str) -> None:
        window = tk.Toplevel(self.root)
        self.timer_window = window
        window.title("Video Countdown")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.quit_all)
        window.attributes("-topmost", True)
        window.configure(background="#F5F5F7")

        frame = tk.Frame(window, background="#F5F5F7", padx=60, pady=60)
        frame.grid(sticky="nsew")
        tk.Label(
            frame,
            text=video_name,
            width=44,
            anchor="center",
            font=("Helvetica", 24),
            background="#F5F5F7",
            foreground="#242424",
        ).grid(
            row=0, column=0, columnspan=2, pady=(0, 32)
        )
        self.countdown_var = tk.StringVar(value=format_remaining(self.duration))
        tk.Label(
            frame,
            textvariable=self.countdown_var,
            font=("Helvetica", 112, "bold"),
            background="#F5F5F7",
            foreground="#242424",
        ).grid(
            row=1, column=0, columnspan=2, pady=(0, 48)
        )
        self.pause_button = RoundedButton(
            frame,
            text="⏸  Pause",
            command=self.toggle_pause,
            color="#1473E6",
            hover_color="#0D5FC3",
        )
        self.pause_button.grid(row=2, column=0, padx=(0, 16))
        RoundedButton(
            frame,
            text="■  Quit",
            command=self.quit_all,
            color="#D83B3B",
            hover_color="#B92E2E",
        ).grid(
            row=2, column=1, padx=(16, 0), sticky="ew"
        )

    def update_timer(self) -> None:
        if self.closing or not self.process:
            return
        now = time.monotonic()
        if not self.paused:
            self.elapsed += now - self.last_tick
        self.last_tick = now
        self.countdown_var.set(format_remaining(self.duration - self.elapsed))
        if self.process.poll() is not None:
            self.quit_all()
            return
        self.root.after(100, self.update_timer)

    def toggle_pause(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        try:
            if self.paused:
                os.killpg(self.process.pid, signal.SIGCONT)
                self.paused = False
                self.last_tick = time.monotonic()
                self.pause_button.set_text("⏸  Pause")
            else:
                now = time.monotonic()
                self.elapsed += now - self.last_tick
                os.killpg(self.process.pid, signal.SIGSTOP)
                self.paused = True
                self.pause_button.set_text("▶  Resume")
        except (OSError, ProcessLookupError):
            self.quit_all()

    def quit_all(self) -> None:
        if self.closing:
            return
        self.closing = True
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGCONT)
                os.killpg(self.process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    VideoTimerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
