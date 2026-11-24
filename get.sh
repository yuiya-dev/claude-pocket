#!/bin/bash
# Downloads (or updates) Claude Pocket on macOS and runs its installer. Paste this into Terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/yuiya-dev/claude-pocket/main/get.sh | bash
#
# Installer options go after "bash -s --", for example:
#   curl -fsSL https://raw.githubusercontent.com/yuiya-dev/claude-pocket/main/get.sh | bash -s -- --startup
#
# It puts the app in ~/Library/Application Support/Claude Pocket. Running it again updates the app and
# keeps your office.json and everything the office has saved (desks, to-dos, calendar link).
{
zip_url="https://github.com/yuiya-dev/claude-pocket/archive/refs/heads/main.zip"
dest="${CLAUDE_POCKET_DIR:-$HOME/Library/Application Support/Claude Pocket}"
tmp="$(mktemp -d)" || exit 1
trap 'rm -rf "$tmp"' EXIT

fail() {
  printf '\n\033[31mClaude Pocket could not be installed: %s\033[0m\n' "$1"
  printf '\033[33m%s\033[0m\n' "You can also download it from https://github.com/yuiya-dev/claude-pocket (Code > Download ZIP),"
  printf '\033[33m%s\033[0m\n' "unzip it, and run install.sh in that folder."
  exit 1
}

if [ -n "$CLAUDE_POCKET_ZIP" ]; then
  cp "$CLAUDE_POCKET_ZIP" "$tmp/claude-pocket.zip" || fail "could not read $CLAUDE_POCKET_ZIP"
else
  printf '\033[36m%s\033[0m\n' "Downloading Claude Pocket..."
  curl -fsSL "$zip_url" -o "$tmp/claude-pocket.zip" || fail "the download failed"
fi
unzip -q "$tmp/claude-pocket.zip" -d "$tmp/x" || fail "the download was not a ZIP file"
src="$(find "$tmp/x" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -f "$src/install.sh" ] || fail "the download did not contain Claude Pocket"

# A running office is stopped first, so the new files are the ones that start.
case " $* " in
  *" --no-start "*) ;;
  *) curl -s -m 2 -o /dev/null -X POST -H "Content-Type: application/json" -d "{}" http://127.0.0.1:4545/quit && sleep 1 ;;
esac

update=0; [ -f "$dest/install.sh" ] && update=1
mkdir -p "$dest" || fail "could not create $dest"
for item in "$src"/* "$src"/.[!.]*; do
  [ -e "$item" ] || continue
  name="$(basename "$item")"
  [ "$name" = "office.json" ] && [ -f "$dest/office.json" ] && continue  # keep your settings
  cp -R "$item" "$dest/" || fail "could not copy $name"
done
if [ "$update" = 1 ]; then printf '\033[32mUpdated in %s\033[0m\n' "$dest"; else printf '\033[32mInstalled in %s\033[0m\n' "$dest"; fi

bash "$dest/install.sh" "$@"
}
