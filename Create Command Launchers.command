#!/bin/zsh

# Create portable Finder launchers for every .command file beside this script.
# A launcher passes the folder containing it as the command's first argument.

setopt null_glob

SCRIPT_PATH="${0:A}"
SOURCE_DIR="${SCRIPT_PATH:h}"
OUTPUT_DIR="$SOURCE_DIR/Portable Command Launchers"
WRAPPER_DIR="$SOURCE_DIR/.command_launcher_targets"

mkdir -p "$OUTPUT_DIR" "$WRAPPER_DIR" || {
  echo "ERROR: Could not create:"
  echo "  $OUTPUT_DIR"
  exit 1
}

echo "Searching for command files in:"
echo "  $SOURCE_DIR"
echo
echo "Saving Finder launchers in:"
echo "  $OUTPUT_DIR"
echo

created=0
skipped=0
failed=0

for command_file in "$SOURCE_DIR"/*.command; do
  [[ "${command_file:A}" == "$SCRIPT_PATH" ]] && continue

  command_name="${command_file:t:r}"
  launcher_name="$command_name Launcher"
  launcher_path="$OUTPUT_DIR/$launcher_name"
  wrapper_path="$WRAPPER_DIR/$command_name Launcher.command"

  {
    echo '#!/bin/zsh'
    printf 'ORIGINAL_COMMAND=%q\n' "$command_file"
    cat <<'WRAPPER'

TARGET_DIR=$(osascript <<'APPLESCRIPT'
tell application "Finder"
  if (count of windows) > 0 then
    return POSIX path of (target of front window as alias)
  end if
end tell
return ""
APPLESCRIPT
)

TARGET_DIR="${TARGET_DIR%/}"
if [[ -z "$TARGET_DIR" || ! -d "$TARGET_DIR" ]]; then
  TARGET_DIR=$(osascript <<'APPLESCRIPT'
try
  return POSIX path of (choose folder with prompt "Choose the folder this command should use:")
on error number -128
  return ""
end try
APPLESCRIPT
)
  TARGET_DIR="${TARGET_DIR%/}"
fi

[[ -z "$TARGET_DIR" ]] && exit 0
exec "$ORIGINAL_COMMAND" "$TARGET_DIR"
WRAPPER
  } > "$wrapper_path"
  chmod 755 "$wrapper_path"

  if [[ -e "$launcher_path" || -L "$launcher_path" ]]; then
    echo "SKIP: $launcher_name already exists"
    (( skipped++ ))
    continue
  fi

  result=$(osascript - "$wrapper_path" "$OUTPUT_DIR" "$launcher_name" <<'APPLESCRIPT' 2>&1
on run argv
  set commandPath to item 1 of argv
  set outputPath to item 2 of argv
  set launcherName to item 3 of argv

  set commandItem to POSIX file commandPath as alias
  set outputFolder to POSIX file outputPath as alias

  tell application "Finder"
    set newAlias to make new alias file at outputFolder to commandItem
    set name of newAlias to launcherName
  end tell
end run
APPLESCRIPT
)

  if [[ $? -eq 0 ]]; then
    echo "CREATED: $launcher_name"
    (( created++ ))
  else
    echo "ERROR: Could not create $launcher_name"
    echo "  $result"
    (( failed++ ))
  fi
done

echo
echo "Finished."
echo "Created: $created"
echo "Already existed: $skipped"
echo "Failed: $failed"
echo

if [[ "$created" -gt 0 ]]; then
  open "$OUTPUT_DIR"
fi

if [[ -t 0 ]]; then
  read -k 1 "?Press any key to close..."
fi

exit $(( failed > 0 ? 1 : 0 ))
