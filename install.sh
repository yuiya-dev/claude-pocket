#!/bin/bash
# Sets up Claude Pocket on macOS for whoever runs it. It changes nothing outside your own user folder.
#
#   ./install.sh               install and open the office
#   ./install.sh --startup     also start it when you log in
#   ./install.sh --no-app      no "Claude Pocket" app in ~/Applications
#   ./install.sh --yes         install Python without asking, if it is missing (needs Homebrew)
#   ./install.sh --uninstall   take the hook, the app and the login item out again
#
# What it does:
#   1. Checks that Python 3 and Claude (the desktop app or Claude Code) are installed.
#   2. Copies hooks/claude_pocket_hook.py to ~/.claude/hooks/.
#   3. Adds that hook to ~/.claude/settings.json for the seven events the office listens to, keeping
#      every other setting and hook. Before the first change, a copy is kept as
#      settings.json.claude-pocket.bak, and later runs never overwrite it.
#   4. Adds a "Claude Pocket" app to ~/Applications (so Spotlight and Launchpad find it) and opens
#      the office in your browser.
# Running it again is safe: it never adds the same hook twice.

here="$(cd "$(dirname "$0")" && pwd)"
yes=0; startup=0; noapp=0; nostart=0; uninstall=0
for a in "$@"; do
  case "$a" in
    --yes|-y) yes=1 ;;
    --startup) startup=1 ;;
    --no-app) noapp=1 ;;
    --no-start) nostart=1 ;;
    --uninstall) uninstall=1 ;;
    *) echo "Unknown option: $a" >&2; sed -n '2,9p' "$0" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
stop_with() { printf '\n\033[31m%s\033[0m\n\n' "$*"; exit 1; }

[ "$(uname)" = "Darwin" ] || stop_with "This installer is for macOS. On Windows, double-click Install.cmd or run install.ps1."

claude="$HOME/.claude"
hookdir="$claude/hooks"
hookfile="$hookdir/claude_pocket_hook.py"
settings="$claude/settings.json"
app="$HOME/Applications/Claude Pocket.app"
agent_label="com.claude-pocket.office"
agent="$HOME/Library/LaunchAgents/$agent_label.plist"
pyfile="$here/python-path.txt"  # claude-pocket reads it, so Python need not be on PATH

find_python() {
  for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    exe="$("$c" -c 'import sys; print(sys.executable if sys.version_info[0] == 3 else "")' 2>/dev/null)" || continue
    [ -n "$exe" ] && [ -x "$exe" ] && { echo "$exe"; return 0; }
  done
  return 1
}

# The app's icon, made from icon.png with the tools macOS ships.
make_icon() {
  local set_dir s
  set_dir="$(mktemp -d)/icon.iconset" && mkdir -p "$set_dir" || return 1
  for s in 16 32 64 128; do
    sips -z $s $s "$here/icon.png" --out "$set_dir/icon_${s}x${s}.png" >/dev/null 2>&1 || return 1
  done
  mv "$set_dir/icon_32x32.png" "$set_dir/icon_16x16@2x.png" && cp "$set_dir/icon_16x16@2x.png" "$set_dir/icon_32x32.png" &&
    mv "$set_dir/icon_64x64.png" "$set_dir/icon_32x32@2x.png" || return 1
  iconutil -c icns "$set_dir" -o "$app/Contents/Resources/applet.icns" 2>/dev/null
}

remove_login_item() {
  if [ -f "$agent" ]; then
    launchctl bootout "gui/$(id -u)/$agent_label" 2>/dev/null || launchctl unload "$agent" 2>/dev/null
    rm -f "$agent"
  fi
}

# ---------- uninstall ----------
if [ "$uninstall" = 1 ]; then
  py="$(cat "$pyfile" 2>/dev/null)"; [ -x "$py" ] || py="$(find_python)"
  [ -f "$settings" ] && [ -n "$py" ] && "$py" "$here/pocket_hooks.py" uninstall "$settings"
  rm -f "$hookfile" "$pyfile"
  remove_login_item
  case "$app" in */Applications/Claude\ Pocket.app) rm -rf "$app" ;; esac
  "$here/claude-pocket" stop >/dev/null 2>&1
  say "Claude Pocket's hook, app and login item are removed. Delete this folder if you like: $here"
  exit 0
