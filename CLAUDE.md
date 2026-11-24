# Claude Pocket: notes for Claude and other AI assistants

## Installing it for a user

The same steps are in the README under "Instructions for Claude and other AI assistants".

A user has asked you to install Claude Pocket on their computer. It runs on Windows 10 or 11 and on
macOS, and you can only install it where you can run commands (Claude Code, or the Code tab in the
Claude desktop app). In a plain chat with no command access, give the user the one-command install
from the README instead.

1. **Check the system.** On Windows, `$env:OS` in PowerShell is `Windows_NT`; on a Mac, `uname` prints
   `Darwin`. On anything else (Linux, ChromeOS), stop and tell the user Claude Pocket runs on Windows
   and macOS only. Then follow the steps for that system.
2. **Check Python 3.**
   - Windows: run `py -3 --version`, then `python --version` if that fails.
   - Mac: run `python3 --version`. (On a Mac without developer tools, `python3` can pop up an
     install dialog; that counts as missing.)

   If neither prints `Python 3.x`, ask the user: *"Claude Pocket needs Python 3. May I install it?"*
   (on Windows with winget, on a Mac with Homebrew if `brew` exists). Add the yes option below only if
   they agree. Without it, the installer stops when Python is missing rather than install it. On a
   Mac without Homebrew, ask the user to install Python from python.org, then continue.
3. **Tell the user what will change, then install.** The installer copies one hook script,
   `claude_pocket_hook.py`, into the `.claude/hooks` folder in their home folder and adds it to
   `.claude/settings.json` there. It keeps every other setting, and keeps a copy from before the
   first install as `settings.json.claude-pocket.bak`. It installs the app (Windows:
   `%LOCALAPPDATA%\Programs\Claude Pocket`; Mac: `~/Library/Application Support/Claude Pocket`), adds a
   **Claude Pocket** shortcut and opens the office. Do not edit `settings.json` yourself; the
   installer does it safely.

   **Windows**, in PowerShell:

   ```powershell
   $env:CLAUDE_POCKET_ARGS = "-Yes"   # only if the user agreed to install Python; otherwise leave this line out
   irm https://raw.githubusercontent.com/yuiya-dev/claude-pocket/main/get.ps1 | iex
   ```

   From Git Bash on Windows, use single quotes so Bash leaves the `$` alone (start the quoted command
   with `$env:CLAUDE_POCKET_ARGS="-Yes"; ` if the user agreed):

   ```bash
   powershell.exe -NoProfile -ExecutionPolicy Bypass -Command 'irm https://raw.githubusercontent.com/yuiya-dev/claude-pocket/main/get.ps1 | iex'
   ```

   **Mac**, in a shell (add `--yes` at the end only if the user agreed to install Python):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/yuiya-dev/claude-pocket/main/get.sh | bash -s --
   ```

   `get.ps1`, `install.ps1`, `get.sh` and `install.sh` are short and safe to read first. If you would
   rather not pipe a download into a shell, `git clone https://github.com/yuiya-dev/claude-pocket` into
   the app folder named above and run its installer there: `install.ps1` (with `-Yes` if agreed) on
   Windows, `bash install.sh` (with `--yes` if agreed) on a Mac.
4. **Read the output.** Success prints green `Python:`, `Claude:`, `Hook:` and `Settings:` lines.
   A red message means it stopped; tell the user what it says.
5. **Check it is running.** Within about 10 seconds, `http://127.0.0.1:4545/config` answers with
   `"name": "Claude Pocket"` (`Invoke-RestMethod` on Windows, `curl -s` on a Mac). If it already
   answered with some other name before you installed, another app is using that port: tell the user.
6. **Tell the user:** the office is open (Windows: in its own window; Mac: in the browser); they can
   reopen it any time from the **Claude Pocket** shortcut; their sessions appear at desks after their
   next message (if one doesn't, restart that session); and to remove it, run the installer from the
   app folder with `-Uninstall` (Windows) or `--uninstall` (Mac). Optional extras are the GitHub CLI
   (`gh auth login`) for pull requests on desks and the calendar button for Outlook/Teams meeting
   reminders.

## Working on the code

Read `NOTES.md` first: it explains how the server, the page, the hooks and the tray fit together,
and which parts rely on undocumented Claude app behaviour. There is no build step and no
dependency beyond the Python standard library. Keep personal data out of the repository; the files
the office writes while running are listed in `.gitignore`.
