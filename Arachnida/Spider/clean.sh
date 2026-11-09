#!/bin/sh
# clean - remove every directory listed in spider's path history,
#         then wipe the history file.
#
# Usage: ./clean [HISTORY_FILE]
# Default history file: <this script's folder>/src/path_history.txt

DIR="$(cd "$(dirname "$0")" && pwd -P)"
HISTORY="${1:-$DIR/src/path_history.txt}"

if [ ! -f "$HISTORY" ]; then
    echo "clean: no history file at $HISTORY, nothing to do"
    exit 0
fi

# Absolute location of the history file (used to protect it from deletion)
HISTORY_ABS="$(cd "$(dirname "$HISTORY")" && pwd -P)/$(basename "$HISTORY")"

# A path is protected if deleting it would be a disaster: the root, the home
# folder, the project folder or any of its parents, or a folder that contains
# the history file itself.
is_protected() {
    case "$1" in
        /|"$HOME") return 0 ;;
    esac
    case "$DIR/" in
        "$1"/*) return 0 ;;
    esac
    case "$HISTORY_ABS" in
        "$1"/*) return 0 ;;
    esac
    return 1
}

removed=0
skipped=0

# "|| [ -n "$path" ]" also processes a last line that has no trailing newline
while IFS= read -r path || [ -n "$path" ]; do
    [ -z "$path" ] && continue

    case "$path" in
        /*) ;;
        *) echo "skip (not an absolute path): $path"; skipped=$((skipped + 1)); continue ;;
    esac
    if [ -L "$path" ]; then
        echo "skip (symbolic link): $path"; skipped=$((skipped + 1)); continue
    fi
    if [ ! -d "$path" ]; then
        echo "skip (not an existing directory): $path"; skipped=$((skipped + 1)); continue
    fi
    if is_protected "$path"; then
        echo "skip (protected path): $path"; skipped=$((skipped + 1)); continue
    fi

    if rm -rf -- "$path"; then
        echo "removed: $path"
        removed=$((removed + 1))
    else
        echo "failed to remove: $path"
        skipped=$((skipped + 1))
    fi
done < "$HISTORY"

# Wipe the history file (keeps the file, empties it)
: > "$HISTORY"
echo "clean: $removed removed, $skipped skipped, history wiped"
