ACCESSIBLE SUBTITLE EXTRACTOR FOR macOS
=======================================

Files in this package
---------------------
1. Accessible_Subtitle_Extractor.py
2. Launch_Accessible_Subtitle_Extractor.command

Keep both files in the same folder.

Requirements
------------
- macOS
- Python 3.10 or newer
- An internet connection on the first launch, so the private dependencies and
  the selected speech-recognition model can be downloaded

The app automatically creates its own isolated environment under:
~/Library/Application Support/AccessibleSubtitleExtractor/

It does not install Python packages globally and does not require Homebrew or a
system-wide FFmpeg installation.

Launching
---------
Double-click Launch_Accessible_Subtitle_Extractor.command in Finder.

If macOS blocks the downloaded launcher, Control-click it, choose Open, and
confirm. Alternatively, open Terminal in this folder and run:

    chmod +x Launch_Accessible_Subtitle_Extractor.command
    ./Launch_Accessible_Subtitle_Extractor.command

Using the app
-------------
1. Choose Add files. The chooser starts in your Downloads folder.
2. Select one or several video/audio files.
3. Choose the mode, recognition model, and spoken language.
4. Activate Start SRT extraction.

The default mode first looks for an embedded text subtitle track. If no usable
text track exists, it transcribes speech. Each result is saved beside its source
as the same base name with an .srt extension.

Safety and repeatability
------------------------
- Existing non-empty SRT files are skipped by default.
- Enable Replace only when you intentionally want to regenerate them.
- Output is written to a temporary file and moved into place atomically.
- Completed files remain saved if a later file fails or the batch is cancelled.

Keyboard shortcuts
------------------
Command-O           Add files
Command-Return      Start SRT extraction
Escape              Request cancellation
Delete/Backspace    Remove selected files
Command-Shift-K     Clear the file list
Command-Q            Quit

Accessibility
-------------
The interface uses labeled standard Qt controls, keyboard navigation, explicit
accessibility names/descriptions, a readable activity log, and optional spoken
major status updates through the macOS `say` command.

Logs
----
Runtime log:
~/Library/Logs/AccessibleSubtitleExtractor.log

Dependency installation log:
~/Library/Logs/AccessibleSubtitleExtractor-bootstrap.log
