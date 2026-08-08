# Command Support

This visible folder contains the shared implementations used by commands in the
parent `scripts` folder.

- `move_first_mp3_mp4.zsh` supports all four `Move First MP3 and MP4` commands.
- `list_mp3_mp4_by_duration.zsh` supports both `List MP3 and MP4` commands.

Keep this folder beside the `.command` files. The Finder launchers call the
parent commands, and those commands call the support scripts here.