fi

# Files from a downloaded ZIP carry macOS's quarantine mark; these are the files you chose to install.
xattr -dr com.apple.quarantine "$here" 2>/dev/null
chmod +x "$here/claude-pocket" "$here/install.sh" 2>/dev/null

# ---------- 1. Python ----------
python="$(find_python)"
if [ -z "$python" ]; then
  warn "Python 3 is not installed. Claude Pocket needs it (nothing else)."
  if command -v brew >/dev/null 2>&1; then
    answer=""
    if [ "$yes" = 1 ]; then
      answer=y
    elif ( : </dev/tty ) 2>/dev/null; then
      read -r -p "Install Python 3 now with Homebrew? [Y/n] " answer </dev/tty
      [ -z "$answer" ] && answer=y
    else
      stop_with "Python 3 is not installed, and no one is here to answer whether to install it.
Ask the user, and if they agree, run this again with --yes to install Python 3 with Homebrew."
    fi
    case "$answer" in [Nn]*) ;; *) brew install python && python="$(find_python)" ;; esac
  fi
fi
[ -n "$python" ] || stop_with "Python 3 is not installed.
Install it from https://www.python.org/downloads/macos/ (or run: xcode-select --install),
then run this again."
say "Python:  $python"
printf '%s\n' "$python" > "$pyfile"

# ---------- 2. Claude ----------
[ -d "$claude" ] || stop_with "Claude has not been set up on this Mac yet ($claude does not exist).
1. Install the Claude desktop app from https://claude.ai/download (or Claude Code).
2. Open it and sign in to your own Claude account.
3. Open the Code tab and start one session, so Claude creates its folder.
Then run this again."
say "Claude:  $claude"

# ---------- 3. the hook ----------
mkdir -p "$hookdir"
cp "$here/hooks/claude_pocket_hook.py" "$hookfile"
say "Hook:    $hookfile"
"$python" "$here/pocket_hooks.py" install "$settings" "$python" "$hookfile" || exit 1

# ---------- 4. app, login item ----------
if [ "$noapp" = 0 ] && command -v osacompile >/dev/null 2>&1; then
  case "$app" in */Applications/Claude\ Pocket.app) rm -rf "$app" ;; esac
  mkdir -p "$HOME/Applications"
  esc="$(printf '%s' "$here/claude-pocket" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  if osacompile -o "$app" -e "do shell script quoted form of \"$esc\" & \" open\"" 2>/dev/null; then
    make_icon && touch "$app"  # best effort: the app works with the default icon too
    say "App:     Claude Pocket is in ~/Applications (Spotlight and Launchpad find it)"
  else
    warn "Could not create the Claude Pocket app; open the office with: \"$here/claude-pocket\""
  fi
fi

remove_login_item
if [ "$startup" = 1 ]; then
  mkdir -p "$(dirname "$agent")"
  cat > "$agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$agent_label</string>
  <key>ProgramArguments</key>
  <array><string>$python</string><string>$here/server.py</string></array>
  <key>WorkingDirectory</key><string>$here</string>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$(id -u)" "$agent" 2>/dev/null || launchctl load "$agent" 2>/dev/null
  say "Startup: Claude Pocket starts when you log in"
fi

command -v gh >/dev/null 2>&1 || warn "Optional: install the GitHub CLI (brew install gh), then run 'gh auth login', to see pull requests on desks."

# ---------- 5. start ----------
[ "$nostart" = 1 ] && exit 0
"$here/claude-pocket" open
echo
printf '\033[36m%s\033[0m\n' "Claude Pocket is open in your browser. Send a message in any Claude Code session and its character walks in."
printf '\033[36m%s\033[0m\n' "To open it later: the Claude Pocket app, or \"$here/claude-pocket\". To stop it: \"$here/claude-pocket\" stop"
